import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDown,
  ArrowUp,
  BookOpen,
  Check,
  CheckCircle2,
  Copy,
  Download,
  FileText,
  Loader2,
  PencilLine,
  RefreshCw,
  Stethoscope,
  Table2,
  UserPen,
} from "lucide-react";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import type { MeasurementRegistry, RangeSource, ReclassifyResult, ReferenceRange } from "../lib/api";
import { displayName } from "../lib/measurements";

interface Props {
  reportMd: string;
  caseId: string | null;
  sessionId: string | null;
  analyzing: boolean;
  /** Canonical morphometry names (the geometry keys) — what report text is matched against. */
  measurementNames: string[];
  /** Curated names: the report shows German display names, the canvas keys stay canonical
   *  — the click/hover link must recognise both. */
  registry: MeasurementRegistry;
  /** Structured numeric bounds + provenance per canonical measurement — the authoritative
   *  source for the out-of-range arrows and for the clickable range-source popover. */
  referenceRanges: Record<string, ReferenceRange>;
  /** Currency of the draft: the measured values moved since the report was last written
   *  (edits since the last check, or a reset after a check). Shown HERE, where the draft
   *  lives. */
  stale: boolean;
  reclassifyResult: ReclassifyResult | null;
  reclassifying: boolean;
  onReclassify: () => void;
  /** Editable Beurteilung: current text, its author, and the save handler. Once saved,
   *  the wording is user-authored and protected against free model rewrites. */
  impression: string;
  impressionAuthor: "model" | "user";
  onSaveImpression: (text: string) => Promise<void>;
  editable: boolean;
  activeMeasurement: string | null;
  /** Measurement the pointer is on in the LEFT panel — mirrored here as a highlight. */
  hoveredMeasurement: string | null;
  onSelectMeasurement: (name: string | null) => void;
  onHoverMeasurement: (name: string | null) => void;
}

/** The same normalisation the backend uses to key report measurements to canonical
 *  morphometry names (agent.py::_norm_key): lowercase, punctuation/whitespace stripped.
 *  The system prompt REQUIRES the report to use the exact tool names, so matching here is
 *  exact-modulo-punctuation, deliberately not fuzzy — the backend's comment explains why
 *  fuzzy matching of measurement names is quietly dangerous. */
const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

/** Is a measurement line's value above ("up") or below ("down") its stated reference
 *  range? Parses the value and the "(Normbereich: …)" span out of the report line ITSELF,
 *  the same reading the backend uses (agent.py::_parse_range: a–b range, or a one-sided
 *  < / > / ≤ / ≥ bound). This keeps the arrow consistent-by-construction with the number
 *  printed on that line — and needs no verdict payload, which is computed on a background
 *  thread and can be absent when the report first renders. Fails CLOSED: any doubt returns
 *  null (no arrow), because a wrong arrow on a radiology report is worse than none. */
const NUM = String.raw`-?\d+(?:[.,]\d+)?`;
const toNum = (s: string) => parseFloat(s.replace(",", "."));

/** The measured value a report line leads with, e.g. " 1,06 mm (Referenzbereich: …)" ->
 *  1.06. First number wins (the value precedes any range). */
function printedValue(afterName: string): number | null {
  const m = afterName.match(new RegExp(NUM));
  return m ? toNum(m[0]) : null;
}

