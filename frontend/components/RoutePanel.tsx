"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useMap, useMapsLibrary } from "@vis.gl/react-google-maps";
import { PlaceAutocompleteInput } from "@/components/PlaceSearch";
import { useFollow, useFollowLive } from "@/lib/follow";
import { useTheme } from "@/lib/theme";
import { metersToPath, pathFrom, pointAlong, type LatLng } from "@/lib/geo";
import type { SelectedPlace } from "@/lib/googleMaps";
import { LIVE_FLAGS, LIVE_REFRESH_MS, scoreBand } from "@/lib/stations";
import { TrainGlyph } from "@/lib/crimePin";
import {
  departureTimeFor,
  legsFromRoute,
  routeLines,
  shortLine,
  type LegRequest,
  type RouteMode,
  type StationHop,
  type TransitRouteScore,
  type TransitScoreResponse,
} from "@/lib/transit";

const CORRIDOR_METERS = 150;

function hopBadge(hop: StationHop): string {
  const parts: string[] = [];
  if (hop.flags.includes("no_service")) parts.push("no svc");
  else if (hop.wait_min != null && hop.wait_min >= 1) parts.push(`⏱${Math.round(hop.wait_min)}m`);
  if (hop.flags.includes("skipped_stops")) parts.push("skips");
  if (hop.flags.some((flag) => flag === "service_alert" || flag === "equipment_outage")) parts.push("⚠");
  return parts.join(" ");
}

function preferenceScore(route: RouteSummary, preferSafe: boolean, preferLit: boolean): number {
  const parts: number[] = [];
  if (preferSafe) parts.push(route.safetyScore ?? 0);
  if (preferLit) parts.push(route.lightScore ?? 0);
  if (parts.length === 0) return 0;
  return parts.reduce((sum, value) => sum + value, 0) / parts.length;
}

function pickIndex(summaries: RouteSummary[], preferSafe: boolean, preferLit: boolean): number {
  return [...summaries].sort(
    (a, b) => preferenceScore(b, preferSafe, preferLit) - preferenceScore(a, preferSafe, preferLit) || a.index - b.index,
  )[0]?.index ?? 0;
}

function placeLabel(place: SelectedPlace | null): string {
  if (!place) return "";
  return place.address || place.name;
}

type RouteScore = {
  route_id: string;
  safety_score: number;
  incident_count: number;
  light_score: number;
  light_count: number;
};

type RouteSummary = {
  index: number;
  distance: string;
  duration: string;
  summary: string;
  safetyScore: number | null;
  incidentCount: number | null;
  lightScore: number | null;
  lightCount: number | null;
  mode: RouteMode;
  lines?: string[];
  stations?: StationHop[];
  walkScore?: number | null;
  weakestStation?: number | null;
  longestWait?: number | null;
  historyCount?: number | null;
};

type RoutePanelProps = {
  origin: SelectedPlace | null;
  destination: SelectedPlace | null;
  onOriginSelect: (place: SelectedPlace | null) => void;
  onDestinationSelect: (place: SelectedPlace | null) => void;
};

