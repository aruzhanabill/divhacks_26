"""Score candidate routes by crimes inside a meter corridor.

Higher safety_score is safer. The raw risk is the severity of calls in the
buffer, discounted by age against the end of the simulation window.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.db import pool
from app.ingest import as_nyc
from app.lights import LOOKBACK

CRIME_CATEGORIES = ("violent", "property", "disorder", "alarm")
DEFAULT_CORRIDOR_METERS = 150
RECENCY_TAU_SECONDS = 6 * 3600

SCORE_SQL = """
SELECT category,
       count(*)::int AS incident_count,
       coalesce(sum(
           severity * exp(
               -greatest(extract(epoch FROM (%(anchor)s - timestamp)), 0) / %(tau)s
           )
       ), 0)::float AS weight
FROM incidents
WHERE timestamp >= %(start)s
  AND timestamp <= %(end)s
  AND category = ANY(%(categories)s)
  AND ST_Covers(
        ST_Buffer(ST_GeogFromText(%(wkt)s), %(corridor_meters)s),
        geom
      )
GROUP BY category
"""

LIGHT_SQL = """
SELECT count(*)::int AS light_count,
       coalesce(sum(
           severity * exp(
               -greatest(extract(epoch FROM (%(anchor)s - timestamp)), 0) / %(tau)s
           )
       ), 0)::float AS weight
FROM street_lights
WHERE timestamp >= %(start)s
  AND timestamp <= %(end)s
  AND ST_Covers(
        ST_Buffer(ST_GeogFromText(%(wkt)s), %(corridor_meters)s),
        geom
      )
"""

LIGHT_TAU_SECONDS = 7 * 24 * 3600


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RouteCandidate(BaseModel):
    route_id: str = Field(min_length=1, max_length=64)
    polyline: str | None = None
    path: list[LatLng] | None = None


class ScoreRequest(BaseModel):
    routes: list[RouteCandidate] = Field(min_length=1, max_length=6)
    corridor_meters: float = Field(default=DEFAULT_CORRIDOR_METERS, ge=50, le=500)
    sim_now: datetime | None = None
    # Off by default: adds the time-of-day street-complaint baseline. Walking
    # scores are byte-identical to before when this is false.
    include_history: bool = False


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Return (lat, lng) pairs from a Google encoded polyline."""
    index = 0
    lat = 0
    lng = 0
    coords: list[tuple[float, float]] = []
    while index < len(encoded):
        delta_lat, index = _decode_chunk(encoded, index)
        delta_lng, index = _decode_chunk(encoded, index)
        lat += delta_lat
        lng += delta_lng
        coords.append((lat / 1e5, lng / 1e5))
    return coords


