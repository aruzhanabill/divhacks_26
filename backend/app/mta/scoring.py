"""Station safety scores.

Same shape as route scoring: `safety_score = 100 / (1 + weight)`, higher is
safer. Weight is the sum of

- complaints: NYPD subway complaints at the complex in the sim clock's hour
  band (night / morning / midday / evening) across all loaded history, as a
  per-30-day severity rate. Independent of the replay window, so "tonight"
  uses every night we have data for even when the newest complaint is months
  old;
- calls: transit-flagged Calls for Service within `CALL_RADIUS_METERS` of a
  member station inside the simulation window, same 6 h decay as walking;
- ridership: an emptiness term when the hour is quiet for that complex (fewer
  eyes on the platform), from `station_ridership_hourly`; also scales the
  complaint term per rider so hubs are not penalised for foot traffic alone;
- live: active MTA alerts and equipment outages when `include_live` is set;
- realtime: GTFS-RT waits, headways, skipped stops and no-service, when
  `include_live` is set. A long wait on an empty platform is weighted up.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from app.mta import gtfsrt
from app.mta.history import band_for, band_hours, coverage_days, rate_factor
from app.scoring import CRIME_CATEGORIES, RECENCY_TAU_SECONDS, _safety_score

COMPLAINT_LOOKBACK = timedelta(days=90)  # used by station_detail for the recent list
# Per-30-day severity rate in the hour band. Christopher St at night is ~10
# severity-units per 30 days; W 4 St is ~40. Scale puts the median station in
# the 55–70 range and hubs at 15–35 before the per-rider correction.
COMPLAINT_SCALE = 0.12
CALL_RADIUS_METERS = 200
EMPTY_HOUR_PENALTY = 0.6
QUIET_RATIO = 0.35
QUIET_ABSOLUTE = 40  # riders per hour; below this a platform is empty regardless of its norm

# Realtime exposure. Waiting is free for the first WAIT_FREE_MINUTES, then each
# 10 minutes adds WAIT_WEIGHT_PER_10MIN; a quiet platform multiplies by
# (1 + QUIET_WAIT_BONUS). Routes not stopping here add fixed penalties.
WAIT_FREE_MINUTES = 5
WAIT_WEIGHT_PER_10MIN = 0.3
WAIT_WEIGHT_CAP = 1.0
QUIET_WAIT_BONUS = 0.5
SKIP_WEIGHT = 0.5
NO_SERVICE_WEIGHT = 0.4
REALTIME_WEIGHT_CAP = 1.5

# Crimes-per-rider correction. Complaint counts scale with foot traffic, so a
# hub with 10x the riders of the reference station has its complaint weight
# damped by sqrt(1/10). Quiet stations are lifted, capped at BUSY_FACTOR_MAX.
BUSY_REFERENCE_RIDERS = 400  # cap on the hour's reference (daytime)
BUSY_REFERENCE_FLOOR = 100  # floor on the reference so per-rider damping stays moderate at night
BUSY_FACTOR_MIN = 0.35
BUSY_FACTOR_MAX = 1.25

# Live status is context, not crime. Planned-work alerts list every skipped stop,
# so per-station live weight is scaled and capped to stay a nudge.
LIVE_SCALE = 0.5
LIVE_WEIGHT_CAP = 0.75

COMPLEXES_SQL = """
SELECT complex_id,
       array_agg(DISTINCT name ORDER BY name) AS names,
       array_agg(DISTINCT route) FILTER (WHERE route IS NOT NULL) AS routes,
       bool_or(ada) AS ada,
       avg(lat) AS lat,
       avg(lng) AS lng,
       array_agg(DISTINCT structure) FILTER (WHERE structure IS NOT NULL) AS structures
FROM subway_stations s
LEFT JOIN LATERAL unnest(s.routes) AS route ON true
GROUP BY complex_id
"""

COMPLAINTS_SQL = """
SELECT complex_id,
       count(*)::int AS complaint_count,
       coalesce(sum(severity), 0)::float AS severity_sum,
       count(*) FILTER (WHERE category = 'violent')::int AS violent_count
