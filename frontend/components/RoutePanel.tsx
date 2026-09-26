"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useMap, useMapsLibrary } from "@vis.gl/react-google-maps";
import { PlaceAutocompleteInput } from "@/components/PlaceSearch";
import { useFollow, useFollowLive } from "@/lib/follow";
import { metersToPath, pathFrom, pointAlong, type LatLng } from "@/lib/geo";
import type { SelectedPlace } from "@/lib/googleMaps";

const CORRIDOR_METERS = 150;

type RouteScore = {
  route_id: string;
  safety_score: number;
  incident_count: number;
};

type RouteSummary = {
  index: number;
  distance: string;
  duration: string;
  summary: string;
  safetyScore: number | null;
  incidentCount: number | null;
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
  const [open, setOpen] = useState(false);
  const [rerouteTick, setRerouteTick] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [rerouted, setRerouted] = useState(false);
  const [summaries, setSummaries] = useState<RouteSummary[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [safestIndex, setSafestIndex] = useState<number | null>(null);
  const rendererRef = useRef<google.maps.DirectionsRenderer | null>(null);
  const altLinesRef = useRef<google.maps.Polyline[]>([]);
  const resultRef = useRef<google.maps.DirectionsResult | null>(null);
  const activePathRef = useRef<LatLng[]>([]);
  const routeAnchorProgress = useRef(0);
  const handledCalls = useRef(new Set<string>());
  const rerouteLock = useRef(false);
  const callsRef = useRef(follow.calls);
  const destinationRef = useRef(destination);
  const liveRef = useRef(live);
  callsRef.current = follow.calls;
  destinationRef.current = destination;
  liveRef.current = live;

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
    const safest = safestIndex != null && selectedIndex === safestIndex;
    renderer.setOptions({
      polylineOptions: {
        strokeColor: safest ? "#15803d" : "#c2410c",
        strokeWeight: 6,
        zIndex: 2,
      },
    });
    renderer.setRouteIndex(selectedIndex);
    drawAlternatives(result, selectedIndex);
  }, [drawAlternatives, safestIndex, selectedIndex]);

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
          };
        });
        const safest = [...nextSummaries].sort(
          (a, b) => (b.safetyScore ?? -1) - (a.safetyScore ?? -1) || a.index - b.index,
        )[0];
        const chosen = safest?.index ?? 0;
        const chosenPath = result.routes[chosen]?.overview_path.map((point) => ({
          lat: point.lat(),
          lng: point.lng(),
        }));
        activePathRef.current = chosenPath ?? [];
        routeAnchorProgress.current = liveRef.current.progress;
        if (!from) {
          handledCalls.current = new Set(callsRef.current.map((call) => call.source_id));
          setRerouted(false);
        }
        rendererRef.current?.setDirections(result);
        setSummaries(nextSummaries);
        setSafestIndex(chosen);
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
    const clock = liveRef.current;
    if (!clock.playing || !destination || activePathRef.current.length < 2 || rerouteLock.current) {
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
  }, [destination, follow.calls, requestRoute, rerouteTick]);

  return (
    <div className="m-3">
      {open ? (
        <div className="w-[min(100vw-1.5rem,22rem)] rounded-xl bg-white/95 p-3 shadow-lg ring-1 ring-zinc-200 backdrop-blur">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">Directions</p>
            <button
              type="button"
              aria-label="Close directions"
              onClick={() => setOpen(false)}
              className="grid h-7 w-7 place-items-center rounded-full text-zinc-500 hover:bg-zinc-100"
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
          <div className="flex items-start gap-2">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <PlaceAutocompleteInput
                id="route-origin"
                placeholder="Choose starting point"
                onPlaceSelect={onOriginSelect}
              />
              <PlaceAutocompleteInput
                id="route-destination"
                placeholder="Choose destination"
                onPlaceSelect={onDestinationSelect}
              />
            </div>
            <button
              type="button"
              aria-label="Directions"
              onClick={() => void requestRoute()}
              disabled={loading}
              className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-[#1a73e8] text-white disabled:opacity-50"
            >
              <DirectionsIcon />
            </button>
          </div>
          {error ? <p className="mt-2 text-xs text-red-600">{error}</p> : null}
          {rerouted ? <p className="mt-2 text-xs text-green-800">Route updated around a nearby call.</p> : null}
          {summaries.length > 0 ? (
            <ul className="mt-2 flex max-h-48 flex-col gap-1 overflow-auto">
              {[...summaries]
                .sort((a, b) => (b.safetyScore ?? -1) - (a.safetyScore ?? -1) || a.index - b.index)
                .map((route) => {
                  const selected = route.index === selectedIndex;
                  const safest = route.index === safestIndex;
                  return (
                    <li key={route.index}>
                      <button
                        type="button"
                        onClick={() => setSelectedIndex(route.index)}
                        className={`flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm ${
                          selected
                            ? safest
                              ? "border-green-700 bg-green-50 text-green-950"
                              : "border-orange-700 bg-orange-50 text-orange-950"
                            : "border-zinc-200 bg-white text-zinc-800"
                        }`}
                      >
                        {safest ? <ShieldIcon /> : <span className="w-4" />}
                        <span className="min-w-0">
                          <span className="block font-medium">
                            {route.duration}
                            {route.safetyScore != null ? ` · ${route.safetyScore}` : ""}
                          </span>
                          <span className="block text-xs text-zinc-500">
                            {route.distance}
                            {route.incidentCount != null
                              ? ` · ${route.incidentCount} ${route.incidentCount === 1 ? "call" : "calls"} nearby`
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
          aria-label="Directions"
          onClick={() => setOpen(true)}
          className="grid h-11 w-11 place-items-center rounded-full bg-white text-[#1a73e8] shadow-lg ring-1 ring-zinc-200"
        >
          <DirectionsIcon />
        </button>
      )}
    </div>
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

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 text-green-700" aria-label="Safest route">
      <path
        fill="currentColor"
        d="M12 2 4 5v6.1c0 5 3.4 9.7 8 10.9 4.6-1.2 8-5.9 8-10.9V5zm-1.1 13.2-3.2-3.2 1.4-1.4 1.8 1.8 3.8-3.8 1.4 1.4z"
      />
    </svg>
  );
}
