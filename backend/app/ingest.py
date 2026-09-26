"""Pull a historical window from NYPD Calls for Service into PostGIS.

Playback does not poll this API. One ingest snapshots the window; the
simulation clock replays `timestamp` (CAD add time, America/New_York).
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.categorize import categorize
from app.db import pool

NYC = ZoneInfo("America/New_York")
SODA_URL = "https://data.cityofnewyork.us/api/v3/views/n2zq-pubd/query.json"
PAGE_SIZE = 2000
MAX_ROWS = 20000

# Rough NYC extent. Drops null-island and other bad geocodes.
LAT_MIN, LAT_MAX = 40.30, 41.10
LNG_MIN, LNG_MAX = -74.40, -73.50

UPSERT = """
INSERT INTO incidents (source_id, lat, lng, geom, category, severity, timestamp)
VALUES (
    %(source_id)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
    %(category)s,
    %(severity)s,
    %(timestamp)s
)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    category = EXCLUDED.category,
    severity = EXCLUDED.severity,
    timestamp = EXCLUDED.timestamp
"""


def as_nyc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NYC)
    return dt.astimezone(NYC)


def _soql_literal(dt: datetime) -> str:
    return as_nyc(dt).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_timestamp(row: dict) -> datetime | None:
    add_ts = row.get("add_ts")
    if add_ts:
        raw = str(add_ts).replace("Z", "")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=NYC)
        return parsed.astimezone(NYC)
    incident_date = row.get("incident_date")
    incident_time = row.get("incident_time")
    if not incident_date or not incident_time:
        return None
    day = str(incident_date)[:10]
    parsed = datetime.fromisoformat(f"{day}T{incident_time}")
    return parsed.replace(tzinfo=NYC)


def _in_nyc(lat: float, lng: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("NYC_OPEN_DATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    return headers


def _fetch_page(client: httpx.Client, start: datetime, end: datetime, offset: int) -> list[dict]:
    start_lit = _soql_literal(start)
    end_lit = _soql_literal(end)
    query = (
        "SELECT cad_evnt_id, add_ts, incident_date, incident_time, typ_desc, cip_jobs, latitude, longitude "
        f"WHERE add_ts >= '{start_lit}' AND add_ts < '{end_lit}' "
        "AND latitude IS NOT NULL AND longitude IS NOT NULL "
        "ORDER BY add_ts, cad_evnt_id "
        f"LIMIT {PAGE_SIZE} OFFSET {offset}"
    )
    response = client.post(SODA_URL, headers=_headers(), json={"query": query})
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("rows") or []
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected response from NYPD Calls for Service")
    return payload


def _normalize(rows: list[dict]) -> list[dict]:
    by_source: dict[str, dict] = {}
    for row in rows:
        source_id = str(row.get("cad_evnt_id") or "").strip()
        if not source_id:
            continue
        try:
            lat = float(row["latitude"])
            lng = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _in_nyc(lat, lng):
            continue
        timestamp = _parse_timestamp(row)
        if timestamp is None:
            continue
        category, severity = categorize(row.get("typ_desc"), row.get("cip_jobs"))
        by_source[source_id] = {
            "source_id": source_id,
            "lat": lat,
            "lng": lng,
            "category": category,
            "severity": severity,
            "timestamp": timestamp,
        }
    return list(by_source.values())


def ingest_window(start: datetime, end: datetime) -> dict:
    start = as_nyc(start)
    end = as_nyc(end)
    fetched = 0
    upserted = 0
    offset = 0
    with httpx.Client(timeout=60) as client, pool.connection() as conn:
        while offset < MAX_ROWS:
            page = _fetch_page(client, start, end, offset)
            if not page:
                break
            fetched += len(page)
            records = _normalize(page)
            if records:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT, records)
                upserted += len(records)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return {
        "fetched": fetched,
        "upserted": upserted,
        "start": start,
        "end": end,
    }


def fetch_latest_add_ts() -> datetime | None:
    query = (
        "SELECT add_ts WHERE latitude IS NOT NULL "
        "ORDER BY add_ts DESC LIMIT 1"
    )
    with httpx.Client(timeout=60) as client:
        response = client.post(SODA_URL, headers=_headers(), json={"query": query})
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("rows") or []
    if not payload:
        return None
    return _parse_timestamp(payload[0])
