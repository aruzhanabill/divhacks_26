"use client";

import { useEffect, useState } from "react";
import { ControlPosition, MapControl, useMap } from "@vis.gl/react-google-maps";
import { MapCanvas } from "@/components/MapCanvas";
import { Playback } from "@/components/Playback";
import { RoutePanel } from "@/components/RoutePanel";
import { FollowProvider } from "@/lib/follow";
import { ThemeProvider } from "@/lib/theme";
import type { SelectedPlace } from "@/lib/googleMaps";

type MapsAppProps = {
  apiKey: string;
};

export function MapsApp({ apiKey }: MapsAppProps) {
  const [origin, setOrigin] = useState<SelectedPlace | null>(null);
  const [destination, setDestination] = useState<SelectedPlace | null>(null);

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
    <ThemeProvider>
      <FollowProvider>
        <MapCanvas apiKey={apiKey}>
          <MapControl position={ControlPosition.TOP_LEFT}>
            <RoutePanel
              origin={origin}
              destination={destination}
              onOriginSelect={setOrigin}
              onDestinationSelect={setDestination}
            />
          </MapControl>
          <MapControl position={ControlPosition.LEFT_BOTTOM}>
            <Playback />
          </MapControl>
          <MapControl position={ControlPosition.RIGHT_BOTTOM}>
            <ZoomControls />
          </MapControl>
        </MapCanvas>
      </FollowProvider>
    </ThemeProvider>
  );
}

function ZoomControls() {
  const map = useMap();

  useEffect(() => {
    if (!map) return;
    const options = { cameraControl: false, zoomControl: false };
    map.setOptions(options as google.maps.MapOptions);
  }, [map]);

  function zoomBy(delta: number) {
    if (!map) return;
    const current = map.getZoom() ?? 12;
    map.setZoom(Math.min(21, Math.max(3, current + delta)));
  }

  return (
    <div className="mb-10 mr-4 flex flex-col gap-1">
      <button
        type="button"
        aria-label="Zoom in"
        onClick={() => zoomBy(1)}
        className="hud grid h-10 w-10 place-items-center text-lg font-semibold leading-none"
      >
        +
      </button>
      <button
        type="button"
        aria-label="Zoom out"
        onClick={() => zoomBy(-1)}
        className="hud grid h-10 w-10 place-items-center text-lg font-semibold leading-none"
      >
        −
      </button>
    </div>
  );
}
