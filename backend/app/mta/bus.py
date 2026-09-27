"""Bus context: service alerts, hourly ridership by route, optional live arrivals.

Bus stops are street corners, so their crime term is the street-complaint
history around the stop (see `history.point_history`). This module adds what
is bus-specific:

- alerts: MTA bus service alerts (no key), keyed by route short name such as
  "M4" or "B41". Detours and skipped stops matter because the rider ends up
  waiting at a stop the bus will not serve.
- ridership: MTA Bus Hourly Ridership (data.ny.gov gxb3-akrn) aggregated to
  riders per hour by route, day type and local hour. A route carrying 12
  riders an hour at 2 AM is a long, lonely wait; 400 is a crowd.
- live (optional): MTA Bus Time SIRI StopMonitoring when MTA_BUS_TIME_KEY is
  set. Without a key, waits fall back to the scheduled wait Google reports.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.db import pool
from app.ingest import _headers, as_nyc
from app.mta.history import day_type_for, record_run, run_covers
from app.mta.realtime import ALERT_SEVERITY, DEFAULT_ALERT_SEVERITY, _active, _text

BUS_ALERTS_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fbus-alerts.json"
BUS_RIDERSHIP_URL = "https://data.ny.gov/resource/gxb3-akrn.json"
BUS_TIME_STOPS_URL = "https://bustime.mta.info/api/where/stops-for-location.json"
BUS_TIME_MONITOR_URL = "https://bustime.mta.info/api/siri/stop-monitoring.json"
CACHE_SECONDS = 60
RIDERSHIP_WEEKS = 4

BUS_ALERT_SEVERITY = {
    **ALERT_SEVERITY,
    "Detour": 0.45,
    "Planned - Detour": 0.3,
    "Planned - Stops Skipped": 0.5,
    "Boarding Change": 0.1,
    "Special Notice": 0.1,
}

_lock = threading.Lock()
_alerts_cache: dict = {"at": 0.0, "observed_at": None, "by_route": {}}


def normalize_route(name: str | None) -> str | None:
    """'M4', 'm4', 'MTA NYCT_M4', 'M4-SBS', 'Bx12+' -> 'M4' / 'BX12' style keys."""
    if not name:
        return None
    text = str(name).split("_")[-1].strip().upper()
    text = text.replace("-SBS", "").replace(" SBS", "").replace("+", "")
    return text or None


# --- alerts -------------------------------------------------------------------


def _fetch_alerts(client: httpx.Client) -> dict[str, list[dict]]:
    response = client.get(BUS_ALERTS_URL)
    response.raise_for_status()
    payload = response.json()
    now_s = time.time()
    by_route: dict[str, list[dict]] = {}
    for entity in payload.get("entity", []):
        alert = entity.get("alert") or {}
        if not _active(alert.get("active_period") or [], now_s):
            continue
        mercury = alert.get("transit_realtime.mercury_alert") or {}
        alert_type = str(mercury.get("alert_type") or "").strip()
        severity = BUS_ALERT_SEVERITY.get(alert_type, DEFAULT_ALERT_SEVERITY)
        if severity <= 0:
            continue
        header = _text(alert.get("header_text")) or alert_type or "Service alert"
        summary = f"{alert_type}: {header}" if alert_type and not header.startswith(alert_type) else header
        routes = {normalize_route(informed.get("route_id")) for informed in alert.get("informed_entity", [])}
        for route in routes:
            if route:
                by_route.setdefault(route, []).append(
                    {"kind": "alert", "severity": severity, "summary": summary[:300], "route": route, "alert_type": alert_type}
                )
    return by_route


def alerts(force: bool = False) -> dict:
    """{route -> [alert items]} for every bus route with an active alert (cached)."""
    with _lock:
        age = time.time() - _alerts_cache["at"]
        if not force and _alerts_cache["by_route"] and age < CACHE_SECONDS:
            return {"observed_at": _alerts_cache["observed_at"], "by_route": _alerts_cache["by_route"], "cached": True}
        with httpx.Client(timeout=30) as client:
            by_route = _fetch_alerts(client)
        observed_at = datetime.now(timezone.utc)
        _alerts_cache.update({"at": time.time(), "observed_at": observed_at, "by_route": by_route})
        return {"observed_at": observed_at, "by_route": by_route, "cached": False}


def alerts_for(route: str | None) -> list[dict]:
    key = normalize_route(route)
    if not key:
        return []
    try:
        return list(alerts()["by_route"].get(key, []))
    except Exception:  # noqa: BLE001
        return list(_alerts_cache["by_route"].get(key, []))


# --- ridership ----------------------------------------------------------------

RIDERSHIP_UPSERT = """
INSERT INTO bus_ridership_hourly (bus_route, day_type, local_hour, riders_per_hour, sample_hours)
VALUES (%(bus_route)s, %(day_type)s, %(local_hour)s, %(riders)s, %(hours)s)
ON CONFLICT (bus_route, day_type, local_hour) DO UPDATE SET
    riders_per_hour = EXCLUDED.riders_per_hour,
    sample_hours = EXCLUDED.sample_hours
