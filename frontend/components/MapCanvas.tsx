"use client";

import { type ReactNode } from "react";
import { APIProvider, ColorScheme, Map } from "@vis.gl/react-google-maps";
import {
  GOOGLE_MAP_ID,
  MAP_LIBRARIES,
  MAPS_JS_VERSION,
  NYC_CENTER,
  NYC_DEFAULT_ZOOM,
} from "@/lib/googleMaps";
import { useTheme } from "@/lib/theme";

type MapCanvasProps = {
  apiKey: string;
  children?: ReactNode;
};

export function MapCanvas({ apiKey, children }: MapCanvasProps) {
  const { theme } = useTheme();
  return (
    <APIProvider apiKey={apiKey} libraries={MAP_LIBRARIES} version={MAPS_JS_VERSION}>
      <div className="relative h-dvh w-full">
        <Map
          className="absolute inset-0 h-full w-full"
          defaultCenter={NYC_CENTER}
          defaultZoom={NYC_DEFAULT_ZOOM}
          mapId={GOOGLE_MAP_ID}
          colorScheme={theme === "dark" ? ColorScheme.DARK : ColorScheme.LIGHT}
          gestureHandling="greedy"
          mapTypeControl={false}
          streetViewControl={false}
          fullscreenControl={false}
          zoomControl={false}
        >
          {children}
        </Map>
        <img
          src="/safe-path-banner.png"
          alt="Safe Path"
          className="pointer-events-none absolute top-3 left-1/2 z-10 h-16 w-auto max-w-[min(92vw,28rem)] -translate-x-1/2"
        />
      </div>
    </APIProvider>
  );
}
