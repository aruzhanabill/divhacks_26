"""Score transit routes: walking legs by corridor, rides by where you wait.

A Google Directions TRANSIT route is a sequence of walking and transit steps.
Walking legs reuse the exact corridor scoring used for walking routes (plus
the time-of-day history term). Each ride adds the boarding stop (full
weight), the alighting stop (half weight, you are leaving), and a wait term:
how long you stand on that platform or corner, weighted up when the place is
quiet. When one leg ends where the next begins, that stop is a transfer and
counted once.

Subway stops are scored by station safety (`mta.scoring`). Bus stops are
street corners, scored by the street-complaint history around the stop, bus
alerts on the route, and route ridership at this hour.

The number of stops on the ride is *not* a term: a local train is not less
safe than an express one.

`safety_score` has the same shape as walking: 100 / (1 + total weight),
higher is safer, so walking, subway and bus routes can be shown side by side.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db import pool
from app.mta import bus, gtfsrt
from app.mta.history import point_history
from app.mta.scoring import (
    LIVE_SCALE,
    LIVE_WEIGHT_CAP,
    nearest_complex,
    score_stations_by_id,
    wait_weight,
)
from app.scoring import (
    DEFAULT_CORRIDOR_METERS,
    LatLng,
    _safety_score,
    _wkt,
    corridor_terms,
    decode_polyline,
    explicit_window,
)

# Station weights summarise a band of history; corridor weights summarise the
# replay hour. STATION_ROUTE_SCALE brings a station's contribution into the same
# range as a walking leg so neither term swamps the other.
STATION_ROUTE_SCALE = 0.5
BOARD_WEIGHT = 1.0 * STATION_ROUTE_SCALE
ALIGHT_WEIGHT = 0.5 * STATION_ROUTE_SCALE
TRANSFER_WEIGHT = 0.75 * STATION_ROUTE_SCALE
STATION_MATCH_METERS = 250
BUS_STOP_RADIUS_METERS = 100
BUS_QUIET_RIDERS = 60  # riders per hour on the route; below this a late bus stop is a lonely one


class StationRef(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    name: str | None = None
    complex_id: str | None = None


class WalkLeg(BaseModel):
    kind: Literal["walk"]
    path: list[LatLng] | None = None
    polyline: str | None = None


class TransitLeg(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["transit"]
    line: str | None = None
    vehicle: str | None = None
    from_stop: StationRef = Field(alias="from")
    to_stop: StationRef = Field(alias="to")
    num_stops: int = Field(default=0, ge=0, le=200)
    duration_s: float = Field(default=0, ge=0)
    # Scheduled wait at the boarding stop per Google (seconds). Realtime
    # replaces it for the subway when the feed has the trip.
    wait_s: float | None = Field(default=None, ge=0)


class TransitRouteCandidate(BaseModel):
    route_id: str = Field(min_length=1, max_length=64)
    legs: list[WalkLeg | TransitLeg] = Field(min_length=1, max_length=24)


class TransitScoreRequest(BaseModel):
    routes: list[TransitRouteCandidate] = Field(min_length=1, max_length=6)
    corridor_meters: float = Field(default=DEFAULT_CORRIDOR_METERS, ge=50, le=500)
    sim_now: datetime | None = None
    include_live: bool = False
    include_history: bool = True


def _walk_points(leg: WalkLeg) -> list[tuple[float, float]]:
    if leg.path:
        return [(point.lat, point.lng) for point in leg.path]
    if leg.polyline:
        try:
            return decode_polyline(leg.polyline)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"walk leg: {exc}") from exc
    return []


def _is_bus(leg: TransitLeg) -> bool:
    return (leg.vehicle or "").upper() in ("BUS", "INTERCITY_BUS", "TROLLEYBUS")


class _Context:
    def __init__(self, conn, body: TransitScoreRequest, anchor: datetime, start: datetime, end: datetime) -> None:
        self.conn = conn
        self.body = body
        self.anchor = anchor
        self.start = start
        self.end = end
        self.scored = score_stations_by_id(conn, anchor, start, end, include_live=body.include_live)
        self.complex_cache: dict[tuple[float, float], str | None] = {}
        self.stops = gtfsrt.complex_stops(conn) if body.include_live else {}
        self.snapshot = gtfsrt.current() if body.include_live else None

    def resolve_complex(self, stop: StationRef) -> str | None:
        if stop.complex_id:
            return stop.complex_id
        key = (round(stop.lat, 5), round(stop.lng, 5))
        if key not in self.complex_cache:
            self.complex_cache[key] = nearest_complex(self.conn, stop.lat, stop.lng, STATION_MATCH_METERS)
        return self.complex_cache[key]


def _subway_hop(ctx: _Context, role: str, stop: StationRef, complex_id: str | None, weight_scale: float, line: str | None = None) -> dict:
    station = ctx.scored.get(complex_id) if complex_id else None
    realtime = station["breakdown"].get("realtime") if station else None
    # The station's own realtime term is for the overlay (any line). Here the
    # wait for the specific line is charged by _apply_wait instead.
    station_weight = (station["weight"] - (realtime["weight"] if realtime else 0.0)) if station else 0.0
    return {
        "kind": "station",
        "role": role,
        "complex_id": complex_id,
        "name": station["name"] if station else stop.name,
        "lat": station["lat"] if station else stop.lat,
        "lng": station["lng"] if station else stop.lng,
        "routes": station["routes"] if station else [],
        "safety_score": station["safety_score"] if station else None,
        "flags": list(station["flags"]) if station else [],
        "notes": _line_notes(realtime["notes"], line) if realtime else [],
        "quiet": bool(station["breakdown"]["ridership"]["quiet"]) if station and station["breakdown"]["ridership"] else False,
        "weight": round(station_weight * weight_scale, 2),
        "wait_min": None,
        "wait_weight": 0.0,
        "matched": station is not None,
    }


def _line_notes(notes: list[str], line: str | None) -> list[str]:
    """Station notes that concern the line being ridden (skip notes for other lines are noise here)."""
    key = gtfsrt.normalize_route(line)
    out = []
    for note in notes:
        if " trains" in note:
            if key and note.split(" trains")[0].split()[-1] == key:
                out.append(note)
        else:
            out.append(note)
    return out


def _bus_hop(ctx: _Context, role: str, stop: StationRef, leg: TransitLeg, weight_scale: float) -> dict:
    history = point_history(ctx.conn, stop.lat, stop.lng, BUS_STOP_RADIUS_METERS, ctx.anchor)
    riders = bus.typical_riders(ctx.conn, leg.line, ctx.anchor)
    quiet = riders is not None and riders < BUS_QUIET_RIDERS
    alerts = bus.alerts_for(leg.line) if ctx.body.include_live else []
    alert_weight = round(min(LIVE_WEIGHT_CAP, sum(item["severity"] for item in alerts) * LIVE_SCALE), 2) if alerts else 0.0
    weight = history["weight"] + alert_weight
    flags: list[str] = []
    if history["violent"] > 0:
        flags.append("violent_history")
    if quiet:
        flags.append("quiet_hour")
    if alerts:
        flags.append("service_alert")
        if any(item.get("alert_type", "").endswith("Stops Skipped") or "Detour" in item.get("alert_type", "") for item in alerts):
            flags.append("skipped_stops")
    notes = [item["summary"] for item in alerts[:3]]
    if riders is not None:
        notes.append(f"{leg.line or 'This route'} carries ~{riders:.0f} riders/hour at this time")
    return {
        "kind": "bus_stop",
        "role": role,
        "complex_id": None,
        "name": stop.name,
        "lat": stop.lat,
        "lng": stop.lng,
        "routes": [leg.line] if leg.line else [],
        "safety_score": _safety_score(weight),
        "flags": flags,
        "notes": notes,
        "quiet": quiet,
        "weight": round(weight * weight_scale, 2),
        "wait_min": None,
        "wait_weight": 0.0,
        "matched": True,
        "history": history,
        "riders_per_hour": None if riders is None else round(riders),
    }


def _subway_wait(ctx: _Context, leg: TransitLeg, board_id: str | None, alight_id: str | None) -> tuple[float | None, str, list[str]]:
    """(wait minutes, source, notes) for boarding this ride."""
    notes: list[str] = []
    line_key = gtfsrt.normalize_route(leg.line) or leg.line
    if ctx.snapshot is not None and leg.line and board_id and alight_id:
        from_bases = ctx.stops.get(board_id, set())
        to_bases = ctx.stops.get(alight_id, set())
        live = ctx.snapshot.leg_wait(from_bases, to_bases, leg.line) if from_bases and to_bases else None
        if live is not None:
            if live["skipping_here"] and live["through_trips"] == 0:
                notes.append(f"{line_key} trains are currently passing this station without stopping")
                return None, "realtime_no_service", notes
            if live["wait_min"] is not None:
                if live["skipping_here"]:
                    notes.append(f"{live['skipping_here']} upcoming {line_key} trains skip this station")
                return live["wait_min"], "realtime", notes
            if live["through_trips"] == 0:
                notes.append(f"No {line_key} train to your stop in the next hour")
                return None, "realtime_no_service", notes
    if leg.wait_s is not None:
        return round(leg.wait_s / 60, 1), "scheduled", notes
    return None, "unknown", notes


def _apply_wait(hop: dict, wait_min: float | None, source: str, notes: list[str]) -> None:
    hop["wait_min"] = wait_min
    hop["wait_source"] = source
    hop["notes"] = [*hop.get("notes", []), *notes]
    if source == "realtime_no_service":
        hop["wait_weight"] = 0.6
        if "no_service" not in hop["flags"]:
            hop["flags"].append("no_service")
    else:
        hop["wait_weight"] = wait_weight(wait_min, hop.get("quiet", False))
        if wait_min is not None and wait_min > 10 and "long_wait" not in hop["flags"]:
            hop["flags"].append("long_wait")
    hop["weight"] = round(hop["weight"] + hop["wait_weight"], 2)


def score_transit_routes(body: TransitScoreRequest) -> dict:
    start, end, anchor = explicit_window(body.sim_now)
    if end < start:
        end = start
    results = []
    with pool.connection() as conn:
        ctx = _Context(conn, body, anchor, start, end)
        for route in body.routes:
            walk_weight = 0.0
            walk_incidents = 0
            walk_history = 0
            light_weight = 0.0
            light_count = 0
            walk_breakdown: dict[str, dict] = {}
            walk_legs = 0
            hops: list[dict] = []
            lines: list[str] = []
            vehicles: set[str] = set()
            previous_alight_key: str | None = None

            for leg in route.legs:
                if isinstance(leg, WalkLeg):
                    points = _walk_points(leg)
                    if len(points) < 2:
                        continue
                    walk_legs += 1
                    terms = corridor_terms(
                        conn, _wkt(points), start, end, anchor, body.corridor_meters, include_history=body.include_history
                    )
                    walk_weight += terms["weight"]
                    walk_incidents += terms["incident_count"]
                    light_weight += terms["light_weight"]
                    light_count += terms["light_count"]
                    if terms["history"]:
                        walk_history += terms["history"]["count"]
                    for category, item in terms["breakdown"].items():
                        slot = walk_breakdown.setdefault(category, {"count": 0, "weight": 0.0})
                        slot["count"] += item["count"]
                        slot["weight"] = round(slot["weight"] + item["weight"], 3)
                    continue

                if leg.line:
                    lines.append(leg.line)
                if _is_bus(leg):
                    vehicles.add("bus")
                    board_key = f"bus:{round(leg.from_stop.lat, 4)},{round(leg.from_stop.lng, 4)}"
                    alight_key = f"bus:{round(leg.to_stop.lat, 4)},{round(leg.to_stop.lng, 4)}"
                    if previous_alight_key == board_key and hops:
                        hops[-1] = _bus_hop(ctx, "transfer", leg.from_stop, leg, TRANSFER_WEIGHT)
                    else:
                        hops.append(_bus_hop(ctx, "board", leg.from_stop, leg, BOARD_WEIGHT))
                    live = bus.live_wait(leg.from_stop.lat, leg.from_stop.lng, leg.line) if body.include_live else None
                    if live and live.get("wait_min") is not None:
                        _apply_wait(hops[-1], live["wait_min"], "bus_time", [])
                    elif leg.wait_s is not None:
                        _apply_wait(hops[-1], round(leg.wait_s / 60, 1), "scheduled", [])
                    hops.append(_bus_hop(ctx, "alight", leg.to_stop, leg, ALIGHT_WEIGHT))
                    previous_alight_key = alight_key
                    continue

                vehicles.add("subway")
                board_id = ctx.resolve_complex(leg.from_stop)
                alight_id = ctx.resolve_complex(leg.to_stop)
                board_key = f"station:{board_id}" if board_id else None
                if board_key and previous_alight_key == board_key and hops:
                    # Same complex as the previous alighting: a transfer, counted once at transfer weight.
                    hops[-1] = _subway_hop(ctx, "transfer", leg.from_stop, board_id, TRANSFER_WEIGHT, leg.line)
                else:
                    hops.append(_subway_hop(ctx, "board", leg.from_stop, board_id, BOARD_WEIGHT, leg.line))
                wait_min, source, notes = _subway_wait(ctx, leg, board_id, alight_id)
                _apply_wait(hops[-1], wait_min, source, notes)
                hops.append(_subway_hop(ctx, "alight", leg.to_stop, alight_id, ALIGHT_WEIGHT, leg.line))
                previous_alight_key = f"station:{alight_id}" if alight_id else None

            station_weight = sum(hop["weight"] for hop in hops)
            wait_weight_total = sum(hop["wait_weight"] for hop in hops)
            total_weight = walk_weight + station_weight
            station_calls = 0
            for hop in hops:
                item = ctx.scored.get(hop["complex_id"]) if hop.get("complex_id") else None
                if item:
                    station_calls += item["breakdown"]["calls"]["count"]
            weakest = min((hop["safety_score"] for hop in hops if hop["safety_score"] is not None), default=None)
            longest_wait = max((hop["wait_min"] for hop in hops if hop["wait_min"] is not None), default=None)
            results.append(
                {
                    "route_id": route.route_id,
                    "safety_score": _safety_score(total_weight),
                    "walk_score": _safety_score(walk_weight),
                    "station_score": _safety_score(station_weight),
                    "weakest_station_score": weakest,
                    "longest_wait_min": longest_wait,
                    "incident_count": walk_incidents + station_calls,
                    "history_count": walk_history,
                    "light_score": _safety_score(light_weight),
                    "light_count": light_count,
                    "lines": lines,
                    "vehicles": sorted(vehicles),
                    "stations": hops,
                    "breakdown": {
                        "walk": walk_breakdown,
                        "walk_weight": round(walk_weight, 2),
                        "station_weight": round(station_weight - wait_weight_total, 2),
                        "wait_weight": round(wait_weight_total, 2),
                        "walk_legs": walk_legs,
                    },
                }
            )
    realtime = gtfsrt.summary(ctx.snapshot) if ctx.snapshot is not None else None
    return {
        "corridor_meters": body.corridor_meters,
        "start": start,
        "end": end,
        "sim_now": anchor,
        "include_history": body.include_history,
        "realtime": realtime,
        "routes": results,
    }
