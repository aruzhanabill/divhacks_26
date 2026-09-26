export type LatLng = { lat: number; lng: number };

const EARTH_METERS = 6_371_000;

function toRad(degrees: number): number {
  return (degrees * Math.PI) / 180;
}

export function haversineMeters(a: LatLng, b: LatLng): number {
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_METERS * Math.asin(Math.min(1, Math.sqrt(h)));
}

function distanceToSegmentMeters(point: LatLng, start: LatLng, end: LatLng): number {
  const length = haversineMeters(start, end);
  if (length === 0) return haversineMeters(point, start);
  const dx = end.lng - start.lng;
  const dy = end.lat - start.lat;
  const t = Math.max(
    0,
    Math.min(1, ((point.lng - start.lng) * dx + (point.lat - start.lat) * dy) / (dx * dx + dy * dy)),
  );
  return haversineMeters(point, { lat: start.lat + t * dy, lng: start.lng + t * dx });
}

export function metersToPath(point: LatLng, path: LatLng[]): number {
  if (path.length === 0) return Infinity;
  if (path.length === 1) return haversineMeters(point, path[0]);
  let best = Infinity;
  for (let index = 0; index < path.length - 1; index += 1) {
    best = Math.min(best, distanceToSegmentMeters(point, path[index], path[index + 1]));
  }
  return best;
}

export function pointAlong(path: LatLng[], fraction: number): LatLng {
  if (path.length === 0) return { lat: 0, lng: 0 };
  if (path.length === 1 || fraction <= 0) return path[0];
  const lengths: number[] = [];
  let total = 0;
  for (let index = 0; index < path.length - 1; index += 1) {
    const segment = haversineMeters(path[index], path[index + 1]);
    lengths.push(segment);
    total += segment;
  }
  if (total === 0 || fraction >= 1) return path[path.length - 1];
  let remaining = total * fraction;
  for (let index = 0; index < lengths.length; index += 1) {
    if (remaining <= lengths[index]) {
      const t = lengths[index] === 0 ? 0 : remaining / lengths[index];
      return {
        lat: path[index].lat + (path[index + 1].lat - path[index].lat) * t,
        lng: path[index].lng + (path[index + 1].lng - path[index].lng) * t,
      };
    }
    remaining -= lengths[index];
  }
  return path[path.length - 1];
}

export function pathFrom(path: LatLng[], fraction: number): LatLng[] {
  if (path.length < 2) return path;
  const here = pointAlong(path, fraction);
  let covered = 0;
  let total = 0;
  const lengths: number[] = [];
  for (let index = 0; index < path.length - 1; index += 1) {
    const segment = haversineMeters(path[index], path[index + 1]);
    lengths.push(segment);
    total += segment;
  }
  const target = total * Math.min(1, Math.max(0, fraction));
  for (let index = 0; index < lengths.length; index += 1) {
    if (covered + lengths[index] >= target) {
      return [here, ...path.slice(index + 1)];
    }
    covered += lengths[index];
  }
  return [here];
}
