"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useMap, useMapsLibrary } from "@vis.gl/react-google-maps";
import { PlaceAutocompleteInput } from "@/components/PlaceSearch";
import { useFollow, useFollowLive } from "@/lib/follow";
import { useTheme } from "@/lib/theme";
import { metersToPath, pathFrom, pointAlong, type LatLng } from "@/lib/geo";
import type { SelectedPlace } from "@/lib/googleMaps";

const CORRIDOR_METERS = 150;

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
  const friendPhoneRef = useRef(friendPhone);
  callsRef.current = follow.calls;
  friendPhoneRef.current = friendPhone;
  destinationRef.current = destination;
  liveRef.current = live;
  preferSafeRef.current = preferSafe;
  preferLitRef.current = preferLit;

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

  useEffect(() => {
    if (summaries.length === 0) return;
    const chosen = pickIndex(summaries, preferSafe, preferLit);
    setSafestIndex(preferSafe || preferLit ? chosen : null);
    setSelectedIndex(chosen);
  }, [preferLit, preferSafe, summaries]);

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
    if (!preferSafe || !clock.playing || !destination || activePathRef.current.length < 2 || rerouteLock.current) {
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
            </div>
            <span className="text-xs font-medium text-[var(--muted)]">{preferenceCount} of 2</span>
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
                onClick={() => void requestRoute()}
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
