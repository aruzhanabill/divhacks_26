export const CATEGORIES = [
  "violent",
  "property",
  "disorder",
  "alarm",
  "traffic",
  "medical",
  "admin",
  "other",
] as const;

export type Category = (typeof CATEGORIES)[number];

export type Incident = {
  source_id: string;
  lat: number;
  lng: number;
  category: Category;
  severity: number;
  timestamp: string;
};

export const CATEGORY_LABEL: Record<Category, string> = {
  violent: "Violent",
  property: "Property",
  disorder: "Disorder",
  alarm: "Alarm",
  traffic: "Traffic",
  medical: "Medical",
  admin: "Admin",
  other: "Other",
};

export const CATEGORY_COLOR: Record<Category, string> = {
  violent: "#e11d48",
  property: "#ea580c",
  disorder: "#7c3aed",
  alarm: "#0284c7",
  traffic: "#64748b",
  medical: "#059669",
  admin: "#a8a29e",
  other: "#78716c",
};

export const DEFAULT_CATEGORIES: Category[] = ["violent", "property", "disorder"];

export function addMinutes(local: string, minutes: number): string {
  const [date, time] = local.split("T");
  const [year, month, day] = date.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  const next = new Date(Date.UTC(year, month - 1, day, hour, minute) + minutes * 60 * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}T${pad(next.getUTCHours())}:${pad(next.getUTCMinutes())}`;
}

export function formatNyc(ms: number): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(ms));
}
