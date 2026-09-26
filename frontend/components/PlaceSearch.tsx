"use client";

import { useEffect, useRef } from "react";
import { useMapsLibrary } from "@vis.gl/react-google-maps";
import { NYC_BOUNDS, placeFromAutocomplete, type SelectedPlace } from "@/lib/googleMaps";

type PlaceAutocompleteInputProps = {
  id: string;
  placeholder: string;
  onPlaceSelect: (place: SelectedPlace | null) => void;
  onError?: (message: string) => void;
};

export function PlaceAutocompleteInput({
  id,
  placeholder,
  onPlaceSelect,
  onError,
}: PlaceAutocompleteInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const places = useMapsLibrary("places");

  useEffect(() => {
    if (!places || !inputRef.current) return;

    const autocomplete = new places.Autocomplete(inputRef.current, {
      fields: ["geometry", "name", "formatted_address"],
      bounds: NYC_BOUNDS,
      strictBounds: false,
      componentRestrictions: { country: "us" },
    });

    const listener = autocomplete.addListener("place_changed", () => {
      const result = autocomplete.getPlace();
      const parsed = placeFromAutocomplete(result);
      if (!parsed) {
        onPlaceSelect(null);
        onError?.(
          `No details available for input: '${result.name ?? inputRef.current?.value ?? ""}'`,
        );
        return;
      }
      onError?.("");
      onPlaceSelect(parsed);
    });

    return () => {
      listener.remove();
      google.maps.event.clearInstanceListeners(autocomplete);
    };
  }, [onError, onPlaceSelect, places]);

  return (
    <input
      id={id}
      ref={inputRef}
      type="text"
      placeholder={placeholder}
      autoComplete="off"
      className="w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 shadow-sm outline-none placeholder:text-zinc-400 focus:border-zinc-400"
    />
  );
}

type PlaceSearchProps = {
  onPlaceSelect: (place: SelectedPlace | null) => void;
  error?: string;
};

export function PlaceSearch({ onPlaceSelect, error }: PlaceSearchProps) {
  return (
    <div className="rounded-xl bg-white/95 p-3 shadow-lg ring-1 ring-zinc-200 backdrop-blur">
      <label htmlFor="place-search" className="mb-1 block text-xs font-medium text-zinc-500">
        Search a place
      </label>
      <PlaceAutocompleteInput
        id="place-search"
        placeholder="Enter an address"
        onPlaceSelect={onPlaceSelect}
      />
      {error ? <p className="mt-2 text-xs text-red-600">{error}</p> : null}
    </div>
  );
}