FROM station_complaints
WHERE complex_id IS NOT NULL
  AND extract(hour FROM timestamp AT TIME ZONE 'America/New_York')::int = ANY(%(hours)s)
  AND (%(cutoff)s::timestamptz IS NULL OR timestamp <= %(cutoff)s)
GROUP BY complex_id
"""

CALLS_SQL = """
SELECT near.complex_id,
       count(*)::int AS call_count,
       coalesce(sum(
           near.severity * exp(
               -greatest(extract(epoch FROM (%(anchor)s - near.timestamp)), 0) / %(tau)s
           )
       ), 0)::float AS weight
FROM (
    SELECT i.severity,
           i.timestamp,
           (
               SELECT s.complex_id
               FROM subway_stations s
               WHERE ST_DWithin(s.geom, i.geom, %(radius)s)
               ORDER BY ST_Distance(s.geom, i.geom)
               LIMIT 1
           ) AS complex_id
    FROM incidents i
    WHERE i.is_transit
      AND i.timestamp >= %(start)s
      AND i.timestamp <= %(end)s
      AND i.category = ANY(%(categories)s)
) AS near
WHERE near.complex_id IS NOT NULL
GROUP BY near.complex_id
"""

RIDERSHIP_SQL = """
WITH target AS (
    SELECT date_trunc('hour', %(anchor)s::timestamptz) AS hour,
           extract(hour FROM %(anchor)s::timestamptz AT TIME ZONE 'America/New_York')::int AS local_hour,
           (extract(isodow FROM %(anchor)s::timestamptz AT TIME ZONE 'America/New_York') >= 6) AS weekend
),
anchor_hour AS (
    SELECT r.complex_id, r.ridership
    FROM station_ridership_hourly r, target
    WHERE r.hour = target.hour
),
typical AS (
    SELECT r.complex_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ridership) AS median
    FROM station_ridership_hourly r, target
    WHERE extract(hour FROM r.hour AT TIME ZONE 'America/New_York')::int = target.local_hour
      AND (extract(isodow FROM r.hour AT TIME ZONE 'America/New_York') >= 6) = target.weekend
    GROUP BY r.complex_id
)
SELECT t.complex_id, a.ridership, t.median
FROM typical t
LEFT JOIN anchor_hour a USING (complex_id)
"""

LIVE_SQL = """
SELECT complex_id, route_id, kind, severity, summary, observed_at
FROM station_status_snapshots
WHERE observed_at = (SELECT max(observed_at) FROM station_status_snapshots)
"""


def _complexes(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for complex_id, names, routes, ada, lat, lng, structures in conn.execute(COMPLEXES_SQL).fetchall():
        out[complex_id] = {
            "complex_id": complex_id,
            "name": " / ".join(names or []),
            "routes": sorted(routes or [], key=_route_sort_key),
            "ada": bool(ada),
            "lat": float(lat),
            "lng": float(lng),
            "structure": (structures or [None])[0],
        }
    return out


def _route_sort_key(route: str) -> tuple[int, str]:
    return (0, route.zfill(2)) if route.isdigit() else (1, route)


def _complaint_terms(conn, anchor: datetime) -> tuple[dict[str, dict], dict]:
    """Per-complex band baseline plus the shared context (band, coverage)."""
    days, cutoff = coverage_days(conn, "station_complaints", anchor)
    band = band_for(anchor)
    context = {"band": band, "coverage_days": round(days), "cutoff": cutoff}
    if days <= 0:
        return {}, context
    factor = rate_factor(days)
    rows = conn.execute(COMPLAINTS_SQL, {"hours": band_hours(band), "cutoff": cutoff}).fetchall()
    return (
        {
            complex_id: {
                "count": count,
                "violent": violent,
                "per_30d": round(count * factor, 1),
                "weight": round(severity_sum * factor * COMPLAINT_SCALE, 2),
            }
            for complex_id, count, severity_sum, violent in rows
        },
        context,
    )


def _call_terms(conn, anchor: datetime, start: datetime, end: datetime) -> dict[str, dict]:
    rows = conn.execute(
        CALLS_SQL,
        {
            "anchor": anchor,
            "start": start,
            "end": end,
            "tau": RECENCY_TAU_SECONDS,
            "radius": CALL_RADIUS_METERS,
            "categories": list(CRIME_CATEGORIES),
        },
    ).fetchall()
    return {complex_id: {"count": count, "weight": round(weight, 2)} for complex_id, count, weight in rows}


def _ridership_terms(conn, anchor: datetime) -> dict[str, dict]:
    try:
        rows = conn.execute(RIDERSHIP_SQL, {"anchor": anchor}).fetchall()
    except Exception:  # table empty or feature not loaded; ridership is optional
        return {}
    out: dict[str, dict] = {}
    typicals = [float(median) for _, _, median in rows if median is not None]
    # Per-rider reference is the typical station *at this hour*: at 2 AM most
    # platforms are near-empty, so hubs are compared against their peers then.
    reference = max(BUSY_REFERENCE_FLOOR, min(BUSY_REFERENCE_RIDERS, statistics.median(typicals))) if typicals else BUSY_REFERENCE_RIDERS
    for complex_id, ridership, median in rows:
        if median is None:
            continue
        median = float(median)
        current = None if ridership is None else int(ridership)
        # Emptiness is graded: 2 riders an hour is a deserted platform, 35 is
        # merely quiet. Uses this exact hour if loaded, else the typical hour.
        level = float(current) if current is not None else median
        if current is not None and median > 0 and current < median * QUIET_RATIO:
            level = min(level, median * QUIET_RATIO)
        emptiness = max(0.0, 1.0 - level / QUIET_ABSOLUTE)
        quiet = emptiness > 0
        busy = max(median, 1.0)
        factor = min(BUSY_FACTOR_MAX, max(BUSY_FACTOR_MIN, (reference / busy) ** 0.5))
        out[complex_id] = {
            "hourly": current,
            "typical": round(median),
            "reference": round(reference),
            "quiet": quiet,
            "emptiness": round(emptiness, 2),
            "busy_factor": round(factor, 2),
            "weight": round(EMPTY_HOUR_PENALTY * emptiness, 3),
        }
    return out


def _live_terms(conn, complexes: dict[str, dict]) -> dict[str, dict]:
    try:
        rows = conn.execute(LIVE_SQL).fetchall()
    except Exception:
        return {}
    by_route: dict[str, list[dict]] = defaultdict(list)
    by_complex: dict[str, list[dict]] = defaultdict(list)
    observed = None
    for complex_id, route_id, kind, severity, summary, observed_at in rows:
        observed = observed_at
        item = {"kind": kind, "severity": float(severity), "summary": summary, "route": route_id}
        if complex_id:
            by_complex[complex_id].append(item)
        elif route_id:
            by_route[route_id].append(item)
    out: dict[str, dict] = {}
    for complex_id, info in complexes.items():
        items = list(by_complex.get(complex_id, []))
        seen = {(item["kind"], item["summary"]) for item in items}
        for route in info["routes"]:
            for item in by_route.get(route, []):
                key = (item["kind"], item["summary"])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        if not items:
            continue
        items.sort(key=lambda item: -item["severity"])
        out[complex_id] = {
            "count": len(items),
            "weight": round(min(LIVE_WEIGHT_CAP, sum(item["severity"] for item in items) * LIVE_SCALE), 2),
            "items": items[:6],
            "observed_at": observed,
        }
    return out


def wait_weight(wait_min: float | None, quiet: bool) -> float:
    if wait_min is None:
        return 0.0
    extra = max(0.0, wait_min - WAIT_FREE_MINUTES)
    weight = min(WAIT_WEIGHT_CAP, extra / 10 * WAIT_WEIGHT_PER_10MIN)
    return round(weight * (1 + QUIET_WAIT_BONUS if quiet else 1.0), 3)


def _realtime_terms(conn, complexes: dict[str, dict], ridership: dict[str, dict]) -> tuple[dict[str, dict], dict | None]:
    """GTFS-RT wait / skip / no-service per complex. Empty when the feeds are unreachable."""
    snapshot = gtfsrt.current()
    if snapshot is None or not snapshot.routes_seen:
        return {}, None
    per_complex = gtfsrt.realtime_for_complexes(conn, snapshot, complexes)
    out: dict[str, dict] = {}
    for complex_id, routes in per_complex.items():
        quiet = bool(ridership.get(complex_id, {}).get("quiet"))
        waits = [r["wait_min"] for r in routes.values() if r["wait_min"] is not None]
        headways = [r["headway_min"] for r in routes.values() if r["headway_min"] is not None]
        skipping = {route: r["skipping"] for route, r in routes.items() if r["skipping"] > 0}
        no_service = [route for route, r in routes.items() if r["no_service"]]
        # Best available train is what a rider waits for; the shortest wait counts.
        best_wait = min(waits) if waits else None
        weight = wait_weight(best_wait, quiet)
        # Per-route skips and absences are notes at station level (a rider picks
        # one line; route scoring charges the line they take). The station is
        # only penalised when nothing is stopping here at all.
        stopping_any = any(r["stopping"] > 0 for r in routes.values())
        if not stopping_any:
            if any(r["skipping"] > 0 for r in routes.values()):
                weight += SKIP_WEIGHT
            elif no_service and len(no_service) == len(routes):
                weight += NO_SERVICE_WEIGHT
        notes: list[str] = []
        for route, count in skipping.items():
            stopping = routes[route]["stopping"]
            if stopping == 0:
                notes.append(f"{route} trains are not stopping here right now ({count} passing through)")
            else:
                notes.append(f"{count} of {count + stopping} upcoming {route} trains skip this station")
        for route in no_service:
            notes.append(f"No {route} trains due here in the next {NO_SERVICE_MINUTES_LABEL}")
        if best_wait is not None and best_wait > WAIT_FREE_MINUTES:
            notes.append(f"Next train in {best_wait:.0f} min" + (f", running every ~{min(headways):.0f} min" if headways else ""))
        out[complex_id] = {
            "wait_min": best_wait,
            "headway_min": min(headways) if headways else None,
            "quiet": quiet,
            "skipping": skipping,
            "no_service": no_service,
            "routes": routes,
            "notes": notes,
            "weight": round(min(REALTIME_WEIGHT_CAP, weight), 2),
        }
    return out, {"observed_at": snapshot.observed_at, "feed_errors": snapshot.feed_errors}


NO_SERVICE_MINUTES_LABEL = f"{gtfsrt.NO_SERVICE_MINUTES} min"


def score_stations(
    conn,
    anchor: datetime,
    window_start: datetime,
    window_end: datetime,
    include_live: bool = False,
) -> list[dict]:
    complexes = _complexes(conn)
    complaints, context = _complaint_terms(conn, anchor)
    calls = _call_terms(conn, anchor, window_start, window_end)
    ridership = _ridership_terms(conn, anchor)
    live = _live_terms(conn, complexes) if include_live else {}
    realtime, realtime_meta = _realtime_terms(conn, complexes, ridership) if include_live else ({}, None)

    results = []
    for complex_id, info in complexes.items():
        c = dict(complaints.get(complex_id, {"count": 0, "violent": 0, "per_30d": 0.0, "weight": 0.0}))
        c["band"] = context["band"]
        k = calls.get(complex_id, {"count": 0, "weight": 0.0})
        r = ridership.get(complex_id)
        l = live.get(complex_id)
        rt = realtime.get(complex_id)
        if r:
            # Per-rider correction: busy hubs see more complaints because more people pass through.
            c["raw_weight"] = c["weight"]
            c["weight"] = round(c["weight"] * r["busy_factor"], 2)
        weight = (
            c["weight"]
            + k["weight"]
            + (r["weight"] if r else 0.0)
            + (l["weight"] if l else 0.0)
            + (rt["weight"] if rt else 0.0)
        )
        flags = []
        if c["violent"] > 0:
            flags.append("violent_history")
        if k["count"] > 0:
            flags.append("active_calls")
        if r and r["quiet"]:
            flags.append("quiet_hour")
        if l:
            if any(item["kind"] == "alert" for item in l["items"]):
                flags.append("service_alert")
            if any(item["kind"] in ("elevator", "escalator") for item in l["items"]):
                flags.append("equipment_outage")
        if rt:
            stopping_any = any(r["stopping"] > 0 for r in rt["routes"].values())
            if rt["wait_min"] is not None and rt["wait_min"] > WAIT_FREE_MINUTES + 5:
                flags.append("long_wait")
            if rt["skipping"]:
                flags.append("skipped_stops")
            if rt["no_service"] and (not stopping_any or len(rt["no_service"]) == len(rt["routes"])):
                flags.append("no_service")
        results.append(
            {
                **info,
                "safety_score": _safety_score(weight),
                "weight": round(weight, 2),
                "breakdown": {
                    "complaints": c,
                    "calls": k,
                    "ridership": r,
                    "live": l,
                    "realtime": rt,
                },
                "flags": flags,
            }
        )
    results.sort(key=lambda item: item["safety_score"])
    return results


def scoring_context(conn, anchor: datetime, include_live: bool = False) -> dict:
    """Band / coverage / realtime metadata to show next to a station list."""
    days, cutoff = coverage_days(conn, "station_complaints", anchor)
    context: dict = {"band": band_for(anchor), "coverage_days": round(days), "history_until": cutoff}
    if include_live:
        snapshot = gtfsrt.current()
        context["realtime"] = gtfsrt.summary(snapshot) if snapshot else None
    return context


def score_stations_by_id(conn, anchor, window_start, window_end, include_live=False) -> dict[str, dict]:
    return {item["complex_id"]: item for item in score_stations(conn, anchor, window_start, window_end, include_live)}


def nearest_complex(conn, lat: float, lng: float, radius_meters: float = 250) -> str | None:
    row = conn.execute(
        """
        SELECT complex_id
        FROM subway_stations
        WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, %(radius)s)
        ORDER BY ST_Distance(geom, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography)
        LIMIT 1
        """,
        {"lat": lat, "lng": lng, "radius": radius_meters},
    ).fetchone()
    return row[0] if row else None


def station_detail(conn, complex_id: str, anchor: datetime, window_start: datetime, window_end: datetime) -> dict | None:
    scored = score_stations_by_id(conn, anchor, window_start, window_end, include_live=True).get(complex_id)
    if scored is None:
        return None
    days, cutoff = coverage_days(conn, "station_complaints", anchor)
    band = band_for(anchor)
    params = {"complex_id": complex_id, "hours": band_hours(band), "cutoff": cutoff}
    recent = conn.execute(
        """
        SELECT source_id, category, severity, law_cat, offense, timestamp
        FROM station_complaints
        WHERE complex_id = %(complex_id)s
          AND extract(hour FROM timestamp AT TIME ZONE 'America/New_York')::int = ANY(%(hours)s)
          AND (%(cutoff)s::timestamptz IS NULL OR timestamp <= %(cutoff)s)
        ORDER BY timestamp DESC
        LIMIT 25
        """,
        params,
    ).fetchall()
    by_category = conn.execute(
        """
        SELECT category, count(*)::int
        FROM station_complaints
        WHERE complex_id = %(complex_id)s
          AND extract(hour FROM timestamp AT TIME ZONE 'America/New_York')::int = ANY(%(hours)s)
          AND (%(cutoff)s::timestamptz IS NULL OR timestamp <= %(cutoff)s)
        GROUP BY category ORDER BY count(*) DESC
        """,
        params,
    ).fetchall()
    members = conn.execute(
        "SELECT station_id, name, routes, structure, ada, lat, lng FROM subway_stations WHERE complex_id = %(id)s ORDER BY name",
        {"id": complex_id},
    ).fetchall()
    return {
        **scored,
        "lookback_days": round(days),
        "band": band,
        "recent_complaints": [
            {
                "source_id": source_id,
                "category": category,
                "severity": severity,
                "law_cat": law_cat,
                "offense": offense,
                "timestamp": timestamp,
            }
            for source_id, category, severity, law_cat, offense, timestamp in recent
        ],
        "complaints_by_category": {category: count for category, count in by_category},
        "stations": [
            {"station_id": sid, "name": name, "routes": routes, "structure": structure, "ada": ada, "lat": lat, "lng": lng}
            for sid, name, routes, structure, ada, lat, lng in members
        ],
    }
