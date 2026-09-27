"""Time-of-day historical baselines.

The replay window is one hour of Calls for Service. NYPD complaint data lags
that by months, so a baseline that only looks back from the sim clock is
empty whenever the clock sits past the newest complaint. Instead, history is
read as a *rate*: complaints in the sim clock's hour band (night, morning,
midday, evening) across everything loaded, normalised per 30 days of
coverage. "Tonight" at 116 St therefore reflects every night at 116 St we
have data for.

Street complaints (all premises except the subway itself) power the optional
history term on walking corridors and the bus-stop terms. Subway premises
stay in `station_complaints`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import httpx

from app.db import pool
from app.ingest import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, NYC, _headers, as_nyc
from app.mta.complaints import HISTORIC_URL, YTD_URL, _parse_timestamp, classify_offense

# Local hours in each band. Night wraps midnight: 21:00 through 04:59.
BANDS: dict[str, tuple[int, ...]] = {
    "night": (21, 22, 23, 0, 1, 2, 3, 4),
    "morning": (5, 6, 7, 8, 9),
    "midday": (10, 11, 12, 13, 14, 15),
    "evening": (16, 17, 18, 19, 20),
}
BAND_LABEL = {
    "night": "9 PM – 5 AM",
    "morning": "5 AM – 10 AM",
    "midday": "10 AM – 4 PM",
    "evening": "4 PM – 9 PM",
}
RATE_DAYS = 30  # weights are "per 30 days in this band"
MIN_COVERAGE_DAYS = 14  # below this, the whole table is used regardless of the anchor

SUBWAY_PREMISES = ("TRANSIT - NYC SUBWAY", "TRANSIT FACILITY (OTHER)")
BUS_PREMISES = ("BUS (NYC TRANSIT)", "BUS (OTHER)", "BUS STOP", "BUS TERMINAL")

PAGE_SIZE = 5000
MAX_ROWS = 250000
DEFAULT_LOOKBACK = timedelta(days=90)


def band_for(anchor: datetime) -> str:
    hour = as_nyc(anchor).hour
    for name, hours in BANDS.items():
        if hour in hours:
            return name
    return "night"


def band_hours(band: str) -> list[int]:
    return list(BANDS.get(band, BANDS["night"]))


def day_type_for(anchor: datetime) -> str:
    return "weekend" if as_nyc(anchor).weekday() >= 5 else "weekday"


def coverage_days(conn, table: str, anchor: datetime) -> tuple[float, datetime | None]:
    """Days of loaded history usable at `anchor`, and the cutoff to apply.

    Uses rows up to the anchor when that leaves at least MIN_COVERAGE_DAYS,
    otherwise the whole table (the sim clock is earlier than, or barely inside,
    the loaded data). Returns (days, cutoff) where cutoff is None for "no cap".
    """
    row = conn.execute(f"SELECT min(timestamp), max(timestamp) FROM {table}").fetchone()
    if row is None or row[0] is None:
        return 0.0, None
    first, last = as_nyc(row[0]), as_nyc(row[1])
    anchor = as_nyc(anchor)
    capped_last = min(last, anchor)
    days = (capped_last - first).total_seconds() / 86400
    if days >= MIN_COVERAGE_DAYS:
        return days, anchor
    return max((last - first).total_seconds() / 86400, 1.0), None


def rate_factor(days: float) -> float:
    """Scale a raw sum over `days` of coverage to a per-RATE_DAYS rate."""
    return RATE_DAYS / max(days, 1.0)


# --- ingest bookkeeping ---------------------------------------------------


def run_covers(conn, source: str, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM ingest_runs
        WHERE source = %(source)s AND range_start <= %(start)s AND range_end >= %(end)s
        LIMIT 1
        """,
        {"source": source, "start": start, "end": end},
    ).fetchone()
    return row is not None


def record_run(conn, source: str, start: datetime, end: datetime, rows: int) -> None:
    conn.execute(
        """
        INSERT INTO ingest_runs (source, range_start, range_end, rows_written)
        VALUES (%(source)s, %(start)s, %(end)s, %(rows)s)
        """,
        {"source": source, "start": start, "end": end, "rows": rows},
    )


# --- street complaints -----------------------------------------------------

