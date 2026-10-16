import { useEffect, useRef, useState } from "react";
import { ChevronDown, Gauge } from "lucide-react";

// Referenzumfang -> number of reference images the matching uses: more references = more
// robust matching, but slower.
//
// Deliberately WITHOUT time estimates. They only ever described the morphometry step,
// while a reader reasonably read them as the wait for the finished report — which is
// dominated by the LLM calls that follow and varies with the backbone. They also went
// stale the moment matching got faster (17 Sep 2026). The reference count is the honest
// number: it is the thing this control actually sets.
export const ACCURACY_LEVELS = [
  { label: "Minimal", refs: 1 },
  { label: "Schnell", refs: 10 },
  { label: "Standard", refs: 20 },
  { label: "Erweitert", refs: 40 },
];

export function AccuracySelect({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const current = ACCURACY_LEVELS.find((l) => l.refs === value) ?? ACCURACY_LEVELS[1];

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Referenzumfang der Morphometrie: mehr Referenzbilder = robusteres Matching, aber langsamer"
        className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100"
      >
        <Gauge className="h-3.5 w-3.5 text-slate-400" />
        {current.label}
        <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-60 rounded-lg border border-slate-200 bg-white p-1 shadow-lg">
          <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-slate-400">
            Referenzumfang (Morphometrie)
          </div>
          {ACCURACY_LEVELS.map((l) => (
            <button
              key={l.refs}
              onClick={() => {
                onChange(l.refs);
                setOpen(false);
              }}
              className={
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs " +
                (l.refs === value ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-100")
              }
            >
              <span className="font-medium">{l.label}</span>
              <span className="ml-auto text-slate-400">
                {l.refs} {l.refs === 1 ? "Referenzbild" : "Referenzbilder"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
