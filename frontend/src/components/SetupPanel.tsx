import { useRef, useState } from "react";
import { Eye, EyeOff, ImagePlus, Loader2, Play, Square } from "lucide-react";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { Button } from "./ui/button";
import { ViewSelector } from "./ViewSelector";
import { LandmarkCanvas } from "./LandmarkCanvas";
import { MeasurementPanel } from "./MeasurementPanel";
import { AssessmentChips } from "./AssessmentChips";
import type {
  Assessment,
  Capability,
  GeometryMap,
  MeasurementRegistry,
  ReclassifyResult,
} from "../lib/api";

interface Props {
  imageUrl: string | null;
  /** The frame the landmarks live in (the server's processed image). Landmarks and all
   *  geometry are in ITS pixel space, so this — not the raw upload — is what must be
   *  drawn under the overlay whenever it exists. */
  processedImage: string | null;
  lateralityCorrected: boolean;
  onPickImage: (file: File) => void;

  landmarks: number[][] | null;
  landmarkNames: string[];
  geometry: GeometryMap;
  measurements: Record<string, number | null>;
  measurementsPredicted: Record<string, number | null>;
  landmarksEdited: boolean;
  activeMeasurement: string | null;
  onSelectMeasurement: (m: string | null) => void;
  /** Two-way hover link with the report: highlight chips the report points at, and vice versa. */
  hoveredMeasurement: string | null;
  onHoverMeasurement: (m: string | null) => void;
  onLandmarksChange: (landmarks: number[][], commit: boolean) => void;
  onResetLandmarks: () => void;
  reclassifyResult: ReclassifyResult | null;

  /** Legacy static PNGs — only used when the morphometry server predates /recompute. */
  visualizations: { measurement: string; image: string }[];
  imageView: string;
  onSelectView: (view: string) => void;

  /** Region/projection selection, shown left of the analyse button: check it, then run. */
  capabilities: Capability[];
  /** Curated measurement presentation (names, groups, formats). */
  registry: MeasurementRegistry;
  assessment: Assessment | null;
  pendingAssessment: { anatomy: string; projection: string } | null;
  onChangeAssessment: (next: { anatomy: string; projection: string }) => void;

  /** Before the first run: the region/projection chips are locked so the pipeline
   *  assesses the image ITSELF first. A reader who pre-sets them makes the agent skip
   *  assess_image entirely (anatomy_source="user"), and what the model would have answered
   *  is then unrecoverable. Set by a plugin that records the model's own routing. Unlocks
   *  as soon as the first run has produced an assessment, so correcting a misroute stays
   *  possible. */
  lockAssessment?: boolean;

  /** The image comes from a plugin (e.g. a case library), so the picker is HIDDEN —
   *  not disabled. An affordance that can load the wrong film is the hazard; removing it
   *  is what makes a mislabelled record impossible rather than unlikely. */
  lockImage?: boolean;

  onAnalyze: () => void;
  /** Stop button while a run is in progress. */
  onCancelAnalyze: () => void;
  /** Run with the reader's corrected region/projection, cancelling a run in progress
   *  first. Cheap mid-run: the assessment is the pipeline's first step. */
  onApplyCorrection: () => void;
  analyzing: boolean;
  /** A live session exists, so edits can be pushed to the server. */
  canEdit: boolean;
}

