export interface Profile {
  name: string;
  model: string;
  /** Optional clinical display name from .env (<PREFIX>_LABEL); "" when unset. */
  label: string;
  live: boolean;
}

export interface Step {
  type:
    | "thought"
    // Transient phase hint shown as the wait indicator's label; never a list entry.
    | "status"
    | "tool"
    | "guideline"
    | "assessment"
    | "visualization"
    | "geometry"
    | "reclassify"
    | "final";
  text?: string;
  // geometry steps: pushed as soon as morphometry returns, so the image becomes interactive
  // while the agent is still writing the report.
  landmarks?: number[][];
  landmarks_predicted?: number[][];
  landmark_names?: string[];
  geometry?: GeometryMap;
  measurements?: Record<string, number | null>;
  processed_image?: string;
  laterality_corrected?: boolean;
  // reclassify steps: the label comparison, shown so the reader sees this phase happening
  // instead of an unexplained pause before the agent starts talking.
  outcome?: string;
  changes?: LabelChange[];
  unresolved?: LabelChange[];
  moved?: string[];
  no_range?: string[];
  tool?: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  seconds?: number | null;
  // guideline steps:
  purpose?: string;
  query?: string;
  count?: number;
  results?: { title: string; score?: number | null; text?: string; document_id?: string }[];
  // assessment steps:
  anatomy?: string;
  projection?: string;
  // Canonical service keys ("knee"/"lateral") — these, never the model's free-text German
  // description, are what the chips and morphometry run on.
  anatomy_key?: string;
  projection_key?: string;
  supported?: boolean | null;
  source?: "model" | "user";
}

/** The region/projection a case ran with. Keys are canonical service tokens. */
export interface Assessment {
  anatomy: string;
  projection: string;
  /** null = unknown (the morphometry service was unreachable), which is NOT "unsupported". */
  supported: boolean | null;
  source: "model" | "user";
}

/** One measurement's construction, in the same [x, y] pixel space as the landmarks.
 *  Recomputed server-side on every landmark edit; drawn here as SVG. */
export interface Geometry {
  value: number | null;
  kind?: string; // "angle" | "ratio" | "distance"
  unit?: string;
  landmark_indices: number[]; // which landmarks this measurement depends on
  points: number[][];
  solid_lines: number[][][];
  dotted_lines: number[][][];
}

export type GeometryMap = Record<string, Geometry>;

/** The provenance of a reference range: which retrieved guideline passage states it
 *  ("literature", carrying the source excerpt), or that the reader supplied it ("user"). */
export interface RangeSource {
  type?: string; // "user" | "literature" (absent for an older literature source)
  title?: string;
  score?: number | null;
  excerpt?: string; // the sentence around the bound, for the clickable source popover
  document_id?: string;
  pages?: number[]; // PDF page number(s) of the passage, for the citation
  names_measurement?: boolean; // did the passage also name the measurement (strong match)?
  /** Absent/null when the passage quotes the bounds. Otherwise the mean and spread it
   *  states, and the factor the bounds were derived with (mean ∓ k·sd). */
  derived_from?: { mean: number; sd: number; k: number } | null;
}

export interface ReferenceRange {
  lower: number | null;
  upper: number | null;
  reference: string;
  source?: RangeSource | null;
}