STREET_UPSERT = """
INSERT INTO street_complaints
    (source_id, lat, lng, geom, category, severity, law_cat, offense, premise, local_hour, timestamp)
SELECT r.source_id, r.lat, r.lng,
       ST_SetSRID(ST_MakePoint(r.lng, r.lat), 4326)::geography,
       r.category, r.severity, r.law_cat, r.offense, r.premise, r.local_hour, r.timestamp
FROM unnest(
    %(source_id)s::text[], %(lat)s::float8[], %(lng)s::float8[], %(category)s::text[],
    %(severity)s::float8[], %(law_cat)s::text[], %(offense)s::text[], %(premise)s::text[],
    %(local_hour)s::int2[], %(timestamp)s::timestamptz[]
) AS r(source_id, lat, lng, category, severity, law_cat, offense, premise, local_hour, timestamp)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    category = EXCLUDED.category,
    severity = EXCLUDED.severity,
    law_cat = EXCLUDED.law_cat,
    offense = EXCLUDED.offense,
    premise = EXCLUDED.premise,
    local_hour = EXCLUDED.local_hour,
    timestamp = EXCLUDED.timestamp
"""


def _street_where(start: datetime, end: datetime) -> str:
    start_lit = as_nyc(start).strftime("%Y-%m-%dT%H:%M:%S")
    end_lit = as_nyc(end).strftime("%Y-%m-%dT%H:%M:%S")
    excluded = ", ".join(f"'{name}'" for name in SUBWAY_PREMISES)
    return (
        f"(prem_typ_desc IS NULL OR prem_typ_desc NOT IN ({excluded})) "
        "AND latitude IS NOT NULL AND longitude IS NOT NULL "
        f"AND cmplnt_fr_dt >= '{start_lit}' AND cmplnt_fr_dt < '{end_lit}'"
    )


def _normalize_street(rows: Iterable[dict]) -> dict[str, list]:
    columns: dict[str, list] = {
        key: [] for key in ("source_id", "lat", "lng", "category", "severity", "law_cat", "offense", "premise", "local_hour", "timestamp")
    }
    seen: set[str] = set()
    for row in rows:
        source_id = str(row.get("cmplnt_num") or "").strip()
        if not source_id or source_id in seen:
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
        seen.add(source_id)
        law_cat = str(row.get("law_cat_cd") or "").strip().upper() or None
        offense = str(row.get("ofns_desc") or "").strip() or None
        category, severity = classify_offense(offense, law_cat)
        columns["source_id"].append(source_id)
        columns["lat"].append(lat)
        columns["lng"].append(lng)
        columns["category"].append(category)
        columns["severity"].append(severity)
        columns["law_cat"].append(law_cat)
        columns["offense"].append(offense)
        columns["premise"].append(str(row.get("prem_typ_desc") or "").strip() or None)
        columns["local_hour"].append(timestamp.astimezone(NYC).hour)
        columns["timestamp"].append(timestamp)
    return columns


