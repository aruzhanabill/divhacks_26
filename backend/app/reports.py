"""Citizen photo reports from Photon (or a direct API call).

Images are classified into the seven mapped incident categories or a
street-light outage, then upserted into the same PostGIS tables the map and
route scorer already read. The pin time is when the photo was taken (EXIF)
or when the report was uploaded, not the simulation clock.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from app.categorize import CATEGORY_SEVERITY, INCIDENT_CATEGORIES, categorize
from app.db import pool
from app.ingest import NYC, as_nyc, _in_nyc
from app.lights import DARK

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

INCIDENT_UPSERT = """
INSERT INTO incidents (source_id, lat, lng, geom, category, severity, timestamp, headline)
VALUES (
    %(source_id)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
    %(category)s,
    %(severity)s,
    %(timestamp)s,
    %(headline)s
)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    category = EXCLUDED.category,
    severity = EXCLUDED.severity,
    timestamp = EXCLUDED.timestamp,
    headline = EXCLUDED.headline
"""

LIGHT_UPSERT = """
INSERT INTO street_lights (source_id, lat, lng, geom, severity, timestamp, headline)
VALUES (
    %(source_id)s,
    %(lat)s,
    %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
    %(severity)s,
    %(timestamp)s,
    %(headline)s
)
ON CONFLICT (source_id) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = EXCLUDED.geom,
    severity = EXCLUDED.severity,
    timestamp = EXCLUDED.timestamp,
    headline = EXCLUDED.headline
