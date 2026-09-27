from __future__ import annotations

import base64
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.db import close_db, init_db, pool
from app.ingest import NYC, as_nyc, fetch_latest_add_ts, ingest_window
from app.lights import ingest_street_lights
from app.mta import bus, gtfsrt
from app.mta.complaints import DEFAULT_LOOKBACK as COMPLAINT_LOOKBACK, ingest_complaints
from app.mta.history import ingest_street_complaints
from app.mta.realtime import ensure_fresh_status, refresh_status
from app.mta.ridership import ingest_ridership, ingest_ridership_profile
from app.mta.routes import TransitScoreRequest, score_transit_routes
from app.mta.scoring import score_stations, scoring_context, station_detail
from app.mta.stations import ingest_stations, station_count
from app.reports import ingest_photo_report
from app.scoring import ScoreRequest, explicit_window, score_routes

MAX_WINDOW = timedelta(hours=6)
SELECT_INCIDENTS = """
SELECT source_id, lat, lng, category, severity, timestamp, headline, is_transit
FROM incidents
WHERE timestamp >= %(start)s AND timestamp <= %(end)s
ORDER BY timestamp, source_id
"""
SELECT_LIGHTS = """
SELECT source_id, lat, lng, severity, timestamp, headline
FROM street_lights
WHERE timestamp >= %(start)s AND timestamp <= %(end)s
ORDER BY timestamp, source_id
"""
SELECT_PHOTON_INCIDENTS = """
SELECT source_id, lat, lng, category, severity, timestamp, headline
FROM incidents
WHERE source_id LIKE %(prefix)s
ORDER BY timestamp DESC
LIMIT 50
"""
SELECT_PHOTON_LIGHTS = """
SELECT source_id, lat, lng, severity, timestamp, headline
FROM street_lights
WHERE source_id LIKE %(prefix)s
ORDER BY timestamp DESC
LIMIT 50
"""


class IngestBody(BaseModel):
    start: datetime
    end: datetime


class SimBody(BaseModel):
    sim_now: datetime
    replay_start: datetime
    replay_end: datetime


class ReportBody(BaseModel):
    caption: str | None = None
    lat: float | None = None
    lng: float | None = None
    message_id: str | None = None
    image_base64: str | None = None


def _check_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start = as_nyc(start)
    end = as_nyc(end)
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if end - start > MAX_WINDOW:
        raise HTTPException(status_code=400, detail="window must be 6 hours or less")
    return start, end


def _rows(sql: str, params: dict) -> list[dict]:
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        columns = [col.name for col in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _merge_rows(primary: list[dict], extra: list[dict]) -> list[dict]:
    by_id = {row["source_id"]: row for row in primary}
    for row in extra:
        by_id[row["source_id"]] = row
    return list(by_id.values())


def _latest_stored() -> datetime | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT max(timestamp) FROM incidents WHERE source_id NOT LIKE 'photon:%'"
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


def _read_sim() -> dict | None:
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT sim_now, replay_start, replay_end FROM sim_state WHERE id = 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"sim_now": row[0], "replay_start": row[1], "replay_end": row[2]}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield
    close_db()


app = FastAPI(title="NYC Safe Routing", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT PostGIS_Version()")
    return {"ok": True}


@app.get("/dataset")
def dataset() -> dict:
    latest = _latest_stored()
    source = "Tiger Cloud"
    if latest is None:
        try:
            latest = fetch_latest_add_ts()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="NYPD Calls for Service request failed") from exc
        source = "NYPD Calls for Service (Year to Date)"
    suggested = None
    if latest is not None:
        floored = (as_nyc(latest) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        suggested = floored.strftime("%Y-%m-%dT%H:%M")
    return {
        "source": source,
        "latest_add_ts": latest,
        "suggested_start": suggested,
        "timezone": "America/New_York",
    }


@app.post("/ingest")
def ingest(body: IngestBody) -> dict:
    start, end = _check_window(body.start, body.end)
    try:
        result = ingest_window(start, end)
        result["street_lights"] = ingest_street_lights(end)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] or "NYPD Calls for Service request failed"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="NYPD Calls for Service request failed") from exc
    return result


@app.post("/routes/score")
def routes_score(body: ScoreRequest) -> dict:
    """Higher safety_score is safer. Risk is crimes inside the corridor, not heatmap pixels."""
    return score_routes(body)


