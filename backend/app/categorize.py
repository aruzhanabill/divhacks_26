"""Map NYPD typ_desc into the closed incident category set and a severity weight."""

from __future__ import annotations

# First match wins. Alarm is before property so "ALARMS: COMMERCIAL/BURGLARY"
# stays an alarm rather than a completed burglary.
_RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    (
        "violent",
        5.0,
        (
            "HOMICIDE",
            "MURDER",
            "RAPE",
            "ROBBERY",
            "ASSAULT",
            "SHOT",
            "STABB",
            "FIREARM",
            "GUNPOINT",
            "WEAPON",
        ),
    ),
    ("alarm", 1.5, ("ALARM",)),
    (
        "property",
        3.0,
        (
            "BURGLARY",
            "LARCENY",
            "GRAND LARCENY",
            "STOLEN",
            "CRIMINAL MISCHIEF",
            "CRIM MISCHIEF",
        ),
    ),
    (
        "disorder",
        2.0,
        (
            "HARASSMENT",
            "DISPUTE",
            "DISORDERLY",
            "TRESPASS",
            "PROWLER",
            "SUSP PERSON",
            "SUSP VEHICLE",
            "POSSIBLE CRIME",
            "CALLS FOR HELP",
        ),
    ),
    ("traffic", 1.0, ("VEHICLE ACCIDENT", "TRAFFIC")),
    (
        "medical",
        0.5,
        ("AMBULANCE", "VERIFY AMB", "EDP", "CARDIAC", "UNCONSCIOUS"),
    ),
    (
        "admin",
        0.2,
        (
            "VISIBILITY PATROL",
            "STATION INSPECTION",
            "TRAIN RUN",
            "QUALITY OF LIFE",
            "MOBILE ORDER",
        ),
    ),
)

_CRITICAL_CIP = frozenset({"CIP", "CRITICAL", "YES", "Y"})

# Closed set used by the map, minus `other`. Citizen photo reports use these.
INCIDENT_CATEGORIES = (
    "violent",
    "property",
    "disorder",
    "alarm",
    "traffic",
    "medical",
    "admin",
)
CATEGORY_SEVERITY = {name: weight for name, weight, _ in _RULES}


def categorize(typ_desc: str | None, cip_jobs: str | None) -> tuple[str, float]:
    text = (typ_desc or "").upper()
    category = "other"
    severity = 1.0
    for name, weight, needles in _RULES:
        if any(needle in text for needle in needles):
            category = name
            severity = weight
            break
    cip = (cip_jobs or "").strip().upper()
    if cip in _CRITICAL_CIP or (cip and cip not in {"NON CIP", "NON-CIP", "N"} and "CIP" in cip and "NON" not in cip):
        severity = min(5.0, severity + 0.5)
    return category, severity
