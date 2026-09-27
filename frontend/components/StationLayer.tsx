"use client";

import { useEffect, useRef, useState } from "react";
import { useMap } from "@vis.gl/react-google-maps";
import {
  BAND_LABEL,
  FLAG_LABEL,
  LIVE_FLAGS,
  LIVE_REFRESH_MS,
  scoreBand,
  stationClockKey,
  type StationDetail,
  type StationScore,
  type StationsResponse,
} from "@/lib/stations";

type StationLayerProps = {
  enabled: boolean;
  simMs: number | null;
  includeLive: boolean;
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&#39;";
    }
  });
}

function formatTime(iso: string | null): string {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(iso));
}

function cardHtml(station: StationScore, detail: StationDetail | null): string {
  const band = scoreBand(station.safety_score);
  const routes = station.routes.map((route) => `<span class="bullet">${escapeHtml(route)}</span>`).join("");
  const complaints = station.breakdown.complaints;
  const calls = station.breakdown.calls;
  const ridership = station.breakdown.ridership;
  const live = station.breakdown.live;
  const realtime = station.breakdown.realtime ?? null;
  const flags = station.flags
    .map((flag) => {
      const bad = flag === "violent_history" || flag === "active_calls" || flag === "no_service";
      const liveFlag = LIVE_FLAGS.has(flag);
      return `<span class="flag ${bad ? "flag-bad" : liveFlag ? "flag-live" : ""}">${escapeHtml(FLAG_LABEL[flag] ?? flag)}</span>`;
    })
    .join("");
  const byCategory = detail
    ? Object.entries(detail.complaints_by_category)
        .slice(0, 4)
        .map(([category, count]) => `${escapeHtml(category)} ${count}`)
        .join(" · ")
    : "";
  const bandLabel = BAND_LABEL[complaints.band ?? detail?.band ?? ""] ?? "this time of day";
  const coverage = detail?.lookback_days ? `, ${detail.lookback_days} days of data` : "";
  const perMonth = complaints.per_30d != null ? ` · ${complaints.per_30d}/month` : "";
  const trainsHtml = realtime
    ? `<div class="live"><strong>Trains now</strong>${Object.entries(realtime.routes)
        .map(([route, info]) => {
          const wait = info.no_service
            ? "not stopping here"
            : info.wait_min == null
              ? "—"
              : `next in ${Math.round(info.wait_min)} min${info.headway_min ? `, every ~${Math.round(info.headway_min)}` : ""}`;
          const skip = info.skipping > 0 ? ` · ${info.skipping} skip` : "";
          return `<p><span class="bullet">${escapeHtml(route)}</span> ${escapeHtml(wait)}${skip}</p>`;
        })
        .join("")}</div>`
    : "";
  const liveHtml = live
    ? `<div class="live"><strong>Alerts${live.observed_at ? ` · ${formatTime(live.observed_at)}` : ""}</strong>${live.items
        .slice(0, 3)
        .map((item) => `<p>${escapeHtml(item.summary)}</p>`)
        .join("")}</div>`
    : "";
  return `
    <div class="station-card">
      <h3>${escapeHtml(station.name)}</h3>
      <div class="routes">${routes}${station.ada ? '<span class="flag">ADA</span>' : ""}</div>
      <div class="row"><span>Safety score</span><strong class="score score-${band}">${station.safety_score}</strong></div>
      <div class="row"><span>Complaints, ${escapeHtml(bandLabel)}${coverage}</span><strong>${complaints.count}${perMonth}${complaints.violent ? ` · ${complaints.violent} violent` : ""}</strong></div>
      ${byCategory ? `<div class="row"><span>${byCategory}</span></div>` : ""}
      <div class="row"><span>Calls in this replay</span><strong>${calls.count}</strong></div>
      ${
        ridership
          ? `<div class="row"><span>Riders this hour</span><strong>${ridership.hourly ?? "—"}${ridership.typical ? ` / typical ${ridership.typical}` : ""}${ridership.quiet ? " · quiet" : ""}</strong></div>`
          : ""
      }
      <div>${flags}</div>
      ${trainsHtml}
      ${liveHtml}
    </div>`;
}