def _decode_chunk(encoded: str, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    length = len(encoded)
    while True:
        if index >= length:
            raise ValueError("truncated polyline")
        byte = ord(encoded[index]) - 63
        index += 1
        result |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            break
    delta = ~(result >> 1) if result & 1 else result >> 1
    return delta, index


def _points_for(route: RouteCandidate) -> list[tuple[float, float]]:
    if route.path:
        return [(point.lat, point.lng) for point in route.path]
    if route.polyline:
        try:
            return decode_polyline(route.polyline)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{route.route_id}: {exc}") from exc
    raise HTTPException(status_code=400, detail=f"{route.route_id} needs a path or polyline")


def _wkt(points: list[tuple[float, float]]) -> str:
    if len(points) < 2:
        raise HTTPException(status_code=400, detail="a route needs at least two points")
    if len(points) > 4000:
        raise HTTPException(status_code=400, detail="route path is too long")
    body = ", ".join(f"{lng:.6f} {lat:.6f}" for lat, lng in points)
    return f"SRID=4326;LINESTRING({body})"


def _window(requested: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT sim_now, replay_start, replay_end FROM sim_state WHERE id = 1"
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail="load calls before scoring routes",
        )
    sim_now, replay_start, replay_end = (as_nyc(value) for value in row)
    if requested is not None:
        sim_now = as_nyc(requested)
    if replay_start < sim_now <= replay_end:
        return replay_start, sim_now, sim_now
    return replay_start, replay_end, replay_end


def explicit_window(requested: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """Like `_window`, but an explicit `requested` anchor is honoured even outside the replay.

    Walking scoring keeps the clamping behaviour above. Station and transit
    scoring use this so a June anchor is scored in June when the replay is set
    to tonight (and vice versa). Outside the replay the window is the hour up
    to the anchor.
    """
    if requested is None:
        return _window(None)
    anchor = as_nyc(requested)
    with pool.connection() as conn:
        row = conn.execute("SELECT replay_start, replay_end FROM sim_state WHERE id = 1").fetchone()
    if row is not None:
        replay_start, replay_end = (as_nyc(value) for value in row)
        if replay_start < anchor <= replay_end:
            return replay_start, anchor, anchor
    return anchor - timedelta(hours=1), anchor, anchor


def _safety_score(weight: float) -> float:
    return round(100.0 / (1.0 + weight), 1)


def corridor_terms(
    conn,
    wkt: str,
    start: datetime,
    end: datetime,
    anchor: datetime,
    corridor_meters: float,
    include_history: bool = False,
) -> dict:
    """Raw crime and street-light terms for one LineString corridor.

    Shared by walking route scoring and the walking legs of subway routes.
    Returns weights (not scores) so callers can combine legs before scoring.
    With `include_history`, adds the time-of-day street-complaint baseline as
    breakdown key "history" and folds it into `weight`.
    """
    rows = conn.execute(
        SCORE_SQL,
        {
            "anchor": anchor,
            "tau": RECENCY_TAU_SECONDS,
            "start": start,
            "end": end,
            "categories": list(CRIME_CATEGORIES),
            "wkt": wkt,
            "corridor_meters": corridor_meters,
        },
    ).fetchall()
    breakdown = {
        category: {"count": count, "weight": round(weight, 2)}
        for category, count, weight in rows
    }
    incident_count = sum(item["count"] for item in breakdown.values())
    weight = sum(item["weight"] for item in breakdown.values())
    history = None
    if include_history:
        from app.mta.history import corridor_history  # local import: mta depends on this module

        history = corridor_history(conn, wkt, corridor_meters, anchor)
        breakdown["history"] = {"count": history["count"], "weight": history["weight"]}
        weight += history["weight"]
    light_row = conn.execute(
        LIGHT_SQL,
        {
            "anchor": anchor,
            "tau": LIGHT_TAU_SECONDS,
            "start": anchor - LOOKBACK,
            "end": anchor,
            "wkt": wkt,
            "corridor_meters": corridor_meters,
        },
    ).fetchone()
    light_count, light_weight = light_row if light_row is not None else (0, 0.0)
    return {
        "breakdown": breakdown,
        "incident_count": incident_count,
        "weight": weight,
        "light_count": light_count,
        "light_weight": light_weight,
        "history": history,
    }


def score_routes(body: ScoreRequest) -> dict:
    start, end, anchor = _window(body.sim_now)
    if end < start:
        end = start
    results = []
    with pool.connection() as conn:
        for route in body.routes:
            wkt = _wkt(_points_for(route))
            terms = corridor_terms(
                conn, wkt, start, end, anchor, body.corridor_meters, include_history=body.include_history
            )
            item = {
                "route_id": route.route_id,
                "safety_score": _safety_score(terms["weight"]),
                "incident_count": terms["incident_count"],
                "breakdown": terms["breakdown"],
                "light_score": _safety_score(terms["light_weight"]),
                "light_count": terms["light_count"],
            }
            if terms["history"] is not None:
                item["history"] = terms["history"]
            results.append(item)
    return {
        "corridor_meters": body.corridor_meters,
        "start": start,
        "end": end,
        "sim_now": anchor,
        "routes": results,
    }
