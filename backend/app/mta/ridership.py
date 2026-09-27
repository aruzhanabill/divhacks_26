"""MTA Subway Hourly Ridership (data.ny.gov 5wq4-mkjj), summed per complex and hour.

Only the replay window's hours are pulled, for the window day and the same
weekday in the previous `WEEKS_BACK` weeks. That keeps a Load to a few
thousand rows while still giving each complex a "typical for this hour"
baseline that the station score compares against.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from app.db import pool
from app.ingest import NYC, _headers, as_nyc

RIDERSHIP_URL = "https://data.ny.gov/resource/5wq4-mkjj.json"
PAGE_SIZE = 5000
WEEKS_BACK = 4

UPSERT = """
INSERT INTO station_ridership_hourly (complex_id, hour, ridership)
VALUES (%(complex_id)s, %(hour)s, %(ridership)s)
ON CONFLICT (complex_id, hour) DO UPDATE SET ridership = EXCLUDED.ridership
"""


def _hour_floor(value: datetime) -> datetime:
    return as_nyc(value).replace(minute=0, second=0, microsecond=0)


def _literal(value: datetime) -> str:
    return as_nyc(value).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_hour(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "")[:19])
    return parsed.replace(tzinfo=NYC)


def _pull_span(client: httpx.Client, conn, start: datetime, end: datetime) -> tuple[int, int]:
    fetched = 0
    upserted = 0
    offset = 0
    while True:
        response = client.get(
            RIDERSHIP_URL,
            headers=_headers(),
            params={
                "$select": "station_complex_id, transit_timestamp, sum(ridership) AS ridership",
                "$where": (
                    f"transit_timestamp >= '{_literal(start)}' AND transit_timestamp < '{_literal(end)}' "
                    "AND transit_mode = 'subway'"
                ),
                "$group": "station_complex_id, transit_timestamp",
                "$order": "transit_timestamp, station_complex_id",
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
            complex_id = str(row.get("station_complex_id") or "").strip()
            stamp = row.get("transit_timestamp")
            if not complex_id or not stamp:
                continue
            try:
                ridership = int(float(row.get("ridership") or 0))
            except (TypeError, ValueError):
                continue
            records.append({"complex_id": complex_id, "hour": _parse_hour(str(stamp)), "ridership": ridership})
        if records:
            with conn.cursor() as cur:
                cur.executemany(UPSERT, records)
            upserted += len(records)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return fetched, upserted


PROFILE_DAYS = 14
PROFILE_PAGE_SIZE = 20000


def ingest_ridership_profile(end: datetime | None = None, days: int = PROFILE_DAYS, force: bool = False) -> dict:
    """Every hour of the `days` before `end` (or the newest data), for every complex.

    This gives each complex a full weekday/weekend hour-of-day profile, so the
    "typical riders at 2 AM on a Sunday" baseline exists whatever the replay
    window is. Skipped when a covering range was already loaded.
    """
    from app.mta.history import record_run, run_covers

    with httpx.Client(timeout=120) as client, pool.connection() as conn:
        latest = client.get(RIDERSHIP_URL, headers=_headers(), params={"$select": "max(transit_timestamp)"})
        latest.raise_for_status()
        newest_raw = (latest.json() or [{}])[0].get("max_transit_timestamp")
        if not newest_raw:
            return {"fetched": 0, "upserted": 0, "error": "ridership dataset is empty"}
        newest = _parse_hour(newest_raw)
        until = min(_hour_floor(end), newest) if end is not None else newest
        since = until - timedelta(days=days)
        if not force and run_covers(conn, "station_ridership_profile", since, until):
            total = conn.execute("SELECT count(*) FROM station_ridership_hourly").fetchone()[0]
            return {"fetched": 0, "upserted": 0, "cached": int(total), "start": since, "end": until}
        global PAGE_SIZE
        previous = PAGE_SIZE
        PAGE_SIZE = PROFILE_PAGE_SIZE
        try:
            fetched, upserted = _pull_span(client, conn, since, until)
        finally:
            PAGE_SIZE = previous
        record_run(conn, "station_ridership_profile", since, until, upserted)
    return {"fetched": fetched, "upserted": upserted, "start": since, "end": until}


def ingest_ridership(start: datetime, end: datetime, weeks_back: int = WEEKS_BACK) -> dict:
    """Hourly ridership for [start, end] plus the same hours on the same weekday in prior weeks."""
    span_start = _hour_floor(start)
    span_end = _hour_floor(end) + timedelta(hours=1)
    fetched = 0
    upserted = 0
    spans = 0
    with httpx.Client(timeout=90) as client, pool.connection() as conn:
        for week in range(0, weeks_back + 1):
            shift = timedelta(days=7 * week)
            got, put = _pull_span(client, conn, span_start - shift, span_end - shift)
            fetched += got
            upserted += put
            spans += 1
    return {"fetched": fetched, "upserted": upserted, "spans": spans, "start": span_start, "end": span_end}
