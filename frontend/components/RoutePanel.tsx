"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useMap, useMapsLibrary } from "@vis.gl/react-google-maps";
import { PlaceAutocompleteInput } from "@/components/PlaceSearch";
import type { SelectedPlace } from "@/lib/googleMaps";

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
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [summaries, setSummaries] = useState<RouteSummary[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [safestIndex, setSafestIndex] = useState<number | null>(null);
  const rendererRef = useRef<google.maps.DirectionsRenderer | null>(null);
  const altLinesRef = useRef<google.maps.Polyline[]>([]);
  const resultRef = useRef<google.maps.DirectionsResult | null>(null);

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
        strokeColor: "#2563eb",
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

  const requestRoute = useCallback(async () => {
    if (!routesLib) {
      setError("Directions library is still loading.");
      return;
    }
    if (!origin || !destination) {
      setError("Choose an origin and a destination.");
      return;
    }

    setLoading(true);
    setError("");
    const service = new routesLib.DirectionsService();

    try {
      const result = await service.route({
        origin: origin.location,
        destination: destination.location,
        travelMode: google.maps.TravelMode.WALKING,
        provideRouteAlternatives: true,
      });
      resultRef.current = result;
      const scoreResponse = await fetch("/api/routes/score", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          corridor_meters: 150,
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
      rendererRef.current?.setDirections(result);
      setSummaries(nextSummaries);
      setSafestIndex(safest?.index ?? 0);
      setSelectedIndex(safest?.index ?? 0);
    } catch (err) {
      resultRef.current = null;
      rendererRef.current?.set("directions", null);
      rendererRef.current?.setMap(map ?? null);
      clearAltLines();
      setSummaries([]);
      setSafestIndex(null);
      const status = err instanceof Error ? err.message : "Directions request failed";
      setError(
        status.includes("ZERO_RESULTS")
          ? "No walking routes found between those places."
          : status,
      );
    } finally {
      setLoading(false);
    }
  }, [clearAltLines, destination, map, origin, routesLib]);

  return (
    <div className="rounded-xl bg-white/95 p-3 shadow-lg ring-1 ring-zinc-200 backdrop-blur">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
        Walking route
      </p>
      <div className="flex flex-col gap-2">
        <div>
          <label htmlFor="route-origin" className="mb-1 block text-xs text-zinc-500">
            Origin
          </label>
          <PlaceAutocompleteInput
            id="route-origin"
            placeholder="Starting point"
            onPlaceSelect={onOriginSelect}
          />
        </div>
        <div>
          <label htmlFor="route-destination" className="mb-1 block text-xs text-zinc-500">
            Destination
          </label>
          <PlaceAutocompleteInput
            id="route-destination"
            placeholder="Where to?"
            onPlaceSelect={onDestinationSelect}
          />
        </div>
        <button
          type="button"
          onClick={requestRoute}
          disabled={loading}
          className="rounded-lg bg-zinc-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {loading ? "Scoring routes…" : "Find safer route"}
        </button>
        <p className="text-xs text-zinc-500">
          Compares walks and keeps the one with fewer crimes within 150 m. Higher score is safer.
        </p>
        {error ? <p className="text-xs text-red-600">{error}</p> : null}
        {summaries.length > 0 ? (
          <ul className="flex max-h-48 flex-col gap-1 overflow-auto">
            {[...summaries]
              .sort(
                (a, b) =>
                  (b.safetyScore ?? -1) - (a.safetyScore ?? -1) || a.index - b.index,
              )
              .map((route) => {
                const selected = route.index === selectedIndex;
                const safest = route.index === safestIndex;
                return (
                  <li key={route.index}>
                    <button
                      type="button"
                      onClick={() => setSelectedIndex(route.index)}
                      className={`w-full rounded-lg border px-3 py-2 text-left text-sm ${
                        selected
                          ? safest
                            ? "border-green-700 bg-green-50 text-green-950"
                            : "border-orange-700 bg-orange-50 text-orange-950"
                          : "border-zinc-200 bg-white text-zinc-800"
                      }`}
                    >
                      <span className="block font-medium">
                        {safest ? "Safest · " : ""}
                        {route.duration}
                        {route.safetyScore != null ? ` · ${route.safetyScore}` : ""}
                      </span>
                      <span className="block text-xs text-zinc-500">
                        {route.distance}
                        {route.incidentCount != null
                          ? ` · ${route.incidentCount} calls nearby`
                          : ""}
                        {route.summary ? ` · ${route.summary}` : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
