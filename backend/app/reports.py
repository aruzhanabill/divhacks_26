"""Citizen photo reports from Photon (or a direct API call).

Images are classified into the seven mapped incident categories or a
street-light outage, then upserted into the same PostGIS tables the map and
route scorer already read. Timestamps follow `sim_now` so a report lands in
the replay window instead of wall-clock now.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from app.categorize import CATEGORY_SEVERITY, INCIDENT_CATEGORIES, categorize
from app.db import pool
from app.ingest import NYC, UPSERT as INCIDENT_UPSERT, as_nyc, _in_nyc
from app.lights import DARK, UPSERT as LIGHT_UPSERT

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
OPENAI_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_LIGHT_NEEDLES = (
    "STREET LIGHT",
    "STREETLAMP",
    "STREET LAMP",
    "LAMPPOST",
    "LAMP POST",
    "BROKEN LAMP",
    "LIGHT OUT",
    "LIGHTS OUT",
    "DARK STREET",
)

# Captions people actually send, without changing NYPD typ_desc matching.
_CITIZEN_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("traffic", ("CRASH", "COLLISION", "CAR ACCIDENT", "WRECK", "HIT AND RUN", "FENDER")),
    ("violent", ("ASSAULT", "ROBBERY", "SHOOTING", "STABBING", "GUN", "WEAPON")),
    ("property", ("STOLEN", "THEFT", "BURGLARY", "VANDAL", "BROKEN WINDOW", "SMASHED CAR")),
    ("disorder", ("FIGHT", "HARASS", "PROWLER", "TRESPASS", "SCREAMING")),
    ("alarm", ("ALARM",)),
    ("medical", ("INJURED", "UNCONSCIOUS", "AMBULANCE", "OVERDOSE")),
    ("admin", ("POLICE CAR", "PATROL")),
)

_COORD = re.compile(
    r"(?P<lat>-?\d{1,2}\.\d+)\s*[, ]\s*(?P<lng>-?\d{1,3}\.\d+)"
)

_VISION_SCHEMA = """Return JSON only with this shape:
{
  "kind": "incident" | "street_light" | "unrelated",
  "category": "violent" | "property" | "disorder" | "alarm" | "traffic" | "medical" | "admin" | null,
  "summary": "one short sentence of what is happening",
  "place_text": "cross-street, neighborhood, or null if unknown",
  "confidence": 0.0
}

kind=street_light for a broken, out, missing, knocked-down, or dim street lamp.
kind=incident for something that matches one of the seven categories:
  violent (assault, robbery, weapon, shooting),
  property (theft, burglary, vandalism, stolen vehicle),
  disorder (fight, harassment, prowler, trespass),
  alarm (burglar/fire alarm going off),
  traffic (car crash, collision, wreck),
  medical (injury, unconscious person, ambulance scene),
  admin (police presence, patrol, quality-of-life without a crime).