export interface ReportPayload {
  session_id?: string;
  report_markdown: string;
  report: Record<string, unknown> | null;
  /** Who LAST wrote the current Beurteilung, derived server-side by text comparison:
   *  "user" only while the text verbatim is the reader's saved wording. */
  impression_author?: "model" | "user";
  /** The reader has authored the Beurteilung at some point — model rewrites run under
   *  the minimal-diff contract (backend state; the UI only badges the "user" author). */
  impression_protected?: boolean;
  trace: string[];
  steps: Step[];
  landmarks: number[][];
  landmarks_predicted?: number[][]; // the AI's geometry, for "reset to prediction"
  landmark_names?: string[]; // per-index labels for the drag handles
  landmarks_edited?: boolean;
  geometry?: GeometryMap;
  measurements?: Record<string, number | null>;
  /** Structured numeric bounds per canonical measurement — authoritative for the
   *  out-of-range arrows and re-check. `lower`/`upper` are null for a one-sided range. */
  reference_ranges?: Record<string, ReferenceRange>;
  anatomy_key?: string;
  projection_key?: string;
  anatomy_source?: "model" | "user";
  supported?: boolean | null;
  morpho_status?: string;  // non-"ok" -> the case was NOT measured; the report has no numbers
  morpho_message?: string;
  processed_image?: string;  // mirrored radiograph the server measured on ("" if no mirror)
  laterality_corrected?: boolean;  // did the server mirror the image for laterality?
  visualizations: { measurement: string; image: string }[];  // static PNGs; legacy fallback
  case_id: string;
  reply?: string;
  reclassify?: ReclassifyResult;  // only on /api/reclassify_stream
  recheck?: ReclassifyResult | null;  // verdict for the CURRENT landmarks (server truth)
}

export interface RecomputeResult {
  status: string;
  measurements: Record<string, number | null>;
  geometry: GeometryMap;
  landmarks_edited: boolean;
  /** Verdict for the landmarks as they now stand; null once they are moved again. */
  recheck: ReclassifyResult | null;
}

/** One measurement's normal/abnormal call that flipped after a landmark correction. */
export interface LabelChange {
  measurement: string;
  value_before: number | null;
  value_after: number | null;
  verdict_before: string;
  verdict_after: string;
  reference: string;
}

export interface ReclassifyResult {
  // Did the reader's landmark corrections change any normal/abnormal label?
  outcome: "accepted" | "corrected_no_label_change" | "corrected_label_change";
  /** Genuine normal <-> abnormal flips. Only these count as a label change. */
  changes: LabelChange[];
  /** Transitions to/from "unbekannt" (a reference range was found, or lost). Recorded for
   *  analysis but NOT a diagnostic flip, so they never drive the outcome. */
  unresolved?: LabelChange[];
  classification: Record<string, { value: number; verdict: string; reference: string }>;
  measurements: Record<string, number | null>;
}

/** Push edited landmarks to the server and get the recomputed measurements back.
 *  `commit` is true on pointer-up (the geometry the reader settled on) — that is what
 *  gets written to the audit log; intermediate drag frames do not. */
export async function updateLandmarks(
  sessionId: string,
  landmarks: number[][],
  commit: boolean,
  signal?: AbortSignal
): Promise<RecomputeResult> {
  const r = await fetch("/api/landmarks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, landmarks, commit }),
    signal,
  });
  if (!r.ok) return asError(r);
  return r.json();
}

export async function resetLandmarks(sessionId: string): Promise<RecomputeResult> {
  const r = await fetch("/api/landmarks/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!r.ok) return asError(r);
  return r.json();
}

/** What the reader "says" to the agent when they ask it to re-check after correcting
 *  landmarks. Shown as their own chat message, so the exchange reads like a conversation
 *  rather than a silent background request. */
export const RECHECK_MESSAGE =
  "Ich habe die Landmarken manuell korrigiert. Bitte prüfe den Bericht noch einmal " +
  "anhand der neuen Messwerte und aktualisiere ihn, falls sich die Beurteilung ändert.";

/** Re-check after a correction: streams the agent's reasoning, and carries the structured
 *  label-change verdict in the final payload. */
export async function reclassifyStream(
  sessionId: string,
  h: StreamHandlers,
  /** Current Denkmodus; applied to the session from this turn on. */
  thinking?: boolean
): Promise<void> {
  const resp = await fetch("/api/reclassify_stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message: RECHECK_MESSAGE, thinking }),
  });
  await readSSE(resp, h);
}

async function asError(r: Response): Promise<never> {
  let detail = `${r.status} ${r.statusText}`;
  try {
    const body = await r.json();
    if (body?.detail) detail = body.detail;
  } catch {
    /* ignore */
  }
  throw new Error(detail);
}

export async function getProfiles(): Promise<{ profiles: Profile[]; active: string }> {
  const r = await fetch("/api/profiles");
  if (!r.ok) return asError(r);
  return r.json();
}

