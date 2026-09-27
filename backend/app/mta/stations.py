"""MTA Subway Stations reference table (data.ny.gov 39hk-dx4f).

One row per station (platform group). Stations that share a fare-controlled
area share a `complex_id`; scores aggregate at the complex level.
"""

from __future__ import annotations

import httpx

from app.db import pool
from app.ingest import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, _headers

STATIONS_URL = "https://data.ny.gov/resource/39hk-dx4f.json"
PAGE_SIZE = 1000

UPSERT = """
INSERT INTO subway_stations
    (complex_id, station_id, gtfs_stop_id, name, borough, routes, structure, ada, lat, lng, geom)
VALUES (
    %(complex_id)s,
    %(station_id)s,
    %(gtfs_stop_id)s,
    %(name)s,
    %(borough)s,
    %(routes)s,
    %(structure)s,
    %(ada)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography
)
ON CONFLICT (gtfs_stop_id) DO UPDATE SET
    complex_id = EXCLUDED.complex_id,
    station_id = EXCLUDED.station_id,
    name = EXCLUDED.name,
    borough = EXCLUDED.borough,
    routes = EXCLUDED.routes,
    structure = EXCLUDED.structure,
    ada = EXCLUDED.ada,
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom
"""


def _normalize(row: dict) -> dict | None:
    try:
        lat = float(row["gtfs_latitude"])
        lng = float(row["gtfs_longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
        return None
    station_id = str(row.get("station_id") or "").strip()
    gtfs_stop_id = str(row.get("gtfs_stop_id") or "").strip()
    if not station_id or not gtfs_stop_id:
        return None
    routes = [part for part in str(row.get("daytime_routes") or "").split() if part]
    ada_raw = str(row.get("ada") or "0").strip()
    return {
        "complex_id": str(row.get("complex_id") or station_id).strip(),
        "station_id": station_id,
        "gtfs_stop_id": gtfs_stop_id,
        "name": str(row.get("stop_name") or "").strip() or gtfs_stop_id,
        "borough": str(row.get("borough") or "").strip() or None,
        "routes": routes,
        "structure": str(row.get("structure") or "").strip() or None,
        "ada": ada_raw not in {"0", "", "false", "False"},
        "lat": lat,
        "lng": lng,
    }


def station_count() -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM subway_stations").fetchone()
    return int(row[0]) if row else 0


def ingest_stations() -> dict:
    fetched = 0
    upserted = 0
    offset = 0
    with httpx.Client(timeout=60) as client, pool.connection() as conn:
        while True:
            response = client.get(
                STATIONS_URL,
                headers=_headers(),
                params={"$limit": PAGE_SIZE, "$offset": offset, "$order": "station_id"},
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list) or not page:
                break
            fetched += len(page)
            records = [record for record in (_normalize(row) for row in page) if record]
            if records:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT, records)
                upserted += len(records)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return {"fetched": fetched, "upserted": upserted}
