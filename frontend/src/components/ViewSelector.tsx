import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

/** Image-view control: a segmented Original/Landmarken toggle + a measurement dropdown.
 *  Cleaner than a long row of pills when there are many measurements. */
export function ViewSelector({
  hasLandmarks,
  measurements,
  view,
  onSelect,
}: {
  hasLandmarks: boolean;
  measurements: string[];
  view: string;
  onSelect: (view: string) => void;
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

  const activeMeasurement = measurements.includes(view) ? view : null;

  const seg = (key: string, label: string) => (
    <button
      onClick={() => onSelect(key)}
      className={
        "px-2.5 py-1 text-xs font-medium " +
        (view === key ? "bg-sky-600 text-white" : "bg-white text-slate-600 hover:bg-slate-100")
      }
    >
      {label}
    </button>
  );

  return (
    <div className="mb-1 flex items-center gap-2">
      <div className="flex overflow-hidden rounded-lg border border-slate-200">
        {seg("original", "Original")}
        {hasLandmarks && <div className="w-px bg-slate-200" />}
        {hasLandmarks && seg("landmarks", "Landmarken")}
      </div>

      {measurements.length > 0 && (
        <div ref={ref} className="relative">
          <button
            onClick={() => setOpen((o) => !o)}
            className={
              "flex max-w-[13rem] items-center gap-1 rounded-lg border px-2.5 py-1 text-xs font-medium " +
              (activeMeasurement
                ? "border-sky-300 bg-sky-50 text-sky-700"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-100")
            }
          >
            <span className="truncate">{activeMeasurement ?? "Messung"}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          </button>
          {open && (
            <div className="absolute left-0 z-20 mt-1 max-h-64 w-64 overflow-auto rounded-lg border border-slate-200 bg-white p-1 shadow-lg">
              {measurements.map((m) => (
                <button
                  key={m}
                  onClick={() => {
                    onSelect(m);
                    setOpen(false);
                  }}
                  className={
                    "block w-full truncate rounded-md px-2 py-1.5 text-left text-xs " +
                    (view === m ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-100")
                  }
                >
                  {m}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
