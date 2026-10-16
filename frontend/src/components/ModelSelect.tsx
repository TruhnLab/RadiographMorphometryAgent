import { useEffect, useRef, useState } from "react";
import { ChevronDown, Cpu } from "lucide-react";
import type { Profile } from "../lib/api";

/** Compact model picker for the header: a pill that opens a list with live/offline dots. */
export function ModelSelect({
  profiles,
  value,
  onChange,
}: {
  profiles: Profile[];
  value: string;
  onChange: (name: string) => void;
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
        className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100"
        title="Sprachmodell (LLM) für Analyse & Chat"
      >
        <Cpu className="h-3.5 w-3.5 text-slate-400" />
        {/* Clinical alias when the .env provides one — the technical id stays in the
            dropdown's tooltip, not permanently in the header. */}
        {profiles.find((p) => p.name === value)?.label || value || "kein Modell"}
        <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-64 rounded-lg border border-slate-200 bg-white p-1 shadow-lg">
          <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-slate-400">
            Sprachmodell (LLM)
          </div>
          {profiles.map((p) => (
            <button
              key={p.name}
              disabled={!p.live}
              onClick={() => {
                onChange(p.name);
                setOpen(false);
              }}
              className={
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs " +
                (p.name === value ? "bg-sky-50 text-sky-700 " : "text-slate-600 hover:bg-slate-100 ") +
                (!p.live ? "cursor-not-allowed opacity-40" : "")
              }
            >
              <span
                className={"h-1.5 w-1.5 shrink-0 rounded-full " + (p.live ? "bg-emerald-500" : "bg-slate-300")}
              />
              <span className="truncate" title={p.model || p.name}>
                {p.label || p.name}
              </span>
              {!p.live && <span className="ml-auto text-[10px] text-slate-400">offline</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
