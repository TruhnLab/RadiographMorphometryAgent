import { useEffect, useRef, useState } from "react";
import { ChevronDown, Sparkles, TriangleAlert } from "lucide-react";
import { de } from "../lib/utils";
import type { Assessment, Capability } from "../lib/api";

/** Region and projection, shown as two small editable chips.
 *
 *  Both the options and the values are the morphometry service's canonical keys
 *  ("knee"/"lateral") taken straight from /api/capabilities — the same source the
 *  "Unterstützt" panel reads — so what the reader can pick is exactly what the service can
 *  measure. The model's own free-text German ("Kniegelenk, seitlich") is deliberately not
 *  used here: it drifts from the key vocabulary and would make the chips unselectable.
 */
export function AssessmentChips({
  capabilities,
  assessment,
  pending,
  onChange,
  disabled,
}: {
  capabilities: Capability[];
  /** What the last run actually used; null before the first analysis. */
  assessment: Assessment | null;
  /** A reader selection not yet analysed. Takes precedence in the display. */
  pending: { anatomy: string; projection: string } | null;
  onChange: (next: { anatomy: string; projection: string }) => void;
  disabled?: boolean;
}) {
  const current = pending ?? assessment;
  const anatomies = [...new Set(capabilities.map((c) => c.anatomy))];
  const projections = [
    ...new Set(
      capabilities.filter((c) => c.anatomy === current?.anatomy).map((c) => c.projection)
    ),
  ];

  // Switching region can strand the projection (shoulder has no "lateral"), so carry the
  // old one over only when the new region actually offers it.
  const pickAnatomy = (anatomy: string) => {
    const valid = capabilities.filter((c) => c.anatomy === anatomy).map((c) => c.projection);
    const projection =
      current && valid.includes(current.projection) ? current.projection : valid[0] ?? "";
    onChange({ anatomy, projection });
  };

  // Strictly false. `null` means the service was unreachable so support is unknown — badging
  // that as "nicht unterstützt" would blame the image for an infrastructure outage.
  const unsupported = !!assessment && assessment.supported === false && !pending;

  return (
    <div className="flex items-center gap-1.5">
      <Chip
        label={current ? `Körperregion: ${de(current.anatomy)}` : "Körperregion"}
        options={anatomies}
        value={current?.anatomy ?? ""}
        onSelect={pickAnatomy}
        title="Untersuchte Körperregion — vom Modell erkannt, hier korrigierbar"
        state={chipState(assessment, pending, unsupported)}
        disabled={disabled || anatomies.length === 0}
      />
      <Chip
        label={current ? `Projektion: ${de(current.projection)}` : "Projektion"}
        options={projections}
        value={current?.projection ?? ""}
        onSelect={(projection) => onChange({ anatomy: current?.anatomy ?? "", projection })}
        title="Projektion/Ebene — vom Modell erkannt, hier korrigierbar"
        state={chipState(assessment, pending, unsupported)}
        disabled={disabled || projections.length === 0}
      />
      {unsupported && (
        <span
          title="Diese Kombination wird von der Morphometrie nicht unterstützt. Falls das Bild falsch eingeordnet wurde, hier korrigieren."
          className="flex items-center gap-1 text-[11px] font-medium text-amber-700"
        >
          <TriangleAlert className="h-3.5 w-3.5" /> nicht unterstützt
        </span>
      )}
    </div>
  );
}

type State = "auto" | "model" | "user" | "unsupported";

function chipState(
  assessment: Assessment | null,
  pending: { anatomy: string; projection: string } | null,
  unsupported: boolean
): State {
  if (pending) return "user";
  if (unsupported) return "unsupported";
  if (!assessment) return "auto";
  return assessment.source === "user" ? "user" : "model";
}

const STYLES: Record<State, string> = {
  auto: "border-dashed border-slate-300 bg-white text-slate-400 hover:bg-slate-50",
  model: "border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100",
  user: "border-sky-200 bg-sky-50 text-sky-700 hover:bg-sky-100",
  unsupported: "border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100",
};

function Chip({
  label,
  options,
  value,
  onSelect,
  title,
  state,
  disabled,
}: {
  label: string;
  options: string[];
  value: string;
  onSelect: (v: string) => void;
  title: string;
  state: State;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        title={title}
        className={
          "flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-medium " +
          "disabled:cursor-not-allowed disabled:opacity-50 " +
          STYLES[state]
        }
      >
        {state === "model" && <Sparkles className="h-3 w-3 text-slate-400" />}
        {label}
        <ChevronDown className="h-3.5 w-3.5 opacity-60" />
      </button>
      {open && (
        <div className="absolute left-0 z-20 mt-1 min-w-36 rounded-lg border border-slate-200 bg-white p-1 shadow-lg">
          {options.map((o) => (
            <button
              key={o}
              onClick={() => {
                onSelect(o);
                setOpen(false);
              }}
              className={
                "flex w-full items-center rounded-md px-2 py-1.5 text-left text-xs " +
                (o === value ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-100")
              }
            >
              {de(o)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
