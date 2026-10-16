import { useRef, useState } from "react";
import { clamp, useImageViewport } from "../shared/useImageViewport";
import type { Geometry, GeometryMap } from "../lib/api";
import { preferredNames } from "../lib/measurements";
import { format } from "./MeasurementPanel";

interface Props {
  image: string; // the radiograph the landmarks live in (the server's processed image)
  landmarks: number[][];
  landmarkNames: string[];
  geometry: GeometryMap;
  activeMeasurement: string | null; // null = show every measurement's construction
  /** Fired continuously while dragging (commit=false) and once on release (commit=true).
   *  The caller decides what to do with the intermediate frames; only the committed one
   *  is authoritative. */
  onChange: (landmarks: number[][], commit: boolean) => void;
  disabled?: boolean;
  /** false hides every drawn layer (landmarks, lines, annotations) — the bare
   *  radiograph stays zoom/pan-able for unobstructed reading. */
  showOverlay?: boolean;
}

/** Interactive landmark overlay.
 *
 *  The radiograph is drawn INSIDE the <svg> rather than behind it, so zoom and pan are
 *  nothing but viewBox arithmetic and the overlay cannot drift out of registration with
 *  the pixels — the whole point, since the reader is judging geometry against bone.
 *
 *  Landmark coordinates are the morphometry service's own image-pixel space, so what we
 *  send back is exactly what it expects. Screen<->image conversion goes through the SVG's
 *  own CTM, which stays correct under any zoom, pan or letterboxing.
 */
