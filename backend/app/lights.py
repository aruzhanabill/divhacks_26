"""NYC 311 Street Light Condition reports.

The city does not publish a lamp inventory. These rows are places where a
light was reported out, missing, or dim. A route with fewer of them is
better lit.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from app.db import pool
from app.ingest import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, NYC, as_nyc

SODA_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
LOOKBACK = timedelta(days=14)
PAGE_SIZE = 2000
MAX_ROWS = 20000

# Higher severity means a darker block.
DARK: dict[str, float] = {
    "Multiple Street Lights Out": 3.0,
    "Street Light Out": 2.0,
    "Street Light Lamp Missing": 2.0,
    "Lamppost Missing": 2.0,
    "Lamppost Knocked Down": 2.0,
    "Fixture/Luminaire Missing": 2.0,
    "Street Light Lamp Dim": 1.0,
    "Flood Light Lamp Out": 1.0,
}

UPSERT = """
INSERT INTO street_lights (source_id, lat, lng, geom, severity, timestamp)
VALUES (
    %(source_id)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
    %(severity)s,
    %(timestamp)s
)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    severity = EXCLUDED.severity,
    timestamp = EXCLUDED.timestamp
"""


def _where(start: datetime, end: datetime) -> str:
    start_lit = as_nyc(start).strftime("%Y-%m-%dT%H:%M:%S")
    end_lit = as_nyc(end).strftime("%Y-%m-%dT%H:%M:%S")
    descriptors = ", ".join(f"'{name}'" for name in DARK)
    return (
        "complaint_type = 'Street Light Condition' "
        f"AND descriptor in ({descriptors}) "
        "AND latitude IS NOT NULL AND longitude IS NOT NULL "
        f"AND created_date >= '{start_lit}' AND created_date < '{end_lit}'"
    )


def _parse_created(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", ""))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=NYC)
    return parsed.astimezone(NYC)


def ingest_street_lights(end: datetime) -> dict:
    """Store dark-spot reports from the 14 days before `end`."""
    end = as_nyc(end)
    start = end - LOOKBACK
    fetched = 0
    upserted = 0
    offset = 0
    with httpx.Client(timeout=60) as client, pool.connection() as conn:
        conn.execute(
            "DELETE FROM street_lights WHERE timestamp < %(start)s OR timestamp >= %(end)s",
            {"start": start, "end": end},
        )
        while offset < MAX_ROWS:
            response = client.get(
                SODA_URL,
                params={
                    "$select": "unique_key,created_date,descriptor,latitude,longitude",
                    "$where": _where(start, end),
                    "$order": "created_date, unique_key",
                    "$limit": PAGE_SIZE,
                    "$offset": offset,
                },
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list) or not page:
                break
            fetched += len(page)
            records = []
            for row in page:
                descriptor = str(row.get("descriptor") or "")
                severity = DARK.get(descriptor)
                if severity is None:
                    continue
                try:
                    lat = float(row["latitude"])
                    lng = float(row["longitude"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
                    continue
                source_id = str(row.get("unique_key") or "").strip()
                created = row.get("created_date")
                if not source_id or not created:
                    continue
                records.append(
                    {
                        "source_id": source_id,
                        "lat": lat,
                        "lng": lng,
                        "severity": severity,
                        "timestamp": _parse_created(str(created)),
                    }
                )
            if records:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT, records)
                upserted += len(records)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return {"fetched": fetched, "upserted": upserted, "start": start, "end": end}