@app.get("/incidents")
def incidents(
    sim_now: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    lookback_hours: float = Query(default=24, gt=0, le=168),
) -> dict:
    stored = _read_sim()
    if sim_now is not None:
        until = as_nyc(sim_now)
        since = as_nyc(start) if start is not None else until - timedelta(hours=lookback_hours)
    elif start is not None and end is not None:
        since, until = _check_window(start, end)
    elif stored is not None:
        since = stored["replay_start"]
        until = stored["sim_now"]
    else:
        raise HTTPException(
            status_code=400,
            detail="provide start and end, sim_now, or set the simulation clock",
        )
    if until < since:
        until = since
    photon = {"prefix": "photon:%"}
    return {
        "sim_now": until,
        "start": since,
        "end": until,
        "timezone": str(NYC),
        "incidents": _merge_rows(
            _rows(SELECT_INCIDENTS, {"start": since, "end": until}),
            _rows(SELECT_PHOTON_INCIDENTS, photon),
        ),
        "street_lights": _merge_rows(
            _rows(SELECT_LIGHTS, {"start": since, "end": until}),
            _rows(SELECT_PHOTON_LIGHTS, photon),
        ),
    }


@app.get("/reports/live")
def live_reports() -> dict:
    """Photon rows only, so the map can show a just-logged pin without a replay window."""
    photon = {"prefix": "photon:%"}
    return {
        "incidents": _rows(SELECT_PHOTON_INCIDENTS, photon),
        "street_lights": _rows(SELECT_PHOTON_LIGHTS, photon),
    }


# --- MTA subway safety (additive; walking endpoints above are unchanged) ---


class MtaIngestBody(BaseModel):
    start: datetime
    end: datetime
    complaint_lookback_days: int = COMPLAINT_LOOKBACK.days
    include_street: bool = True
    include_bus: bool = True


COMPLAINT_PUBLICATION_LAG = timedelta(days=100)


def _history_range(start: datetime, end: datetime, lookback: timedelta) -> tuple[datetime, datetime]:
    """NYPD complaint data is published quarterly, months behind. A lookback
    from a window set to tonight would be empty, so the lookback is measured
    from whichever is earlier: the window end, or now minus the lag."""
    newest_plausible = datetime.now(NYC) - COMPLAINT_PUBLICATION_LAG
    return min(end, newest_plausible) - lookback, end


_mta_ingest_lock = threading.Lock()


@app.post("/mta/ingest")
def mta_ingest(body: MtaIngestBody) -> dict:
    """Load station reference (once), subway + street complaints for the lookback, ridership baselines.

    One load at a time: a second call while one is running returns {"busy": true}
    instead of racing it on the same rows."""
    if not _mta_ingest_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        return _mta_ingest(body)
    finally:
        _mta_ingest_lock.release()


def _mta_ingest(body: MtaIngestBody) -> dict:
    start, end = _check_window(body.start, body.end)
    result: dict = {}
    lookback = timedelta(days=max(1, min(365, body.complaint_lookback_days)))
    hist_start, hist_end = _history_range(start, end, lookback)
    try:
        if station_count() == 0:
            result["stations"] = ingest_stations()
        else:
            result["stations"] = {"fetched": 0, "upserted": 0, "cached": station_count()}
        result["complaints"] = ingest_complaints(hist_start, hist_end)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] or "MTA / NYPD complaint request failed"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="MTA / NYPD complaint request failed") from exc
    # Everything below is a normaliser or an optional term; a slow open-data host must not fail the load.
    if body.include_street:
        try:
            result["street_complaints"] = ingest_street_complaints(hist_start, hist_end)
        except Exception as exc:  # noqa: BLE001
            result["street_complaints"] = {"error": str(exc)[:200]}
    try:
        result["ridership"] = ingest_ridership(start, end)
    except Exception as exc:  # noqa: BLE001
        result["ridership"] = {"error": str(exc)[:200]}
    try:
        result["ridership_profile"] = ingest_ridership_profile(end)
    except Exception as exc:  # noqa: BLE001
        result["ridership_profile"] = {"error": str(exc)[:200]}
    if body.include_bus:
        try:
            result["bus_ridership"] = bus.ingest_bus_ridership(end)
        except Exception as exc:  # noqa: BLE001
            result["bus_ridership"] = {"error": str(exc)[:200]}
    return result