export function RoutePanel({
  origin,
  destination,
  onOriginSelect,
  onDestinationSelect,
}: RoutePanelProps) {
  const map = useMap();
  const routesLib = useMapsLibrary("routes");
  const follow = useFollow();
  const live = useFollowLive();
  const { theme, toggleTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<RouteMode>("walk");
  // Walk mode only: adds the time-of-day street-complaint history to the corridor
  // score. Off by default so walking scores stay exactly as before.
  const [useHistory, setUseHistory] = useState(false);
  const [liveInfo, setLiveInfo] = useState<{ observedAt: string; routes: number } | null>(null);
  const [preferSafe, setPreferSafe] = useState(true);
  const [preferLit, setPreferLit] = useState(true);
  const [locating, setLocating] = useState(false);
  const [rerouteTick, setRerouteTick] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [rerouted, setRerouted] = useState(false);
  const [summaries, setSummaries] = useState<RouteSummary[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [safestIndex, setSafestIndex] = useState<number | null>(null);
  const [friendPhone, setFriendPhone] = useState("");
  const rendererRef = useRef<google.maps.DirectionsRenderer | null>(null);
  const altLinesRef = useRef<google.maps.Polyline[]>([]);
  const resultRef = useRef<google.maps.DirectionsResult | null>(null);
  const activePathRef = useRef<LatLng[]>([]);
  const routeAnchorProgress = useRef(0);
  const handledCalls = useRef(new Set<string>());
  const arrivedSent = useRef(false);
  const rerouteLock = useRef(false);
  const callsRef = useRef(follow.calls);
  const destinationRef = useRef(destination);
  const liveRef = useRef(live);
  const preferSafeRef = useRef(preferSafe);
  const preferLitRef = useRef(preferLit);
  const modeRef = useRef(mode);
  const useHistoryRef = useRef(useHistory);
  const transitLegsRef = useRef<LegRequest[][] | null>(null);
  const friendPhoneRef = useRef(friendPhone);
  callsRef.current = follow.calls;
  friendPhoneRef.current = friendPhone;
  destinationRef.current = destination;
  liveRef.current = live;
  preferSafeRef.current = preferSafe;
  preferLitRef.current = preferLit;
  modeRef.current = mode;
  useHistoryRef.current = useHistory;

  const clearAltLines = useCallback(() => {
    altLinesRef.current.forEach((line) => line.setMap(null));
    altLinesRef.current = [];
  }, []);

  const drawAlternatives = useCallback(
    (result: google.maps.DirectionsResult, activeIndex: number) => {
      if (!map) return;
      clearAltLines();
      result.routes.forEach((route, index) => {
        if (index === activeIndex) return;
        const line = new google.maps.Polyline({
          path: route.overview_path,
          strokeColor: "#64748b",
          strokeOpacity: 0.55,
          strokeWeight: 5,
          zIndex: 1,
          map,
        });
        line.addListener("click", () => setSelectedIndex(index));
        altLinesRef.current.push(line);
      });
    },
    [clearAltLines, map],
  );

  useEffect(() => {
    if (!map || !routesLib) return;
    const renderer = new routesLib.DirectionsRenderer({
      map,
      suppressMarkers: false,
      polylineOptions: {
        strokeColor: "#15803d",
        strokeWeight: 6,
        zIndex: 2,
      },
    });
    rendererRef.current = renderer;
    return () => {
      renderer.setMap(null);
      rendererRef.current = null;
      clearAltLines();
    };
  }, [clearAltLines, map, routesLib]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const result = resultRef.current;
    if (!renderer || !result) return;
    const preferred = preferSafe || preferLit;
    const matches = preferred && safestIndex != null && selectedIndex === safestIndex;
    renderer.setOptions({
      polylineOptions: {
        strokeColor: !preferred ? "#1a73e8" : matches ? "#15803d" : "#c2410c",
        strokeWeight: 6,
        zIndex: 2,
      },
    });
    renderer.setRouteIndex(selectedIndex);
    const route = result.routes[selectedIndex];
    if (route) {
      activePathRef.current = route.overview_path.map((point) => ({
        lat: point.lat(),
        lng: point.lng(),
      }));
    }
    drawAlternatives(result, selectedIndex);
  }, [drawAlternatives, preferLit, preferSafe, safestIndex, selectedIndex]);

  const requestRoute = useCallback(
    async (from?: LatLng) => {
      if (!routesLib) {
        setError("Directions library is still loading.");
        return;
      }
      const start = from ?? origin?.location;
      const end = destinationRef.current;
      if (!start || !end) {
        setError("Choose an origin and a destination.");
        return;
      }

      setLoading(true);
      setError("");
      const service = new routesLib.DirectionsService();

      try {
        const result = await service.route({
          origin: start,
          destination: end.location,
          travelMode: google.maps.TravelMode.WALKING,
          provideRouteAlternatives: true,
        });
        resultRef.current = result;
        const clock = liveRef.current;
        const simNow =
          clock.playing && clock.simMs != null ? new Date(clock.simMs).toISOString() : undefined;
        const scoreResponse = await fetch("/api/routes/score", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            corridor_meters: CORRIDOR_METERS,
            sim_now: simNow,
            include_history: useHistoryRef.current,
            routes: result.routes.map((route, index) => ({
              route_id: String(index),
              path: route.overview_path.map((point) => ({
                lat: point.lat(),
                lng: point.lng(),
              })),
            })),
          }),
        });
        if (!scoreResponse.ok) {
          const body = await scoreResponse.json().catch(() => null);
          const detail = body && typeof body.detail === "string" ? body.detail : "";
          throw new Error(detail || "Could not score routes against calls");
        }
        const scored = (await scoreResponse.json()) as { routes: RouteScore[] };
        const byId = new Map(scored.routes.map((route) => [route.route_id, route]));
        const nextSummaries = result.routes.map((route, index) => {
          const leg = route.legs[0];
          const score = byId.get(String(index));
          return {
            index,
            distance: leg?.distance?.text ?? "—",
            duration: leg?.duration?.text ?? "—",
            summary: route.summary || `Walking route ${index + 1}`,
            safetyScore: score?.safety_score ?? null,
            incidentCount: score?.incident_count ?? null,
            lightScore: score?.light_score ?? null,
            lightCount: score?.light_count ?? null,
            mode: "walk" as const,
          };
        });
        const chosen = pickIndex(nextSummaries, preferSafeRef.current, preferLitRef.current);
        const chosenPath = result.routes[chosen]?.overview_path.map((point) => ({
          lat: point.lat(),
          lng: point.lng(),
        }));
        activePathRef.current = chosenPath ?? [];
        routeAnchorProgress.current = liveRef.current.progress;
        if (!from) {
          handledCalls.current = new Set(callsRef.current.map((call) => call.source_id));
          arrivedSent.current = false;
          setRerouted(false);
          const friend = friendPhoneRef.current.trim();
          if (friend) {
            void fetch("/api/checkin/leaving", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                friend,
                destination: placeLabel(end),
              }),
            }).catch(() => undefined);
          }
        }
        rendererRef.current?.setDirections(result);
        setSummaries(nextSummaries);
        setSafestIndex(preferSafeRef.current || preferLitRef.current ? chosen : null);
        setSelectedIndex(chosen);
        if (from) setRerouted(true);
      } catch (err) {
        if (!from) {
          resultRef.current = null;
          rendererRef.current?.set("directions", null);
          rendererRef.current?.setMap(map ?? null);
          clearAltLines();
          setSummaries([]);
          setSafestIndex(null);
          activePathRef.current = [];
        }
        const status = err instanceof Error ? err.message : "Directions request failed";
        setError(
          status.includes("ZERO_RESULTS")
            ? "No walking routes found between those places."
            : status,
        );
      } finally {
        setLoading(false);
      }
    },
    [clearAltLines, destination, map, origin, routesLib],
  );

  /**
   * Score already-planned transit alternatives. Used right after Directions
   * returns and again every minute while live, so waits, skipped stops and
   * alerts stay current without re-asking Google.
   */
  const scoreTransitLegs = useCallback(
    async (result: google.maps.DirectionsResult, legsByRoute: LegRequest[][], transitMode: "subway" | "bus") => {
      const clock = liveRef.current;
      const simNow = clock.simMs != null ? new Date(clock.simMs).toISOString() : undefined;
      const scoreResponse = await fetch("/api/routes/transit/score", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          corridor_meters: CORRIDOR_METERS,
          sim_now: simNow,
          include_live: true,
          include_history: true,
          routes: legsByRoute.map((legs, index) => ({ route_id: String(index), legs })),
        }),
      });
      if (!scoreResponse.ok) {
        const body = await scoreResponse.json().catch(() => null);
        const detail = body && typeof body.detail === "string" ? body.detail : "";
        throw new Error(detail || `Could not score ${transitMode} routes`);
      }
      const scored = (await scoreResponse.json()) as TransitScoreResponse;
      setLiveInfo(
        scored.realtime
          ? { observedAt: scored.realtime.observed_at, routes: scored.realtime.routes_running.length }
          : null,
      );
      const byId = new Map(scored.routes.map((route) => [route.route_id, route]));
      const label = transitMode === "bus" ? "bus" : "train";
      return result.routes.map((route, index): RouteSummary => {
        const leg = route.legs[0];
        const score = byId.get(String(index));
        const lines = (score?.lines?.length ? score.lines : routeLines(legsByRoute[index])).map(shortLine);
        return {
          index,
          distance: leg?.distance?.text ?? "—",
          duration: leg?.duration?.text ?? "—",
          summary: lines.length > 0 ? `${lines.join(" → ")} ${label}` : `${transitMode} route ${index + 1}`,
          safetyScore: score?.safety_score ?? null,
          incidentCount: score?.incident_count ?? null,
          lightScore: score?.light_score ?? null,
          lightCount: score?.light_count ?? null,
          mode: transitMode,
          lines,
          stations: score?.stations ?? [],
          walkScore: score?.walk_score ?? null,
          weakestStation: score?.weakest_station_score ?? null,
          longestWait: score?.longest_wait_min ?? null,
          historyCount: score?.history_count ?? null,
        };
      });
    },
    [],
  );

  /**
   * Subway / bus mode. Same Directions service with TRANSIT, then each
   * alternative is split into walking legs (corridor-scored) and rides
   * (scored by the stops you wait at) by POST /routes/transit/score. The
   * walking path above is untouched; this is a separate request path.
   *
   * Google only plans transit for the near future, so the departure time is
   * the sim clock's NYC weekday and time-of-day in the coming week.
   */
  const requestTransitRoute = useCallback(
    async (transitMode: "subway" | "bus") => {
      if (!routesLib) {
        setError("Directions library is still loading.");
        return;
      }
      const start = origin?.location;
      const end = destinationRef.current;
      if (!start || !end) {
        setError("Choose an origin and a destination.");
        return;
      }

      setLoading(true);
      setError("");
      setRerouted(false);
      const service = new routesLib.DirectionsService();

      try {
        const result = await service.route({
          origin: start,
          destination: end.location,
          travelMode: google.maps.TravelMode.TRANSIT,
          transitOptions: {
            modes: [transitMode === "bus" ? google.maps.TransitMode.BUS : google.maps.TransitMode.SUBWAY],
            departureTime: departureTimeFor(liveRef.current.simMs),
          },
          provideRouteAlternatives: true,
        });
        resultRef.current = result;
        const legsByRoute = result.routes.map((route) => legsFromRoute(route));
        transitLegsRef.current = legsByRoute;
        const nextSummaries = await scoreTransitLegs(result, legsByRoute, transitMode);
        const chosen = pickIndex(nextSummaries, preferSafeRef.current, preferLitRef.current);
        const chosenPath = result.routes[chosen]?.overview_path.map((point) => ({
          lat: point.lat(),
          lng: point.lng(),
        }));
        activePathRef.current = chosenPath ?? [];
        routeAnchorProgress.current = liveRef.current.progress;
        handledCalls.current = new Set(callsRef.current.map((call) => call.source_id));
        rendererRef.current?.setDirections(result);
        setSummaries(nextSummaries);
        setSafestIndex(preferSafeRef.current || preferLitRef.current ? chosen : null);
        setSelectedIndex(chosen);
      } catch (err) {
        resultRef.current = null;
        transitLegsRef.current = null;
        rendererRef.current?.set("directions", null);
        rendererRef.current?.setMap(map ?? null);
        clearAltLines();
        setSummaries([]);
        setSafestIndex(null);
        activePathRef.current = [];
        const status = err instanceof Error ? err.message : "Directions request failed";
        setError(
          status.includes("ZERO_RESULTS")
            ? `No ${transitMode} routes found between those places.`
            : status,
        );
      } finally {
        setLoading(false);
      }
    },
    [clearAltLines, map, origin, routesLib, scoreTransitLegs],
  );

  const requestForMode = useCallback(() => {
    if (modeRef.current === "subway" || modeRef.current === "bus") return requestTransitRoute(modeRef.current);
    return requestRoute();
  }, [requestRoute, requestTransitRoute]);

  // Live re-score: every minute while a transit result is shown, re-run the
  // scoring (not the Directions request) so waits, skipped stops and alerts
  // track the feeds. If the safest alternative changes, the pick follows it.
  useEffect(() => {
    if (mode === "walk" || summaries.length === 0) return;
    const timer = window.setInterval(() => {
      const result = resultRef.current;
      const legs = transitLegsRef.current;
      const current = modeRef.current;
      if (!result || !legs || current === "walk") return;
      void scoreTransitLegs(result, legs, current)
        .then((next) => {
          if (resultRef.current === result) setSummaries(next);
        })
        .catch(() => undefined);
    }, LIVE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [mode, scoreTransitLegs, summaries.length]);

  useEffect(() => {
    if (summaries.length === 0) return;
    const chosen = pickIndex(summaries, preferSafe, preferLit);
    setSafestIndex(preferSafe || preferLit ? chosen : null);
    setSelectedIndex(chosen);
  }, [preferLit, preferSafe, summaries]);

  // Switching modes with a pair already chosen re-requests in the new mode.
  const lastModeRef = useRef(mode);
  useEffect(() => {
    if (lastModeRef.current === mode) return;
    lastModeRef.current = mode;
    if (origin && destination) void requestForMode();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  // Toggling history in Walk mode re-scores the walking alternatives.
  const lastHistoryRef = useRef(useHistory);
  useEffect(() => {
    if (lastHistoryRef.current === useHistory) return;
    lastHistoryRef.current = useHistory;
    if (modeRef.current === "walk" && origin && destination) void requestRoute();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useHistory]);

  useEffect(() => {
    if (!map) return;
    const marker = new google.maps.Marker({
      map,
      clickable: false,
      zIndex: 5,
      visible: false,
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: 8,
        fillColor: "#1a73e8",
        fillOpacity: 1,
        strokeColor: "#ffffff",
        strokeWeight: 2,
      },
      title: "You",
    });
    let frame = 0;
    const step = () => {
      const path = activePathRef.current;
      const clock = liveRef.current;
      if (path.length > 1 && (clock.playing || clock.progress > 0)) {
        const span = Math.max(0.001, 1 - routeAnchorProgress.current);
        const fraction = Math.min(1, Math.max(0, (clock.progress - routeAnchorProgress.current) / span));
        marker.setPosition(pointAlong(path, fraction));
        marker.setVisible(true);
      } else {
        marker.setVisible(false);
      }
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(frame);
      marker.setMap(null);
    };
  }, [map]);

  useEffect(() => {
    if (!destination || activePathRef.current.length < 2 || arrivedSent.current) return;
    const span = Math.max(0.001, 1 - routeAnchorProgress.current);
    const fraction = Math.min(1, Math.max(0, (follow.progress - routeAnchorProgress.current) / span));
    if (fraction < 0.98) return;
    arrivedSent.current = true;
    void fetch("/api/checkin/arrived", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        destination: placeLabel(destination),
        friend: friendPhoneRef.current.trim() || undefined,
      }),
    }).catch(() => undefined);
  }, [destination, follow.progress, summaries.length]);

  useEffect(() => {
    const clock = liveRef.current;
    // Live reroute around a new call only applies to walking. Subway routes are
    // fixed to their stations, so a nearby street call does not re-plan them.
    if (
      modeRef.current !== "walk" ||
      !preferSafe ||
      !clock.playing ||
      !destination ||
      activePathRef.current.length < 2 ||
      rerouteLock.current
    ) {
      return;
    }
    const span = Math.max(0.001, 1 - routeAnchorProgress.current);
    const fraction = Math.min(1, Math.max(0, (clock.progress - routeAnchorProgress.current) / span));
    const ahead = pathFrom(activePathRef.current, fraction);
    const nearby = follow.calls.filter(
      (call) => !handledCalls.current.has(call.source_id) && metersToPath(call, ahead) <= CORRIDOR_METERS,
    );
    if (nearby.length === 0) return;
    for (const call of nearby) handledCalls.current.add(call.source_id);
    const here = pointAlong(activePathRef.current, fraction);
    rerouteLock.current = true;
    void requestRoute(here).finally(() => {
      rerouteLock.current = false;
      setRerouteTick((value) => value + 1);
    });
  }, [destination, follow.calls, preferSafe, requestRoute, rerouteTick]);

  function locateMe() {
    if (!navigator.geolocation) {
      setError("This browser cannot read your location.");
      return;
    }
    setLocating(true);
    setError("");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        onOriginSelect({
          name: "Your location",
          address: "Your location",
          location: { lat: position.coords.latitude, lng: position.coords.longitude },
        });
        setLocating(false);
      },
      () => {
        setError("Allow location access to use your current location.");
        setLocating(false);
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 },
    );
  }

  const preferenceCount = Number(preferSafe) + Number(preferLit);

  return (
    <div className="w-full">
      {open ? (
        <div className="hud max-h-[calc(100dvh-12rem)] w-full overflow-auto">
          <div className="flex items-center justify-between px-3 pt-2">
            <p className="hud-label">Directions</p>
            <span className="flex items-center gap-1">
              <button type="button" onClick={toggleTheme} className="chip px-2 py-1 text-[10px] font-semibold uppercase">
                {theme === "dark" ? "Light" : "Dark"}
              </button>
              <button
                type="button"
                aria-label="Close search"
                onClick={() => setOpen(false)}
                className="grid h-7 w-7 place-items-center"
              >
                <span aria-hidden="true">×</span>
              </button>
            </span>
          </div>
          <div className="flex items-center justify-between gap-2 px-3 pb-2">
            <div className="flex gap-1">
              <button
                type="button"
                aria-pressed={mode === "walk"}
                onClick={() => setMode("walk")}
                className="chip inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium"
                title="Walking routes"
              >
                <WalkIcon />
                Walk
              </button>
              <button
                type="button"
                aria-pressed={mode === "subway"}
                onClick={() => setMode("subway")}
                className="chip inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium"
                title="Subway routes scored by station safety"
              >
                <TrainGlyph hot={mode === "subway"} />
                Subway
              </button>
              <button
                type="button"
                aria-pressed={mode === "bus"}
                onClick={() => setMode("bus")}
                className="chip inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium"
                title="Bus routes scored by the stops you wait at"
              >
                <BusIcon hot={mode === "bus"} />
                Bus
              </button>
            </div>
            <div className="flex gap-1">
              <button
                type="button"
                aria-pressed={preferSafe}
                onClick={() => setPreferSafe((value) => !value)}
                className="chip inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium"
              >
                <ShieldIcon hot={preferSafe} />
                Safe
              </button>
              <button
                type="button"
                aria-pressed={preferLit}
                onClick={() => setPreferLit((value) => !value)}
                className="chip inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium"
              >
                <LampIcon />
                Well-lit
              </button>
              <span className="self-center text-xs font-medium text-[var(--muted)]">{preferenceCount} of 2</span>
            </div>
          </div>
          <div className="flex flex-col gap-2 px-3 pb-3">
            <label className="hud-label block" htmlFor="route-origin">
              Starting point
              <PlaceAutocompleteInput
                id="route-origin"
                placeholder="Choose starting point"
                value={placeLabel(origin)}
                onPlaceSelect={onOriginSelect}
                className="hud-field"
              />
            </label>
            <button
              type="button"
              onClick={locateMe}
              disabled={locating}
              className="chip self-start px-2.5 py-1 text-xs font-medium disabled:opacity-50"
            >
              {locating ? "Finding you…" : "Your location"}
            </button>
            <div className="flex items-end gap-2">
              <label className="hud-label block min-w-0 flex-1" htmlFor="route-destination">
                Destination
                <PlaceAutocompleteInput
                  id="route-destination"
                  placeholder="Choose destination"
                  value={placeLabel(destination)}
                  onPlaceSelect={onDestinationSelect}
                  className="hud-field"
                />
              </label>
              <button
                type="button"
                aria-label="Directions"
                onClick={() => void requestForMode()}
                disabled={loading}
                className="mb-0.5 grid h-10 w-10 shrink-0 place-items-center bg-[var(--accent)] text-white disabled:opacity-50"
              >
                <DirectionsIcon />
              </button>
            </div>
            <label className="hud-label block" htmlFor="route-friend">
              Text a friend when I arrive
              <input
                id="route-friend"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                placeholder="Friend’s number"
                value={friendPhone}
                onChange={(event) => setFriendPhone(event.target.value)}
                className="hud-field"
              />
            </label>
          </div>
          {mode === "walk" ? (
            <label className="flex items-center gap-2 px-3 pb-2 text-xs text-[var(--muted)]">
              <input
                type="checkbox"
                checked={useHistory}
                onChange={(event) => setUseHistory(event.target.checked)}
                className="accent-[var(--accent)]"
              />
              Include historical crime for this time of day
            </label>
          ) : (
            <p className="px-3 pb-2 text-[10px] text-[var(--muted)]">
              Scored with historical complaints for this time of day
              {liveInfo
                ? ` · live MTA feed ${new Intl.DateTimeFormat("en-US", {
                    timeZone: "America/New_York",
                    hour: "numeric",
                    minute: "2-digit",
                  }).format(new Date(liveInfo.observedAt))}, ${liveInfo.routes} lines running`
                : ""}
            </p>
          )}
          {error ? <p className="px-3 pb-2 text-xs text-red-400">{error}</p> : null}
          {rerouted ? <p className="px-3 pb-2 text-xs text-[var(--good)]">Route updated around a nearby call.</p> : null}
          {summaries.length > 0 ? (
            <ul className="flex max-h-48 flex-col gap-1 overflow-auto px-3 pb-3">
              {[...summaries]
                .sort(
                  (a, b) =>
                    preferenceScore(b, preferSafe, preferLit) - preferenceScore(a, preferSafe, preferLit) ||
                    a.index - b.index,
                )
                .map((route) => {
                  const selected = route.index === selectedIndex;
                  const best = route.index === safestIndex;
                  return (
                    <li key={route.index}>
                      <button
                        type="button"
                        onClick={() => setSelectedIndex(route.index)}
                        className={`flex w-full items-center gap-2 border px-3 py-2 text-left text-sm ${
                          selected
                            ? best
                              ? "border-[var(--edge)] bg-[var(--good-bg)]"
                              : preferSafe || preferLit
                                ? "border-[var(--line)] bg-[var(--warn-bg)]"
                                : "border-[var(--accent)] bg-[var(--field)]"
                            : "border-[var(--line)] bg-[var(--field)]"
                        }`}
                      >
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-center gap-2 font-medium">
                            <span>{route.duration}</span>
                            {preferSafe && route.safetyScore != null ? (
                              <span className="inline-flex items-center gap-1 text-xs">
                                <ShieldIcon hot={best} />
                                {route.safetyScore}
                              </span>
                            ) : null}
                            {preferLit && route.lightScore != null ? (
                              <span className="inline-flex items-center gap-1 text-xs">
                                <LampIcon hot={best} />
                                {route.lightScore}
                              </span>
                            ) : null}
                          </span>
                          <span className="block text-xs text-[var(--muted)]">
                            {route.distance}
                            {preferSafe && route.incidentCount != null
                              ? ` · ${route.incidentCount} ${route.incidentCount === 1 ? "call" : "calls"}`
                              : ""}
                            {preferLit && route.lightCount != null
                              ? ` · ${route.lightCount} dark ${route.lightCount === 1 ? "spot" : "spots"}`
                              : ""}
                            {route.summary ? ` · ${route.summary}` : ""}
                          </span>
                          {route.mode !== "walk" && route.stations && route.stations.length > 0 ? (
                            <span className="mt-1 flex flex-wrap items-center gap-1">
                              {route.lines?.map((line) => (
                                <span key={line} className={`route-bullet${route.mode === "bus" ? " route-bullet-bus" : ""}`}>
                                  {line}
                                </span>
                              ))}
                              {route.stations.map((hop, hopIndex) => {
                                const badge = hopBadge(hop);
                                const liveHop = hop.flags.some((flag) => LIVE_FLAGS.has(flag));
                                const tip = [
                                  `${hop.role}: ${hop.name ?? "stop"} · ${hop.safety_score ?? "—"}`,
                                  hop.wait_min != null ? `wait ${Math.round(hop.wait_min)} min (${hop.wait_source ?? "scheduled"})` : "",
                                  hop.quiet ? "quiet at this hour" : "",
                                  ...hop.notes,
                                ]
                                  .filter(Boolean)
                                  .join("\n");
                                return (
                                  <span
                                    key={`${hop.complex_id ?? hop.name}-${hopIndex}`}
                                    className={`station-chip station-chip-${scoreBand(hop.safety_score)}${liveHop ? " station-chip-live" : ""}`}
                                    title={tip}
                                  >
                                    {hop.role === "transfer" ? "⇄ " : hop.role === "alight" ? "→ " : ""}
                                    {(hop.name ?? "stop").replace(/ ?- ?/g, "-").slice(0, 22)}
                                    {hop.safety_score != null ? ` ${hop.safety_score}` : ""}
                                    {badge ? <span className="station-chip-badge">{badge}</span> : null}
                                  </span>
                                );
                              })}
                              {route.walkScore != null && preferSafe ? (
                                <span className="text-[10px] text-[var(--muted)]">
                                  walk {route.walkScore}
                                  {route.longestWait != null && route.longestWait >= 1
                                    ? ` · wait up to ${Math.round(route.longestWait)} min`
                                    : ""}
                                </span>
                              ) : null}
                            </span>
                          ) : null}
                        </span>
                      </button>
                    </li>
                  );
                })}
            </ul>
          ) : null}
        </div>
      ) : (
        <button
          type="button"
          aria-label="Search an address"
          onClick={() => setOpen(true)}
          className="hud grid h-11 w-11 place-items-center"
        >
          <SearchIcon />
        </button>
      )}
    </div>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 5 1.5-1.5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14"
      />
    </svg>
  );
}

function DirectionsIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="m21.71 11.29-9-9a1 1 0 0 0-1.42 0l-9 9a1 1 0 0 0 0 1.42l9 9a1 1 0 0 0 1.42 0l9-9a1 1 0 0 0 0-1.42M14 14.5V12h-4v3H8v-4a1 1 0 0 1 1-1h5V7.5l3.5 3.5z"
      />
    </svg>
  );
}

function WalkIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true">
      <path
        fill="currentColor"
        d="M13.5 5.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4M9.8 8.9 7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3c1.3 1.5 3.3 2.5 5.5 2.5v-2c-1.9 0-3.5-1-4.3-2.4l-1-1.6c-.4-.6-1-1-1.7-1-.3 0-.5.1-.8.1L6 8.3V13h2V9.6z"
      />
    </svg>
  );
}

function BusIcon({ hot = false }: { hot?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={`h-4 w-4 shrink-0 ${hot ? "text-[var(--good)]" : ""}`} aria-hidden="true">
      <path
        fill="currentColor"
        d="M4 16c0 .88.39 1.67 1 2.22V20a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1h8v1a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1.78c.61-.55 1-1.34 1-2.22V6c0-3.5-3.58-4-8-4S4 2.5 4 6zm3.5 1a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3m9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3M18 11H6V6h12z"
      />
    </svg>
  );
}

function ShieldIcon({ hot = false }: { hot?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={`h-4 w-4 shrink-0 ${hot ? "text-[var(--good)]" : "text-[var(--muted)]"}`} aria-label="Safe">
      <path
        fill="currentColor"
        d="M12 2 4 5v6.1c0 5 3.4 9.7 8 10.9 4.6-1.2 8-5.9 8-10.9V5zm-1.1 13.2-3.2-3.2 1.4-1.4 1.8 1.8 3.8-3.8 1.4 1.4z"
      />
    </svg>
  );
}

function LampIcon({ hot = false }: { hot?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={`h-4 w-4 shrink-0 ${hot ? "text-[#7ad7ff]" : ""}`} aria-label="Well-lit">
      <path fill="currentColor" d="M9 1h6v3H9zm-2 3h10v8H7zm2 8h6v5H9zm-2 5h10v3H7z" />
    </svg>
  );
}
