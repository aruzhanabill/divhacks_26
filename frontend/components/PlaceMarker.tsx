"use client";

import { AdvancedMarker, InfoWindow, useMap } from "@vis.gl/react-google-maps";
import { useEffect, useState } from "react";
import type { SelectedPlace } from "@/lib/googleMaps";

type PlaceMarkerProps = {
  place: SelectedPlace | null;
};

export function PlaceMarker({ place }: PlaceMarkerProps) {
  const map = useMap();
  const [infoOpen, setInfoOpen] = useState(true);

  useEffect(() => {
    if (!map || !place) return;
    setInfoOpen(true);
    if (place.viewport) {
      map.fitBounds(place.viewport);
      return;
    }
    map.setCenter(place.location);
    map.setZoom(17);
  }, [map, place]);

  if (!place) return null;

  return (
    <>
      <AdvancedMarker
        position={place.location}
        title={place.name}
        onClick={() => setInfoOpen(true)}
      />
      {infoOpen ? (
        <InfoWindow
          position={place.location}
          onCloseClick={() => setInfoOpen(false)}
        >
          <div className="max-w-xs text-zinc-900">
            <strong>{place.name}</strong>
            {place.address ? (
              <>
                <br />
                <span>{place.address}</span>
              </>
            ) : null}
          </div>
        </InfoWindow>
      ) : null}
    </>
  );
}
