import type { LatLng } from "@/lib/geo";

export type RouteMode = "walk" | "subway" | "bus";

export type StopRef = { lat: number; lng: number; name: string | null };

export type WalkLegRequest = { kind: "walk"; path: LatLng[] };

export type TransitLegRequest = {
  kind: "transit";
  line: string | null;
  vehicle: string | null;
  from: StopRef;
  to: StopRef;
  num_stops: number;
  duration_s: number;
  /** Scheduled wait at the boarding stop per Google, seconds. */
  wait_s: number | null;
};

export type LegRequest = WalkLegRequest | TransitLegRequest;

export type StationHop = {
  kind: "station" | "bus_stop";
  role: "board" | "alight" | "transfer";
  complex_id: string | null;
  name: string | null;
  lat: number;
  lng: number;
  routes: string[];
  safety_score: number | null;
  flags: string[];
  notes: string[];
  quiet: boolean;
  weight: number;
  wait_min: number | null;
  wait_source?: "realtime" | "scheduled" | "bus_time" | "realtime_no_service" | "unknown";
  wait_weight: number;
  matched: boolean;
  riders_per_hour?: number | null;
};

export type TransitRouteScore = {
  route_id: string;
  safety_score: number;
  walk_score: number;
  station_score: number;
  weakest_station_score: number | null;
  longest_wait_min: number | null;
  incident_count: number;
  history_count: number;
  light_score: number;
  light_count: number;
  lines: string[];
  vehicles: string[];
  stations: StationHop[];
};

export type TransitScoreResponse = {
  sim_now: string;
  include_history: boolean;
  realtime: { observed_at: string; age_s: number; routes_running: string[]; feed_errors: string[] } | null;
  routes: TransitRouteScore[];
};

function toLatLng(point: google.maps.LatLng | google.maps.LatLngLiteral): LatLng {
  if (typeof (point as google.maps.LatLng).lat === "function") {
    const value = point as google.maps.LatLng;
    return { lat: value.lat(), lng: value.lng() };
  }
  const literal = point as google.maps.LatLngLiteral;
  return { lat: literal.lat, lng: literal.lng };
}

function stopRef(stop: google.maps.TransitStop | undefined): StopRef | null {
  if (!stop?.location) return null;
  const point = toLatLng(stop.location);
  return { ...point, name: stop.name ?? null };
}

/**
 * Split a Directions TRANSIT route into the legs the backend scores: merged
 * walking segments (scored by corridor) and rides (scored by the stops you
 * wait at). The wait at each boarding stop is the gap between when you get
 * there (leg departure plus the steps so far) and the ride's departure.
 */
export function legsFromRoute(route: google.maps.DirectionsRoute): LegRequest[] {
  const legs: LegRequest[] = [];
  let walking: LatLng[] = [];
  const flushWalk = () => {
    if (walking.length >= 2) legs.push({ kind: "walk", path: walking });
    walking = [];
  };

  for (const leg of route.legs) {
    let clockS = leg.departure_time?.value ? leg.departure_time.value.getTime() / 1000 : null;
    for (const step of leg.steps) {
      if (step.travel_mode === google.maps.TravelMode.TRANSIT && step.transit) {
        flushWalk();
        const from = stopRef(step.transit.departure_stop);
        const to = stopRef(step.transit.arrival_stop);
        if (!from || !to) continue;
        const line = step.transit.line;
        const departS = step.transit.departure_time?.value ? step.transit.departure_time.value.getTime() / 1000 : null;
        const waitS = clockS != null && departS != null ? Math.max(0, departS - clockS) : null;
        legs.push({
          kind: "transit",
          line: line?.short_name || line?.name || null,
          vehicle: line?.vehicle?.type ? String(line.vehicle.type) : null,
          from,
          to,
          num_stops: step.transit.num_stops ?? 0,
          duration_s: step.duration?.value ?? 0,
          wait_s: waitS,
        });
        if (departS != null) clockS = departS + (step.duration?.value ?? 0);
        else if (clockS != null) clockS += step.duration?.value ?? 0;
        continue;
      }
      if (clockS != null) clockS += step.duration?.value ?? 0;
      const path = (step.path ?? []).map(toLatLng);
      if (path.length === 0 && step.start_location && step.end_location) {
        path.push(toLatLng(step.start_location), toLatLng(step.end_location));
      }
      if (walking.length > 0 && path.length > 0) {
        const last = walking[walking.length - 1];
        if (last.lat === path[0].lat && last.lng === path[0].lng) path.shift();
      }
      walking.push(...path);
    }
  }
  flushWalk();
  return legs;
}

export function isSubwayRoute(legs: LegRequest[]): boolean {
  return legs.some((leg) => leg.kind === "transit" && (leg.vehicle === "SUBWAY" || leg.vehicle == null));
}

/** Route lines in order, deduplicated, for badges. */
export function routeLines(legs: LegRequest[]): string[] {
  const seen: string[] = [];
  for (const leg of legs) {
    if (leg.kind === "transit" && leg.line && !seen.includes(leg.line)) seen.push(leg.line);
  }
  return seen;
}

/** "1 Line" -> "1", "M4-SBS" -> "M4 SBS" for compact bullets. */
export function shortLine(line: string): string {
  return line
    .replace(/\b(line|train)\b/gi, "")
    .replace(/-/g, " ")
    .trim();
}

const NYC_TZ = "America/New_York";

function nycParts(date: Date): { weekday: number; hour: number; minute: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: NYC_TZ,
    weekday: "short",
    hour: "numeric",
    minute: "numeric",
    hour12: false,
  }).formatToParts(date);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  const weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return {
    weekday: weekdays.indexOf(get("weekday")),
    hour: Number(get("hour")) % 24,
    minute: Number(get("minute")),
  };
}

/**
 * Google only plans transit for the near future, so a replay set in the past
 * (or far future) gets the same NYC weekday and time-of-day in the coming week.
 * Schedules repeat weekly, so this keeps late-night headways and overnight
 * service patterns matching what the sim clock is showing. Returns `now` when
 * the sim clock is unset or already within the plannable window.
 */
export function departureTimeFor(simMs: number | null, now = new Date()): Date {
  if (simMs == null) return now;
  const sim = new Date(simMs);
  const deltaMs = sim.getTime() - now.getTime();
  if (deltaMs > -15 * 60_000 && deltaMs < 7 * 24 * 3600_000) return deltaMs < 0 ? now : sim;
  const want = nycParts(sim);
  const here = nycParts(now);
  let dayShift = (want.weekday - here.weekday + 7) % 7;
  const wantMinutes = want.hour * 60 + want.minute;
  const hereMinutes = here.hour * 60 + here.minute;
  if (dayShift === 0 && wantMinutes <= hereMinutes) dayShift = 7;
  const candidate = new Date(now.getTime() + dayShift * 24 * 3600_000 + (wantMinutes - hereMinutes) * 60_000);
  return candidate;
}