function outOfRange(line: string): "up" | "down" | null {
  try {
    // Bounds live after the range marker. The model is not consistent about the word:
    // "Normbereich" in some runs, "Referenzbereich" in others — accept both (and the
    // ungrounded "kein … Referenzbereich hinterlegt", which carries no numbers, falls
    // through to null below).
    const at = line.search(/norm|referenz/i);
    if (at < 0) return null; // no reference range on this line (e.g. a Befund bullet)
    const head = line.slice(0, at);
    const seg = line.slice(at);
    const headNums = head.match(new RegExp(NUM, "g"));
    if (!headNums) return null;
    const value = toNum(headNums[headNums.length - 1]); // the value sits just before "Norm"

    let lower: number | null = null;
    let upper: number | null = null;
    const r = seg.match(new RegExp(`(${NUM})\\s*(?:–|—|-|bis|to)\\s*(${NUM})`, "i"));
    if (r) {
      lower = toNum(r[1]);
      upper = toNum(r[2]);
      if (lower > upper) [lower, upper] = [upper, lower];
    } else {
      const up = seg.match(new RegExp(`(?:<|≤|kleiner)\\s*=?\\s*(${NUM})`, "i"));
      const lo = seg.match(new RegExp(`(?:>|≥|größer|groesser)\\s*=?\\s*(${NUM})`, "i"));
      if (up) upper = toNum(up[1]);
      if (lo) lower = toNum(lo[1]);
    }
    if (lower === null && upper === null) return null;
    if (Number.isNaN(value)) return null;
    if (upper !== null && value > upper) return "up";
    if (lower !== null && value < lower) return "down";
    return null;
  } catch {
    return null;
  }
}

/** Flatten rendered markdown children to their plain text. */
function textOf(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  if (children && typeof children === "object" && "props" in children)
    return textOf((children as { props: { children?: ReactNode } }).props.children);
  return "";
}

/** Tidy a raw corpus excerpt for display: LaTeX-ish math to unicode, drop the $…$ and
 *  stray command/brace noise, demote headings to bold and drop the double "- •" bullets,
 *  so ReactMarkdown renders something readable instead of raw "## …$\beta \leq 145^\circ$". */
