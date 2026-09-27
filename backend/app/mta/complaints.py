"""NYPD Complaint Data rows that happened in the subway.

Source: NYPD Complaint Data Current (YTD, 5uac-w243) and Historic (qgea-i56i),
filtered to `prem_typ_desc` transit premises. These are reported crimes, not
CAD calls, and the file lags real time, so they are the historical baseline
for a station. Each row is matched to the nearest station complex within
`MATCH_RADIUS_METERS`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from app.categorize import categorize
from app.db import pool
from app.ingest import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, NYC, _headers, as_nyc

YTD_URL = "https://data.cityofnewyork.us/resource/5uac-w243.json"
HISTORIC_URL = "https://data.cityofnewyork.us/resource/qgea-i56i.json"
PAGE_SIZE = 2000
MAX_ROWS = 40000
MATCH_RADIUS_METERS = 300
DEFAULT_LOOKBACK = timedelta(days=90)

PREMISES = ("TRANSIT - NYC SUBWAY", "TRANSIT FACILITY (OTHER)")

# NYPD offense labels first (they differ from CAD typ_desc), then categorize() as a fallback.
# "OTHER OFFENSES RELATED TO THEFT" is mostly theft of services (fare evasion) in the
# subway; it is the single largest bucket and not a personal-safety signal, so it is admin.
_OFFENSE_RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("violent", 5.0, ("MURDER", "RAPE", "SEX CRIMES", "ROBBERY", "FELONY ASSAULT", "KIDNAPPING", "DANGEROUS WEAPONS")),
    ("violent", 3.5, ("ASSAULT 3", "OFFENSES AGAINST THE PERSON")),
    ("admin", 0.3, ("OTHER OFFENSES RELATED TO THEFT", "OFFENSES AGAINST PUBLIC ADMINI", "ADMINISTRATIVE CODE",
                    "MISCELLANEOUS PENAL LAW", "FRAUDS", "FORGERY", "OTHER STATE LAWS", "VEHICLE AND TRAFFIC")),
    ("property", 3.0, ("GRAND LARCENY", "BURGLARY", "POSSESSION OF STOLEN")),
    ("property", 2.0, ("PETIT LARCENY", "CRIMINAL MISCHIEF", "THEFT")),
    ("disorder", 2.0, ("HARRASSMENT", "HARASSMENT", "OFF. AGNST PUB ORD", "DANGEROUS DRUGS", "CRIMINAL TRESPASS", "MENACING", "INTOXICATED")),
)

_LAW_SCALE = {"FELONY": 1.5, "MISDEMEANOR": 1.0, "VIOLATION": 0.5}

UPSERT = """
INSERT INTO station_complaints
    (source_id, lat, lng, geom, category, severity, law_cat, offense, timestamp)
VALUES (
    %(source_id)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
    %(category)s,
    %(severity)s,
    %(law_cat)s,
    %(offense)s,
    %(timestamp)s
)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    category = EXCLUDED.category,
    severity = EXCLUDED.severity,
    law_cat = EXCLUDED.law_cat,
    offense = EXCLUDED.offense,
    timestamp = EXCLUDED.timestamp
"""

MATCH_STATIONS = """
UPDATE station_complaints c
SET complex_id = (
    SELECT s.complex_id
    FROM subway_stations s
    WHERE ST_DWithin(s.geom, c.geom, %(radius)s)
    ORDER BY ST_Distance(s.geom, c.geom)
    LIMIT 1
)
WHERE c.complex_id IS NULL
  AND c.timestamp >= %(start)s
  AND c.timestamp <= %(end)s
