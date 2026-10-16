import { useState } from "react";
import { ChevronDown, ChevronRight, Terminal } from "lucide-react";
import { cn } from "../lib/utils";

interface Props {
  trace: string[];
}

export function TracePanel({ trace }: Props) {
  const [open, setOpen] = useState(false);
  if (trace.length === 0) return null;

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-900 text-slate-100 shadow-sm">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-4 py-2 text-xs font-medium text-slate-300"
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <Terminal className="h-3.5 w-3.5" />
        Analyseprotokoll ({trace.length})
      </button>
      <pre
        className={cn(
          "overflow-auto px-4 pb-3 font-mono text-[11px] leading-relaxed text-slate-300 transition-all",
          open ? "max-h-48" : "max-h-0 py-0"
        )}
      >
        {trace.join("\n")}
      </pre>
    </div>
  );
}