@app.get("/mta/realtime")
def mta_realtime(complex_id: str | None = None, refresh: bool = False) -> dict:
    """Live GTFS-RT state: routes running, and per-route waits / skipped stops for one complex."""
    snapshot = gtfsrt.refresh(force=refresh) if refresh else gtfsrt.current()
    if snapshot is None:
        raise HTTPException(status_code=502, detail="GTFS-RT feeds unavailable")
    out = gtfsrt.summary(snapshot)
    if complex_id:
        with pool.connection() as conn:
            stops = gtfsrt.complex_stops(conn).get(complex_id)
            row = conn.execute(
                "SELECT array_agg(DISTINCT r) FROM subway_stations s, unnest(s.routes) r WHERE complex_id = %(id)s",
                {"id": complex_id},
            ).fetchone()
        if not stops:
            raise HTTPException(status_code=404, detail="unknown station complex")
        out["complex_id"] = complex_id
        out["station"] = snapshot.station_status(stops, list(row[0] or []))
    try:
        out["bus_alerts"] = {"routes_with_alerts": len(bus.alerts()["by_route"])}
    except Exception:  # noqa: BLE001
        out["bus_alerts"] = None
    return out


@app.get("/mta/status")
def mta_status(refresh: bool = False) -> dict:
    """Live subway alerts and elevator/escalator outages (cached 60 s, snapshot persisted)."""
    try:
        return refresh_status(force=refresh)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="MTA status feed request failed") from exc


@app.post("/routes/transit/score")
def routes_transit_score(body: TransitScoreRequest) -> dict:
    """Subway / bus routes: walking legs by corridor plus the stops you wait at. Higher is safer."""
    if body.include_live:
        ensure_fresh_status()
    return score_transit_routes(body)


@app.get("/stations")
def stations(sim_now: datetime | None = None, include_live: bool = False) -> dict:
    """Every station complex with a safety score at the sim clock. Overlay data, not routing truth.

    An explicit `sim_now` is honoured even outside the replay window."""
    start, end, anchor = explicit_window(sim_now)
    if include_live:
        ensure_fresh_status()
    with pool.connection() as conn:
        items = score_stations(conn, anchor, start, end, include_live=include_live)
        context = scoring_context(conn, anchor, include_live=include_live)
    return {"sim_now": anchor, "start": start, "end": end, "count": len(items), "context": context, "stations": items}


@app.get("/stations/{complex_id}")
def station(complex_id: str, sim_now: datetime | None = None) -> dict:
    start, end, anchor = explicit_window(sim_now)
    ensure_fresh_status()
    with pool.connection() as conn:
        detail = station_detail(conn, complex_id, anchor, start, end)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown station complex")
    return {"sim_now": anchor, "start": start, "end": end, **detail}


@app.get("/sim")
def get_sim() -> dict:
    stored = _read_sim()
    if stored is None:
        raise HTTPException(status_code=404, detail="simulation clock is not set")
    return stored


@app.post("/reports")
def create_report(body: ReportBody) -> dict:
    """Classify a Photon (or test) photo and upsert into incidents or street_lights."""
    image = None
    if body.image_base64:
        try:
            image = base64.b64decode(body.image_base64, validate=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="image_base64 is not valid") from exc
        if len(image) > 8_000_000:
            raise HTTPException(status_code=413, detail="image is too large")
    return ingest_photo_report(
        image=image,
        caption=body.caption,
        lat=body.lat,
        lng=body.lng,
        message_id=body.message_id,
    )


@app.put("/sim")
def put_sim(body: SimBody) -> dict:
    sim_now = as_nyc(body.sim_now)
    replay_start = as_nyc(body.replay_start)
    replay_end = as_nyc(body.replay_end)
    if replay_end <= replay_start:
        raise HTTPException(status_code=400, detail="replay_end must be after replay_start")
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO sim_state (id, sim_now, replay_start, replay_end)
            VALUES (1, %(sim_now)s, %(replay_start)s, %(replay_end)s)
            ON CONFLICT (id) DO UPDATE SET
                sim_now = EXCLUDED.sim_now,
                replay_start = EXCLUDED.replay_start,
                replay_end = EXCLUDED.replay_end
            """,
            {
                "sim_now": sim_now,
                "replay_start": replay_start,
                "replay_end": replay_end,
            },
        )
    return {"sim_now": sim_now, "replay_start": replay_start, "replay_end": replay_end}
