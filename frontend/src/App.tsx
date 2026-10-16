import { useEffect, useRef, useState } from "react";
import { Activity, AlertTriangle, Brain, Info } from "lucide-react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { SetupPanel } from "./components/SetupPanel";
import { ModelSelect } from "./components/ModelSelect";
import { AccuracySelect } from "./components/AccuracySelect";
import { ReportPanel } from "./components/ReportPanel";
import { ChatPanel, type Message } from "./components/ChatPanel";
import { TracePanel } from "./components/TracePanel";
import {
  analyzeStream,
  cancelSession,
  chatStream,
  getCapabilities,
  getProfiles,
  reclassifyStream,
  RECHECK_MESSAGE,
  resetLandmarks,
  updateImpression,
  updateLandmarks,
  type Assessment,
  type Capability,
  type ReferenceRange,
  type GeometryMap,
  type MeasurementRegistry,
  type Profile,
  type ReclassifyResult,
  type Step,
} from "./lib/api";
import { preferredNames } from "./lib/measurements";
import { de } from "./lib/utils";
import { useSessionPlugin, type CaseHost } from "./lib/plugins";

/** Replace the last message in the list via a transform (immutably). */
function updateLast(msgs: Message[], fn: (m: Message) => Message): Message[] {
  if (msgs.length === 0) return msgs;
  const copy = msgs.slice();
  copy[copy.length - 1] = fn(copy[copy.length - 1]);
  return copy;
}

