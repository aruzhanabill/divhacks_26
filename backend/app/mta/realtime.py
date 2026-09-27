"""Live MTA status: subway service alerts and elevator/escalator outages.

These are the only wall-clock feeds in the app. They are fetched from the
backend on demand (never from the browser), cached for `CACHE_SECONDS`, and
written to `station_status_snapshots` with a shared `observed_at` so a demo
can be replayed from a captured snapshot. Scoring treats them as a small
status penalty on top of the historical terms, not as a crime signal.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.db import pool

ALERTS_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
OUTAGES_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fnyct_ene.json"
CACHE_SECONDS = 60
KEEP_SNAPSHOTS_HOURS = 24

# Alert types from the Mercury extension. Anything unlisted gets DEFAULT_ALERT_SEVERITY.
ALERT_SEVERITY: dict[str, float] = {
    "Delays": 0.75,
    "Expect Delays": 0.6,
    "Suspended": 0.75,
    "Part Suspended": 0.6,
    "Planned - Suspended": 0.5,
    "Planned - Part Suspended": 0.4,
    "Reduced Service": 0.4,
    "Planned - Reroute": 0.35,
    "Service Change": 0.35,
    "Boarding Change": 0.2,
    "Planned - Express to Local": 0.15,
    "Planned - Stops Skipped": 0.3,
    "Station Notice": 0.15,
    "Special Schedule": 0.1,
    "Extra Service": 0.0,
}
DEFAULT_ALERT_SEVERITY = 0.2
ELEVATOR_SEVERITY = 0.25
ESCALATOR_SEVERITY = 0.1

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "observed_at": None, "items": []}


def _norm_name(value: str) -> str:
    text = value.lower()
    text = text.replace("&", "and").replace("/", " ").replace("-", " ")
    text = re.sub(r"(\d)(st|av|nd|rd|th)\b", r"\1 \2", text)  # "42st" -> "42 st"
    text = re.sub(r"\b(\d+)\s+(nd|rd|th)\b", r"\1", text)  # "42 nd" -> "42"
    text = re.sub(r"\b(st|street)\b", "st", text)
    text = re.sub(r"\b(av|ave|avenue)\b", "av", text)
    text = re.sub(r"\b(blvd|boulevard)\b", "blvd", text)
    text = re.sub(r"\b(pkwy|parkway)\b", "pkwy", text)
    text = re.sub(r"\b(sq|square)\b", "sq", text)
    text = re.sub(r"\b(ctr|center)\b", "ctr", text)
    text = re.sub(r"\b(rd|road)\b", "rd", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return " ".join(text.split())


def _station_index(conn) -> tuple[dict[str, str], list[tuple[str, str, set[str]]]]:
    by_stop: dict[str, str] = {}
    by_name: list[tuple[str, str, set[str]]] = []
    for gtfs_stop_id, complex_id, name, routes in conn.execute(
        "SELECT gtfs_stop_id, complex_id, name, routes FROM subway_stations"
    ).fetchall():
        by_stop[gtfs_stop_id] = complex_id
        by_name.append((_norm_name(name), complex_id, set(routes or [])))
    return by_stop, by_name


def _match_name(name: str, routes: set[str], by_name: list[tuple[str, str, set[str]]]) -> str | None:
    target = _norm_name(name)
    if not target:
        return None
    best: tuple[int, str] | None = None
    for norm, complex_id, station_routes in by_name:
        if norm != target and not (len(target) > 6 and (target in norm or norm in target)):
            continue
        shared = len(routes & station_routes) if routes else 0
        score = (2 if norm == target else 1) * 10 + shared
        if best is None or score > best[0]:
            best = (score, complex_id)
    return best[1] if best else None


def _text(block: dict | None) -> str:
    if not block:
        return ""
    for item in block.get("translation", []):
        if item.get("language", "en") in ("en", "en-html"):
            return str(item.get("text") or "").strip()
    return ""


def _active(periods: list[dict], now_s: float) -> bool:
    if not periods:
        return True
    for period in periods:
        start = float(period.get("start") or 0)
        end = period.get("end")
        if start <= now_s and (end is None or now_s <= float(end)):
            return True
    return False


def _fetch_alerts(client: httpx.Client, by_stop: dict[str, str]) -> list[dict]:
    response = client.get(ALERTS_URL)
    response.raise_for_status()
    payload = response.json()
    now_s = time.time()
    items: list[dict] = []
    for entity in payload.get("entity", []):
        alert = entity.get("alert") or {}
        if not _active(alert.get("active_period") or [], now_s):
            continue
        mercury = alert.get("transit_realtime.mercury_alert") or {}
        alert_type = str(mercury.get("alert_type") or "").strip()
        severity = ALERT_SEVERITY.get(alert_type, DEFAULT_ALERT_SEVERITY)
        if severity <= 0:
            continue
        header = _text(alert.get("header_text")) or alert_type or "Service alert"
        summary = f"{alert_type}: {header}" if alert_type and not header.startswith(alert_type) else header
        routes: set[str] = set()
        complexes: set[str] = set()
        for informed in alert.get("informed_entity", []):
            route_id = informed.get("route_id")
            stop_id = informed.get("stop_id")
            if stop_id:
                complex_id = by_stop.get(str(stop_id)) or by_stop.get(str(stop_id).rstrip("NS"))
                if complex_id:
                    complexes.add(complex_id)
            elif route_id:
                routes.add(str(route_id))
        payload_min = {"alert_type": alert_type, "id": entity.get("id")}
        # Stop-scoped alerts attach to those complexes; route-scoped ones attach to the route.
        for complex_id in complexes:
            items.append(
                {"complex_id": complex_id, "route_id": None, "kind": "alert", "severity": severity, "summary": summary[:300], "payload": payload_min}
            )
        if not complexes:
            for route_id in routes:
                items.append(
                    {"complex_id": None, "route_id": route_id, "kind": "alert", "severity": severity, "summary": summary[:300], "payload": payload_min}
                )
    return items


def _fetch_outages(client: httpx.Client, by_name: list[tuple[str, str, set[str]]]) -> list[dict]:
    response = client.get(OUTAGES_URL)
    response.raise_for_status()
    payload = response.json()
    items: list[dict] = []
    for row in payload if isinstance(payload, list) else []:
        if str(row.get("isupcomingoutage") or "N").upper().startswith("Y"):
            continue
        kind = "elevator" if str(row.get("equipmenttype") or "").upper() == "EL" else "escalator"
        routes = {part.strip() for part in str(row.get("trainno") or "").split("/") if part.strip() and part.strip() != "LIRR"}
        complex_id = _match_name(str(row.get("station") or ""), routes, by_name)
        ada = str(row.get("ADA") or "N").upper().startswith("Y")
        severity = (ELEVATOR_SEVERITY if kind == "elevator" else ESCALATOR_SEVERITY) * (1.5 if ada else 1.0)
        serving = str(row.get("serving") or "").strip()
        reason = str(row.get("reason") or "").strip()
        summary = f"{kind.title()} out{' (ADA)' if ada else ''}: {serving or row.get('equipment', '')}"
        if reason:
            summary += f" — {reason}"
        items.append(
            {
                "complex_id": complex_id,
                "route_id": None,
                "kind": kind,
                "severity": round(severity, 3),
                "summary": summary[:300],
                "payload": {
                    "station": row.get("station"),
                    "equipment": row.get("equipment"),
                    "ada": ada,
                    "return": row.get("estimatedreturntoservice"),
                },
            }
        )
    return items


def refresh_status(force: bool = False) -> dict:
    """Fetch both feeds (cached), persist a snapshot, return the normalized items."""
    with _lock:
        age = time.time() - _cache["at"]
        if not force and _cache["items"] and age < CACHE_SECONDS:
            return {"observed_at": _cache["observed_at"], "items": _cache["items"], "cached": True}
        observed_at = datetime.now(timezone.utc)
        with pool.connection() as conn:
            by_stop, by_name = _station_index(conn)
            with httpx.Client(timeout=30) as client:
                items = _fetch_alerts(client, by_stop) + _fetch_outages(client, by_name)
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO station_status_snapshots
                        (observed_at, complex_id, route_id, kind, severity, summary, payload)
                    VALUES (%(observed_at)s, %(complex_id)s, %(route_id)s, %(kind)s, %(severity)s, %(summary)s, %(payload)s::jsonb)
                    """,
                    [
                        {**item, "observed_at": observed_at, "payload": _json(item["payload"])}
                        for item in items
                    ],
                )
            conn.execute(
                "DELETE FROM station_status_snapshots WHERE observed_at < %(cutoff)s",
                {"cutoff": observed_at - timedelta(hours=KEEP_SNAPSHOTS_HOURS)},
            )
        _cache.update({"at": time.time(), "observed_at": observed_at, "items": items})
        return {"observed_at": observed_at, "items": items, "cached": False}


def ensure_fresh_status() -> None:
    """Best effort: refresh if stale, never raise. Scoring falls back to the last snapshot."""
    try:
        refresh_status()
    except Exception:
        pass


def _json(value: dict) -> str:
    return json.dumps(value, default=str)
