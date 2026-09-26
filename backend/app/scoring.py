"""Score candidate routes by crimes inside a meter corridor.

Higher safety_score is safer. The raw risk is the severity of calls in the
buffer, discounted by age against the end of the simulation window.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.db import pool
from app.ingest import as_nyc

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


def _window() -> tuple[datetime, datetime, datetime]:
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
    anchor = sim_now if sim_now > replay_start else replay_end
    if anchor < replay_start:
        anchor = replay_end
    return replay_start, replay_end, anchor


def _safety_score(weight: float) -> float:
    return round(100.0 / (1.0 + weight), 1)


def score_routes(body: ScoreRequest) -> dict:
    start, end, anchor = _window()
    if end < start:
        end = start
    results = []
    with pool.connection() as conn:
        for route in body.routes:
            wkt = _wkt(_points_for(route))
            rows = conn.execute(
                SCORE_SQL,
                {
                    "anchor": anchor,
                    "tau": RECENCY_TAU_SECONDS,
                    "start": start,
                    "end": end,
                    "categories": list(CRIME_CATEGORIES),
                    "wkt": wkt,
                    "corridor_meters": body.corridor_meters,
                },
            ).fetchall()
            breakdown = {
                category: {"count": count, "weight": round(weight, 2)}
                for category, count, weight in rows
            }
            incident_count = sum(item["count"] for item in breakdown.values())
            weight = sum(item["weight"] for item in breakdown.values())
            results.append(
                {
                    "route_id": route.route_id,
                    "safety_score": _safety_score(weight),
                    "incident_count": incident_count,
                    "breakdown": breakdown,
                }
            )
    return {
        "corridor_meters": body.corridor_meters,
        "start": start,
        "end": end,
        "sim_now": anchor,
        "routes": results,
    }
