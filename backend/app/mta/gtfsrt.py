"""Live subway train positions from the MTA GTFS-Realtime feeds (no API key).

Eight protobuf feeds cover every line. Each is a list of trip updates whose
stop_time_updates give predicted arrival times per GTFS stop id (e.g. "117N"
is 116 St-Columbia northbound). From these we derive, per station complex and
route:

- wait: minutes until the next train in each direction, and the headway
  (typical gap between the next few trains). A 20-minute headway at 3 AM on a
  quiet platform is the exposure this app cares about.
- skipped: upcoming trips of a route that pass a station without stopping,
  e.g. 1 trains running express past 103/110/116 St during late-night work.
  A trip skips a stop when the feed shows it serving stops both before and
  after that stop but not the stop itself. Stop order comes from the feed
  (any trip that serves two stops fixes their order).
- no_service: a route that normally serves the station has trains in the feed
  but none stopping here inside the horizon.

Everything is wall-clock: it is the state of the system right now, cached
for CACHE_SECONDS and labelled `observed_at`. It is never confused with the
replay clock; scoring treats it as a live nudge with its own flags.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from app.db import pool

FEEDS = {
    "1234567S": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs",
    "ACE": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace",
    "BDFM": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm",
    "G": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-g",
    "JZ": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-jz",
    "NQRW": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw",
    "L": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-l",
    "SIR": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-si",
}
CACHE_SECONDS = 45
HORIZON_MINUTES = 60
NO_SERVICE_MINUTES = 45  # a served route with no arrival inside this is "not stopping here"
HEADWAY_TRAINS = 4

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "snapshot": None}

_ROUTE_NOISE = ("LINE", "TRAIN", "LOCAL", "EXPRESS", "SHUTTLE", "SUBWAY")


def normalize_route(name: str | None) -> str | None:
    """Google's '1 Line', 'A', '6 Express', 'S 42 St Shuttle' -> GTFS-RT '1', 'A', '6X', 'GS'.

    Express variants in the feed are '6X', '7X', 'FX'. The shuttles are 'GS'
    (42 St), 'FS' (Franklin Av) and 'H' (Rockaway Park); the stations table
    lists them as 'S', which cannot be told apart, so 'S' maps to 'GS'.
    """
    if not name:
        return None
    text = str(name).upper().replace("-", " ").strip()
    tokens = [t for t in text.split() if t not in _ROUTE_NOISE]
    if not tokens:
        return None
    head = tokens[0]
    if "EXPRESS" in text and len(head) == 1 and head in "67F":
        return f"{head}X"
    if head == "S":
        return "GS"
    return head


class Snapshot:
    """Parsed feed state. Built once per refresh, read by scoring."""

    def __init__(self, observed_at: datetime, now_s: float) -> None:
        self.observed_at = observed_at
        self.now_s = now_s
        # (stop_base, route) -> list of (minutes, direction, trip_id)
        self.arrivals: dict[tuple[str, str], list[tuple[float, str, str]]] = defaultdict(list)
        # route -> direction -> list of (trip_id, [stop_base, ...]) for upcoming stops only
        self.trips: dict[str, dict[str, list[tuple[str, list[str]]]]] = defaultdict(lambda: defaultdict(list))
        # (route, direction) -> {a: set(stops known to come after a)}. Direction
        # matters: northbound 96 St precedes 116 St, southbound it follows.
        self.after: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.routes_seen: set[str] = set()
        self.feed_errors: list[str] = []

    # --- construction -----------------------------------------------------

    def add_trip(self, route: str, trip_id: str, updates: list[tuple[str, float | None]]) -> None:
        """`updates` is the ordered [(stop_id, epoch_seconds)] for one trip."""
        if not route or not updates:
            return
        route = normalize_route(route) or route
        self.routes_seen.add(route)
        ordered = [stop for stop, _ in updates]
        direction = self._direction(ordered)
        bases = [self._base(stop) for stop in ordered]
        after = self.after[(route, direction)]
        for i, a in enumerate(bases):
            for b in bases[i + 1 :]:
                if a != b:
                    after[a].add(b)
        # Stops the trip has not long passed, in order. Used for skip detection
        # and through-trip matching; recent past stops keep the ordering intact
        # for a train that is already between stations.
        remaining: list[str] = []
        for stop, when in updates:
            if when is None:
                continue
            minutes = (when - self.now_s) / 60
            if minutes < -10:
                continue
            remaining.append(self._base(stop))
            if -1 <= minutes <= HORIZON_MINUTES:
                self.arrivals[(self._base(stop), route)].append((round(minutes, 1), self._direction([stop]), trip_id))
        if remaining:
            self.trips[route][direction].append((trip_id, remaining))

    def finish(self) -> None:
        for key in self.arrivals:
            self.arrivals[key].sort()

    @staticmethod
    def _base(stop_id: str) -> str:
        return stop_id[:-1] if stop_id and stop_id[-1] in "NS" else stop_id

    @staticmethod
    def _direction(stops: list[str]) -> str:
        for stop in stops:
            if stop and stop[-1] in "NS":
                return stop[-1]
        return "?"

    # --- queries -----------------------------------------------------------

    def next_arrivals(self, bases: set[str], route: str) -> dict[str, list[float]]:
        out: dict[str, list[float]] = defaultdict(list)
        for base in bases:
            for minutes, direction, _ in self.arrivals.get((base, route), []):
                if minutes >= -0.5:
                    out[direction].append(minutes)
        return {direction: sorted(values) for direction, values in out.items()}

    def skipping_trips(self, bases: set[str], route: str) -> tuple[int, int]:
        """(trips that pass these stops without stopping, trips that stop) within the horizon.

        A trip skips when, in its own direction's stop order, it still has a
        stop before the station and a stop after it but never the station.
        """
        skip = 0
        stop = 0
        for direction, direction_trips in self.trips.get(route, {}).items():
            after = self.after.get((route, direction), {})
            for _, stops in direction_trips:
                served = set(stops)
                if served & bases:
                    stop += 1
                    continue
                before_ok = any(any(base in after.get(s, ()) for base in bases) for s in served)
                after_ok = any(any(s in after.get(base, ()) for base in bases) for s in served)
                if before_ok and after_ok:
                    skip += 1
        return skip, stop

    def station_status(self, bases: set[str], routes: list[str]) -> dict:
        """Per-route wait/headway/skip summary for one complex."""
        per_route: dict[str, dict] = {}
        # Static routes plus anything actually stopping here now (the A runs
        # local overnight and serves stations the daytime list gives to the C).
        static = {normalize_route(r) or r for r in routes}
        candidates = static | {route for (base, route) in self.arrivals if base in bases}
        for route in sorted(candidates):
            if route not in self.routes_seen:
                continue  # feed has no trips for this route (e.g. not running overnight)
            arrivals = self.next_arrivals(bases, route)
            skip, stop = self.skipping_trips(bases, route)
            if route not in static and stop < 3:
                continue  # an express passing through, or a one-off reroute: not this station's line
            waits = {d: values[0] for d, values in arrivals.items() if values}
            headways = []
            for values in arrivals.values():
                sample = [v for v in values if v >= 0][: HEADWAY_TRAINS + 1]
                headways.extend(b - a for a, b in zip(sample, sample[1:]))
            per_route[route] = {
                "next_min": {d: round(v, 1) for d, v in waits.items()},
                "wait_min": round(min(waits.values()), 1) if waits else None,
                "headway_min": round(statistics.median(headways), 1) if headways else None,
                "skipping": skip,
                "stopping": stop,
                "no_service": not waits and stop == 0 and skip == 0 and route in self.routes_seen,
            }
        return per_route

    def leg_wait(self, from_bases: set[str], to_bases: set[str], route: str) -> dict | None:
        """Next trip of `route` that stops at both ends, in order. None if the route is absent."""
        route = normalize_route(route) or route
        if route not in self.routes_seen:
            return None
        best: tuple[float, str] | None = None
        through = 0
        passing = 0
        for direction, direction_trips in self.trips.get(route, {}).items():
            for trip_id, stops in direction_trips:
                from_idx = next((i for i, s in enumerate(stops) if s in from_bases), None)
                to_idx = next((i for i, s in enumerate(stops) if s in to_bases), None)
                if from_idx is None or to_idx is None or to_idx <= from_idx:
                    continue
                through += 1
                for minutes, d, tid in self.arrivals.get((stops[from_idx], route), []):
                    if tid == trip_id and minutes >= -0.5 and (best is None or minutes < best[0]):
                        best = (minutes, direction)
        skip, _ = self.skipping_trips(from_bases, route)
        passing = skip
        return {
            "wait_min": round(max(0.0, best[0]), 1) if best else None,
            "direction": best[1] if best else None,
            "through_trips": through,
            "skipping_here": passing,
        }


def _fetch_feed(client: httpx.Client, name: str, url: str) -> tuple[str, bytes | None, str | None]:
    try:
        response = client.get(url)
        response.raise_for_status()
        return name, response.content, None
    except Exception as exc:  # noqa: BLE001
        return name, None, f"{name}: {exc.__class__.__name__}"


def _parse_into(snapshot: Snapshot, payload: bytes) -> None:
    from google.transit import gtfs_realtime_pb2 as pb

    feed = pb.FeedMessage()
    feed.ParseFromString(payload)
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update
        route = trip.trip.route_id
        updates: list[tuple[str, float | None]] = []
        for update in trip.stop_time_update:
            when = None
            if update.HasField("arrival") and update.arrival.time:
                when = float(update.arrival.time)
            elif update.HasField("departure") and update.departure.time:
                when = float(update.departure.time)
            updates.append((update.stop_id, when))
        snapshot.add_trip(route, trip.trip.trip_id, updates)


def refresh(force: bool = False) -> Snapshot:
    with _lock:
        age = time.time() - _cache["at"]
        if not force and _cache["snapshot"] is not None and age < CACHE_SECONDS:
            return _cache["snapshot"]
        now_s = time.time()
        snapshot = Snapshot(datetime.now(timezone.utc), now_s)
        with httpx.Client(timeout=20) as client, ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda item: _fetch_feed(client, *item), FEEDS.items()))
        for name, payload, error in results:
            if error:
                snapshot.feed_errors.append(error)
                continue
            try:
                _parse_into(snapshot, payload or b"")
            except Exception as exc:  # noqa: BLE001
                snapshot.feed_errors.append(f"{name}: parse {exc.__class__.__name__}")
        snapshot.finish()
        if snapshot.routes_seen or _cache["snapshot"] is None:
            _cache.update({"at": time.time(), "snapshot": snapshot})
        return _cache["snapshot"]


def current() -> Snapshot | None:
    """Best effort: refreshed snapshot, or the last good one, or None."""
    try:
        return refresh()
    except Exception:  # noqa: BLE001
        return _cache["snapshot"]


def complex_stops(conn) -> dict[str, set[str]]:
    """complex_id -> set of GTFS stop bases (one per member station)."""
    out: dict[str, set[str]] = defaultdict(set)
    for complex_id, gtfs_stop_id in conn.execute("SELECT complex_id, gtfs_stop_id FROM subway_stations").fetchall():
        if gtfs_stop_id:
            out[complex_id].add(str(gtfs_stop_id))
    return out


def summary(snapshot: Snapshot) -> dict:
    routes = sorted(snapshot.routes_seen)
    per_route = {}
    for route in routes:
        trips = sum(len(v) for v in snapshot.trips.get(route, {}).values())
        per_route[route] = {"upcoming_trips": trips}
    return {
        "observed_at": snapshot.observed_at,
        "age_s": round(time.time() - snapshot.now_s),
        "routes_running": routes,
        "routes": per_route,
        "feed_errors": snapshot.feed_errors,
    }


def realtime_for_complexes(conn, snapshot: Snapshot, complexes: dict[str, dict]) -> dict[str, dict]:
    stops = complex_stops(conn)
    out: dict[str, dict] = {}
    for complex_id, info in complexes.items():
        bases = stops.get(complex_id)
        if not bases:
            continue
        status = snapshot.station_status(bases, info.get("routes", []))
        if status:
            out[complex_id] = status
    return out