export function LandmarkCanvas({
  image,
  landmarks,
  landmarkNames,
  geometry,
  activeMeasurement,
  onChange,
  disabled = false,
  showOverlay = true,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);
  // Where inside the handle the reader grabbed it, in image units. Dragging is relative to
  // this, so the landmark moves WITH the cursor instead of snapping its centre onto it —
  // which matters when the correction being made is only a few pixels.
  const grabRef = useRef<{ dx: number; dy: number }>({ dx: 0, dy: 0 });

  // Zoom, pan and screen<->image coordinates are shared with any other viewer of the same
  // radiograph (see shared/useImageViewport). Only the overlay below is landmark-specific.
  const { dims, view, setView, toImage, scale, rScreen, stroke, beginPan, panTo, endPan } =
    useImageViewport(image, svgRef);
  const r = rScreen * scale;
  const rHit = Math.max(2.2 * rScreen, 14) * scale; // grab target, decoupled from the dot

  const onPointerMove = (e: React.PointerEvent) => {
    if (dragging !== null && !disabled) {
      const p = toImage(e.clientX, e.clientY);
      if (!p || !dims) return;
      const { dx, dy } = grabRef.current;
      const next = landmarks.map((lm, i) =>
        i === dragging
          ? [clamp(p.x - dx, 0, dims.w), clamp(p.y - dy, 0, dims.h)]
          : lm
      );
      onChange(next, false);
      return;
    }
    panTo(e.clientX, e.clientY);
  };

  const endDrag = (e: React.PointerEvent) => {
    if (dragging !== null) {
      onChange(landmarks, true); // authoritative: this is the geometry the reader settled on
      setDragging(null);
    }
    endPan();
    (e.target as Element).releasePointerCapture?.(e.pointerId);
  };

  if (!dims || !view) return null;

  // Which constructions to draw, and which landmarks they depend on. In the "Alle"
  // view, plain twins of robust variants are skipped — their near-identical lines only
  // stacked as ghost geometry.
  const preferredSet = new Set(preferredNames(Object.keys(geometry)));
  const shown = activeMeasurement
    ? Object.entries(geometry).filter(([name]) => name === activeMeasurement)
    : Object.entries(geometry).filter(([name]) => preferredSet.has(name));
  const relevant = new Set<number>();
  for (const [, g] of shown) for (const i of g.landmark_indices ?? []) relevant.add(i);
  // With no measurement selected every landmark is "relevant" — nothing to de-emphasise.
  const highlighting = activeMeasurement !== null && relevant.size > 0;

  const vb = `${view.x} ${view.y} ${view.w} ${view.h}`;

  const isDim = (i: number) => highlighting && !relevant.has(i);

  // How the active measurement's number is BUILT, not just where its parts are: roles
  // (ratio numerator/denominator, angle vertex) recovered from the bare geometry and shown
  // only when the arithmetic reproduces the server's value — see buildAnnotation.
  const activeGeo = activeMeasurement ? geometry[activeMeasurement] : undefined;
  const ann = activeGeo ? buildAnnotation(activeGeo) : null;

  /** Halo'd label, same style as the landmark name labels.
   *
   *  `pin` snaps the label into the visible window: a construction's vertex may
   *  legitimately sit outside the current view (patella tilt), but its VALUE should
   *  always be readable. The clamp is in view coordinates, so panning the true position
   *  back into view releases the label to its natural spot — geometry (arc, rays,
   *  vertex) is never moved, only the text. */
  const label = (
    x: number,
    y: number,
    text: string,
    size = 11,
    anchor: "start" | "middle" | "end" = "middle",
    pin = false
  ) => {
    if (pin && view) {
      const w = text.length * size * 0.62 * scale; // rough rendered text width
      const mx = 8 * scale;
      const my = (size * 0.7 + 6) * scale;
      // How far the text reaches either side of its anchor point — the clamp has to keep
      // the whole box inside the window, not just the anchor.
      const lead = anchor === "start" ? 0 : anchor === "middle" ? w / 2 : w;
      const trail = anchor === "end" ? 0 : anchor === "middle" ? w / 2 : w;
      const left = view.x + mx + lead;
      const right = view.x + view.w - mx - trail;
      x = clamp(x, left, Math.max(left, right));
      y = clamp(y, view.y + my, view.y + view.h - my);
    }
    return (
    <text
      x={x}
      y={y}
      fontSize={size * scale}
      fontWeight={600}
      fill="#F8FAFC"
      stroke="#10151C"
      strokeWidth={3 * scale}
      paintOrder="stroke"
      textAnchor={anchor}
      dominantBaseline="middle"
      style={{ pointerEvents: "none" }}
    >
      {text}
    </text>
    );
  };

  /** Tag beside a line's midpoint, offset perpendicular so it clears the line itself.
   *  `away` (a point) picks the side pointing away from it — keeps the a/b tags out of
   *  the gap between the two ratio lines, where they collide with the other line.
   *  `pin` forwards to label(): used for value texts, never for the bare a/b letters. */
  const lineTag = (l: number[][], text: string, away?: number[], pin = false) => {
    const [p, q] = l;
    const mx = (p[0] + q[0]) / 2;
    const my = (p[1] + q[1]) / 2;
    const L = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1;
    let ox = (-(q[1] - p[1]) / L) * 12 * scale;
    let oy = ((q[0] - p[0]) / L) * 12 * scale;
    if (away && ox * (mx - away[0]) + oy * (my - away[1]) < 0) {
      ox = -ox;
      oy = -oy;
    }
    return label(mx + ox, my + oy, text, 12, "middle", pin);
  };

  // Angle-arc radius, ~40 screen px: thin angles (tibial slope, 10–20°) need a large
  // radius before the two rays visibly separate — at a small one the arc is a sliver
  // buried between them.
  const arcR = 40 * scale;

  /** One landmark: halo, visible dot, invisible grab target, label. All landmarks are
   *  draggable, including ones no displayed measurement uses — the reader corrects the
   *  anatomy, not the formula. */
  const renderLandmark = ([x, y]: number[], i: number) => {
    const dim = isDim(i);
    const active = dragging === i || hovered === i;
    // Dimmed (unused-by-this-measurement) dots recede: smaller and more transparent than
    // the amber ones. Only the VISUAL shrinks — the grab target keeps its full size.
    const rBase = dim ? r * 0.7 : r;
    const rDot = active ? rBase * 1.4 : rBase;
    return (
      <g key={i}>
        {/* Dark halo under every handle: amber alone disappears against bright cortical
            bone, which is exactly where the landmarks sit. */}
        <circle
          cx={x}
          cy={y}
          r={rDot + stroke * 1.6}
          fill="#10151C"
          fillOpacity={dim ? 0.35 : 0.55}
          style={{ pointerEvents: "none" }}
        />
        <circle
          cx={x}
          cy={y}
          r={rDot}
          fill={dim ? "#94A3B8" : "#F5A11E"}
          fillOpacity={dim ? 0.35 : 1}
          stroke="#FFFFFF"
          strokeWidth={stroke}
          strokeOpacity={dim ? 0.5 : 0.9}
          style={{ pointerEvents: "none" }}
        />
        {/* Invisible grab target, larger than the dot: keeps dragging comfortable even
            where the visible dot has shrunk. Overlapping targets resolve to the last-
            drawn (topmost) landmark — zoom in to disambiguate close neighbours. */}
        <circle
          data-testid="landmark"
          data-index={i}
          cx={x}
          cy={y}
          r={rHit}
          fill="transparent"
          className={disabled ? "" : "cursor-grab"}
          style={{ pointerEvents: disabled ? "none" : "all" }}
          onPointerDown={(e) => {
            e.stopPropagation();
            (e.target as Element).setPointerCapture(e.pointerId);
            const p = toImage(e.clientX, e.clientY);
            grabRef.current = p ? { dx: p.x - x, dy: p.y - y } : { dx: 0, dy: 0 };
            setDragging(i);
          }}
          onPointerEnter={() => setHovered(i)}
          onPointerLeave={() => setHovered(null)}
        >
          <title>{landmarkNames[i] ? `${i + 1}. ${landmarkNames[i]}` : `Landmarke ${i + 1}`}</title>
        </circle>
        {active && landmarkNames[i] && (
          <text
            x={x + r * 2}
            y={y - r}
            fontSize={11 * scale}
            fill="#F8FAFC"
            stroke="#10151C"
            strokeWidth={3 * scale}
            paintOrder="stroke"
            style={{ pointerEvents: "none" }}
          >
            {i + 1}. {landmarkNames[i]}
          </text>
        )}
      </g>
    );
  };

  return (
    <svg
      ref={svgRef}
      viewBox={vb}
      preserveAspectRatio="xMidYMid meet"
      className={"h-full w-full touch-none select-none overscroll-contain " + (dragging !== null ? "cursor-grabbing" : "")}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      // NOT onPointerLeave: with pointer capture the cursor may legitimately travel outside
      // the svg mid-drag, and treating that as a release would commit a half-finished
      // correction. pointercancel is the event that actually means "this drag is over".
      onPointerCancel={endDrag}
      onPointerDown={(e) => {
        if (e.target === e.currentTarget || (e.target as Element).tagName === "image") {
          const rect = svgRef.current?.getBoundingClientRect();
          if (rect) beginPan(e.clientX, e.clientY);
        }
      }}
      onDoubleClick={() => setView({ x: 0, y: 0, w: dims.w, h: dims.h })} // reset zoom
    >
      <image href={image} x={0} y={0} width={dims.w} height={dims.h} />

      {showOverlay && (
      <>
      {/* Paint order, bottom to top: dimmed (unused) landmarks, construction lines,
          relevant landmarks. The lines cover the grey dots the measurement does not use,
          but never the amber ones the reader is actually judging and dragging. */}
      {landmarks.map((lm, i) => (isDim(i) ? renderLandmark(lm, i) : null))}

      {/* Construction lines: the same geometry the pipeline's vis_* functions produce,
          recomputed server-side after every edit. pointer-events off — they sit ABOVE the
          dimmed dots and must not swallow their grab targets. */}
      {shown.map(([name, g]) => (
        <g key={name} style={{ pointerEvents: "none" }}>
          {(g.dotted_lines ?? []).map((ln, i) => (
            <line
              key={`d${i}`}
              x1={ln[0][0]}
              y1={ln[0][1]}
              x2={ln[1][0]}
              y2={ln[1][1]}
              stroke="#22C3DD"
              strokeWidth={stroke * 1.2}
              strokeDasharray={`${stroke * 2} ${stroke * 4}`}
              strokeLinecap="round"
              opacity={0.9}
            />
          ))}
          {(g.solid_lines ?? []).map((ln, i) => (
            <line
              key={`s${i}`}
              x1={ln[0][0]}
              y1={ln[0][1]}
              x2={ln[1][0]}
              y2={ln[1][1]}
              stroke="#22C3DD"
              strokeWidth={stroke * 1.6}
              strokeLinecap="round"
              opacity={0.95}
            />
          ))}
        </g>
      ))}

      {/* Relevant landmarks: the amber dots stay on top of the construction lines. */}
      {landmarks.map((lm, i) => (isDim(i) ? null : renderLandmark(lm, i)))}

      {/* Construction semantics: which line is a, which is b, where the angle sits. Only
          rendered when verified against the value (see buildAnnotation) — no match, no
          label, never a guessed one. TOPMOST layer, above the dots: the arc must stay
          visible when the vertex lands near a landmark (posterior slope); pointer-events
          are off, so dragging through it is unaffected. */}
      {ann && (
        <g style={{ pointerEvents: "none" }}>
          {ann.kind === "ratio" &&
            (() => {
              // The value lives IN the image like the angle labels do — but OUTSIDE the
              // construction: beside the two lines' bounding box, vertically centred.
              // Between the lines it sat exactly where the dots and line junctions cluster.
              // No measurement name — the chip already says it.
              //
              // WHICH side: the one whose image margin is nearer — the shorter way out of
              // the anatomy. A patellar-height construction sits close to one edge of a
              // lateral radiograph, so the label lands on the dark background beside it
              // rather than across the femur.
              //
              // Three details keep that from producing occasional ugly frames:
              //  · the comparison uses the IMAGE bounds, never the current view, so panning
              //    cannot make the label hop sides;
              //  · a deadband keeps a roughly centred construction on the right (the angle
              //    labels' canonical side) instead of flipping on a one-pixel drag;
              //  · the nearer margin only wins if the text actually FITS in it. The nearer
              //    margin is by definition the narrower one, so without this the viewport
              //    clamp below would push the label straight back across the construction —
              //    reintroducing the overlap this placement exists to avoid.
              const pts = [...ann.a, ...ann.b];
              const minX = Math.min(...pts.map((p) => p[0]));
              const maxX = Math.max(...pts.map((p) => p[0]));
              const ys = pts.map((p) => p[1]);
              const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
              const valueText = `a/b = ${format(activeGeo!.value, "ratio", activeGeo!.unit)}`;
              // Same width estimate and clearances the pin clamp uses, in image units.
              const need = valueText.length * 11 * 0.62 * scale + 24 * scale;
              const fitsLeft = minX >= need;
              const fitsRight = dims.w - maxX >= need;
              const nearerLeft = minX < dims.w - maxX - 0.02 * dims.w;
              const toLeft = nearerLeft ? fitsLeft || !fitsRight : fitsLeft && !fitsRight;
              const ma = [(ann.a[0][0] + ann.a[1][0]) / 2, (ann.a[0][1] + ann.a[1][1]) / 2];
              const mb = [(ann.b[0][0] + ann.b[1][0]) / 2, (ann.b[0][1] + ann.b[1][1]) / 2];
              return (
                <>
                  {lineTag(ann.a, "a", mb)}
                  {lineTag(ann.b, "b", ma)}
                  {label(
                    toLeft ? minX - 16 * scale : maxX + 16 * scale,
                    cy,
                    valueText,
                    11,
                    toLeft ? "end" : "start",
                    true
                  )}
                </>
              );
            })()}
          {ann.kind === "angle" &&
            (() => {
              const { v, u, w } = ann;
              const cross = u[0] * w[1] - u[1] * w[0];
              const val = activeGeo!.value ?? 0;
              // Label placement: for a THIN wedge (tibial slope, 10–20°) the bisector
              // position sits ON the nearly-parallel rays — put the label outside
              // instead, just past the tip of the w-ray, offset to the side away from
              // the other ray. Wide angles get the classic bisector label.
              let lx: number;
              let ly: number;
              if (Math.abs(val) < 25) {
                const s1 = [w[1], -w[0]];
                const s2 = [-w[1], w[0]];
                const s = s1[0] * u[0] + s1[1] * u[1] < 0 ? s1 : s2;
                lx = v[0] + w[0] * (arcR * 1.5 + 10 * scale) + s[0] * 8 * scale;
                ly = v[1] + w[1] * (arcR * 1.5 + 10 * scale) + s[1] * 8 * scale;
              } else {
                let bx = u[0] + w[0];
                let by = u[1] + w[1];
                const bl = Math.hypot(bx, by);
                if (bl > 1e-6) {
                  bx /= bl;
                  by /= bl;
                } else {
                  bx = -u[1];
                  by = u[0];
                }
                lx = v[0] + bx * (arcR + 14 * scale);
                ly = v[1] + by * (arcR + 14 * scale);
              }
              return (
                <>
                  {/* Extensions to the vertex, when a drawn segment ends short of it. */}
                  {ann.ext.map((l, i) => (
                    <line
                      key={`ext${i}`}
                      x1={l[0][0]}
                      y1={l[0][1]}
                      x2={l[1][0]}
                      y2={l[1][1]}
                      stroke="#22C3DD"
                      strokeWidth={stroke * 1.2}
                      strokeDasharray={`${stroke * 2} ${stroke * 4}`}
                      strokeLinecap="round"
                      opacity={0.75}
                    />
                  ))}
                  {/* The angle is measured against a perpendicular that is not itself a
                      drawn line (tibial-slope style): show it as a dashed ray. */}
                  {ann.helper && (
                    <line
                      x1={v[0]}
                      y1={v[1]}
                      x2={v[0] + w[0] * arcR * 1.5}
                      y2={v[1] + w[1] * arcR * 1.5}
                      stroke="#22C3DD"
                      strokeWidth={stroke * 1.2}
                      strokeDasharray={`${stroke * 2} ${stroke * 4}`}
                      strokeLinecap="round"
                      opacity={0.9}
                    />
                  )}
                  <path
                    d={
                      `M ${v[0] + u[0] * arcR} ${v[1] + u[1] * arcR} ` +
                      `A ${arcR} ${arcR} 0 0 ${cross > 0 ? 1 : 0} ` +
                      `${v[0] + w[0] * arcR} ${v[1] + w[1] * arcR}`
                    }
                    fill="none"
                    stroke="#22C3DD"
                    strokeWidth={stroke * 1.6}
                    opacity={0.95}
                  />
                  {/* Vertex marker: where the (possibly extended) lines actually meet. */}
                  <circle cx={v[0]} cy={v[1]} r={stroke * 1.5} fill="#22C3DD" opacity={0.95} />
                  {label(lx, ly, format(activeGeo!.value, "angle", activeGeo!.unit), 11, "middle", true)}
                </>
              );
            })()}
          {ann.kind === "distance" &&
            lineTag(ann.line, format(activeGeo!.value, "distance", activeGeo!.unit), undefined, true)}
        </g>
      )}
      </>
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------------------
// Construction semantics, recovered from bare geometry.
//
// The server's geometry payload is just lines — it does not say which one is the ratio's
// numerator or where an angle's vertex is. The roles are recoverable by arithmetic, and
// verifiably so: a candidate labelling is accepted ONLY if it reproduces the server's own
// value (ratio within 2 %, angle within 1°). No match → no annotation. Same discipline as
// the backend's exact-only measurement-name matching: a plausible-but-wrong label is worse
// than none.

type Line = number[][];

type Annotation =
  | { kind: "ratio"; a: Line; b: Line }
  // Vertex + the two unit rays spanning the angle. `helper` = w is a constructed
  // perpendicular (not a drawn line), rendered as a dashed ray so the reader sees what
  // the angle is actually measured against (e.g. tibial slope vs the tibial axis).
  // `ext` = dashed extension segments connecting each drawn line to the vertex when the
  // vertex lies beyond its end — without them the arc floats in space, visibly detached
  // from the construction it belongs to (tibial slope: the axis is drawn mid-shaft, the
  // vertex is up at the plateau).
  | { kind: "angle"; v: number[]; u: number[]; w: number[]; helper: boolean; ext: Line[] }
  | { kind: "distance"; line: Line };

function lineLen(l: Line): number {
  return Math.hypot(l[1][0] - l[0][0], l[1][1] - l[0][1]);
}

function buildAnnotation(g: Geometry): Annotation | null {
  const value = g.value;
  if (value == null || !Number.isFinite(value)) return null;
  const lines = [
    ...(g.solid_lines ?? []).map((pts) => ({ pts, dotted: false })),
    ...(g.dotted_lines ?? []).map((pts) => ({ pts, dotted: true })),
  ].filter((l) => l.pts?.length === 2);

  if (g.kind === "ratio") {
    let best: { a: Line; b: Line; err: number } | null = null;
    for (const { pts: a } of lines)
      for (const { pts: b } of lines) {
        if (a === b) continue;
        const lb = lineLen(b);
        if (lb < 1e-6) continue;
        const err = Math.abs(lineLen(a) / lb - value) / Math.max(Math.abs(value), 1e-6);
        if (err < 0.02 && (!best || err < best.err)) best = { a, b, err };
      }
    return best && { kind: "ratio", a: best.a, b: best.b };
  }

  if (g.kind === "angle") {
    // Angles can be SIGNED (congruence angle: negative when the patellar ridge sits
    // medial to the sulcus bisector) — geometry recovery matches the magnitude, the
    // label still shows the signed value.
    const av = Math.abs(value);
    interface Cand {
      v: number[];
      u: number[];
      w: number[];
      err: number;
      helper: boolean;
      axisDotted: boolean;
      axisLen: number;
      axisGap: number; // distance from the vertex to the axis SEGMENT (0 = vertex on it)
      pair: [Line, Line];
    }
    const cands: Cand[] = [];
    for (let i = 0; i < lines.length; i++)
      for (let j = i + 1; j < lines.length; j++) {
        const A = lines[i];
        const B = lines[j];
        const v = intersect(A.pts, B.pts);
        if (!v) continue;
        // Rays point toward each line's FARTHER endpoint — the angle a viewer perceives
        // between the drawn segments, not its supplement.
        const uA = rayToFarther(v, A.pts);
        const uB = rayToFarther(v, B.pts);
        if (!uA || !uB) continue;
        const deg = degBetween(uA, uB);
        const pair: [Line, Line] = [A.pts, B.pts];
        if (Math.abs(deg - av) < 1)
          cands.push({
            v, u: uA, w: uB,
            err: Math.abs(deg - av), helper: false,
            axisDotted: false, axisLen: 0, axisGap: 0, pair,
          });
        else if (Math.abs(180 - deg - av) < 1)
          cands.push({
            v, u: uA, w: [-uB[0], -uB[1]],
            err: Math.abs(180 - deg - av), helper: false,
            axisDotted: false, axisLen: 0, axisGap: 0, pair,
          });
        // The measurement may live against the PERPENDICULAR of one line — tibial slope:
        // plateau tangent vs the perpendicular of the tibial axis (= 90° − drawn angle).
        // Verified against the real service: the three slope measurements only match this
        // way. Both role assignments are tried; the sort below breaks the (mathematically
        // exact) tie.
        for (const [rayU, axis] of [
          [uA, B],
          [uB, A],
        ] as const) {
          const ad = unit([axis.pts[1][0] - axis.pts[0][0], axis.pts[1][1] - axis.pts[0][1]]);
          if (!ad) continue;
          const p1 = [-ad[1], ad[0]];
          const p2 = [ad[1], -ad[0]];
          // CANONICAL side, fixed in the image frame (larger x, tiebreak larger y) — not
          // "nearest to the ray". Related measurements share a (near-)parallel axis (the
          // three tibial slopes), and the nearest-to-ray choice mirrored the construction
          // per measurement depending on where the vertex happened to land along the
          // tangent: same definition, three different-looking drawings.
          const w = p1[0] > p2[0] || (p1[0] === p2[0] && p1[1] > p2[1]) ? p1 : p2;
          // The tangent ray on the SAME side as the perpendicular, so the arc spans the
          // acute gap between them.
          const u2 = rayU[0] * w[0] + rayU[1] * w[1] >= 0 ? rayU : [-rayU[0], -rayU[1]];
          const pdeg = degBetween(u2, w);
          // Distance from the vertex to the axis SEGMENT (the vertex is always on its
          // infinite line): the anatomical axis is the one drawn AWAY from the vertex
          // (tibial shaft), while the tangent contains it.
          const [ap, aq] = axis.pts;
          const adx = aq[0] - ap[0];
          const ady = aq[1] - ap[1];
          const at = ((v[0] - ap[0]) * adx + (v[1] - ap[1]) * ady) / Math.max(adx * adx + ady * ady, 1e-9);
          const axisGap =
            at >= 0 && at <= 1
              ? 0
              : Math.min(Math.hypot(v[0] - ap[0], v[1] - ap[1]), Math.hypot(v[0] - aq[0], v[1] - aq[1]));
          if (Math.abs(pdeg - av) < 1)
            cands.push({
              v, u: u2, w,
              err: Math.abs(pdeg - av), helper: true,
              axisDotted: axis.dotted, axisLen: lineLen(axis.pts), axisGap, pair,
            });
        }
      }
    // Fan construction: the value is the angle between one ray and the BISECTOR of the
    // other two — the congruence angle (patellar-ridge ray vs the sulcus bisector,
    // value = sulcus/2 − center). The bisector is not a drawn line, so the pairwise
    // search above cannot find it. The morphometry server's own PNG renderer handles
    // exactly this case; verified against the live service (err = 0.000).
    if (lines.length === 3) {
      const vtx = commonVertex(lines.map((l) => l.pts));
      const rays = vtx
        ? lines
            .map((l) => rayToFarther(vtx, l.pts))
            .filter((r): r is number[] => r !== null)
        : [];
      if (vtx && rays.length === 3) {
        for (let k = 0; k < 3; k++) {
          const [o1, o2] = rays.filter((_, m) => m !== k);
          for (const sgn of [1, -1]) {
            const bis = unit([sgn * (o1[0] + o2[0]), sgn * (o1[1] + o2[1])]);
            if (!bis) continue;
            const deg = degBetween(bis, rays[k]);
            if (Math.abs(deg - av) < 1)
              cands.push({
                v: vtx, u: rays[k], w: bis, // w dashed via `helper`, like the perpendicular
                err: Math.abs(deg - av), helper: true,
                axisDotted: false, axisLen: 0, axisGap: 0,
                pair: [lines[k].pts, lines[(k + 1) % 3].pts], // both touch vtx -> no ext
              });
          }
        }
      }
    }
    // Errors compare with a tolerance: the two perpendicular role assignments match with
    // the SAME error (the arithmetic is symmetric), so preference decides — a direct
    // construction beats a helper one; among helpers the axis is the segment drawn AWAY
    // from the vertex (largest gap — the shaft axis, verified: length/dottedness alone
    // picked a different axis per tibial slope), then the dotted line, then the longer.
    cands.sort((a, b) => {
      const de = a.err - b.err;
      if (Math.abs(de) > 1e-6) return de;
      return (
        Number(a.helper) - Number(b.helper) ||
        b.axisGap - a.axisGap ||
        Number(b.axisDotted) - Number(a.axisDotted) ||
        b.axisLen - a.axisLen
      );
    });
    const best = cands[0];
    if (!best) return null;
    // Dashed extensions: connect each drawn segment to the vertex when it lies beyond an
    // end (verified visually against a real case — without these, the slope arcs float).
    const ext: Line[] = [];
    for (const pts of best.pair) {
      const [p, q] = pts;
      const dx = q[0] - p[0];
      const dy = q[1] - p[1];
      const L2 = dx * dx + dy * dy;
      if (L2 < 1e-9) continue;
      const t = ((best.v[0] - p[0]) * dx + (best.v[1] - p[1]) * dy) / L2;
      if (t >= 0 && t <= 1) continue; // vertex on the segment: nothing to extend
      ext.push([t < 0 ? p : q, best.v]);
    }
    return { kind: "angle", v: best.v, u: best.u, w: best.w, helper: best.helper, ext };
  }

  if (g.kind === "distance") {
    // Only unambiguous when there is exactly one solid (measured) segment.
    const solids = (g.solid_lines ?? []).filter((l) => l?.length === 2);
    if (solids.length === 1) return { kind: "distance", line: solids[0] };
  }
  return null;
}

/** A point shared by every segment (the apex of a fan of rays), or null. */
function commonVertex(segs: Line[]): number[] | null {
  for (const c of [segs[0][0], segs[0][1]]) {
    if (segs.every((s) => s.some((p) => Math.hypot(p[0] - c[0], p[1] - c[1]) < 0.5)))
      return c;
  }
  return null;
}

/** Intersection of the two infinite lines, or null when near-parallel (unstable vertex). */
function intersect(l1: Line, l2: Line): number[] | null {
  const d1 = unit([l1[1][0] - l1[0][0], l1[1][1] - l1[0][1]]);
  const d2 = unit([l2[1][0] - l2[0][0], l2[1][1] - l2[0][1]]);
  if (!d1 || !d2) return null;
  const denom = d1[0] * d2[1] - d1[1] * d2[0]; // sin of the angle between the lines
  if (Math.abs(denom) < 1e-3) return null;
  const t = ((l2[0][0] - l1[0][0]) * d2[1] - (l2[0][1] - l1[0][1]) * d2[0]) / denom;
  return [l1[0][0] + t * d1[0], l1[0][1] + t * d1[1]];
}

function degBetween(u: number[], w: number[]): number {
  return (Math.acos(clamp(u[0] * w[0] + u[1] * w[1], -1, 1)) * 180) / Math.PI;
}

function rayToFarther(v: number[], l: Line) {
  const [p, q] = l;
  const far =
    Math.hypot(p[0] - v[0], p[1] - v[1]) > Math.hypot(q[0] - v[0], q[1] - v[1]) ? p : q;
  return unit([far[0] - v[0], far[1] - v[1]]);
}

function unit(d: number[]): number[] | null {
  const L = Math.hypot(d[0], d[1]);
  return L < 1e-6 ? null : [d[0] / L, d[1] / L];
}
