"use client";

import { useEffect, useRef, useState } from "react";
import { useMapsLibrary } from "@vis.gl/react-google-maps";
import { NYC_BOUNDS, placeFromAutocomplete, type SelectedPlace } from "@/lib/googleMaps";

type PlaceAutocompleteInputProps = {
  id: string;
  placeholder: string;
  onPlaceSelect: (place: SelectedPlace | null) => void;
  onError?: (message: string) => void;
  className?: string;
  value?: string;
};

export function PlaceAutocompleteInput({
  id,
  placeholder,
  onPlaceSelect,
  onError,
  className = "w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 outline-none placeholder:text-zinc-400 focus:border-zinc-400",
  value,
}: PlaceAutocompleteInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const places = useMapsLibrary("places");

  useEffect(() => {
    if (!inputRef.current || value == null || document.activeElement === inputRef.current) return;
    inputRef.current.value = value;
  }, [value]);

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
      className={className}
    />
  );
}

type PlaceSearchProps = {
  onPlaceSelect: (place: SelectedPlace | null) => void;
  error?: string;
};

export function PlaceSearch({ onPlaceSelect, error }: PlaceSearchProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="m-3">
      {open ? (
        <div className="flex w-[min(100vw-5rem,22rem)] items-center gap-2 rounded-full bg-white/95 py-1 pl-3 pr-1 shadow-lg ring-1 ring-zinc-200 backdrop-blur">
          <SearchIcon />
          <div className="min-w-0 flex-1">
            <PlaceAutocompleteInput
              id="place-search"
              placeholder="Enter an address"
              onPlaceSelect={onPlaceSelect}
              className="w-full border-0 bg-transparent px-0 py-2 text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
            />
          </div>
          <button
            type="button"
            aria-label="Close search"
            onClick={() => setOpen(false)}
            className="grid h-9 w-9 place-items-center rounded-full text-zinc-500 hover:bg-zinc-100"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      ) : (
        <button
          type="button"
          aria-label="Search an address"
          onClick={() => setOpen(true)}
          className="grid h-11 w-11 place-items-center rounded-full bg-white text-zinc-800 shadow-lg ring-1 ring-zinc-200"
        >
          <SearchIcon />
        </button>
      )}
      {error ? <p className="mt-2 max-w-xs rounded-lg bg-white px-2 py-1 text-xs text-red-600 shadow">{error}</p> : null}
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
