"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMap } from "@vis.gl/react-google-maps";
import {
  CATEGORIES,
  CATEGORY_COLOR,
  CATEGORY_LABEL,
  DEFAULT_CATEGORIES,
  addMinutes,
  formatNyc,
  type Category,
  type Incident,
} from "@/lib/incidents";

const WINDOWS = [
  { minutes: 15, label: "15 min" },
  { minutes: 30, label: "30 min" },
  { minutes: 60, label: "1 hour" },
  { minutes: 120, label: "2 hours" },
];

const PITCHES = [30, 45, 60, 90, 120];

type Seek = { id: number; progress: number };

type HeatLayer = {
  setData: (data: { location: google.maps.LatLng; weight: number }[]) => void;
  setMap: (map: google.maps.Map | null) => void;
};

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* ignore malformed error bodies */
  }
  return `Request failed (${response.status})`;
}

export function Playback() {
  const map = useMap();
  const [startLocal, setStartLocal] = useState("2026-06-30T22:00");
  const [windowMinutes, setWindowMinutes] = useState(60);
  const [pitchSeconds, setPitchSeconds] = useState(60);
  const [categories, setCategories] = useState<Category[]>(DEFAULT_CATEGORIES);
  const [heatmapOn, setHeatmapOn] = useState(false);
  const [latestLabel, setLatestLabel] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [batchId, setBatchId] = useState("");
  const [replayStart, setReplayStart] = useState<string | null>(null);
  const [replayEnd, setReplayEnd] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [simMs, setSimMs] = useState<number | null>(null);
  const [visibleCount, setVisibleCount] = useState(0);
  const [status, setStatus] = useState<"idle" | "loading" | "ready">("idle");
  const [error, setError] = useState<string | null>(null);
  const [seek, setSeek] = useState<Seek | null>(null);
  const startEdited = useRef(false);
  const lastClockPush = useRef(0);

  const markersRef = useRef<Map<string, google.maps.Marker>>(new Map());
  const infoRef = useRef<google.maps.InfoWindow | null>(null);
  const heatRef = useRef<HeatLayer | null>(null);
  const progressRef = useRef(0);
  const playingRef = useRef(playing);
  const pitchRef = useRef(pitchSeconds);
  const incidentsRef = useRef(incidents);
  const categoriesRef = useRef(categories);
  const startRef = useRef<number | null>(null);
  const endRef = useRef<number | null>(null);
  const heatmapOnRef = useRef(heatmapOn);
  const resetRef = useRef(false);
  const seenSeek = useRef<number | null>(null);
  const seenBatch = useRef(batchId);

  playingRef.current = playing;
  pitchRef.current = pitchSeconds;
  incidentsRef.current = incidents;
  categoriesRef.current = categories;
  heatmapOnRef.current = heatmapOn;
  startRef.current = replayStart ? Date.parse(replayStart) : null;
  endRef.current = replayEnd ? Date.parse(replayEnd) : null;
  if (seenBatch.current !== batchId) {
    seenBatch.current = batchId;
    progressRef.current = 0;
    resetRef.current = true;
  }
  if (seek && seek.id !== seenSeek.current) {
    seenSeek.current = seek.id;
    progressRef.current = seek.progress;
  }

  useEffect(() => {
    let cancelled = false;
    fetch("/api/dataset")
      .then(async (response) => {
        if (!response.ok) throw new Error(await readError(response));
        return response.json();
      })
      .then((data: { latest_add_ts?: string; suggested_start?: string }) => {
        if (cancelled) return;
        if (data.latest_add_ts) setLatestLabel(formatNyc(Date.parse(data.latest_add_ts)));
        if (data.suggested_start && !startEdited.current) setStartLocal(data.suggested_start);
      })
      .catch(() => {
        if (!cancelled) setLatestLabel(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!map) return;
    infoRef.current = new google.maps.InfoWindow();
    let cancelled = false;
    google.maps
      .importLibrary("visualization")
      .then((lib) => {
        if (cancelled) return;
        const Ctor = (
          lib as unknown as {
            HeatmapLayer: new (opts?: object) => HeatLayer;
          }
        ).HeatmapLayer;
        heatRef.current = new Ctor({
          data: [],
          radius: 28,
          opacity: 0.72,
          gradient: ["rgba(0,0,0,0)", "#fde68a", "#fb923c", "#dc2626"],
        });
      })
      .catch(() => {
        heatRef.current = null;
      });

    const markers = markersRef.current;
    let frame = 0;
    let last = performance.now();
    let lastEmit = 0;
    let heatSignature = "";

    const draw = (simMsValue: number) => {
      if (resetRef.current) {
        resetRef.current = false;
        for (const marker of markers.values()) marker.setMap(null);
        markers.clear();
        heatSignature = "";
        const batch = incidentsRef.current;
        if (batch.length > 0) {
          const bounds = new google.maps.LatLngBounds();
          for (const incident of batch) bounds.extend({ lat: incident.lat, lng: incident.lng });
          map.fitBounds(bounds, { top: 48, right: 48, bottom: 48, left: 380 });
        }
      }

      const allowed = new Set(categoriesRef.current);
      const visible = incidentsRef.current.filter(
        (incident) => allowed.has(incident.category) && Date.parse(incident.timestamp) <= simMsValue,
      );
      const visibleIds = new Set(visible.map((incident) => incident.source_id));
      for (const [id, marker] of markers) {
        if (!visibleIds.has(id)) {
          marker.setMap(null);
          markers.delete(id);
        }
      }

      const freshCount = visible.filter((incident) => !markers.has(incident.source_id)).length;
      const animate = freshCount > 0 && freshCount <= 12;
      for (const incident of visible) {
        if (markers.has(incident.source_id)) continue;
        const marker = new google.maps.Marker({
          map,
          position: { lat: incident.lat, lng: incident.lng },
          animation: animate ? google.maps.Animation.DROP : undefined,
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 5 + Math.min(incident.severity, 5) * 0.6,
            fillColor: CATEGORY_COLOR[incident.category],
            fillOpacity: 0.94,
            strokeColor: "#ffffff",
            strokeWeight: 1,
          },
          title: CATEGORY_LABEL[incident.category],
          optimized: false,
        });
        marker.addListener("click", () => {
          const when = new Intl.DateTimeFormat("en-US", {
            timeZone: "America/New_York",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
            second: "2-digit",
          }).format(new Date(incident.timestamp));
          infoRef.current?.setContent(
            `<div class="callout"><strong>${CATEGORY_LABEL[incident.category]}</strong><span>${when}</span></div>`,
          );
          infoRef.current?.open({ map, anchor: marker });
        });
        markers.set(incident.source_id, marker);
      }

      const signature = `${heatmapOnRef.current}:${categoriesRef.current.join(",")}:${visible.length}:${visible.at(-1)?.source_id ?? ""}`;
      const heat = heatRef.current;
      if (heat && signature !== heatSignature) {
        heatSignature = signature;
        heat.setData(
          visible.map((incident) => ({
            location: new google.maps.LatLng(incident.lat, incident.lng),
            weight: Math.max(incident.severity, 0.2),
          })),
        );
        heat.setMap(heatmapOnRef.current ? map : null);
      }
      return visible.length;
    };

    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      const start = startRef.current;
      const end = endRef.current;
      if (start != null && end != null && end > start && playingRef.current && progressRef.current < 1) {
        progressRef.current = Math.min(1, progressRef.current + dt / (pitchRef.current * 1000));
      }
      const progressValue = progressRef.current;
      const simValue = start != null && end != null ? start + progressValue * (end - start) : (start ?? 0);
      const visible = draw(simValue);
      if (start != null && now - lastEmit > 100) {
        lastEmit = now;
        setProgress(progressValue);
        setSimMs(simValue);
        setVisibleCount(visible);
        if (progressValue >= 1) setPlaying(false);
        const wall = performance.now();
        if (replayStart && replayEnd && wall - lastClockPush.current > 1000) {
          lastClockPush.current = wall;
          void fetch("/api/sim", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              sim_now: new Date(simValue).toISOString(),
              replay_start: replayStart,
              replay_end: replayEnd,
            }),
          });
        }
      }
      frame = requestAnimationFrame(tick);
    };

    frame = requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      for (const marker of markers.values()) marker.setMap(null);
      markers.clear();
      heatRef.current?.setMap(null);
      heatRef.current = null;
      infoRef.current?.close();
    };
  }, [map, replayEnd, replayStart]);

  const filteredTotal = useMemo(() => {
    const allowed = new Set(categories);
    return incidents.filter((incident) => allowed.has(incident.category)).length;
  }, [incidents, categories]);

  const speed = Math.round((windowMinutes * 60) / pitchSeconds);
  const clockText = simMs != null ? formatNyc(simMs) : "Load a window to start";

  const loadCalls = useCallback(async () => {
    setError(null);
    setPlaying(false);
    setStatus("loading");
    const endLocal = addMinutes(startLocal, windowMinutes);
    try {
      const ingestResponse = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start: startLocal, end: endLocal }),
      });
      if (!ingestResponse.ok) throw new Error(await readError(ingestResponse));
      const ingested = (await ingestResponse.json()) as { upserted: number; start: string; end: string };
      const params = new URLSearchParams({ start: ingested.start, end: ingested.end });
      const incidentsResponse = await fetch(`/api/incidents?${params.toString()}`);
      if (!incidentsResponse.ok) throw new Error(await readError(incidentsResponse));
      const payload = (await incidentsResponse.json()) as {
        incidents: Incident[];
        start: string;
        end: string;
      };
      setIncidents(payload.incidents);
      setReplayStart(payload.start);
      setReplayEnd(payload.end);
      setBatchId(`${payload.start}:${payload.incidents.length}:${Date.now()}`);
      setProgress(0);
      setVisibleCount(0);
      setSimMs(Date.parse(payload.start));
      setSeek({ id: Date.now(), progress: 0 });
      setStatus("ready");
      if (ingested.upserted === 0) {
        setError("No geocoded calls in that window. The public file currently ends in June 2026.");
      }
    } catch (err) {
      setStatus(incidents.length > 0 ? "ready" : "idle");
      setError(err instanceof Error ? err.message : "Could not load calls");
    }
  }, [incidents.length, startLocal, windowMinutes]);

  function toggleCategory(category: Category) {
    setCategories((current) =>
      current.includes(category) ? current.filter((item) => item !== category) : [...current, category],
    );
  }

  function scrub(next: number) {
    setPlaying(false);
    setProgress(next);
    setSeek({ id: Date.now(), progress: next });
  }

  return (
    <div className="rounded-xl bg-white/95 p-3 shadow-lg ring-1 ring-zinc-200 backdrop-blur">
        <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">Calls for Service</p>
        <p className="mt-1 text-sm font-medium text-zinc-900">{clockText}</p>
        <p className="mt-1 text-xs leading-relaxed text-zinc-500">
          Historical 911 calls appear when the simulation reaches the moment each call was logged.
        </p>

        <label className="mt-3 block text-xs font-medium text-zinc-500">
          Starting time
          <input
            type="datetime-local"
            value={startLocal}
            onChange={(event) => {
              startEdited.current = true;
              setStartLocal(event.target.value);
            }}
            className="mt-1 w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900"
          />
        </label>
        <p className="mt-1 text-xs text-zinc-500">
          {latestLabel ? `Latest call in the dataset: ${latestLabel}` : "Times are New York local."}
        </p>

        <p className="mt-3 text-xs font-medium text-zinc-500">History in this replay</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {WINDOWS.map((option) => (
            <button
              key={option.minutes}
              type="button"
              aria-pressed={windowMinutes === option.minutes}
              onClick={() => setWindowMinutes(option.minutes)}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                windowMinutes === option.minutes
                  ? "border-zinc-900 bg-zinc-900 text-white"
                  : "border-zinc-200 bg-white text-zinc-800"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>

        <p className="mt-3 text-xs font-medium text-zinc-500">Playback length</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {PITCHES.map((seconds) => (
            <button
              key={seconds}
              type="button"
              aria-pressed={pitchSeconds === seconds}
              onClick={() => setPitchSeconds(seconds)}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                pitchSeconds === seconds
                  ? "border-zinc-900 bg-zinc-900 text-white"
                  : "border-zinc-200 bg-white text-zinc-800"
              }`}
            >
              {seconds < 120 ? `${seconds}s` : "2 min"}
            </button>
          ))}
        </div>
        <p className="mt-1 text-xs text-zinc-500">
          {windowMinutes} min of calls appear over {pitchSeconds}s ({speed}×).
        </p>

        <p className="mt-3 text-xs font-medium text-zinc-500">Show on the map</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {CATEGORIES.map((category) => (
            <button
              key={category}
              type="button"
              aria-pressed={categories.includes(category)}
              onClick={() => toggleCategory(category)}
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs ${
                categories.includes(category)
                  ? "border-zinc-900 bg-zinc-900 text-white"
                  : "border-zinc-200 bg-white text-zinc-700"
              }`}
            >
              <i className="inline-block h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[category] }} />
              {CATEGORY_LABEL[category]}
            </button>
          ))}
        </div>

        <label className="mt-3 flex items-center gap-2 text-xs font-medium text-zinc-700">
          <input type="checkbox" checked={heatmapOn} onChange={(event) => setHeatmapOn(event.target.checked)} />
          Heatmap overlay
        </label>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => void loadCalls()}
            disabled={status === "loading"}
            className="rounded-lg bg-orange-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {status === "loading" ? "Loading…" : "Load calls"}
          </button>
          <button
            type="button"
            disabled={status !== "ready" || incidents.length === 0}
            onClick={() => {
              if (!playing && progress >= 1) {
                setProgress(0);
                setSeek({ id: Date.now(), progress: 0 });
                setPlaying(true);
                return;
              }
              setPlaying((value) => !value);
            }}
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-900 disabled:opacity-50"
          >
            {playing ? "Pause" : progress >= 1 ? "Replay" : "Play"}
          </button>
          <button
            type="button"
            disabled={status !== "ready"}
            onClick={() => scrub(0)}
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 disabled:opacity-50"
          >
            Reset
          </button>
        </div>

        <div className="mt-3 flex items-baseline justify-between">
          <strong className="text-2xl font-semibold tabular-nums text-zinc-900">{visibleCount}</strong>
          <span className="text-xs text-zinc-500">
            {status === "ready" ? `of ${filteredTotal} shown · ${incidents.length} stored` : "nothing loaded"}
          </span>
        </div>
        <input
          type="range"
          min={0}
          max={1000}
          value={Math.round(progress * 1000)}
          disabled={status !== "ready"}
          onChange={(event) => scrub(Number(event.target.value) / 1000)}
          className="mt-1 w-full accent-orange-700"
        />
      {error ? <p className="mt-2 text-xs text-red-600">{error}</p> : null}
    </div>
  );
}
