"use client";

import { useCallback, useState } from "react";
import { ControlPosition, MapControl } from "@vis.gl/react-google-maps";
import { MapCanvas } from "@/components/MapCanvas";
import { PlaceMarker } from "@/components/PlaceMarker";
import { PlaceSearch } from "@/components/PlaceSearch";
import { Playback } from "@/components/Playback";
import { RoutePanel } from "@/components/RoutePanel";
import type { SelectedPlace } from "@/lib/googleMaps";

type MapsAppProps = {
  apiKey: string;
};

export function MapsApp({ apiKey }: MapsAppProps) {
  const [place, setPlace] = useState<SelectedPlace | null>(null);
  const [placeError, setPlaceError] = useState("");
  const [origin, setOrigin] = useState<SelectedPlace | null>(null);
  const [destination, setDestination] = useState<SelectedPlace | null>(null);

  const onPlaceSelect = useCallback((next: SelectedPlace | null) => {
    if (!next) {
      setPlace(null);
      setPlaceError("No location found for that place.");
      return;
    }
    setPlaceError("");
    setPlace(next);
  }, []);

  if (!apiKey) {
    return (
      <div className="flex h-dvh items-center justify-center bg-zinc-100 px-6 text-center">
        <p className="max-w-md text-sm text-zinc-700">
          Missing Google Maps API key. Copy{" "}
          <code className="rounded bg-zinc-200 px-1">frontend/.env.example</code> to{" "}
          <code className="rounded bg-zinc-200 px-1">frontend/.env.local</code> and set{" "}
          <code className="rounded bg-zinc-200 px-1">NEXT_PUBLIC_GOOGLE_MAPS_API_KEY</code>.
          Enable Maps JavaScript API, Places API, and Directions API.
        </p>
      </div>
    );
  }

  return (
    <MapCanvas apiKey={apiKey}>
      <PlaceMarker place={place} />
      <MapControl position={ControlPosition.TOP_LEFT}>
        <div className="m-2 flex max-h-[calc(100dvh-1rem)] w-[min(100vw-1rem,22rem)] flex-col gap-3 overflow-auto">
          <PlaceSearch onPlaceSelect={onPlaceSelect} error={placeError} />
          <RoutePanel
            origin={origin}
            destination={destination}
            onOriginSelect={setOrigin}
            onDestinationSelect={setDestination}
          />
          <Playback />
        </div>
      </MapControl>
    </MapCanvas>
  );
}