def _pull_street(client: httpx.Client, conn, url: str, start: datetime, end: datetime) -> tuple[int, int]:
    fetched = 0
    upserted = 0
    offset = 0
    while offset < MAX_ROWS:
        response = client.get(
            url,
            headers=_headers(),
            params={
                "$select": "cmplnt_num,cmplnt_fr_dt,cmplnt_fr_tm,ofns_desc,law_cat_cd,prem_typ_desc,latitude,longitude",
                "$where": _street_where(start, end),
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
        columns = _normalize_street(page)
        if columns["source_id"]:
            conn.execute(STREET_UPSERT, columns)
            conn.commit()
            upserted += len(columns["source_id"])
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return fetched, upserted


def ingest_street_complaints(start: datetime, end: datetime, force: bool = False) -> dict:
    """Load NYPD complaints for all non-subway premises in [start, end). Skips ranges already loaded."""
    start = as_nyc(start)
    end = as_nyc(end)
    with pool.connection() as conn:
        if not force and run_covers(conn, "street_complaints", start, end):
            total = conn.execute("SELECT count(*) FROM street_complaints").fetchone()[0]
            return {"fetched": 0, "upserted": 0, "cached": int(total), "start": start, "end": end}
        fetched = 0
        upserted = 0
        with httpx.Client(timeout=120) as client:
            for url in (YTD_URL, HISTORIC_URL):
                got, put = _pull_street(client, conn, url, start, end)
                fetched += got
                upserted += put
        record_run(conn, "street_complaints", start, end, upserted)
    return {"fetched": fetched, "upserted": upserted, "start": start, "end": end}


# --- corridor / point history terms ---------------------------------------

STREET_CORRIDOR_SQL = """
SELECT count(*)::int,
       coalesce(sum(severity), 0)::float,
       count(*) FILTER (WHERE category = 'violent')::int
FROM street_complaints
WHERE local_hour = ANY(%(hours)s)
  AND category = ANY(%(categories)s)
  AND (%(cutoff)s::timestamptz IS NULL OR timestamp <= %(cutoff)s)
  AND ST_Covers(ST_Buffer(ST_GeogFromText(%(wkt)s), %(corridor_meters)s), geom)
"""

STREET_POINT_SQL = """
SELECT count(*)::int,
       coalesce(sum(severity), 0)::float,
       count(*) FILTER (WHERE category = 'violent')::int,
       count(*) FILTER (WHERE premise = ANY(%(bus_premises)s))::int
FROM street_complaints
WHERE local_hour = ANY(%(hours)s)
  AND category = ANY(%(categories)s)
  AND (%(cutoff)s::timestamptz IS NULL OR timestamp <= %(cutoff)s)
  AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, %(radius)s)
"""

# Only crimes against people count for someone on foot. Property complaints at
# night are mostly store burglaries and shoplifting along commercial strips,
# which would make a busy avenue look worse than an empty park.
HISTORY_CATEGORIES = ("violent", "disorder")

# Per-30-day rate of severity inside a 150 m corridor. A quiet residential
# block at night sits near 0; a bar district or a park with robberies reaches
# 40–60. The scale keeps a typical half-mile night walk around 0.3–0.5 weight so
# history nudges the ranking without swamping the live calls term.
STREET_CORRIDOR_SCALE = 0.008
STREET_POINT_SCALE = 0.02
BUS_PREMISE_BONUS = 0.05


def corridor_history(conn, wkt: str, corridor_meters: float, anchor: datetime, categories: tuple[str, ...] = HISTORY_CATEGORIES) -> dict:
    days, cutoff = coverage_days(conn, "street_complaints", anchor)
    if days <= 0:
        return {"count": 0, "violent": 0, "weight": 0.0, "band": band_for(anchor), "coverage_days": 0}
    band = band_for(anchor)
    count, severity_sum, violent = conn.execute(
        STREET_CORRIDOR_SQL,
        {
            "hours": band_hours(band),
            "categories": list(categories),
            "cutoff": cutoff,
            "wkt": wkt,
            "corridor_meters": corridor_meters,
        },
    ).fetchone()
    weight = severity_sum * rate_factor(days) * STREET_CORRIDOR_SCALE
    return {
        "count": int(count),
        "violent": int(violent),
        "weight": round(weight, 3),
        "per_30d": round(count * rate_factor(days), 1),
        "band": band,
        "coverage_days": round(days),
    }


def point_history(conn, lat: float, lng: float, radius: float, anchor: datetime, categories: tuple[str, ...] = HISTORY_CATEGORIES) -> dict:
    days, cutoff = coverage_days(conn, "street_complaints", anchor)
    if days <= 0:
        return {"count": 0, "violent": 0, "bus_premise": 0, "weight": 0.0, "band": band_for(anchor)}
    band = band_for(anchor)
    count, severity_sum, violent, bus_premise = conn.execute(
        STREET_POINT_SQL,
        {
            "hours": band_hours(band),
            "categories": list(categories),
            "cutoff": cutoff,
            "lat": lat,
            "lng": lng,
            "radius": radius,
            "bus_premises": list(BUS_PREMISES),
        },
    ).fetchone()
    factor = rate_factor(days)
    weight = severity_sum * factor * STREET_POINT_SCALE + bus_premise * factor * BUS_PREMISE_BONUS
    return {
        "count": int(count),
        "violent": int(violent),
        "bus_premise": int(bus_premise),
        "weight": round(weight, 3),
        "per_30d": round(count * factor, 1),
        "band": band,
        "coverage_days": round(days),
    }