"""


def classify_offense(ofns_desc: str | None, law_cat: str | None) -> tuple[str, float]:
    text = (ofns_desc or "").upper()
    category = None
    severity = 1.0
    for name, weight, needles in _OFFENSE_RULES:
        if any(needle in text for needle in needles):
            category, severity = name, weight
            break
    if category is None:
        category, severity = categorize(text, None)
    scale = _LAW_SCALE.get((law_cat or "").strip().upper(), 1.0)
    return category, round(min(7.5, severity * scale), 2)


def _parse_timestamp(row: dict) -> datetime | None:
    date_raw = row.get("cmplnt_fr_dt")
    if not date_raw:
        return None
    day = str(date_raw)[:10]
    time_raw = str(row.get("cmplnt_fr_tm") or "").strip()
    if not time_raw or time_raw.startswith("(null"):
        time_raw = "00:00:00"
    try:
        parsed = datetime.fromisoformat(f"{day}T{time_raw[:8]}")
    except ValueError:
        return None
    return parsed.replace(tzinfo=NYC)


def _where(start: datetime, end: datetime) -> str:
    start_lit = as_nyc(start).strftime("%Y-%m-%dT%H:%M:%S")
    end_lit = as_nyc(end).strftime("%Y-%m-%dT%H:%M:%S")
    premises = ", ".join(f"'{name}'" for name in PREMISES)
    return (
        f"prem_typ_desc in ({premises}) "
        "AND latitude IS NOT NULL AND longitude IS NOT NULL "
        f"AND cmplnt_fr_dt >= '{start_lit}' AND cmplnt_fr_dt < '{end_lit}'"
    )


def _normalize(rows: list[dict]) -> list[dict]:
    records: dict[str, dict] = {}
    for row in rows:
        source_id = str(row.get("cmplnt_num") or "").strip()
        if not source_id:
            continue
        try:
            lat = float(row["latitude"])
            lng = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
            continue
        timestamp = _parse_timestamp(row)
        if timestamp is None:
            continue
        law_cat = str(row.get("law_cat_cd") or "").strip().upper() or None
        offense = str(row.get("ofns_desc") or "").strip() or None
        category, severity = classify_offense(offense, law_cat)
        records[source_id] = {
            "source_id": source_id,
            "lat": lat,
            "lng": lng,
            "category": category,
            "severity": severity,
            "law_cat": law_cat,
            "offense": offense,
            "timestamp": timestamp,
        }
    return list(records.values())


def _pull(client: httpx.Client, conn, url: str, start: datetime, end: datetime) -> tuple[int, int]:
    fetched = 0
    upserted = 0
    offset = 0
    while offset < MAX_ROWS:
        response = client.get(
            url,
            headers=_headers(),
            params={
                "$select": "cmplnt_num,cmplnt_fr_dt,cmplnt_fr_tm,ofns_desc,law_cat_cd,latitude,longitude",
                "$where": _where(start, end),
                "$order": "cmplnt_fr_dt, cmplnt_num",
                "$limit": PAGE_SIZE,
                "$offset": offset,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or not page:
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
    return fetched, upserted


def ingest_complaints(start: datetime, end: datetime, force: bool = False) -> dict:
    """Store subway complaints with `start <= cmplnt_fr_dt < end` and match them to stations."""
    from app.mta.history import record_run, run_covers  # local: history imports this module

    start = as_nyc(start)
    end = as_nyc(end)
    fetched = 0
    upserted = 0
    with httpx.Client(timeout=90) as client, pool.connection() as conn:
        if not force and run_covers(conn, "station_complaints", start, end):
            total = conn.execute("SELECT count(*) FROM station_complaints").fetchone()[0]
            return {"fetched": 0, "upserted": 0, "cached": int(total), "start": start, "end": end}
        # Historic ends at the prior year; YTD covers the current year. Both are filtered
        # server-side, so the one that does not cover the window returns nothing.
        for url in (YTD_URL, HISTORIC_URL):
            got, put = _pull(client, conn, url, start, end)
            fetched += got
            upserted += put
        conn.execute(MATCH_STATIONS, {"radius": MATCH_RADIUS_METERS, "start": start, "end": end})
        unmatched = conn.execute(
            "SELECT count(*) FROM station_complaints WHERE complex_id IS NULL AND timestamp >= %(start)s AND timestamp <= %(end)s",
            {"start": start, "end": end},
        ).fetchone()
        record_run(conn, "station_complaints", start, end, upserted)
    return {
        "fetched": fetched,
        "upserted": upserted,
        "unmatched": int(unmatched[0]) if unmatched else 0,
        "start": start,
        "end": end,
    }
