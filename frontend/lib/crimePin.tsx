import type { Category } from "@/lib/incidents";

const GLYPH: Record<Category, string> = {
  violent:
    "M8 1.2 15 14.2H1L8 1.2zm0 4.2.7 3.4h-1.4L8 5.4zm0 5.2a.8.8 0 1 1 0 1.6.8.8 0 0 1 0-1.6z",
  property: "M2.5 7.2 8 2.2l5.5 5v6.8h-3.4V9.4H5.9v4.6H2.5V7.2z",
  disorder: "M1.5 9.2h2.2L5.4 4.6l2.3 7.2 2-4.2h4.8v1.6H10.6L8.2 13 5.8 5.8 4.6 9.2H1.5V9.2z",
  alarm: "M8 1.6a4.2 4.2 0 0 1 4.2 4.2v2.6l1.2 2.2H2.6L3.8 8.4V5.8A4.2 4.2 0 0 1 8 1.6zM6.2 12.6h3.6v1.2H6.2z",
  traffic: "M2 7.2h2.1l1-1.6h5.8l1 1.6H14v3.2h-1.1v1.4H11V10.4H5v1.4H3.1V10.4H2V7.2z",
  medical: "M6.6 1.8h2.8v4h4v2.8h-4v4H6.6v-4h-4V5.8h4v-4z",
  admin: "M4 1.8h5.2L12 4.6v9.6H4V1.8zm5 .4v2.6h2.4M6 7.2h4M6 9.6h4",
  other: "M8 2.2a5.8 5.8 0 1 1 0 11.6A5.8 5.8 0 0 1 8 2.2zm0 2.2a.9.9 0 1 0 0 1.8.9.9 0 0 0 0-1.8zM7.2 8h1.6v3.6H7.2z",
};

export function crimeSvg(category: Category): string {
  return `<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="${GLYPH[category]}"/></svg>`;
}

export function crimePinElement(category: Category): HTMLDivElement {
  const pin = document.createElement("div");
  pin.className = "crime-pin";
  pin.innerHTML = crimeSvg(category);
  return pin;
}

export function lightPinElement(): HTMLDivElement {
  const pin = document.createElement("div");
  pin.className = "crime-pin light-pin";
  pin.innerHTML = crimeSvg("other");
  return pin;
}

export function CrimeGlyph({ category }: { category: Category }) {
  return <span className="crime-pin crime-pin-inline" dangerouslySetInnerHTML={{ __html: crimeSvg(category) }} />;
}
