"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMap } from "@vis.gl/react-google-maps";
import { useFollowLive, usePublishFollow, type RevealedCall } from "@/lib/follow";
import { CrimeGlyph, crimePinElement } from "@/lib/crimePin";
import { useTheme } from "@/lib/theme";
import {
  CATEGORIES,
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
  const { theme, toggleTheme } = useTheme();
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
  const [open, setOpen] = useState(false);
  const [seek, setSeek] = useState<Seek | null>(null);
  const startEdited = useRef(false);
  const lastClockPush = useRef(0);
  const publishFollow = usePublishFollow();
  const publishFollowRef = useRef(publishFollow);
  const live = useFollowLive();
  const liveRef = useRef(live);
  const lastPublishedKey = useRef("");
  publishFollowRef.current = publishFollow;
  liveRef.current = live;

  const markersRef = useRef<Map<string, google.maps.marker.AdvancedMarkerElement>>(new Map());
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

  const showStored = useCallback((payload: { incidents: Incident[]; start: string; end: string }) => {
    setIncidents(payload.incidents);
    setReplayStart(payload.start);
    setReplayEnd(payload.end);
    setBatchId(`${payload.start}:${payload.incidents.length}:${Date.now()}`);
    setProgress(0);
    setVisibleCount(0);
    setSimMs(Date.parse(payload.start));
    setSeek({ id: Date.now(), progress: 0 });
    setStatus(payload.incidents.length > 0 ? "ready" : "idle");
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/dataset")
      .then(async (response) => {
        if (!response.ok) throw new Error(await readError(response));
        return response.json();
      })
      .then(async (data: { latest_add_ts?: string; suggested_start?: string; source?: string }) => {
        if (cancelled) return;
        if (data.latest_add_ts) {
          const stored = data.source === "Tiger Cloud" ? "stored in Tiger Cloud" : "in the dataset";
          setLatestLabel(`${stored}: ${formatNyc(Date.parse(data.latest_add_ts))}`);
        }
        if (data.suggested_start && !startEdited.current) setStartLocal(data.suggested_start);
        if (data.source !== "Tiger Cloud" || !data.suggested_start) return;
        const endLocal = addMinutes(data.suggested_start, windowMinutes);
        const params = new URLSearchParams({ start: data.suggested_start, end: endLocal });
        const incidentsResponse = await fetch(`/api/incidents?${params.toString()}`);
        if (!incidentsResponse.ok || cancelled) return;
        const payload = (await incidentsResponse.json()) as {
          incidents: Incident[];
          start: string;
          end: string;
        };
        if (!cancelled) showStored(payload);
      })
      .catch(() => {
        if (!cancelled) setLatestLabel(null);
      });
    return () => {
      cancelled = true;
    };
    // Load the stored Tiger window once. Later window changes wait for Load calls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showStored]);

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
    let Pin: typeof google.maps.marker.AdvancedMarkerElement | null = null;
    void google.maps.importLibrary("marker").then((lib) => {
      if (cancelled) return;
      Pin = (lib as google.maps.MarkerLibrary).AdvancedMarkerElement;
    });

    const draw = (simMsValue: number) => {
      if (resetRef.current) {
        resetRef.current = false;
        for (const marker of markers.values()) marker.map = null;
        markers.clear();
        heatSignature = "";
        const batch = incidentsRef.current;
        if (batch.length > 0) {
          const bounds = new google.maps.LatLngBounds();
          for (const incident of batch) bounds.extend({ lat: incident.lat, lng: incident.lng });
          map.fitBounds(bounds, { top: 72, right: 48, bottom: 160, left: 48 });
        }
      }

      const allowed = new Set(categoriesRef.current);
      const visible = incidentsRef.current.filter(
        (incident) => allowed.has(incident.category) && Date.parse(incident.timestamp) <= simMsValue,
      );
      const visibleIds = new Set(visible.map((incident) => incident.source_id));
      for (const [id, marker] of markers) {
        if (!visibleIds.has(id)) {
          marker.map = null;
          markers.delete(id);
        }
      }

      for (const incident of visible) {
        if (markers.has(incident.source_id) || !Pin) continue;
        const marker = new Pin({
          map,
          position: { lat: incident.lat, lng: incident.lng },
          content: crimePinElement(incident.category),
          title: CATEGORY_LABEL[incident.category],
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
      liveRef.current.playing = playingRef.current;
      liveRef.current.progress = progressRef.current;
      liveRef.current.simMs = start != null ? simValue : null;
      const crimeCalls: RevealedCall[] = incidentsRef.current
        .filter(
          (incident) =>
            Date.parse(incident.timestamp) <= simValue &&
            (incident.category === "violent" ||
              incident.category === "property" ||
              incident.category === "disorder" ||
              incident.category === "alarm"),
        )
        .map((incident) => ({
          source_id: incident.source_id,
          lat: incident.lat,
          lng: incident.lng,
          category: incident.category,
          timestamp: incident.timestamp,
        }));
      const publishedKey = `${playingRef.current}:${crimeCalls.map((call) => call.source_id).join(",")}`;
      if (publishedKey !== lastPublishedKey.current) {
        lastPublishedKey.current = publishedKey;
        publishFollowRef.current({
          playing: playingRef.current,
          progress: progressRef.current,
          simMs: simValue,
          calls: crimeCalls,
        });
      }
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
      for (const marker of markers.values()) marker.map = null;
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
      showStored(payload);
      if (ingested.upserted === 0) {
        setError("No geocoded calls in that window. The public file currently ends in June 2026.");
      }
    } catch (err) {
      setStatus(incidents.length > 0 ? "ready" : "idle");
      setError(err instanceof Error ? err.message : "Could not load calls");
    }
  }, [incidents.length, showStored, startLocal, windowMinutes]);

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
    <div className="hud flex max-h-[calc(100dvh-11rem)] w-[min(100vw-6.5rem,22rem)] flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="flex min-w-0 flex-1 items-center justify-between gap-3 text-left"
        >
          <span>
            <span className="hud-label block">Calls for Service</span>
            <span className="mt-1 block text-sm font-medium">{clockText}</span>
          </span>
          <svg viewBox="0 0 20 20" className={`h-4 w-4 shrink-0 ${open ? "rotate-180" : ""}`} aria-hidden="true">
            <path fill="currentColor" d="M5.2 7.6 10 12.4l4.8-4.8 1.2 1.2L10 14.8 4 8.8z" />
          </svg>
        </button>
        <button
          type="button"
          onClick={toggleTheme}
          className="chip shrink-0 px-2 py-1 text-[10px] font-semibold uppercase"
        >
          {theme === "dark" ? "Light" : "Dark"}
        </button>
      </div>
      {open ? (
        <div className="min-h-0 flex-1 overflow-auto px-3 pb-3">

        <label className="hud-label mt-3 block">
          Starting time
          <input
            type="datetime-local"
            value={startLocal}
            onChange={(event) => {
              startEdited.current = true;
              setStartLocal(event.target.value);
            }}
            className="hud-field"
          />
        </label>
        <p className="mt-1 text-xs text-[var(--muted)]">
          {latestLabel ? `Latest call ${latestLabel}` : "Times are New York local."}
        </p>

        <p className="hud-label mt-3">History in this replay</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {WINDOWS.map((option) => (
            <button
              key={option.minutes}
              type="button"
              aria-pressed={windowMinutes === option.minutes}
              onClick={() => setWindowMinutes(option.minutes)}
              className="chip px-2.5 py-1 text-xs"
            >
              {option.label}
            </button>
          ))}
        </div>

        <p className="hud-label mt-3">Playback length</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {PITCHES.map((seconds) => (
            <button
              key={seconds}
              type="button"
              aria-pressed={pitchSeconds === seconds}
              onClick={() => setPitchSeconds(seconds)}
              className="chip px-2.5 py-1 text-xs"
            >
              {seconds < 120 ? `${seconds}s` : "2 min"}
            </button>
          ))}
        </div>

        <p className="hud-label mt-3">Show on the map</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {CATEGORIES.map((category) => (
            <button
              key={category}
              type="button"
              aria-pressed={categories.includes(category)}
              onClick={() => toggleCategory(category)}
              className="chip inline-flex items-center gap-1 px-2 py-1 text-xs"
            >
              <CrimeGlyph category={category} />
              {CATEGORY_LABEL[category]}
            </button>
          ))}
        </div>

        <label className="mt-3 flex items-center gap-2 text-xs font-medium">
          <input type="checkbox" checked={heatmapOn} onChange={(event) => setHeatmapOn(event.target.checked)} />
          Heatmap overlay
        </label>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => void loadCalls()}
            disabled={status === "loading"}
            className="chip hud-go px-3 py-2 text-sm font-medium disabled:opacity-50"
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
            className="chip px-3 py-2 text-sm font-medium disabled:opacity-50"
          >
            {playing ? "Pause" : progress >= 1 ? "Replay" : "Play"}
          </button>
          <button
            type="button"
            disabled={status !== "ready"}
            onClick={() => scrub(0)}
            className="chip px-3 py-2 text-sm disabled:opacity-50"
          >
            Reset
          </button>
        </div>

        <div className="mt-3 flex items-baseline justify-between">
          <strong className="text-2xl font-semibold tabular-nums">{visibleCount}</strong>
          <span className="text-xs text-[var(--muted)]">
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
          className="mt-1 w-full accent-[var(--accent)]"
        />
        {error ? <p className="mt-2 text-xs text-red-600">{error}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