export function SetupPanel({
  imageUrl,
  processedImage,
  lateralityCorrected,
  onPickImage,
  landmarks,
  landmarkNames,
  geometry,
  measurements,
  measurementsPredicted,
  landmarksEdited,
  activeMeasurement,
  onSelectMeasurement,
  hoveredMeasurement,
  onHoverMeasurement,
  onLandmarksChange,
  onResetLandmarks,
  reclassifyResult,
  visualizations,
  imageView,
  onSelectView,
  capabilities,
  registry,
  assessment,
  pendingAssessment,
  onChangeAssessment,
  lockImage = false,
  lockAssessment = false,
  onAnalyze,
  onCancelAnalyze,
  onApplyCorrection,
  analyzing,
  canEdit,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  // Reader can blank the overlay for unobstructed reading; zoom/pan stay live.
  const [overlayVisible, setOverlayVisible] = useState(true);
  const baseImage = processedImage || imageUrl;
  const interactive = !!landmarks?.length && Object.keys(geometry).length > 0 && !!baseImage;
  const activeVis = visualizations.find((v) => v.measurement === imageView);

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle>Untersuchung</CardTitle>
      </CardHeader>
      <CardBody className="flex flex-col gap-3">
        {/* Compact controls; the image gets the rest of the panel. Reads left to right as
            the actual workflow: check/correct the region, then analyse. */}
        <div className="flex flex-wrap items-center gap-2">
          <AssessmentChips
            capabilities={capabilities}
            assessment={assessment}
            pending={pendingAssessment}
            onChange={onChangeAssessment}
            disabled={analyzing || !imageUrl || lockAssessment}
          />
          {lockAssessment && imageUrl && (
            <span
              className="text-xs text-slate-400"
              title="Region und Projektion werden zuerst vom System bestimmt. Nach der Analyse können sie korrigiert werden."
            >
              Region/Projektion wird zuerst automatisch bestimmt
            </span>
          )}
          {!imageUrl && !lockImage && (
            <span className="text-xs text-slate-400">Zuerst ein Röntgenbild hochladen.</span>
          )}
          {/* ml-auto: the primary action sits at the right edge of the panel, lining up with
              the top-right corner of the image below it. */}
          {/* While a run is in progress the primary action IS stopping it — a disabled
              "Vermesse…" button left the reader with no way out of a multi-minute run. */}
          {/* THREE states, in priority order:
                1. a run in progress   -> stop it (the chips are locked meanwhile, so a
                                          correction is only possible after interrupting)
                2. a pending correction -> run with it
                3. neither              -> start / restart */}
          {analyzing ? (
            <button
              onClick={onCancelAnalyze}
              title="Analyse abbrechen — der aktuelle Schritt läuft serverseitig noch aus"
              className="ml-auto flex shrink-0 items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm font-medium text-rose-700 hover:bg-rose-100"
            >
              <Loader2 className="h-4 w-4 animate-spin" />
              <Square className="h-3.5 w-3.5 fill-current" /> Abbrechen
            </button>
          ) : pendingAssessment ? (
            <Button
              onClick={onApplyCorrection}
              disabled={!imageUrl}
              className="ml-auto shrink-0"
              title="Analyse mit der korrigierten Region/Projektion starten."
            >
              <Play className="h-4 w-4" /> Mit Auswahl neu berechnen
            </Button>
          ) : canEdit ? (
            /* A case exists: a new analysis DISCARDS it (report, chat, corrections) and
               is easy to hit by accident after editing landmarks. Demoted to a quiet
               secondary action; a confirmation guards it additionally. */
            <button
              onClick={onAnalyze}
              disabled={!imageUrl}
              title="Startet den Fall NEU und verwirft Befundentwurf, Chat und Landmarken-Korrekturen. Korrigierte Werte gelangen über 'Neu prüfen' in den Bericht."
              className="ml-auto flex shrink-0 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-500 hover:bg-slate-100"
            >
              <Play className="h-4 w-4" /> Neue Analyse
            </button>
          ) : (
            <Button onClick={onAnalyze} disabled={!imageUrl} className="ml-auto shrink-0">
              {/* "Bemaßungen", not "Analysieren": the button starts morphometry, not a
                  general diagnostic image analysis. */}
              <Play className="h-4 w-4" /> Bemaßungen vornehmen
            </Button>
          )}
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <input
            ref={inputRef}
            type="file"
            accept="image/png,image/jpeg"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onPickImage(f);
            }}
          />

          {interactive && (
            <MeasurementPanel
              registry={registry}
              geometry={geometry}
              measurements={measurements}
              measurementsPredicted={measurementsPredicted}
              active={activeMeasurement}
              onSelect={onSelectMeasurement}
              hovered={hoveredMeasurement}
              onHover={onHoverMeasurement}
              edited={landmarksEdited}
              onReset={onResetLandmarks}
              result={reclassifyResult}
            />
          )}

          {/* Fallback for a morphometry server without /recompute: static PNG tabs. */}
          {!interactive && imageUrl && visualizations.length > 0 && (
            <ViewSelector
              hasLandmarks={false}
              measurements={visualizations.map((v) => v.measurement)}
              view={imageView}
              onSelect={onSelectView}
            />
          )}

          {imageUrl ? (
            <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-lg border border-slate-200 bg-slate-50">
              {interactive ? (
                <LandmarkCanvas
                  image={baseImage!}
                  landmarks={landmarks!}
                  landmarkNames={landmarkNames}
                  geometry={geometry}
                  activeMeasurement={activeMeasurement}
                  onChange={onLandmarksChange}
                  // Draggable as soon as the geometry lands — deliberately NOT gated on the
                  // run finishing, since the report writing that follows is exactly the wait
                  // we are trying to give the reader something useful to do during.
                  disabled={!canEdit}
                  showOverlay={overlayVisible}
                />
              ) : activeVis ? (
                <img
                  src={activeVis.image}
                  alt={activeVis.measurement}
                  className="h-full w-full object-contain"
                />
              ) : (
                <img
                  src={baseImage ?? undefined}
                  alt="radiograph"
                  className="h-full w-full object-contain"
                />
              )}

              {!lockImage && (
                <button
                  onClick={() => inputRef.current?.click()}
                  title="Bild ändern"
                  className="absolute right-2 top-2 rounded-md bg-white/80 p-1.5 text-slate-600 shadow-sm backdrop-blur hover:bg-white"
                >
                  <ImagePlus className="h-4 w-4" />
                </button>
              )}
              {interactive && (
                <button
                  onClick={() => setOverlayVisible((v) => !v)}
                  title={
                    overlayVisible
                      ? "Overlay ausblenden — Landmarken und Messlinien verbergen, Bild bleibt zoombar"
                      : "Overlay einblenden — Landmarken und Messlinien wieder anzeigen"
                  }
                  className="absolute right-2 top-11 rounded-md bg-white/80 p-1.5 text-slate-600 shadow-sm backdrop-blur hover:bg-white"
                >
                  {overlayVisible ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
                </button>
              )}

              {!interactive && activeVis && (
                <span className="absolute bottom-2 left-2 rounded bg-slate-900/60 px-1.5 py-0.5 text-[11px] text-white">
                  {activeVis.measurement}
                </span>
              )}

              {/* Keyed on the server's flag, not on "did we fetch a processed image" — we
                  now always show the processed frame, mirrored or not. */}
              {lateralityCorrected && (
                <span
                  title="Der Morphometrie-Dienst hat das Bild zur Seitenkorrektur gespiegelt; Ihre Originaldatei bleibt unverändert."
                  className="pointer-events-none absolute right-2 bottom-2 rounded bg-sky-900/60 px-1.5 py-0.5 text-[11px] text-white"
                >
                  seitenkorrigiert (gespiegelt)
                </span>
              )}
            </div>
          ) : lockImage ? (
            <div className="flex min-h-0 w-full flex-1 items-center justify-center rounded-lg border-2 border-dashed border-slate-200 bg-slate-50 text-sm text-slate-400">
              Fallbild wird geladen…
            </div>
          ) : (
            <button
              onClick={() => inputRef.current?.click()}
              className="flex min-h-0 w-full flex-1 items-center justify-center rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 text-slate-400 transition-colors hover:border-sky-400 hover:text-sky-500"
            >
              <span className="flex flex-col items-center gap-2 text-sm">
                <ImagePlus className="h-8 w-8" />
                PNG/JPG hochladen
              </span>
            </button>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
