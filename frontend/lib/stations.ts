export type StationTerm = { count: number; weight: number; violent?: number; per_30d?: number; band?: string };

export type RidershipTerm = {
  hourly: number | null;
  typical: number;
  quiet: boolean;
  emptiness?: number;
  busy_factor?: number;
  weight: number;
} | null;

export type LiveItem = { kind: "alert" | "elevator" | "escalator"; severity: number; summary: string; route: string | null };

export type LiveTerm = { count: number; weight: number; items: LiveItem[]; observed_at: string | null } | null;

export type RealtimeRoute = {
  next_min: Record<string, number>;
  wait_min: number | null;
  headway_min: number | null;
  skipping: number;
  stopping: number;
  no_service: boolean;
};

/** GTFS-RT state for a complex right now (wall clock), when live is on. */
export type RealtimeTerm = {
  wait_min: number | null;
  headway_min: number | null;
  quiet: boolean;
  skipping: Record<string, number>;
  no_service: string[];
  routes: Record<string, RealtimeRoute>;
  notes: string[];
  weight: number;
} | null;

export const BAND_LABEL: Record<string, string> = {
  night: "9 PM – 5 AM",
  morning: "5 – 10 AM",
  midday: "10 AM – 4 PM",
  evening: "4 – 9 PM",
};

export type StationScore = {
  complex_id: string;
  name: string;
  routes: string[];
  ada: boolean;
  lat: number;
  lng: number;
  structure: string | null;
  safety_score: number;
  weight: number;
  breakdown: {
    complaints: StationTerm;
    calls: StationTerm;
    ridership: RidershipTerm;
    live: LiveTerm;
    realtime?: RealtimeTerm;
  };
  flags: string[];
};

export type StationDetail = StationScore & {
  lookback_days: number;
  band?: string;
  recent_complaints: {
    source_id: string;
    category: string;
    severity: number;
    law_cat: string | null;
    offense: string | null;
    timestamp: string;
  }[];
  complaints_by_category: Record<string, number>;
  stations: { station_id: string; name: string; routes: string[]; structure: string | null; ada: boolean }[];
};

export type StationsContext = {
  band: string;
  coverage_days: number;
  history_until: string | null;
  realtime?: { observed_at: string; age_s: number; routes_running: string[]; feed_errors: string[] } | null;
};

export type StationsResponse = {
  sim_now: string;
  start: string;
  end: string;
  count: number;
  context?: StationsContext;
  stations: StationScore[];
};

export type ScoreBand = "safe" | "fair" | "poor" | "bad";

export function scoreBand(score: number | null | undefined): ScoreBand {
  if (score == null) return "fair";
  if (score >= 70) return "safe";
  if (score >= 45) return "fair";
  if (score >= 20) return "poor";
  return "bad";
}

export const FLAG_LABEL: Record<string, string> = {
  violent_history: "Violent complaints",
  active_calls: "Active calls",
  quiet_hour: "Quiet hour",
  service_alert: "Service alert",
  equipment_outage: "Elevator / escalator out",
  long_wait: "Long wait",
  skipped_stops: "Trains skipping",
  no_service: "No service now",
};

/** Flags that come from wall-clock feeds rather than history. */
export const LIVE_FLAGS = new Set(["service_alert", "equipment_outage", "long_wait", "skipped_stops", "no_service"]);

/** Minutes between live refreshes of station and route scores when live is on. */
export const LIVE_REFRESH_MS = 60_000;

/** Bucket the sim clock so station scores refetch every few minutes of replay, not every frame. */
export function stationClockKey(simMs: number | null, stepMinutes = 5): string {
  if (simMs == null) return "none";
  const step = stepMinutes * 60 * 1000;
  return String(Math.floor(simMs / step) * step);
}