"""


def ingest_bus_ridership(end: datetime | None = None, weeks: int = RIDERSHIP_WEEKS, force: bool = False) -> dict:
    """Aggregate riders per hour by route / day type / local hour over the `weeks` before `end`.

    The dataset lags a few weeks; if `end` is past its newest row we anchor on
    the newest row instead so the aggregate is never empty.
    """
    with pool.connection() as conn, httpx.Client(timeout=120) as client:
        latest = client.get(BUS_RIDERSHIP_URL, headers=_headers(), params={"$select": "max(transit_timestamp)"})
        latest.raise_for_status()
        newest_raw = (latest.json() or [{}])[0].get("max_transit_timestamp")
        if not newest_raw:
            return {"rows": 0, "error": "bus ridership dataset is empty"}
        newest = as_nyc(datetime.fromisoformat(newest_raw[:19]))
        until = min(as_nyc(end), newest) if end is not None else newest
        since = until - timedelta(weeks=weeks)
        if not force and run_covers(conn, "bus_ridership", since, until):
            total = conn.execute("SELECT count(*) FROM bus_ridership_hourly").fetchone()[0]
            return {"rows": 0, "cached": int(total), "start": since, "end": until}
        written = 0
        for day_type, dow_clause in (
            ("weekday", "date_extract_dow(transit_timestamp) between 1 and 5"),
            ("weekend", "(date_extract_dow(transit_timestamp) = 0 OR date_extract_dow(transit_timestamp) = 6)"),
        ):
            response = client.get(
                BUS_RIDERSHIP_URL,
                headers=_headers(),
                params={
                    "$select": "bus_route, date_extract_hh(transit_timestamp) AS hh, sum(ridership) AS riders, count(distinct transit_timestamp) AS hours",
                    "$where": (
                        f"transit_timestamp >= '{since.strftime('%Y-%m-%dT%H:%M:%S')}' "
                        f"AND transit_timestamp < '{until.strftime('%Y-%m-%dT%H:%M:%S')}' AND {dow_clause}"
                    ),
                    "$group": "bus_route, hh",
                    "$limit": 20000,
                },
            )
            response.raise_for_status()
            rows = []
            for row in response.json():
                route = normalize_route(row.get("bus_route"))
                try:
                    hour = int(row["hh"])
                    riders = float(row["riders"])
                    hours = max(1, int(float(row["hours"])))
                except (KeyError, TypeError, ValueError):
                    continue
                if not route:
                    continue
                rows.append({"bus_route": route, "day_type": day_type, "local_hour": hour, "riders": riders / hours, "hours": hours})
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(RIDERSHIP_UPSERT, rows)
                written += len(rows)
        record_run(conn, "bus_ridership", since, until, written)
    return {"rows": written, "start": since, "end": until}


def typical_riders(conn, route: str | None, anchor: datetime) -> float | None:
    key = normalize_route(route)
    if not key:
        return None
    row = conn.execute(
        """
        SELECT riders_per_hour FROM bus_ridership_hourly
        WHERE bus_route = %(route)s AND day_type = %(day_type)s AND local_hour = %(hour)s
        """,
        {"route": key, "day_type": day_type_for(anchor), "hour": as_nyc(anchor).hour},
    ).fetchone()
    return float(row[0]) if row else None


# --- live arrivals (optional key) -------------------------------------------------


def bus_time_key() -> str | None:
    return os.environ.get("MTA_BUS_TIME_KEY") or None


def live_wait(lat: float, lng: float, route: str | None) -> dict | None:
    """Minutes until the next bus of `route` at the stop nearest (lat, lng), via Bus Time. None without a key."""
    key = bus_time_key()
    line = normalize_route(route)
    if not key or not line:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            stops = client.get(
                BUS_TIME_STOPS_URL,
                params={"key": key, "lat": lat, "lon": lng, "radius": 120},
            ).json()
            candidates = (stops.get("data") or {}).get("stops") or []
            stop_id = None
            for stop in candidates:
                routes = {normalize_route(r.get("shortName")) for r in stop.get("routes", [])}
                if line in routes:
                    stop_id = stop.get("id")
                    break
            if not stop_id:
                return None
            monitor = client.get(
                BUS_TIME_MONITOR_URL,
                params={"key": key, "version": 2, "MonitoringRef": stop_id, "MaximumStopVisits": 3},
            ).json()
        delivery = monitor["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]
        visits = delivery.get("MonitoredStopVisit") or []
        now = datetime.now(timezone.utc)
        waits = []
        for visit in visits:
            journey = visit.get("MonitoredVehicleJourney") or {}
            if normalize_route(journey.get("PublishedLineName", [None])[0] if isinstance(journey.get("PublishedLineName"), list) else journey.get("PublishedLineName")) != line:
                continue
            call = journey.get("MonitoredCall") or {}
            expected = call.get("ExpectedArrivalTime") or call.get("AimedArrivalTime")
            if expected:
                waits.append((datetime.fromisoformat(expected.replace("Z", "+00:00")) - now).total_seconds() / 60)
        if not waits:
            return {"wait_min": None, "stop_id": stop_id, "source": "bus_time"}
        return {"wait_min": round(max(0.0, min(waits)), 1), "stop_id": stop_id, "source": "bus_time"}
    except Exception:  # noqa: BLE001
        return None