/**
 * Toggleable station overlay. Colour is the station safety score at the sim clock.
 * This is context for the rider; route choice still comes from the route scoring endpoints.
 */
export function StationLayer({ enabled, simMs, includeLive }: StationLayerProps) {
  const map = useMap();
  const [stations, setStations] = useState<StationScore[]>([]);
  const markersRef = useRef<Map<string, google.maps.marker.AdvancedMarkerElement>>(new Map());
  const infoRef = useRef<google.maps.InfoWindow | null>(null);
  const clockKey = stationClockKey(simMs);
  // Live feeds change by the minute; tick to refetch while live is on.
  const [liveTick, setLiveTick] = useState(0);
  useEffect(() => {
    if (!enabled || !includeLive) return;
    const timer = window.setInterval(() => setLiveTick((value) => value + 1), LIVE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [enabled, includeLive]);

  useEffect(() => {
    if (!enabled) {
      setStations([]);
      return;
    }
    let cancelled = false;
    const params = new URLSearchParams();
    if (simMs != null) params.set("sim_now", new Date(simMs).toISOString());
    if (includeLive) params.set("include_live", "true");
    fetch(`/api/stations?${params.toString()}`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`stations ${response.status}`);
        return (await response.json()) as StationsResponse;
      })
      .then((payload) => {
        if (!cancelled) setStations(payload.stations);
      })
      .catch(() => {
        if (!cancelled) setStations([]);
      });
    return () => {
      cancelled = true;
    };
    // clockKey buckets simMs so playback does not refetch every frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, includeLive, clockKey, liveTick]);

  useEffect(() => {
    if (!map) return;
    const markers = markersRef.current;
    if (!infoRef.current) infoRef.current = new google.maps.InfoWindow();
    let cancelled = false;

    if (stations.length === 0) {
      for (const marker of markers.values()) marker.map = null;
      markers.clear();
      return;
    }

    void google.maps.importLibrary("marker").then((lib) => {
      if (cancelled) return;
      const Pin = (lib as google.maps.MarkerLibrary).AdvancedMarkerElement;
      const seen = new Set<string>();
      for (const station of stations) {
        seen.add(station.complex_id);
        const band = scoreBand(station.safety_score);
        const live = station.flags.some((flag) => LIVE_FLAGS.has(flag));
        const title = `${station.name} · ${station.safety_score}`;
        const existing = markers.get(station.complex_id);
        if (existing) {
          const node = existing.content as HTMLElement | null;
          if (node) node.className = `station-pin station-pin-${band}${live ? " station-pin-live" : ""}`;
          existing.title = title;
          continue;
        }
        const node = document.createElement("div");
        node.className = `station-pin station-pin-${band}${live ? " station-pin-live" : ""}`;
        const marker = new Pin({
          map,
          position: { lat: station.lat, lng: station.lng },
          content: node,
          title,
          zIndex: 3,
        });
        marker.addListener("click", () => {
          const current = stations.find((item) => item.complex_id === station.complex_id) ?? station;
          infoRef.current?.setContent(cardHtml(current, null));
          infoRef.current?.open({ map, anchor: marker });
          const params = new URLSearchParams();
          if (simMs != null) params.set("sim_now", new Date(simMs).toISOString());
          fetch(`/api/stations/${encodeURIComponent(station.complex_id)}?${params.toString()}`)
            .then(async (response) => (response.ok ? ((await response.json()) as StationDetail) : null))
            .then((detail) => {
              if (detail) infoRef.current?.setContent(cardHtml(detail, detail));
            })
            .catch(() => undefined);
        });
        markers.set(station.complex_id, marker);
      }
      for (const [id, marker] of markers) {
        if (!seen.has(id)) {
          marker.map = null;
          markers.delete(id);
        }
      }
    });

    return () => {
      cancelled = true;
    };
  }, [map, simMs, stations]);

  useEffect(() => {
    const markers = markersRef.current;
    return () => {
      for (const marker of markers.values()) marker.map = null;
      markers.clear();
      infoRef.current?.close();
    };
  }, []);

  return null;
}
