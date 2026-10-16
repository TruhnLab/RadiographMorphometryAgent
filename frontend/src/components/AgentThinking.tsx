import { useEffect, useRef, useState } from "react";
// ListChecks, not Brain: this block lists the agent's WORKFLOW steps (assessment, tool
// calls, guideline lookups), which run in every mode — the brain icon belongs to the
// Denkmodus toggle and would wrongly suggest this block disappears when thinking is off.
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  FileText,
  ListChecks,
  Loader2,
  Scale,
  ScanSearch,
  Wrench,
} from "lucide-react";
import type { Step } from "../lib/api";
import { cn } from "../lib/utils";

/** Clickable list of guideline source passages — click a title to reveal the
 *  exact excerpt the agent retrieved. */
function GuidelineSources({ results }: { results: NonNullable<Step["results"]> }) {
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  return (
    <ul className="mt-1 space-y-0.5">
      {results.map((r, j) => (
        <li key={j}>
          <button
            onClick={() => setOpenIdx(openIdx === j ? null : j)}
            className="flex w-full items-start gap-1 text-left text-slate-500 hover:text-amber-700"
            title="Quelle anzeigen"
          >
            <FileText className="mt-0.5 h-3 w-3 shrink-0" />
            <span className="truncate">{r.title}</span>
            {typeof r.score === "number" && (
              <span className="ml-auto shrink-0 text-slate-300">{r.score.toFixed(2)}</span>
            )}
          </button>
          {openIdx === j && r.text && (
            <blockquote className="mt-1 ml-4 whitespace-pre-wrap border-l-2 border-amber-200 bg-amber-50/60 px-2 py-1 text-slate-600">
              {r.text}
            </blockquote>
          )}
        </li>
      ))}
    </ul>
  );
}

function secs(s?: number | null): string {
  return s == null ? "" : ` · ${s.toFixed(1)}s`;
}

function summarizeResult(result: unknown): string {
  if (result == null) return "";
  if (typeof result === "object") {
    const r = result as Record<string, unknown>;
    if (r.error) return `error: ${String(r.error)}`;
    if (r.measurements) return Object.keys(r.measurements as object).join(", ");
    return Object.keys(r).slice(0, 4).join(", ");
  }
  return String(result);
}

