from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.db import close_db, init_db, pool
from app.ingest import NYC, as_nyc, fetch_latest_add_ts, ingest_window
from app.lights import ingest_street_lights
from app.reports import ingest_photo_report
from app.scoring import ScoreRequest, score_routes

MAX_WINDOW = timedelta(hours=6)
SELECT_INCIDENTS = """
SELECT source_id, lat, lng, category, severity, timestamp
FROM incidents
WHERE timestamp >= %(start)s AND timestamp <= %(end)s
ORDER BY timestamp, source_id
"""
SELECT_LIGHTS = """
SELECT source_id, lat, lng, severity, timestamp
FROM street_lights
WHERE timestamp >= %(start)s AND timestamp <= %(end)s
ORDER BY timestamp, source_id
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


def _rows(sql: str, start: datetime, end: datetime) -> list[dict]:
    with pool.connection() as conn:
        cur = conn.execute(sql, {"start": start, "end": end})
        columns = [col.name for col in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _latest_stored() -> datetime | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT max(timestamp) FROM incidents").fetchone()
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
    return {
        "sim_now": until,
        "start": since,
        "end": until,
        "timezone": str(NYC),
        "incidents": _rows(SELECT_INCIDENTS, since, until),
        "street_lights": _rows(SELECT_LIGHTS, since, until),
    }


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