function appendStep(msgs: Message[], step: Step): Message[] {
  return updateLast(msgs, (m) => ({ ...m, steps: [...(m.steps ?? []), step] }));
}

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [reportMd, setReportMd] = useState("");
  // Beurteilung text + authorship, for the editable Befundentwurf. "user" is sticky per
  // case: from the first manual edit on, model rewrites must preserve the wording.
  const [impression, setImpression] = useState("");
  const [impressionAuthor, setImpressionAuthor] = useState<"model" | "user">("model");
  const [caseId, setCaseId] = useState<string | null>(null);
  const [trace, setTrace] = useState<string[]>([]);
  // -- landmark refinement state ------------------------------------------
  // `landmarks` is what the reader currently sees and drags; `landmarksPredicted` is the
  // AI's original geometry, kept so we can always go back to it.
  const [landmarks, setLandmarks] = useState<number[][] | null>(null);
  const [landmarksPredicted, setLandmarksPredicted] = useState<number[][]>([]);
  const [landmarkNames, setLandmarkNames] = useState<string[]>([]);
  const [landmarksEdited, setLandmarksEdited] = useState(false);
  const [geometry, setGeometry] = useState<GeometryMap>({});
  const [measurements, setMeasurements] = useState<Record<string, number | null>>({});
  const [measurementsPredicted, setMeasurementsPredicted] = useState<Record<string, number | null>>(
    {}
  );
  // Structured numeric bounds per measurement — drives the out-of-range arrows in the
  // report. Ranges don't change when landmarks move (only the values do), so this is set
  // from the analyze payload and left alone during correction.
  const [referenceRanges, setReferenceRanges] = useState<Record<string, ReferenceRange>>({});
  const [activeMeasurement, setActiveMeasurement] = useState<string | null>(null);
  // Hover link between the report and the measurement chips: one shared value, set by
  // whichever side the pointer is on, highlighted on the other.
  const [hoveredMeasurement, setHoveredMeasurement] = useState<string | null>(null);
  const [reclassifying, setReclassifying] = useState(false);
  const [reclassifyResult, setReclassifyResult] = useState<ReclassifyResult | null>(null);
  // Has the report ever been rewritten by a re-check this case? Needed for staleness
  // after "Zurücksetzen": landmarks return to the AI prediction (landmarksEdited=false),
  // but a report that was rewritten for the corrected values no longer matches them.
  const [everChecked, setEverChecked] = useState(false);

  // The radiograph the landmarks/geometry are expressed in (the server's processed image,
  // mirrored if it corrected laterality). Always drawn under the overlay when present.
  const [processedImage, setProcessedImage] = useState<string | null>(null);
  const [lateralityCorrected, setLateralityCorrected] = useState(false);
  // Static PNGs; only produced when the morphometry server predates /recompute.
  const [visualizations, setVisualizations] = useState<{ measurement: string; image: string }[]>([]);
  const [imageView, setImageView] = useState("original");
  const [messages, setMessages] = useState<Message[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [registry, setRegistry] = useState<MeasurementRegistry>({});
  const [showCaps, setShowCaps] = useState(false);
  // What the last run used, vs. a correction the reader has made but not yet run. Kept
  // apart so the chips can show "this is what produced the report on screen" until the
  // re-run actually happens — otherwise the labels would describe a report that does not
  // exist yet.
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [pendingAssessment, setPendingAssessment] = useState<
    { anatomy: string; projection: string } | null
  >(null);
  const [numReferences, setNumReferences] = useState(10); // morphometry accuracy
  // Denkmodus: whether the reasoning models may use their chain-of-thought. Off by
  // default — much faster, and the pipeline's structured steps rarely benefit; readers
  // can switch it on for hard cases. (Headless batch runs keep the backend default: on.)
  const [thinking, setThinking] = useState(false);

  const [analyzing, setAnalyzing] = useState(false);
  const [chatting, setChatting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Stop button: aborts the SSE stream (UI detaches instantly) and asks the server to
  // cancel at its next step boundary.
  const analyzeAbort = useRef<AbortController | null>(null);

  // --- optional session plugin (lib/plugins.ts) -------------------------------
  // Inert without one, so every recorder call below is safe in ordinary use and there is
  // no second code path to keep in sync.
  const interacted = useRef(false);
  const started = useRef(false);
  // Set when the reader stops a running analysis; cleared once they act on it. Lets the
  // log tell an interrupt-then-correct apart from a correction made at rest.
  const interruptedRun = useRef(false);
  // Filled BEFORE the plugin runs, so it never sees the previous render's case. The two
  // functions are defined further down; the plugin only calls them after this render.
  const host = useRef<CaseHost>(null as unknown as CaseHost);
  host.current = {
    sessionId,
    reportMd,
    get started() {
      return started.current;
    },
    pickImage: (file) => pickImage(file),
    clearCase: () => clearCase(),
  };
  const plugin = useSessionPlugin({ profile: selectedProfile, thinking, numReferences }, host);
  const recorder = plugin.recorder;

  /** First real reader action of the case — logged once. Together with task_started it
   *  gives time-to-first-interaction, which separates "reading the image" from "waiting
   *  for the model" in a way the model-wait marks alone cannot. */
  const noteInteraction = (kind: string) => {
    if (interacted.current) return;
    interacted.current = true;
    recorder.log("first_interaction", { kind });
  };

  useEffect(() => {
    let cancelled = false;
    // The backend may still be starting (run.py launches it moments earlier),
    // so retry a few times before giving up rather than showing a hard error.
    const load = async () => {
      for (let attempt = 0; attempt < 12 && !cancelled; attempt++) {
        try {
          const { profiles, active } = await getProfiles();
          if (cancelled) return;
          setProfiles(profiles);
          const live = profiles.filter((p) => p.live);
          // The .env's ACTIVE_PROFILE is the default — no hardcoded favourites. A live
          // profile beats an offline default (selecting an offline model just fails on
          // analyze), so: active-if-live, else any live, else active, else first.
          const preferred =
            live.find((p) => p.name === active)?.name ??
            live[0]?.name ??
            profiles.find((p) => p.name === active)?.name ??
            profiles[0]?.name ??
            "";
          setSelectedProfile(preferred);
          setError(null);
          return;
        } catch {
          if (!cancelled) setError("Verbinde mit Backend…");
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
      if (!cancelled) setError("Backend auf :8000 nicht erreichbar. Läuft es?");
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // Capabilities drive the region/projection chips. Morphometry may still be warming up
    // when the page loads; fetching once would leave the chips permanently disabled
    // (empty option list) until a manual reload. So keep retrying while the list is empty,
    // and it self-enables the moment the service is reachable.
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      getCapabilities()
        .then((c) => {
          if (cancelled) return;
          setCapabilities(c.morphometry ?? []);
          setRegistry(c.registry ?? {});
          if (!(c.morphometry && c.morphometry.length)) timer = setTimeout(load, 15000);
        })
        .catch(() => {
          if (!cancelled) timer = setTimeout(load, 15000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // The drag loop. The handle itself already follows the cursor locally at 60 fps; what
  // this fetches is the *derived* layer (values + construction lines), which only the
  // server may compute — the browser never reimplements the measurement math.
  //
  // At most one request is in flight; newer landmark positions supersede any queued one,
  // so a slow round trip makes the overlay trail the cursor slightly instead of building
  // a backlog of stale frames.
  const inFlight = useRef(false);
  const queued = useRef<number[][] | null>(null);

  const pushLandmarks = async (sid: string, next: number[][], commit: boolean) => {
    if (inFlight.current && !commit) {
      queued.current = next; // latest wins
      return;
    }
    inFlight.current = true;
    try {
      const res = await updateLandmarks(sid, next, commit);
      setMeasurements(res.measurements);
      setGeometry(res.geometry);
      setLandmarksEdited(res.landmarks_edited);
      // The server says whether THESE landmarks have a verdict; moving them clears it.
      setReclassifyResult(res.recheck ?? null);
    } catch {
      /* a dropped frame mid-drag is not worth interrupting the reader over */
    } finally {
      inFlight.current = false;
      const pending = queued.current;
      queued.current = null;
      if (pending) void pushLandmarks(sid, pending, false);
    }
  };

  /** Steps arrive live over SSE. A "geometry" step is not narration — it is the measured
   *  result, pushed the moment morphometry returns so the reader can start inspecting and
   *  correcting landmarks while the agent is still writing the report. */
  // Did the live "geometry" step already deliver the measured result? If so it is
  // authoritative and the final payload must not clobber it — by then the reader may
  // already have corrected landmarks, and re-seeding the baseline from the corrected values
  // would erase the distinction between the AI's prediction and the reader's correction.
  const gotGeometry = useRef(false);

  const handleStep = (step: Step) => {
    if (step.type === "assessment") {
      // Chips follow the canonical keys, never the model's free-text German.
      setAssessment({
        anatomy: step.anatomy_key ?? "",
        projection: step.projection_key ?? "",
        supported: step.supported ?? null,
        source: step.source ?? "model",
      });
      setPendingAssessment(null); // the run adopted it; it is no longer "pending"
    }
    if (step.type === "geometry") {
      gotGeometry.current = true;
      setLandmarks(step.landmarks ?? null);
      setLandmarksPredicted(step.landmarks_predicted ?? step.landmarks ?? []);
      setLandmarkNames(step.landmark_names ?? []);
      setGeometry(step.geometry ?? {});
      setMeasurements(step.measurements ?? {});
      setMeasurementsPredicted(step.measurements ?? {});
      setLandmarksEdited(false);
      setProcessedImage(step.processed_image || null);
      setLateralityCorrected(!!step.laterality_corrected);
      // Show ONE construction by default, not all of them. Measurements share reference
      // axes (every foot angle draws the same first-metatarsal axis) and several ship a
      // near-identical "(robust)" twin, so "Alle" stacks coincident lines on top of each
      // other and reads as duplicate/ghost geometry. Pick from the PREFERRED names —
      // the raw first key can be a hidden plain twin, which drew an invisible-to-the-
      // chips construction and left no chip selected.
      setActiveMeasurement(preferredNames(Object.keys(step.geometry ?? {}))[0] ?? null);
      return; // state, not a trace entry
    }
    setMessages((m) => appendStep(m, step));
  };

  const handleLandmarksChange = (next: number[][], commit: boolean) => {
    setLandmarks(next); // instant, local — the handle never lags the cursor
    if (commit) {
      // Only the settled geometry is worth recording; intermediate drag frames are noise.
      // The server writes the authoritative landmarks_corrected event with the actual
      // coordinates — this one exists to timestamp the reader's gesture on the client
      // clock, which is what the dwell/effort metrics are built on.
      noteInteraction("landmark_drag");
      recorder.log("landmark_commit", { n_landmarks: next.length });
    }
    if (sessionId) void pushLandmarks(sessionId, next, commit);
  };

  const handleResetLandmarks = async () => {
    if (!sessionId) return;
    recorder.log("landmarks_reset_clicked", {});
    const res = await resetLandmarks(sessionId);
    setLandmarks(landmarksPredicted);
    setMeasurements(res.measurements);
    setGeometry(res.geometry);
    setLandmarksEdited(res.landmarks_edited);
    setReclassifyResult(res.recheck ?? null);
  };

  // Asking the agent to re-check is a conversation, not a silent background call: the
  // reader's request appears as their own chat message and the agent's reasoning streams
  // back in the usual "thinking" style, so they can see what it is doing with their
  // corrections instead of waiting on a spinner.
  const handleReclassify = async () => {
    if (!sessionId) return;
    recorder.log("recheck_requested", {});
    setReclassifying(true);
    setMessages((m) => [
      ...m,
      { role: "user", content: RECHECK_MESSAGE },
      { role: "assistant", content: "", steps: [], streaming: true, startedAt: Date.now() },
    ]);
    try {
      await reclassifyStream(sessionId, {
        onStep: handleStep,
        onDone: (p) => {
          setReportMd(p.report_markdown);
          setImpression(String(p.report?.["impression"] ?? ""));
          setImpressionAuthor(p.impression_author ?? "model");
          setTrace(p.trace);
          setReferenceRanges(p.reference_ranges ?? {}); // a re-check may have added a range
          setReclassifyResult(p.reclassify ?? p.recheck ?? null);
          setEverChecked(true);
          setMessages((m) =>
            updateLast(m, (msg) => ({
              ...msg,
              content: p.reply ?? "",
              steps: p.steps,
              streaming: false,
              elapsed: msg.startedAt ? (Date.now() - msg.startedAt) / 1000 : undefined,
            }))
          );
        },
        onError: (d) =>
          setMessages((m) =>
            updateLast(m, (msg) => ({ ...msg, content: `⚠️ ${d}`, streaming: false }))
          ),
      }, thinking);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setReclassifying(false);
    }
  };

  const resetCase = () => {
    gotGeometry.current = false;
    setLandmarks(null);
    setLandmarksPredicted([]);
    setLandmarkNames([]);
    setLandmarksEdited(false);
    setGeometry({});
    setMeasurements({});
    setMeasurementsPredicted({});
    setReferenceRanges({});
    setActiveMeasurement(null);
    setHoveredMeasurement(null);
    setReclassifyResult(null);
    setEverChecked(false);
    setImpression("");
    setImpressionAuthor("model");
    setProcessedImage(null);
    setLateralityCorrected(false);
    setVisualizations([]);
    setImageView("original");
  };

  const pickImage = (file: File) => {
    setImageFile(file);
    setImageUrl(URL.createObjectURL(file));
    recorder.log("image_ready", { name: file.name, bytes: file.size, source: recorder.active ? "library" : "picker" });
    resetCase();
    // A new radiograph is a new case. The report, the conversation and the session all
    // describe the OLD image — leaving them on screen next to a different patient's film is
    // exactly the kind of stale-context error that gets a finding attributed to the wrong
    // study, so tear all of it down.
    setReportMd("");
    setMessages([]);
    setSessionId(null);
    setCaseId(null);
    setTrace([]);
    setError(null);
    // A different film gets a fresh assessment; carrying the last one over would let the
    // reader run a new image under the previous image's region.
    setAssessment(null);
    setPendingAssessment(null);
  };

  /** Reader saved a hand-edited Beurteilung: persist it, adopt the server's re-rendered
   *  report, and from now on the wording is protected against free model rewrites. */
  const handleSaveImpression = async (text: string) => {
    if (!sessionId) return;
    noteInteraction("impression_edit");
    // The text, not just its length. The earlier reasoning — that impression_edited
    // already carries before/after, so repeating the prose would put patient-describing
    // text in two places — held for the FILE but not for reading a session back: an event
    // that says only "210 Zeichen" makes the reader look elsewhere to find out what was
    // written. Every snapshot already carries the full report anyway, so this adds no
    // class of content the stream did not hold.
    recorder.log("impression_saved", { chars: text.length, text });
    const p = await updateImpression(sessionId, text);
    setReportMd(p.report_markdown);
    setImpression(String(p.report?.["impression"] ?? text));
    setImpressionAuthor(p.impression_author ?? "user");
  };

  /** A measurement name clicked in the REPORT: select it so the left panel shows its
   *  construction. In the interactive path the canvas always renders the active
   *  measurement's geometry; the imageView switch only matters for the legacy static-PNG
   *  fallback, where each measurement is its own tab. */
  const handleSelectFromReport = (name: string | null) => {
    setActiveMeasurement(name);
    recorder.log("measurement_selected", { measurement: name, from: "report" });
    if (name && !Object.keys(geometry).length && visualizations.some((v) => v.measurement === name)) {
      setImageView(name);
    }
  };

  /** The reader corrected the region/projection. Held as pending — the re-run is explicit,
   *  since analysing can take up to a couple of minutes and would discard the current
   *  report. The chips show the correction immediately; the button says what it will do. */
  const handleChangeAssessment = (next: { anatomy: string; projection: string }) => {
    setPendingAssessment(next);
    noteInteraction("assessment_change");
    recorder.log("assessment_changed_by_reader", next);
  };

  /** Clear the case on screen: report, chat, session and landmarks. A plugin calls this
   *  when it ends a case (e.g. back to a worklist). */
  const clearCase = () => {
    resetCase();
    setReportMd("");
    setMessages([]);
    setSessionId(null);
    started.current = false;
    interacted.current = false;
  };

  /** The reader saw a wrong region/projection while the run was still going: stop it and
   *  restart at once with their correction.
   *
   *  Cheap by construction. Assessment is the pipeline's FIRST step (it lands ~5s in), and
   *  a run that is given the region skips assess_image entirely — so restarting here loses
   *  only the seconds already spent, not morphometry, guideline retrieval and the report.
   *  Waiting for the wrong run to finish and re-running afterwards costs a full pipeline.
   */
  const handleApplyCorrection = async () => {
    if (!pendingAssessment) return;
    // The chips are locked while a run is in progress, so a correction can only follow an
    // interrupt. `after_interrupt` records whether the reader stopped a running analysis
    // to make it — which is what distinguishes "spotted the misroute early" from
    // "corrected a finished case".
    recorder.log("assessment_corrected", {
      ...pendingAssessment,
      was: assessment ? `${assessment.anatomy}/${assessment.projection}` : null,
      after_interrupt: interruptedRun.current,
    });
    interruptedRun.current = false;
    await runAnalysis();
  };

  const handleAnalyze = () => runAnalysis();

  /** Start (or restart) the analysis. Whether the reader is correcting the region is not a
   *  parameter: the run picks up `pendingAssessment` as an override on its own, and the
   *  confirmation is gated on there being a report to lose. */
  const runAnalysis = async () => {
    if (!imageFile) {
      setError("Bitte zuerst ein Röntgenbild hochladen.");
      return;
    }
    // A re-run is a FRESH case, not an update — easy to hit by accident right after
    // correcting landmarks. Make the cost explicit before discarding anything.
    //
    // Gated on a REPORT existing, not on a session existing. A session id appears the
    // moment a run starts, so the old check warned about discarding work even when the
    // reader had merely corrected the region before anything was produced — a popup with
    // nothing behind it.
    if (
      reportMd &&
      !window.confirm(
        "Neue Analyse starten?\n\nDer aktuelle Befundentwurf, der Chat-Verlauf und alle " +
          "Landmarken-Korrekturen werden dabei verworfen. Korrigierte Messwerte fließen " +
          "über 'Neu prüfen' in den Bericht ein — nicht über eine neue Analyse."
      )
    )
      return;
    setError(null);
    // Start of the reading task: the FIRST press only. A re-run after correcting the
    // region is part of the same task, not a new clock — restarting it here would
    // silently discard the time already spent.
    if (!started.current) {
      started.current = true;
      recorder.log("task_started", {});
    } else {
      recorder.log("reanalyze_requested", {});
    }
    setAnalyzing(true);
    // Read before resetCase(): a pending correction is the whole point of this run.
    const override = pendingAssessment?.anatomy && pendingAssessment.projection
      ? pendingAssessment
      : undefined;
    // Once the run carries the override, the correction is no longer "pending" — the
    // server uses those keys verbatim and skips assess_image, so they ARE the assessment.
    // Adopting them here keeps the chips showing the reader's choice and stops the
    // correction button from re-appearing over its own run.
    const adopted = override
      ? { anatomy: override.anatomy, projection: override.projection,
          supported: null, source: "user" as const }
      : null;
    resetCase(); // new case
    if (adopted) {
      setAssessment(adopted);
      setPendingAssessment(null);
    }
    setMessages([
      { role: "assistant", content: "", steps: [], streaming: true, startedAt: Date.now() },
    ]);
    analyzeAbort.current = new AbortController();
    try {
      await analyzeStream(imageFile, selectedProfile, numReferences, thinking, {
        onSession: (sid) => {
          setSessionId(sid);
          // Attach the agent's session to the recorded session as early as possible, so its
          // own audit events (baseline, corrections, reclassify) land on the SAME stream
          // as the client's — interleaved, one clock, one order.
          void recorder.bind(sid);
        }, // early: makes the landmarks draggable as soon as they show
        onStep: handleStep,
        onDone: (p) => {
          setSessionId(p.session_id ?? null);
          setReportMd(p.report_markdown);
          setImpression(String(p.report?.["impression"] ?? ""));
          setImpressionAuthor(p.impression_author ?? "model");
          setCaseId(p.case_id);
          setTrace(p.trace);
          setReferenceRanges(p.reference_ranges ?? {}); // structured bounds for the arrows
          if (p.anatomy_key || p.projection_key) {
            setAssessment({
              anatomy: p.anatomy_key ?? "",
              projection: p.projection_key ?? "",
              supported: p.supported ?? null,
              source: p.anatomy_source ?? "model",
            });
            setPendingAssessment(null);
          }
          if (!gotGeometry.current) {
            // No live geometry step (e.g. a morphometry server without /recompute) — fall
            // back to seeding everything from the final payload.
            setLandmarks(p.landmarks?.length ? p.landmarks : null);
            setLandmarksPredicted(p.landmarks_predicted ?? p.landmarks ?? []);
            setLandmarkNames(p.landmark_names ?? []);
            setGeometry(p.geometry ?? {});
            setMeasurements(p.measurements ?? {});
            setMeasurementsPredicted(p.measurements ?? {});
            setProcessedImage(p.processed_image || null);
            setLateralityCorrected(!!p.laterality_corrected);
          }
          setLandmarksEdited(!!p.landmarks_edited);
          // A failed morphometry call still yields a fluent, plausible-looking report — one
          // that simply has no measurements in it. Say so loudly; never let an unmeasured
          // case pass for a normal one.
          if (p.morpho_status && p.morpho_status !== "ok") {
            setError(
              `Morphometrie fehlgeschlagen (${p.morpho_status})` +
                (p.morpho_message ? `: ${p.morpho_message}` : "") +
                " — der Bericht enthält KEINE Messwerte. Bitte Analyse wiederholen."
            );
          }
          setVisualizations(p.visualizations ?? []);
          setMessages((m) =>
            updateLast(m, (msg) => ({
              ...msg,
              content: p.reply ?? "Der Bericht wurde erstellt.",
              steps: p.steps,
              streaming: false,
              elapsed: msg.startedAt ? (Date.now() - msg.startedAt) / 1000 : undefined,
            }))
          );
        },
        onError: (d) =>
          setMessages((m) =>
            updateLast(m, (msg) => ({ ...msg, content: `⚠️ ${d}`, streaming: false }))
          ),
      }, override, analyzeAbort.current.signal, recorder.sessionUid ?? undefined);
    } catch (e) {
      // An aborted stream is the stop button doing its job, not an error.
      if ((e as Error).name !== "AbortError") setError(String((e as Error).message ?? e));
    } finally {
      setAnalyzing(false);
      analyzeAbort.current = null;
    }
  };

  /** Stop button: detach the UI now, tell the server to stop at its next step boundary. */
  const handleCancelAnalyze = () => {
    interruptedRun.current = true;
    if (sessionId) void cancelSession(sessionId);
    analyzeAbort.current?.abort();
    setAnalyzing(false);
    setMessages((m) =>
      updateLast(m, (msg) => ({
        ...msg,
        content: "Analyse abgebrochen.",
        streaming: false,
        elapsed: msg.startedAt ? (Date.now() - msg.startedAt) / 1000 : undefined,
      }))
    );
  };

  const handleSend = async (text: string) => {
    if (!sessionId) return;
    noteInteraction("chat");
    recorder.log("chat_sent", { chars: text.length, text });
    setMessages((m) => [
      ...m,
      { role: "user", content: text },
      { role: "assistant", content: "", steps: [], streaming: true, startedAt: Date.now() },
    ]);
    setChatting(true);
    try {
      await chatStream(sessionId, text, {
        onStep: handleStep,
        onDone: (p) => {
          setReportMd(p.report_markdown);
          setImpression(String(p.report?.["impression"] ?? ""));
          setImpressionAuthor(p.impression_author ?? "model");
          setTrace(p.trace);
          setReferenceRanges(p.reference_ranges ?? {}); // chat may set/clear a range
          // A chat turn can trigger a fresh morphometry run; if it did, adopt the new
          // geometry. Otherwise leave the reader's corrections alone.
          if (p.landmarks?.length) setLandmarks(p.landmarks);
          if (p.landmarks_predicted?.length) setLandmarksPredicted(p.landmarks_predicted);
          if (p.landmark_names?.length) setLandmarkNames(p.landmark_names);
          if (p.geometry && Object.keys(p.geometry).length) setGeometry(p.geometry);
          if (p.measurements && Object.keys(p.measurements).length) {
            setMeasurements(p.measurements);
          }
          setLandmarksEdited(!!p.landmarks_edited);
          if (p.processed_image) setProcessedImage(p.processed_image);
          setLateralityCorrected(!!p.laterality_corrected);
          if (p.visualizations?.length) {
            setVisualizations((list) => {
              const names = new Set(p.visualizations.map((v) => v.measurement));
              return [...list.filter((x) => !names.has(x.measurement)), ...p.visualizations];
            });
          }
          setMessages((m) =>
            updateLast(m, (msg) => ({
              ...msg,
              content: p.reply ?? "",
              steps: p.steps,
              streaming: false,
              elapsed: msg.startedAt ? (Date.now() - msg.startedAt) / 1000 : undefined,
            }))
          );
        },
        onError: (d) =>
          setMessages((m) =>
            updateLast(m, (msg) => ({ ...msg, content: `⚠️ ${d}`, streaming: false }))
          ),
      }, thinking);
    } finally {
      setChatting(false);
    }
  };

  // A plugin may replace the whole view (e.g. with a worklist). All hooks above have run.
  if (plugin.takeover) return <>{plugin.takeover}</>;

  return (
    <div className="flex h-screen flex-col bg-slate-50">
      <header className="flex items-center gap-2 border-b border-slate-200 bg-white px-6 py-3">
        <Activity className="h-5 w-5 text-sky-600" />
        {/* "Modul Morphometrie", not "Befundassistent": the app measures and classifies
            against references — it does not do general image interpretation, and the name
            must not promise that. */}
        <h1 className="text-base font-semibold text-slate-800">
          Radiologie-Assistent <span className="font-normal text-slate-500">· Modul Morphometrie</span>
        </h1>
        <div className="ml-auto flex items-center gap-2">
          {capabilities.length > 0 && (
            <button
              onClick={() => setShowCaps((v) => !v)}
              title="Unterstützte Untersuchungen & Messungen"
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-slate-500 hover:bg-slate-100 hover:text-sky-600"
            >
              <Info className="h-4 w-4" /> Unterstützt
            </button>
          )}
          <button
            onClick={() => setThinking((v) => !v)}
            title={
              thinking
                ? "Denkmodus AN: Das Modell überlegt ausführlich (Standardverhalten, langsamer). Klicken zum Abschalten."
                : "Denkmodus AUS: Antworten ohne ausführliche Überlegung — deutlich schneller, bei komplexen Fällen ggf. oberflächlicher. Klicken zum Einschalten."
            }
            className={
              "flex items-center gap-1 rounded-md px-2 py-1 text-xs " +
              (thinking
                ? "text-sky-600 hover:bg-sky-50"
                : "text-slate-400 hover:bg-slate-100")
            }
          >
            <Brain className="h-4 w-4" /> Denkmodus: {thinking ? "an" : "aus"}
          </button>
          <AccuracySelect value={numReferences} onChange={setNumReferences} />
          <ModelSelect profiles={profiles} value={selectedProfile} onChange={setSelectedProfile} />
        </div>
      </header>

      {plugin.banner}

      {showCaps && capabilities.length > 0 && (
        <div className="border-b border-sky-100 bg-sky-50/70 px-6 py-2 text-xs text-slate-600">
          <span className="font-medium text-slate-700">Aktuell unterstützte Morphometrie:</span>
          <ul className="mt-1 space-y-1">
            {capabilities.map((c) => (
              <li key={`${c.anatomy}_${c.projection}`}>
                <span className="font-medium">
                  {de(c.anatomy)} – {de(c.projection)}:
                </span>{" "}
                <span className="text-slate-500">{c.measurements.join(", ") || "—"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-800">
          <AlertTriangle className="h-4 w-4" /> {error}
        </div>
      )}

      <main className="min-h-0 flex-1 p-3">
        <PanelGroup direction="horizontal" className="h-full" autoSaveId="rra-cols">
          {/* Left: case setup + large image */}
          <Panel defaultSize={50} minSize={28}>
            <div className="h-full">
              <SetupPanel
                imageUrl={imageUrl}
                processedImage={processedImage}
                lateralityCorrected={lateralityCorrected}
                onPickImage={pickImage}
                landmarks={landmarks}
                landmarkNames={landmarkNames}
                geometry={geometry}
                measurements={measurements}
                measurementsPredicted={measurementsPredicted}
                landmarksEdited={landmarksEdited}
                activeMeasurement={activeMeasurement}
                onSelectMeasurement={setActiveMeasurement}
                hoveredMeasurement={hoveredMeasurement}
                onHoverMeasurement={setHoveredMeasurement}
                onLandmarksChange={handleLandmarksChange}
                onResetLandmarks={handleResetLandmarks}
                reclassifyResult={reclassifyResult}
                visualizations={visualizations}
                imageView={imageView}
                onSelectView={setImageView}
                capabilities={capabilities}
                registry={registry}
                assessment={assessment}
                pendingAssessment={pendingAssessment}
                onChangeAssessment={handleChangeAssessment}
                lockImage={plugin.lockImage}
                lockAssessment={plugin.lockAssessmentUntilAssessed && !assessment}
                onAnalyze={handleAnalyze}
                onCancelAnalyze={handleCancelAnalyze}
                onApplyCorrection={handleApplyCorrection}
                analyzing={analyzing}
                canEdit={!!sessionId}
              />
            </div>
          </Panel>

          <PanelResizeHandle className="group flex w-3 items-center justify-center">
            <div className="h-12 w-1 rounded-full bg-slate-200 transition-colors group-hover:bg-sky-400" />
          </PanelResizeHandle>

          {/* Right: report (top) + chat (bottom) */}
          <Panel defaultSize={50} minSize={28}>
            <PanelGroup direction="vertical" className="h-full" autoSaveId="rra-right">
              <Panel defaultSize={55} minSize={20}>
                <div className="h-full">
                  <ReportPanel
                    reportMd={reportMd}
                    caseId={caseId}
                    sessionId={sessionId}
                    analyzing={analyzing}
                    measurementNames={
                      Object.keys(geometry).length
                        ? preferredNames(Object.keys(geometry)) // robust wins the link too
                        : visualizations.map((v) => v.measurement)
                    }
                    registry={registry}
                    referenceRanges={referenceRanges}
                    // Stale = the values moved since the report was last written: either
                    // uncorrected edits, or a reset AFTER a check rewrote the report for
                    // the corrected values (landmarksEdited alone misses that case).
                    stale={!reclassifyResult && (landmarksEdited || everChecked)}
                    reclassifyResult={reclassifyResult}
                    reclassifying={reclassifying}
                    onReclassify={handleReclassify}
                    impression={impression}
                    impressionAuthor={impressionAuthor}
                    onSaveImpression={handleSaveImpression}
                    editable={!!sessionId && !analyzing && !reclassifying && !chatting}
                    activeMeasurement={activeMeasurement}
                    hoveredMeasurement={hoveredMeasurement}
                    onSelectMeasurement={handleSelectFromReport}
                    onHoverMeasurement={setHoveredMeasurement}
                  />
                </div>
              </Panel>

              <PanelResizeHandle className="group flex h-3 items-center justify-center">
                <div className="h-1 w-12 rounded-full bg-slate-200 transition-colors group-hover:bg-sky-400" />
              </PanelResizeHandle>

              <Panel defaultSize={45} minSize={20}>
                <div className="flex h-full min-h-0 flex-col gap-2">
                  <div className="min-h-0 flex-1">
                    <ChatPanel
                      messages={messages}
                      onSend={handleSend}
                      pending={analyzing || chatting}
                      disabled={!sessionId}
                    />
                  </div>
                  <TracePanel trace={trace} />
                </div>
              </Panel>
            </PanelGroup>
          </Panel>
        </PanelGroup>
      </main>
    </div>
  );
}
