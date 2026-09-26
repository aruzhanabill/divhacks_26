"use client";

import { createContext, useContext, useMemo, useRef, useState, type ReactNode } from "react";

export type RevealedCall = {
  source_id: string;
  lat: number;
  lng: number;
  category: string;
  timestamp: string;
};

export type FollowSnapshot = {
  playing: boolean;
  progress: number;
  simMs: number | null;
  calls: RevealedCall[];
};

const EMPTY: FollowSnapshot = { playing: false, progress: 0, simMs: null, calls: [] };

export type FollowLive = {
  playing: boolean;
  progress: number;
  simMs: number | null;
};

const FollowContext = createContext<{
  snapshot: FollowSnapshot;
  setSnapshot: (snapshot: FollowSnapshot) => void;
  live: FollowLive;
} | null>(null);

export function FollowProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<FollowSnapshot>(EMPTY);
  const live = useRef<FollowLive>({ playing: false, progress: 0, simMs: null }).current;
  const value = useMemo(() => ({ snapshot, setSnapshot, live }), [live, snapshot]);
  return <FollowContext.Provider value={value}>{children}</FollowContext.Provider>;
}

export function useFollow(): FollowSnapshot {
  return useContext(FollowContext)?.snapshot ?? EMPTY;
}

export function usePublishFollow(): (snapshot: FollowSnapshot) => void {
  const context = useContext(FollowContext);
  return context?.setSnapshot ?? (() => undefined);
}

const FALLBACK_LIVE: FollowLive = { playing: false, progress: 0, simMs: null };

export function useFollowLive(): FollowLive {
  return useContext(FollowContext)?.live ?? FALLBACK_LIVE;
}
