import type { MeasurementRegistry } from "./api";

/** Frontend accessors for the curated measurement registry (served by the backend via
 *  /api/capabilities). The tool's canonical English names stay the internal keys; these
 *  helpers only affect PRESENTATION — display names, grouping, German number formats —
 *  mirroring tools/measurement_registry.py. */

const ROBUST = /\s*\(robust\)\s*$/i;
export const FALLBACK_GROUP = "Weitere Messwerte";

function baseOf(name: string): string {
  return name.replace(ROBUST, "");
}

export function displayName(name: string, reg: MeasurementRegistry): string {
  // The "(robust)" suffix is an implementation detail the reader never sees.
  const base = baseOf(name);
  return reg[base]?.de ?? base;
}

/** Collapse (X, X (robust)) pairs to ONE visible entry: the robust variant (its value
 *  and geometry), shown under the base name. Plain twins are hidden. */
export function preferredNames(names: string[]): string[] {
  const robustBases = new Set(
    names.filter((n) => ROBUST.test(n)).map((n) => baseOf(n))
  );
  return names.filter((n) => ROBUST.test(n) || !robustBases.has(baseOf(n)));
}

export function groupFor(name: string, reg: MeasurementRegistry): string {
  return reg[baseOf(name)]?.group ?? FALLBACK_GROUP;
}

/** German clinical number format: decimal comma, '°' without and ' mm' with a space,
 *  per-measurement decimals (registry) or kind defaults. E.g. 1,07 · 8,8° · 10,3 mm. */
export function formatValueDE(
  name: string,
  value: number | null | undefined,
  reg: MeasurementRegistry,
  kind?: string,
  unit?: string
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  // One decimal is the house standard (see tools/measurement_registry.py); the registry
  // may deviate per measurement.
  const dec = reg[baseOf(name)]?.decimals ?? 1;
  const num = value.toFixed(dec).replace(".", ",");
  const u = (unit ?? (kind === "angle" ? "°" : kind === "distance" ? "mm" : "")).trim();
  if (!u) return num;
  return u === "°" ? `${num}°` : `${num} ${u}`;
}

/** Group the given canonical names by clinical concept, preserving first-appearance
 *  order of the groups and the given order within each group. */
export function groupNames(
  names: string[],
  reg: MeasurementRegistry
): { group: string; names: string[] }[] {
  const out: { group: string; names: string[] }[] = [];
  for (const n of names) {
    const g = groupFor(n, reg);
    const bucket = out.find((b) => b.group === g);
    if (bucket) bucket.names.push(n);
    else out.push({ group: g, names: [n] });
  }
  return out;
}
