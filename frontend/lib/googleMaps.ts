export const NYC_CENTER = { lat: 40.749933, lng: -73.98633 };

export const NYC_DEFAULT_ZOOM = 13;

export const NYC_BOUNDS = {
  north: 40.9176,
  south: 40.4774,
  east: -73.7004,
  west: -74.2591,
};

export const GOOGLE_MAP_ID = "DEMO_MAP_ID";

export const MAP_LIBRARIES: ("places" | "routes")[] = ["places", "routes"];

export const WALKING_TRAVEL_MODE = "WALKING" as const;

export type SelectedPlace = {
  name: string;
  address: string;
  location: google.maps.LatLngLiteral;
  viewport?: google.maps.LatLngBounds | null;
};

export function latLngLiteral(
  value: google.maps.LatLng | google.maps.LatLngLiteral,
): google.maps.LatLngLiteral {
  if (value instanceof google.maps.LatLng) {
    return { lat: value.lat(), lng: value.lng() };
  }
  return { lat: value.lat, lng: value.lng };
}

export function placeFromAutocomplete(
  place: google.maps.places.PlaceResult,
): SelectedPlace | null {
  const loc = place.geometry?.location;
  if (!loc) return null;
  return {
    name: place.name ?? "Selected place",
    address: place.formatted_address ?? "",
    location: latLngLiteral(loc),
    viewport: place.geometry?.viewport ?? null,
  };
}
