import { RotateCcw } from "lucide-react";
import type { GeometryMap, MeasurementRegistry, ReclassifyResult } from "../lib/api";
import { displayName, formatValueDE, groupNames, preferredNames } from "../lib/measurements";

interface Props {
  /** Curated presentation: German names, clinical grouping, decimal places. */
  registry: MeasurementRegistry;
  geometry: GeometryMap;
  measurements: Record<string, number | null>;
  measurementsPredicted: Record<string, number | null>;
  active: string | null;
  onSelect: (m: string | null) => void;
  /** Measurement the pointer is on in the REPORT — mirrored here as a chip highlight. */
  hovered: string | null;
  /** Fired when the pointer enters/leaves a chip, so the report can mirror it back. */
  onHover: (m: string | null) => void;
  edited: boolean;
  onReset: () => void;
  result: ReclassifyResult | null;
}

/** German clinical number format (decimal comma; '°' without, ' mm' with a space; ONE
 *  decimal — the house standard). For registry-aware per-measurement decimals use
 *  formatValueDE from lib/measurements. */
export function format(value: number | null | undefined, kind?: string, unit?: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const num = value.toFixed(1).replace(".", ",");
  const u = (unit ?? (kind === "angle" ? "°" : kind === "distance" ? "mm" : "")).trim();
  if (!u) return num;
  return u === "°" ? `${num}°` : `${num} ${u}`;
}

/** Measurement chips + the correction controls.
 *
 *  Layout note that is NOT cosmetic: this panel sits directly above the image, so anything
 *  that changes its height resizes the SVG underneath — which changes the letterbox scale
 *  and teleports the landmark out from under the cursor mid-drag. Hence the fixed-height
 *  rows and the non-wrapping, horizontally scrolling chip strip: the image box must keep
 *  exactly the same geometry whether or not the reader has edited anything.
 */
export function MeasurementPanel({
  registry,
  geometry,
  measurements,
  measurementsPredicted,
  active,
  onSelect,
  hovered,
  onHover,
  edited,
  onReset,
  result,
}: Props) {
  // Robust variants take over their base measurement's slot (name, value, geometry);
  // the plain twin is hidden — one concept, one chip.
  const names = preferredNames(Object.keys(geometry));
  if (names.length === 0) return null;

  return (
    <div className="flex shrink-0 flex-col gap-1.5">
      {/* Grouped by clinical concept (Patellahöhe, Tibiaplateau, …) instead of one long
          strip. Each group is one FIXED-HEIGHT row (see layout note: the group count is
          constant per case, so the canvas below never resizes mid-interaction).
          overflow-y-hidden per row: with only overflow-x-auto, the horizontal scrollbar
          eats into the fixed row height and made the row vertically scrollable. */}
      {groupNames(names, registry).map(({ group, names: members }) => (
        <div key={group} className="flex h-9 shrink-0 items-center gap-2">
          <span
            className="w-28 shrink-0 truncate text-[11px] font-medium text-slate-500"
            title={group}
          >
            {group}
          </span>
          <div className="chip-row flex h-full flex-1 items-center gap-1.5 overflow-x-auto overflow-y-hidden whitespace-nowrap">
            {members.map((name) => {
              const g = geometry[name];
              const before = measurementsPredicted[name];
              const now = measurements[name];
              // Compare with a tolerance: floating-point noise must never present itself
              // to a radiologist as "this value changed".
              const moved =
                edited &&
                typeof before === "number" &&
                typeof now === "number" &&
                Math.abs(before - now) > 1e-9;
              return (
                <button
                  key={name}
                  // Selecting, never toggling off: a second click on the active chip must
                  // not jump to "Alle" (that is the explicit chip's job).
                  onClick={() => onSelect(name)}
                  onMouseEnter={() => onHover(name)}
                  onMouseLeave={() => onHover(null)}
                  title={
                    moved
                      ? `Vorhersage: ${formatValueDE(name, before, registry, g.kind, g.unit)}`
                      : undefined
                  }
                  className={
                    "flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs " +
                    (active === name
                      ? "border-sky-300 bg-sky-50 text-sky-700"
                      : hovered === name
                        ? "border-amber-300 bg-amber-50 text-amber-900" // landmark-dot amber = the hover link
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-100")
                  }
                >
                  <span className="font-medium">{displayName(name, registry)}</span>
                  <span className={"tabular-nums " + (moved ? "text-amber-600" : "text-slate-500")}>
                    {formatValueDE(name, now, registry, g.kind, g.unit)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ))}

      {/* Always rendered, fixed height — see the layout note above. */}
      <div className="flex h-7 shrink-0 items-center gap-2">
        <button
          onClick={() => onSelect(null)}
          className={
            "shrink-0 rounded-lg border px-2.5 py-0.5 text-xs font-medium " +
            (active === null
              ? "border-sky-300 bg-sky-50 text-sky-700"
              : "border-slate-200 bg-white text-slate-600 hover:bg-slate-100")
          }
        >
          Alle Bemaßungen
        </button>
        {edited ? (
          <>
            {/* The badge tracks the state of the correction, so it is always obvious whether
                the edited landmarks have actually been fed back through the report yet:
                pending (amber) -> checked, unchanged (green) / checked, changed (red). */}
            {!result ? (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                Landmarken korrigiert — noch nicht geprüft
              </span>
            ) : result.outcome === "corrected_label_change" ? (
              /* Sky, not red: a change the reader initiated and the agent incorporated is
                 INFORMATION. Red implied something was wrong with a reviewed state. */
              <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-800">
                Korrektur geprüft — Beurteilung geändert ({result.changes.length})
              </span>
            ) : (
              <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium text-emerald-800">
                Korrektur geprüft — Beurteilung unverändert
              </span>
            )}
            {/* The badge carries the STATE; the "Neu prüfen" ACTION lives in the
                Befundentwurf header — one action, one home, next to what it updates. */}
            <button
              onClick={() => {
                // Destructive for the reader's work: discards every landmark correction.
                if (
                  window.confirm(
                    "Alle Landmarken-Korrekturen verwerfen und auf die KI-Vorhersage zurücksetzen?\n\nDer Befundentwurf muss danach neu geprüft werden."
                  )
                )
                  onReset();
              }}
              title="Verwirft Ihre Korrekturen und stellt die von der KI vorhergesagten Landmarken wieder her"
              className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Auf KI-Vorhersage zurücksetzen
            </button>
          </>
        ) : (
          <span className="text-[11px] text-slate-400">
            Landmarken ziehen zum Korrigieren · Mausrad = Zoom · Doppelklick = Ansicht
            zurücksetzen
          </span>
        )}
      </div>
    </div>
  );
}

// (The former ReclassifyBanner overlay was removed: the badge above the image carries the
// outcome, the disabled "Befund geprüft" chip carries the state, and the chat reply
// carries the per-measurement details — the banner said all of it a third time.)