export function AgentThinking({
  steps: allSteps,
  live,
  elapsed,
}: {
  steps: Step[];
  live?: boolean;
  elapsed?: number;
}) {
  // "geometry" is a UI-state step (it carries landmarks/measurements, no narration text) and
  // rides along in the final payload's step list. Without this filter it renders as an empty
  // numbered line in the reasoning block.
  const steps = allSteps.filter((s) => s.type !== "geometry" && s.type !== "status");
  // The most recent phase hint, used only as the wait indicator's label. Kept out of the
  // list: it says what the agent was ABOUT to do, and the list is a record of what it
  // actually did.
  const phase = [...allSteps].reverse().find((s) => s.type === "status")?.text ?? "";
  const [open, setOpen] = useState(!!live);
  const wasLive = useRef(live);

  // How long the current step has been silent. Some waits are long and produce nothing
  // until they finish — a report takes ~20s to write, landmark matching ~15s — and a
  // motionless list reads as "stuck" rather than "working". A counter is honest about
  // that without inventing events: it is a UI affordance, so nothing enters a recorded
  // session that did not actually happen in the pipeline.
  const [waiting, setWaiting] = useState(0);
  const stepCount = allSteps.length;
  useEffect(() => {
    if (!live) {
      setWaiting(0);
      return;
    }
    setWaiting(0);
    const since = Date.now();
    const id = window.setInterval(() => setWaiting((Date.now() - since) / 1000), 1000);
    return () => clearInterval(id);
    // Restart on every new step: the counter measures the CURRENT gap, not the turn.
  }, [live, stepCount]);

  // Expand while the agent is actively reasoning; auto-collapse once it's done.
  useEffect(() => {
    if (live) setOpen(true);
    else if (wasLive.current) setOpen(false);
    wasLive.current = live;
  }, [live]);

  if (!steps || steps.length === 0) {
    return live ? (
      <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50/80 px-2.5 py-1.5 text-xs text-slate-500">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <ListChecks className="h-3.5 w-3.5" /> Verarbeite…
      </div>
    ) : null;
  }

  const toolCount = steps.filter((s) => s.type === "tool").length;
  const guideCount = steps.filter((s) => s.type === "guideline").length;
  const parts: string[] = [];
  if (guideCount > 0)
    parts.push(`${guideCount} Quelle${guideCount > 1 ? "n" : ""} geprüft`);
  if (toolCount > 0) parts.push(`${toolCount} Messung${toolCount > 1 ? "en" : ""}`);
  // Wall-clock for the whole turn, so a fast turn is visibly fast and a slow one cannot
  // hide. Summing the step timings would miss the gaps between them — which is exactly
  // where the waiting used to be.
  if (!live && elapsed !== undefined) {
    const dur =
      elapsed < 1
        ? `${(elapsed * 1000).toFixed(0)} ms`
        : elapsed < 60
          ? `${elapsed.toFixed(1)} s`
          : `${Math.floor(elapsed / 60)}:${String(Math.round(elapsed % 60)).padStart(2, "0")} min`;
    parts.push(`Dauer ${dur}`);
  }
  const head = live ? "Arbeitsschritte" : "Arbeitsschritte abgeschlossen";
  const label = parts.length ? `${head} · ${parts.join(" · ")}` : head;

  return (
    <div className="mb-2 rounded-lg border border-slate-200 bg-slate-50/80">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-slate-500 hover:text-slate-700"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        {live ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ListChecks className="h-3.5 w-3.5" />}
        {label}
      </button>
      {open && (
        <ol className="space-y-2 px-3 pb-3 pt-1">
          {steps.map((s, i) => (
            <li key={i} className="flex gap-2 text-xs">
              <span className="mt-0.5 select-none text-slate-300">{i + 1}.</span>
              {s.type === "assessment" ? (
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-medium text-emerald-700">
                    <ScanSearch className="h-3 w-3" />
                    Bildbeurteilung
                  </div>
                  <div className="text-slate-500">
                    Anatomie: {s.anatomy || "—"} · Projektion: {s.projection || "—"}
                  </div>
                </div>
              ) : s.type === "guideline" ? (
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-medium text-amber-700">
                    <BookOpen className="h-3 w-3" />
                    Leitlinie
                    {s.purpose && <span className="font-normal text-slate-400">· {s.purpose}</span>}
                  </div>
                  <div className="text-slate-400">
                    „{s.query}" → {s.count ?? 0} Treffer
                  </div>
                  {s.results && s.results.length > 0 && <GuidelineSources results={s.results} />}
                </div>
              ) : s.type === "tool" ? (
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-medium text-sky-700">
                    <Wrench className="h-3 w-3" />
                    {s.tool}
                    {s.arguments && Object.keys(s.arguments).length > 0 && (
                      <span className="font-normal text-slate-400">
                        ({Object.entries(s.arguments)
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join(", ")})
                      </span>
                    )}
                  </div>
                  <div className="text-slate-400">
                    → {summarizeResult(s.result)}
                    <span className="text-slate-300">{secs(s.seconds)}</span>
                  </div>
                </div>
              ) : s.type === "reclassify" ? (
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-medium text-violet-700">
                    <Scale className="h-3 w-3" />
                    Einordnung geprüft
                    <span className="text-slate-300">{secs(s.seconds)}</span>
                  </div>
                  {s.moved && s.moved.length > 0 && (
                    <div className="text-slate-400">
                      Neue Messwerte: {s.moved.join(" · ")}
                    </div>
                  )}
                  <div className="text-slate-500">
                    {s.changes && s.changes.length > 0
                      ? s.changes
                          .map(
                            (c) => `${c.measurement}: ${c.verdict_before} → ${c.verdict_after}`
                          )
                          .join(" · ")
                      : "Beurteilung (normal/abnormal) unverändert"}
                  </div>
                  {s.no_range && s.no_range.length > 0 && (
                    <div className="text-amber-700">
                      Ohne Normbereich (nicht prüfbar): {s.no_range.join(", ")}
                    </div>
                  )}
                </div>
              ) : (
                <span
                  className={cn(
                    s.type === "final" ? "font-medium text-slate-600" : "text-slate-500"
                  )}
                >
                  {s.text}
                  {s.type === "thought" && <span className="text-slate-300">{secs(s.seconds)}</span>}
                </span>
              )}
            </li>
          ))}
          {/* The gap the agent is in RIGHT NOW, so it sits after the completed steps.
              Shown only once it is long enough to look like a hang; below that it would
              just flicker between steps. */}
          {live && waiting >= 3 && (
            <li className="flex items-center gap-2 text-xs text-slate-400">
              <span className="select-none text-slate-300">{steps.length + 1}.</span>
              <Loader2 className="h-3 w-3 animate-spin" />
              <span>{phase || "arbeitet"}… ({waiting.toFixed(0)} s)</span>
            </li>
          )}
        </ol>
      )}
    </div>
  );
}