const _MATH: [RegExp, string][] = [
  [/\^?\{?\\circ\}?/g, "°"],
  [/\\leq/g, "≤"], [/\\geq/g, "≥"], [/\\neq/g, "≠"], [/\\approx/g, "≈"],
  [/\\pm/g, "±"], [/\\times/g, "×"], [/\\cdot/g, "·"],
  [/\\alpha/g, "α"], [/\\beta/g, "β"], [/\\gamma/g, "γ"], [/\\delta/g, "δ"],
  [/\\theta/g, "θ"], [/\\lambda/g, "λ"], [/\\mu/g, "µ"], [/\\degree/g, "°"],
];
function cleanExcerpt(s: string): string {
  let t = s;
  for (const [re, u] of _MATH) t = t.replace(re, u);
  t = t.replace(/\$([^$]*)\$/g, "$1"); // strip the $…$ math delimiters, keep the content
  t = t.replace(/\\[a-zA-Z]+/g, ""); // any remaining LaTeX command
  t = t.replace(/[{}]/g, "");
  t = t.replace(/^\s*#{1,6}\s*(.+?)\s*$/gm, "**$1**"); // heading -> bold line (no giant H2)
  t = t.replace(/[•‣]/g, ""); // the corpus writes "- •"; drop the literal bullet glyph
  t = t.replace(/[ \t]{2,}/g, " ");
  return t.trim();
}

/** German decimal notation for the numbers quoted in the provenance card. */
function de(n: number): string {
  return String(n).replace(".", ",");
}

/** Inline card revealing where a reference range came from: the reader's own entry, or the
 *  retrieved guideline passage — title, score, and the exact sentence (Tier 1 provenance;
 *  the highlighted-PDF view is a later step). */
function RangeSourceCard({ src }: { src: RangeSource }) {
  if (src.type === "user") {
    return (
      <div className="mt-1 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-[12px] text-violet-900">
        Vom Befunder angegeben — kein Leitlinienbeleg.
      </div>
    );
  }
  return (
    <div className="mt-1 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[12px] text-slate-700">
      <div className="mb-1 flex items-center gap-1.5 font-medium text-sky-800">
        <BookOpen className="h-3 w-3 shrink-0" />
        <span className="truncate">{src.title || "Leitlinie"}</span>
        {src.pages && src.pages.length > 0 && (
          <span className="shrink-0 whitespace-nowrap text-[11px] font-normal text-slate-500">
            · S. {src.pages.join(", ")}
          </span>
        )}
        {typeof src.score === "number" && (
          <span className="ml-auto shrink-0 tabular-nums text-[11px] font-normal text-slate-400">
            Score {src.score.toFixed(2)}
          </span>
        )}
      </div>
      {src.excerpt && (
        <div className="chat-md border-l-2 border-sky-200 pl-2 leading-snug text-slate-600">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{cleanExcerpt(src.excerpt)}</ReactMarkdown>
        </div>
      )}
      {src.derived_from && (
        <p className="mt-1 text-[11px] text-slate-500">
          Bereich abgeleitet aus {de(src.derived_from.mean)} ± {de(src.derived_from.sd)}
          {" "}(Mittelwert ± {src.derived_from.k === 1 ? "" : `${src.derived_from.k}×`}Streuung).
          Die Grenzen stehen so nicht im Text.
        </p>
      )}
      {src.names_measurement === false && (
        <p className="mt-1 text-[11px] text-amber-700">
          Passage nennt den Zahlenbereich, aber nicht die Messung namentlich.
        </p>
      )}
    </div>
  );
}

export function ReportPanel({
  reportMd,
  caseId,
  sessionId,
  analyzing,
  measurementNames,
  registry,
  referenceRanges,
  stale,
  reclassifyResult,
  reclassifying,
  onReclassify,
  impression,
  impressionAuthor,
  onSaveImpression,
  editable,
  activeMeasurement,
  hoveredMeasurement,
  onSelectMeasurement,
  onHoverMeasurement,
}: Props) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  // Beurteilung editor: null = viewing, string = the draft being edited.
  const [editText, setEditText] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const saveImpression = async () => {
    if (editText === null) return;
    setSaving(true);
    try {
      await onSaveImpression(editText);
      setEditText(null);
    } finally {
      setSaving(false);
    }
  };

  const byNorm = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of measurementNames) {
      m.set(norm(n), n); // canonical tool name
      m.set(norm(displayName(n, registry)), n); // curated German display name
    }
    return m;
  }, [measurementNames, registry]);

  // Structured range (bounds + source) indexed by the same normalised name the report line
  // carries (keyed both by the canonical name and its German display, so a line resolves
  // either way).
  const rangeByNorm = useMemo(() => {
    const m = new Map<string, ReferenceRange>();
    for (const [k, r] of Object.entries(referenceRanges)) {
      m.set(norm(k), r);
      m.set(norm(displayName(k, registry)), r);
    }
    return m;
  }, [referenceRanges, registry]);

  // Which measurement's range-source popover is open (keyed by the line's normalised name).
  const [openSource, setOpenSource] = useState<string | null>(null);

  // A selection made on the left brings its report line into view. The auto-selection
  // right after morphometry returns happens while the report is still empty (the agent is
  // still writing it), so this never scrolls on its own — only on a reader's click. A click
  // on the report line itself is a no-op here: the element is already visible, so
  // block:"nearest" moves nothing.
  useEffect(() => {
    if (!activeMeasurement) return;
    bodyRef.current
      ?.querySelector(`[data-measurement="${norm(activeMeasurement)}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeMeasurement]);

  /** Plain-text version for pasting into RIS/clinic systems: markdown markers stripped,
   *  structure kept as simple headed paragraphs. */
  const copyReport = async () => {
    const text = reportMd
      .replace(/^#{1,6}\s*/gm, "")
      .replace(/\*\*/g, "")
      .replace(/^- /gm, "• ")
      .trim();
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const download = async (kind: "docx" | "csv") => {
    if (!sessionId) return;
    const r = await fetch(`/api/report.${kind}?session=${sessionId}`);
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      kind === "docx"
        ? `Radiologiebericht_${caseId ?? "bericht"}.docx`
        : `Morphometrie_${caseId ?? "messwerte"}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Measurement names in the report (the bold "**Name:**" keys of the Messwerte section,
  // plus any bold mention elsewhere) become live: click selects + visualises the
  // construction on the left, hover mirrors as a chip highlight. Non-measurement bold text
  // renders untouched.
  const components = {
    // Out-of-range measurements swap their bullet for a red arrow (up = above the
    // reference range, down = below) — the point becomes the direction. In-range and
    // non-measurement lines (Befund) keep the plain disc, so the arrow reads as a genuine
    // exception, not decoration.
    li: ({ children }: { children?: ReactNode }) => {
      const text = textOf(children);
      // Prefer the structured numeric bounds (authoritative, one-sided-safe, immune to the
      // model's phrasing). The value is read from THIS line so the arrow always matches the
      // number shown next to it (which stays frozen until a re-check). Parsing the printed
      // range is only a fallback for a line with no structured bound.
      const colon = text.indexOf(":");
      const key = colon >= 0 ? norm(text.slice(0, colon)) : "";
      const ref = key ? rangeByNorm.get(key) : undefined;
      let dir: "up" | "down" | null;
      if (ref && (ref.lower != null || ref.upper != null)) {
        const value = printedValue(text.slice(colon + 1));
        dir =
          value == null
            ? null
            : ref.upper != null && value > ref.upper
              ? "up"
              : ref.lower != null && value < ref.lower
                ? "down"
                : null;
      } else {
        dir = outOfRange(text);
      }

      // Clickable provenance: a book icon appears when the range has a source (a retrieved
      // guideline passage, or the reader's own entry); clicking reveals the exact excerpt.
      const src = ref?.source ?? null;
      const hasSource = !!(src && (src.excerpt || src.type === "user"));
      const isOpen = openSource === key;
      const sourceUi = hasSource && (
        <>
          <button
            onClick={() => setOpenSource(isOpen ? null : key)}
            title="Herkunft des Referenzbereichs anzeigen"
            aria-expanded={isOpen}
            className={
              "ml-1 inline-flex translate-y-[1px] items-center rounded p-0.5 align-middle transition-colors " +
              (isOpen ? "bg-sky-100 text-sky-700" : "text-slate-400 hover:bg-sky-50 hover:text-sky-600")
            }
          >
            <BookOpen className="h-3 w-3" />
          </button>
          {isOpen && src && <RangeSourceCard src={src} />}
        </>
      );

      const Arrow = dir === "up" ? ArrowUp : ArrowDown;
      // The arrow hangs in the bullet's marker slot instead of pushing the text inward: it
      // is placed inline with a negative margin equal to the list's padding (-ml-5 = 1.25rem)
      // minus its own width+gap, so the net advance is ~0 and the text stays aligned. The
      // offset sits on the icon, not the <li>: `.report-md li { margin: … }` outweighs a
      // utility margin on the li and would cancel it.
      return (
        <li className={dir ? "list-none" : undefined}>
          {dir && (
            <Arrow
              className="-ml-5 mr-1.5 inline-block h-3.5 w-3.5 align-[-0.15em] text-red-600"
              aria-label={dir === "up" ? "über dem Referenzbereich" : "unter dem Referenzbereich"}
            />
          )}
          {children}
          {sourceUi}
        </li>
      );
    },
    strong: ({ children }: { children?: ReactNode }) => {
      const name = byNorm.get(norm(textOf(children).replace(/:\s*$/, "")));
      if (!name) return <strong>{children}</strong>;
      const isActive = activeMeasurement === name;
      const isHovered = hoveredMeasurement === name;
      return (
        <strong
          data-measurement={norm(name)}
          role="button"
          title="Messung im Bild anzeigen"
          // Selecting, never toggling off: a second click must not flip the canvas into
          // "Alle" (every measurement's dots and lines at once) — that is what the explicit
          // "Alle" chip is for.
          onClick={() => onSelectMeasurement(name)}
          onMouseEnter={() => onHoverMeasurement(name)}
          onMouseLeave={() => onHoverMeasurement(null)}
          // Amber, deliberately: the same colour as the landmark dots in the image, so the
          // highlight visibly IS "these dots over there".
          className={
            "-mx-0.5 cursor-pointer rounded px-0.5 underline decoration-dotted underline-offset-2 transition-colors " +
            (isActive
              ? "bg-amber-100 text-amber-900 decoration-amber-500"
              : isHovered
                ? "bg-amber-50 text-amber-900 decoration-amber-400"
                : "decoration-slate-300 hover:bg-amber-50 hover:text-amber-900")
          }
        >
          {children}
        </strong>
      );
    },
  };

  return (
    // Stale draft: a soft pastel-amber wash over the whole tile is the ambient "not yet
    // up to date" cue — calmer than an alarm, but unmissable because it colours the panel
    // the reader is looking at. The explicit action ("Neu prüfen") still lives in the
    // header. transition-colors so it fades in rather than snapping.
    <Card
      className={
        "h-full transition-colors " +
        (stale && reportMd && !analyzing && !reclassifying ? "bg-amber-50" : "")
      }
    >
      <CardHeader>
        <FileText className="h-4 w-4 text-slate-400" />
        <CardTitle>Befundentwurf</CardTitle>
        {/* Currency chip. Colour = required action, not "did something change": amber
            only while the draft no longer reflects the image (landmarks moved, not yet
            re-checked); a reader-reviewed update is sky (information), an unchanged
            verdict green. Untouched AI drafts carry no chip. */}
        {reportMd && !analyzing && (
          reclassifying ? (
            <span className="ml-1 flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600">
              <Loader2 className="h-3 w-3 animate-spin" /> Prüfe…
            </span>
          ) : stale ? (
            <>
              {/* Soft, not alarming: a small amber dot instead of a warning triangle —
                  the pastel tile already carries the "not current" signal, so this is just
                  a label next to the action that fixes it. */}
              <span
                title="Die Messwerte haben sich seit der letzten Fassung dieses Entwurfs geändert (Korrektur oder Zurücksetzen) — Messwerte und Beurteilung sind möglicherweise nicht mehr aktuell."
                className="ml-1 flex items-center gap-1.5 text-[11px] font-medium text-amber-700"
              >
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-hidden /> Entwurf nicht aktuell
              </span>
              <button
                onClick={onReclassify}
                title="Messwerte gegen die Referenzbereiche prüfen und den Entwurf aktualisieren"
                className="flex items-center gap-1 rounded-md bg-sky-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-sky-700"
              >
                <Stethoscope className="h-3 w-3" /> Neu prüfen
              </button>
            </>
          ) : reclassifyResult ? (
            reclassifyResult.outcome === "corrected_label_change" ? (
              <span
                title={`Der Entwurf wurde nach Ihrer Landmarken-Korrektur aktualisiert (${reclassifyResult.changes.length} geänderte Einordnung${reclassifyResult.changes.length === 1 ? "" : "en"}).`}
                className="ml-1 flex items-center gap-1 rounded bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-800"
              >
                <RefreshCw className="h-3 w-3" /> Nach Korrektur aktualisiert
              </span>
            ) : (
              <span
                title="Ihre Korrektur wurde geprüft — die Beurteilung bleibt unverändert gültig; die Messwerte sind aktuell."
                className="ml-1 flex items-center gap-1 rounded bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium text-emerald-800"
              >
                <CheckCircle2 className="h-3 w-3" /> Geprüft — unverändert
              </span>
            )
          ) : null
        )}
        {/* Authorship, derived from TEXT COMPARISON server-side. ONE badge: shown only
            while the Beurteilung verbatim IS the reader's saved wording. When the model
            was the last writer, the status chips already say so — no second flag. */}
        {reportMd && impressionAuthor === "user" && (
          <span
            title="Die Beurteilung entspricht wörtlich Ihrer Formulierung. Automatische Aktualisierungen ändern nur, was sachlich falsch geworden ist, und diese Kennzeichnung verschwindet dann, bis Sie erneut bearbeiten."
            className="ml-1 flex items-center gap-1 rounded bg-violet-100 px-1.5 py-0.5 text-[11px] font-medium text-violet-800"
          >
            <UserPen className="h-3 w-3" /> von Ihnen formuliert
          </span>
        )}
        {reportMd && editText === null && (
          <div className="ml-auto flex items-center gap-1">
            {/* Primary action for the clinical workflow: paste into the RIS. */}
            <button
              onClick={copyReport}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-sky-600"
              title="Befundentwurf als Text in die Zwischenablage kopieren"
            >
              {copied ? (
                <>
                  <Check className="h-3.5 w-3.5 text-emerald-600" /> Kopiert
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" /> Kopieren
                </>
              )}
            </button>
            <button
              onClick={() => download("docx")}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-sky-600"
              title="Befundentwurf als Word-Datei (.docx) exportieren"
            >
              <Download className="h-3.5 w-3.5" /> DOCX
            </button>
            {/* Measurement TABLE, not the prose report: one row per Messung mit Wert,
                Einordnung, Referenzbereich und Quelle — für die Auswertung in
                Excel. Deutsche Excel-Konventionen (Semikolon, Dezimalkomma). */}
            <button
              onClick={() => download("csv")}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-sky-600"
              title="Messwerte als Tabelle (.csv) exportieren — Wert, Einordnung, Referenzbereich und Quelle je Messung; für Excel (Semikolon, Dezimalkomma)"
            >
              <Table2 className="h-3.5 w-3.5" /> CSV
            </button>
          </div>
        )}
      </CardHeader>
      <CardBody>
        {analyzing ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-slate-400">
            <Loader2 className="h-6 w-6 animate-spin" />
            <p className="text-sm">Der Assistent analysiert das Bild und ruft Tools auf…</p>
          </div>
        ) : reportMd ? (
          /* Technik/Morphometrie always render — the reader references them while
             editing. Only the Beurteilung section swaps between prose and editor. */
          <div ref={bodyRef} className="report-md text-sm text-slate-700">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
              {reportMd.includes("## Beurteilung")
                ? reportMd.slice(0, reportMd.indexOf("## Beurteilung"))
                : reportMd}
            </ReactMarkdown>
            <h2 className="flex items-center gap-1.5">
              Beurteilung
              {/* The pencil sits at the ONE thing that is editable. */}
              {editable && editText === null && (
                <button
                  onClick={() => setEditText(impression)}
                  title="Beurteilung manuell bearbeiten — Ihre Formulierung bleibt bei späteren Aktualisierungen erhalten"
                  className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-sky-600"
                >
                  <PencilLine className="h-3.5 w-3.5" />
                </button>
              )}
            </h2>
            {editText !== null ? (
              <div className="mt-1 flex flex-col gap-2">
                <textarea
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  autoFocus
                  rows={6}
                  className="resize-y rounded-lg border border-slate-300 p-3 text-sm text-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-400/40"
                />
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-slate-400">
                    Werte ändern Sie über die Landmarken im Bild.
                  </span>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setEditText(null)}
                      disabled={saving}
                      className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100"
                    >
                      Verwerfen
                    </button>
                    <button
                      onClick={saveImpression}
                      disabled={saving || !editText.trim()}
                      className="flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-40"
                    >
                      {saving ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Check className="h-3.5 w-3.5" />
                      )}
                      Übernehmen
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{impression || "—"}</ReactMarkdown>
            )}
          </div>
        ) : (
          <div className="flex h-full items-center justify-center text-center text-sm text-slate-400">
            Lade ein Röntgenbild hoch und starte eine Analyse, um einen Bericht zu erstellen.
          </div>
        )}
      </CardBody>
    </Card>
  );
}