kind=unrelated if it is not a street scene of those things.
"""


def _read_sim() -> dict | None:
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT sim_now, replay_start, replay_end FROM sim_state WHERE id = 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"sim_now": row[0], "replay_start": row[1], "replay_end": row[2]}


def report_timestamp() -> datetime:
    stored = _read_sim()
    if stored is None:
        return datetime.now(tz=NYC)
    start = as_nyc(stored["replay_start"])
    end = as_nyc(stored["replay_end"])
    now = as_nyc(stored["sim_now"])
    if now < start:
        return start
    if now >= end:
        return end - timedelta(seconds=1)
    return now


def gps_from_image(data: bytes) -> tuple[float, float] | None:
    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError):
        return None
    exif = image.getexif()
    if not exif:
        return None
    gps = exif.get_ifd(0x8825)
    if not gps:
        return None
    lat_values = gps.get(2)
    lng_values = gps.get(4)
    lat_ref = gps.get(1)
    lng_ref = gps.get(3)
    if not lat_values or not lng_values:
        return None

    def _to_deg(values: Any) -> float | None:
        try:
            deg, minutes, seconds = (float(part) for part in values[:3])
        except (TypeError, ValueError):
            return None
        return deg + minutes / 60.0 + seconds / 3600.0

    lat = _to_deg(lat_values)
    lng = _to_deg(lng_values)
    if lat is None or lng is None:
        return None
    if str(lat_ref).upper() == "S":
        lat = -lat
    if str(lng_ref).upper() == "W":
        lng = -lng
    return lat, lng


def coords_from_caption(caption: str | None) -> tuple[float, float] | None:
    if not caption:
        return None
    match = _COORD.search(caption)
    if not match:
        return None
    lat = float(match.group("lat"))
    lng = float(match.group("lng"))
    return lat, lng


def geocode_place(place_text: str | None) -> tuple[float, float] | None:
    query = (place_text or "").strip()
    if not query:
        return None
    key = os.environ.get("GOOGLE_MAPS_GEOCODE_KEY") or os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    with httpx.Client(timeout=20) as client:
        response = client.get(
            GEOCODE_URL,
            params={"address": query, "components": "administrative_area:NY|country:US", "key": key},
        )
        response.raise_for_status()
        payload = response.json()
    results = payload.get("results") or []
    if not results:
        return None
    location = results[0].get("geometry", {}).get("location") or {}
    try:
        lat = float(location["lat"])
        lng = float(location["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return lat, lng


def _as_jpeg(data: bytes) -> bytes:
    image = Image.open(BytesIO(data))
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _parse_vision_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("vision response was not an object")
    return data


def classify_from_caption(caption: str | None) -> dict | None:
    text = (caption or "").strip()
    if not text:
        return None
    upper = text.upper()
    if any(needle in upper for needle in _LIGHT_NEEDLES):
        return {
            "kind": "street_light",
            "category": None,
            "summary": text[:180],
            "place_text": text,
            "confidence": 0.4,
        }
    for name, needles in _CITIZEN_NEEDLES:
        if any(needle in upper for needle in needles):
            return {
                "kind": "incident",
                "category": name,
                "summary": text[:180],
                "place_text": text,
                "confidence": 0.45,
            }
    category, _severity = categorize(text, None)
    if category == "other":
        return None
    return {
        "kind": "incident",
        "category": category,
        "summary": text[:180],
        "place_text": text,
        "confidence": 0.35,
    }


def classify_image(image: bytes, caption: str | None) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        fallback = classify_from_caption(caption)
        if fallback:
            return fallback
        raise RuntimeError("OPENAI_API_KEY is not set and the caption did not name a mapped category")
    jpeg = _as_jpeg(image)
    import base64

    b64 = base64.b64encode(jpeg).decode("ascii")
    prompt = _VISION_SCHEMA
    if caption:
        prompt += f"\nThe sender also wrote: {caption[:500]}"
    payload = {
        "model": VISION_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60) as client:
        response = client.post(OPENAI_URL, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
    raw = body["choices"][0]["message"]["content"]
    parsed = _parse_vision_json(raw)
    kind = str(parsed.get("kind") or "unrelated")
    category = parsed.get("category")
    if kind == "incident" and category not in INCIDENT_CATEGORIES:
        fallback = classify_from_caption(caption)
        if fallback:
            return fallback
        kind = "unrelated"
        category = None
    if kind == "street_light":
        category = None
    return {
        "kind": kind,
        "category": category,
        "summary": str(parsed.get("summary") or caption or "Citizen photo report")[:240],
        "place_text": parsed.get("place_text") or caption,
        "confidence": float(parsed.get("confidence") or 0),
    }


def _default_coords() -> tuple[float, float] | None:
    lat_raw = os.environ.get("PHOTON_DEFAULT_LAT")
    lng_raw = os.environ.get("PHOTON_DEFAULT_LNG")
    if not lat_raw or not lng_raw:
        return None
    try:
        return float(lat_raw), float(lng_raw)
    except ValueError:
        return None


def resolve_location(
    image: bytes | None,
    caption: str | None,
    lat: float | None,
    lng: float | None,
    place_text: str | None,
) -> tuple[float, float] | None:
    if lat is not None and lng is not None:
        return lat, lng
    if image:
        gps = gps_from_image(image)
        if gps:
            return gps
    caption_coords = coords_from_caption(caption)
    if caption_coords:
        return caption_coords
    for candidate in (place_text, caption):
        geocoded = geocode_place(candidate)
        if geocoded:
            return geocoded
    return _default_coords()


def persist_report(
    *,
    source_id: str,
    kind: str,
    category: str | None,
    lat: float,
    lng: float,
    timestamp: datetime,
    summary: str,
) -> dict:
    if not _in_nyc(lat, lng):
        raise ValueError("location is outside New York City")
    timestamp = as_nyc(timestamp)
    if kind == "street_light":
        severity = DARK["Street Light Out"]
        record = {
            "source_id": source_id,
            "lat": lat,
            "lng": lng,
            "severity": severity,
            "timestamp": timestamp,
        }
        with pool.connection() as conn:
            conn.execute(LIGHT_UPSERT, record)
        return {
            "table": "street_lights",
            "source_id": source_id,
            "kind": kind,
            "category": None,
            "severity": severity,
            "lat": lat,
            "lng": lng,
            "timestamp": timestamp,
            "summary": summary,
        }
    if category not in INCIDENT_CATEGORIES:
        raise ValueError("unsupported incident category")
    severity = CATEGORY_SEVERITY[category]
    record = {
        "source_id": source_id,
        "lat": lat,
        "lng": lng,
        "category": category,
        "severity": severity,
        "timestamp": timestamp,
    }
    with pool.connection() as conn:
        conn.execute(INCIDENT_UPSERT, record)
    return {
        "table": "incidents",
        "source_id": source_id,
        "kind": kind,
        "category": category,
        "severity": severity,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp,
        "summary": summary,
    }


def ingest_photo_report(
    *,
    image: bytes | None,
    caption: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    message_id: str | None = None,
) -> dict:
    if not image and not (caption or "").strip():
        return {"status": "need_photo", "reply": "Send a photo of the street, crash, or crime, plus a cross-street if you can."}

    classification: dict | None = None
    if image:
        try:
            classification = classify_image(image, caption)
        except UnidentifiedImageError:
            return {"status": "bad_image", "reply": "I could not read that image. Try a JPEG or PNG still."}
        except RuntimeError as exc:
            return {"status": "need_vision", "reply": str(exc)}
        except httpx.HTTPError:
            fallback = classify_from_caption(caption)
            if fallback is None:
                return {"status": "vision_failed", "reply": "I could not read that photo just now. Describe what happened and the cross-street."}
            classification = fallback
    else:
        classification = classify_from_caption(caption)

    if classification is None or classification["kind"] == "unrelated":
        return {
            "status": "unrelated",
            "reply": "I only log street lamps out and the seven mapped categories (violent, property, disorder, alarm, traffic, medical, admin). Send another photo if this is one of those.",
        }

    coords = resolve_location(
        image,
        caption,
        lat,
        lng,
        classification.get("place_text"),
    )
    if coords is None:
        return {
            "status": "need_location",
            "kind": classification["kind"],
            "category": classification.get("category"),
            "summary": classification.get("summary"),
            "reply": "Got the photo. Where was this? Reply with a cross-street like 'Broadway and 116th' or coordinates.",
        }

    source_id = f"photon:{(message_id or uuid.uuid4().hex)}"
    try:
        stored = persist_report(
            source_id=source_id,
            kind=classification["kind"],
            category=classification.get("category"),
            lat=coords[0],
            lng=coords[1],
            timestamp=report_timestamp(),
            summary=str(classification.get("summary") or "Citizen photo report"),
        )
    except ValueError as exc:
        return {"status": "rejected", "reply": str(exc)}

    label = "street light out" if stored["kind"] == "street_light" else stored["category"]
    stored["status"] = "stored"
    stored["reply"] = (
        f"Logged as {label} at {stored['lat']:.5f}, {stored['lng']:.5f}. "
        "It is on the map for the current replay clock."
    )
    return stored