export interface Capability {
  anatomy: string;
  projection: string;
  measurements: string[];
  /** Millimetres per pixel for this mode, mirroring the morphometry service's own job
   *  config. Lets a client that measures on its own (e.g. with a ruler tool) work on the
   *  same scale as the pipeline. null when the mode is unknown — distances then show "—"
   *  rather than a guessed, plausible, wrong number. */
  mpp: number | null;
}

/** Curated presentation per canonical measurement name — same source the backend's
 *  report renderer uses, so UI and Befund can never disagree on names or formats. */
export type MeasurementRegistry = Record<
  string,
  { de: string; group: string; decimals: number }
>;

export async function getCapabilities(): Promise<{
  morphometry: Capability[];
  registry: MeasurementRegistry;
}> {
  const r = await fetch("/api/capabilities");
  if (!r.ok) return { morphometry: [], registry: {} };
  return r.json();
}

// --- Streaming (Server-Sent Events) ---------------------------------------

export interface StreamHandlers {
  onStep: (step: Step) => void;
  onDone: (payload: ReportPayload) => void;
  onError: (detail: string) => void;
  /** Sent before any step, so landmarks are draggable (which needs the id) as soon as they
   *  appear, rather than only once the whole run finishes. */
  onSession?: (sessionId: string) => void;
}

async function readSSE(resp: Response, h: StreamHandlers): Promise<void> {
  if (!resp.ok || !resp.body) {
    h.onError(`${resp.status} ${resp.statusText}`);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const msg = JSON.parse(dataLine.slice(5).trim());
      if (msg.event === "step") h.onStep(msg.step as Step);
      else if (msg.event === "session") h.onSession?.(msg.session_id as string);
      else if (msg.event === "done") h.onDone(msg.payload as ReportPayload);
      else if (msg.event === "error") h.onError(msg.detail as string);
    }
  }
}

export async function analyzeStream(
  image: File,
  profile: string,
  numReferences: number,
  /** Denkmodus header toggle: false disables the model's chain-of-thought. */
  thinking: boolean,
  h: StreamHandlers,
  /** Reader-chosen region/projection. Omitted -> the model assesses the image itself. */
  override?: { anatomy: string; projection: string },
  /** Abort detaches the STREAM immediately; pair with cancelSession() so the server
   *  also stops at its next step boundary. */
  signal?: AbortSignal,
  /** Recorded session to bind the run to (see lib/plugins.ts). Sent WITH the request, not
   *  after it: the server starts the analysis worker immediately, so a follow-up bind
   *  arrives too late to record the first turn's model wait — the longest one of the case. */
  recorderSessionUid?: string
): Promise<void> {
  const fd = new FormData();
  fd.append("image", image);
  fd.append("profile", profile);
  fd.append("num_references", String(numReferences));
  fd.append("thinking", String(thinking));
  if (override) {
    fd.append("anatomy", override.anatomy);
    fd.append("projection", override.projection);
  }
  if (recorderSessionUid) fd.append("recorder_session", recorderSessionUid);
  const resp = await fetch("/api/analyze_stream", { method: "POST", body: fd, signal });
  await readSSE(resp, h);
}

/** Save a reader-edited Beurteilung. Marks it user-authored (protected from free model
 *  rewrites) and returns the refreshed report payload. */
export async function updateImpression(
  sessionId: string,
  impression: string
): Promise<ReportPayload> {
  const r = await fetch("/api/report/impression", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, impression }),
  });
  if (!r.ok) return asError(r);
  return r.json();
}

/** Ask the server to cancel the session's running turn (cooperative, next step boundary). */
export async function cancelSession(sessionId: string): Promise<void> {
  await fetch("/api/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => {}); // best-effort: the UI detaches regardless
}

export async function chatStream(
  sessionId: string,
  message: string,
  h: StreamHandlers,
  /** Current Denkmodus; applied to the session from this turn on. */
  thinking?: boolean
): Promise<void> {
  const resp = await fetch("/api/chat_stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message, thinking }),
  });
  await readSSE(resp, h);
}