"""

_HEADLINE = {
    "violent": "Violent crime",
    "property": "Property crime",
    "disorder": "Disorder",
    "alarm": "Alarm",
    "traffic": "Car crash",
    "medical": "Medical emergency",
    "admin": "Police activity",
}
VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
OPENAI_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_LIGHT_NEEDLES = (
    "STREET LIGHT",
    "STREETLIGHT",
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

# Longest needles first. All tokens in a row must appear in the text.
_PLACE_ALIASES: tuple[tuple[tuple[str, ...], float, float], ...] = (
    (("COLUMBIA", "116"), 40.80754, -73.96257),
    (("COLUMBIA", "BROADWAY"), 40.80754, -73.96257),
    (("COLUMBIA", "GATES"), 40.80754, -73.96257),
    (("COLLEGE WALK",), 40.80754, -73.96257),
    (("116TH", "BROADWAY"), 40.80754, -73.96257),
    (("116", "BROADWAY"), 40.80754, -73.96257),
    (("COLUMBIA UNIVERSITY",), 40.80754, -73.96257),
    (("COLUMBIA",), 40.80754, -73.96257),
    (("TIMES SQUARE",), 40.7580, -73.9855),
    (("UNION SQUARE",), 40.7359, -73.9911),
    (("WASHINGTON SQUARE",), 40.7308, -73.9973),
    (("GRAND CENTRAL",), 40.7527, -73.9772),
    (("PENN STATION",), 40.7506, -73.9935),
)

_EVENT_WORDS = re.compile(
    r"\b(car|crash|crashed|collision|accident|wreck|hit|and run|stolen|theft|"
    r"burglary|assault|robbery|fight|harass(?:ment)?|lamp|streetlight|"
    r"street light|broken|photo|picture|image|in front of|near|at|on|"
    r"tonight|today|please|report)\b",
    re.I,
)
_INTERSECTION = re.compile(
    r"(?P<street>\d+)\s*(?:st|nd|rd|th)?(?:\s+street|\s+st)?\s*(?:and|&|/)\s*(?P<ave>[A-Za-z][A-Za-z]+(?:\s+(?:avenue|ave|street|st))?)"
    r"|"
    r"(?P<ave2>[A-Za-z][A-Za-z]+(?:\s+(?:avenue|ave))?)\s*(?:and|&|/)\s*(?P<street2>\d+)\s*(?:st|nd|rd|th)?",
    re.I,
)

# Street-centerline at 116th. Columbus/Amsterdam sit south of Broadway because
# the numbered streets run slightly south as you go east.
_AVENUE_AT_116 = {
    "BROADWAY": (40.8079, -73.9647),
    "AMSTERDAM": (40.8064, -73.9612),
    "COLUMBUS": (40.8052, -73.9606),
    "RIVERSIDE": (40.8076, -73.9710),
    "CLAREMONT": (40.8074, -73.9649),
    "MORNINGSIDE": (40.8058, -73.9600),
    "WESTEND": (40.8078, -73.9734),
}
_LAT_PER_STREET = 0.00072
_BROADWAY_LNG_PER_STREET = 0.00046

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


def headline_for(kind: str, category: str | None, caption: str | None, summary: str | None) -> str:
    text = f"{caption or ''} {summary or ''}".upper()
    if kind == "street_light":
        return "Street light out"
    if category == "traffic":
        if any(word in text for word in ("CRASH", "COLLISION", "WRECK", "ACCIDENT")):
            return "Car crash"
        return "Traffic incident"
    if category == "violent":
        if "SHOOT" in text or "GUN" in text:
            return "Shooting"
        if "STABB" in text:
            return "Stabbing"
        if "ROBBERY" in text:
            return "Robbery"
        if "ASSAULT" in text:
            return "Assault"
        return _HEADLINE["violent"]
    if summary:
        short = summary.strip().split(".")[0].strip()
        if 3 < len(short) <= 48:
            return short[:1].upper() + short[1:]
    if category in _HEADLINE:
        return _HEADLINE[category]
    return "Citizen report"


def datetime_from_image(data: bytes) -> datetime | None:
    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError):
        return None
    exif = image.getexif()
    if not exif:
        return None
    raw = exif.get(0x9003) or exif.get(0x9004) or exif.get(0x0132)
    if not raw:
        try:
            extra = exif.get_ifd(0x8769)
        except Exception:
            extra = {}
        raw = extra.get(0x9003) or extra.get(0x9004)
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return as_nyc(parsed)
        except ValueError:
            continue
    return None


def report_timestamp(image: bytes | None = None) -> datetime:
    if image:
        taken = datetime_from_image(image)
        if taken is not None:
            return taken
    return datetime.now(tz=NYC)


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


def coords_from_known_place(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    upper = re.sub(r"[^A-Z0-9 ]+", " ", text.upper())
    upper = re.sub(r"\s+", " ", upper)
    for needles, lat, lng in _PLACE_ALIASES:
        if all(needle in upper for needle in needles):
            return lat, lng
    return None


def coords_from_intersection(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    match = _INTERSECTION.search(text)
    if not match:
        return None
    street_raw = match.group("street") or match.group("street2")
    avenue_raw = match.group("ave") or match.group("ave2")
    if not street_raw or not avenue_raw:
        return None
    try:
        street = int(street_raw)
    except ValueError:
        return None
    avenue = (avenue_raw or "").upper()
    avenue = re.sub(r"\s+(AVENUE|AVE|STREET)$", "", avenue)
    avenue = re.sub(r"\s+", "", avenue)
    if avenue in {"WESTEND", "WEST"}:
        avenue = "WESTEND"
    origin = _AVENUE_AT_116.get(avenue)
    if origin is None or not (1 <= street <= 220):
        return None
    lat0, lng0 = origin
    lat = lat0 + (street - 116) * _LAT_PER_STREET
    lng = lng0
    if avenue == "BROADWAY":
        lng = lng0 + (street - 116) * _BROADWAY_LNG_PER_STREET
    return lat, lng


def place_candidates(*blobs: str | None) -> list[str]:
    seen: list[str] = []

    def add(value: str | None) -> None:
        text = (value or "").strip()
        if text and text not in seen:
            seen.append(text)

    for blob in blobs:
        if not blob:
            continue
        add(blob)
        for line in blob.splitlines():
            add(line)
        stripped = _EVENT_WORDS.sub(" ", blob)
        stripped = re.sub(r"\s+", " ", stripped).strip(" ,.")
        add(stripped)
        for match in _INTERSECTION.finditer(blob):
            street = match.group("street") or match.group("street2")
            avenue = match.group("ave") or match.group("ave2")
            if street and avenue:
                add(f"{street}th Street and {avenue}")
                add(f"{avenue} and {street}th")
    return seen


def _coords_from_location(location: dict) -> tuple[float, float] | None:
    try:
        return float(location["lat"]), float(location["lng"])
    except (KeyError, TypeError, ValueError):
        return None


def geocode_place(place_text: str | None) -> tuple[float, float] | None:
    known = coords_from_known_place(place_text)
    if known:
        return known
    query = (place_text or "").strip()
    leftover = re.sub(r"\s+", " ", _EVENT_WORDS.sub(" ", query)).strip(" ,.")
    if leftover:
        known = coords_from_known_place(leftover)
        if known:
            return known
    grid = coords_from_intersection(place_text) or coords_from_intersection(leftover)
    if not leftover or len(leftover) < 4:
        return grid
    searches = [leftover, f"{leftover}, Manhattan, New York, NY"]
    key = os.environ.get("GOOGLE_MAPS_GEOCODE_KEY") or os.environ.get("GOOGLE_MAPS_API_KEY")
    if key:
        try:
            with httpx.Client(timeout=20) as client:
                for address in searches:
                    response = client.get(
                        GEOCODE_URL,
                        params={"address": address, "components": "administrative_area:NY|country:US", "key": key},
                    )
                    if not response.is_success:
                        continue
                    results = response.json().get("results") or []
                    if results:
                        found = _coords_from_location(results[0].get("geometry", {}).get("location") or {})
                        if found:
                            return found
        except httpx.HTTPError:
            pass
    try:
        with httpx.Client(timeout=20, headers={"User-Agent": "safepath-photon/1.0"}) as client:
            response = client.get(
                NOMINATIM_URL,
                params={"q": searches[-1], "format": "jsonv2", "limit": 1},
            )
            response.raise_for_status()
            results = response.json()
    except httpx.HTTPError:
        return grid
    if not results:
        return grid
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError, IndexError):
        return grid


def _as_jpeg(data: bytes) -> bytes:
    image = Image.open(BytesIO(data))
    image.thumbnail((1280, 1280))
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
            "headline": "Street light out",
            "place_text": text,
            "confidence": 0.4,
        }
    for name, needles in _CITIZEN_NEEDLES:
        if any(needle in upper for needle in needles):
            return {
                "kind": "incident",
                "category": name,
                "summary": text[:180],
                "headline": headline_for("incident", name, text, text),
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
        "headline": headline_for("incident", category, text, text),
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
        if not response.is_success:
            detail = ""
            try:
                err = response.json().get("error") or {}
                detail = str(err.get("code") or err.get("type") or response.status_code)
            except Exception:
                detail = str(response.status_code)
            print(f"openai vision failed: {response.status_code} {detail}")
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
    summary = str(parsed.get("summary") or caption or "Citizen photo report")[:240]
    return {
        "kind": kind,
        "category": category,
        "summary": summary,
        "headline": headline_for(kind, category if isinstance(category, str) else None, caption, summary),
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
    for candidate in place_candidates(caption, place_text):
        geocoded = geocode_place(candidate)
        if geocoded:
            return geocoded
    if image:
        return _default_coords()
    return None


def persist_report(
    *,
    source_id: str,
    kind: str,
    category: str | None,
    lat: float,
    lng: float,
    timestamp: datetime,
    summary: str,
    headline: str,
) -> dict:
    if not _in_nyc(lat, lng):
        raise ValueError("location is outside New York City")
    timestamp = as_nyc(timestamp)
    if kind == "street_light":
        severity = DARK["Street Light Out"]
        headline = headline or "Street light out"
        record = {
            "source_id": source_id,
            "lat": lat,
            "lng": lng,
            "severity": severity,
            "timestamp": timestamp,
            "headline": headline,
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
            "headline": headline,
            "summary": summary,
        }
    if category not in INCIDENT_CATEGORIES:
        raise ValueError("unsupported incident category")
    severity = CATEGORY_SEVERITY[category]
    headline = headline or _HEADLINE[category]
    record = {
        "source_id": source_id,
        "lat": lat,
        "lng": lng,
        "category": category,
        "severity": severity,
        "timestamp": timestamp,
        "headline": headline,
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
        "headline": headline,
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
        return {
            "status": "need_photo",
            "reply": "Hi — want to report something? Send a photo of the street, crash, or whatever you saw, plus a cross-street if you can.",
        }

    classification: dict | None = None
    if image:
        try:
            classification = classify_image(image, caption)
        except UnidentifiedImageError:
            return {
                "status": "bad_image",
                "reply": "I couldn't open that image. A still JPEG or PNG usually works best.",
            }
        except RuntimeError as exc:
            return {"status": "need_vision", "reply": str(exc)}
        except httpx.HTTPError:
            fallback = classify_from_caption(caption)
            if fallback is None:
                return {
                    "status": "vision_failed",
                    "reply": "I couldn't read that photo just now. Tell me what happened and a cross-street and I'll try again.",
                }
            classification = fallback
    else:
        classification = classify_from_caption(caption)

    if classification is None or classification["kind"] == "unrelated":
        return {
            "status": "unrelated",
            "reply": "I'm not sure that matches a street lamp, crash, or one of the mapped incidents. If it is, send another photo or a short description of what you saw.",
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
            "reply": "Got it. Where in NYC was this? A cross-street like Broadway and 116th is enough.",
        }

    source_id = f"photon:{(message_id or uuid.uuid4().hex)}"
    kind = classification["kind"]
    category = classification.get("category")
    summary = str(classification.get("summary") or caption or "Citizen report")
    headline = str(
        classification.get("headline")
        or headline_for(kind, category if isinstance(category, str) else None, caption, summary)
    )
    try:
        stored = persist_report(
            source_id=source_id,
            kind=kind,
            category=category,
            lat=coords[0],
            lng=coords[1],
            timestamp=report_timestamp(image),
            summary=summary,
            headline=headline,
        )
    except ValueError as exc:
        return {"status": "rejected", "reply": str(exc)}

    stored["status"] = "stored"
    stored["reply"] = (
        f"Thanks — I logged that as {stored.get('headline') or headline} "
        f"at {stored['lat']:.5f}, {stored['lng']:.5f}. It's on the map."
    )
    return stored
