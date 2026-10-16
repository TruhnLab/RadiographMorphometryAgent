import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

/** Zoom, pan and screen↔image coordinates for a radiograph drawn INSIDE an `<svg>`.
 *
 *  Extracted from LandmarkCanvas so the overlay content (landmarks and construction lines
 *  there, ruler primitives elsewhere) is the only thing that differs between viewers. The
 *  coordinate transform in particular has to be one implementation: a reader clicking the
 *  same spot on the same film must get the same image pixel whichever tool is on top of
 *  it, and a second copy is a second chance to get the CTM subtly wrong.
 *
 *  Drawing the image inside the svg rather than behind it is what makes zoom and pan pure
 *  viewBox arithmetic and keeps the overlay from ever drifting out of registration with
 *  the pixels — the whole point, since the reader is judging geometry against bone.
 *
 *  Returns everything a viewer needs; the viewer owns its own pointer semantics (what a
 *  drag MEANS differs per tool) and calls `beginPan`/`panTo` when it decides a gesture is
 *  a pan.
 */
export function useImageViewport(image: string, svgRef: React.RefObject<SVGSVGElement | null>) {
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [rect, setRect] = useState<{ w: number; h: number } | null>(null); // rendered CSS size
  const [view, setView] = useState<Box | null>(null); // current viewBox (zoom/pan window)
  const panRef = useRef<{ x: number; y: number; box: Box } | null>(null);

  // Natural pixel size of the radiograph = the coordinate space everything is expressed in.
  useEffect(() => {
    const img = new Image();
    img.onload = () => {
      setDims({ w: img.naturalWidth, h: img.naturalHeight });
      setView({ x: 0, y: 0, w: img.naturalWidth, h: img.naturalHeight });
    };
    img.src = image;
  }, [image]);

  // Measured before first paint and tracked with a ResizeObserver. Without this the first
  // frame has no rect and `scale` fell back to 1, i.e. handles were sized in IMAGE pixels
  // until some interaction forced a re-render — oversized blobs on low-res radiographs,
  // near-invisible dots on high-res ones. (Also keeps sizes honest across window resizes.)
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const measure = () => {
      const b = svg.getBoundingClientRect();
      setRect({ w: b.width, h: b.height });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(svg);
    return () => ro.disconnect();
  }, [dims, svgRef]);

  /** Client (screen) coords -> image pixel coords, via the SVG's own transform. */
  const toImage = useCallback(
    (clientX: number, clientY: number) => {
      const svg = svgRef.current;
      const ctm = svg?.getScreenCTM();
      if (!svg || !ctm) return null;
      const pt = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
      return { x: pt.x, y: pt.y };
    },
    [svgRef]
  );

  // Image-pixels per screen-pixel. Handles are sized in SCREEN units, so they stay equally
  // visible however far the reader has zoomed — and, deliberately, they cover FEWER image
  // pixels the more you zoom in, which is what makes precise placement possible. (Sizing
  // them relative to the anatomy instead would keep them a constant blob over the bone: no
  // matter how far you zoomed, you could never place a point more accurately than the dot
  // is wide.) So: zoom in to place precisely; the handle is a grip, not a target.
  const scale = (() => {
    if (!rect || !view || !rect.w || !rect.h) return 1;
    // preserveAspectRatio="meet" fits the LIMITING axis, which for a tall radiograph is the
    // height, not the width. Taking view.w/rect.w alone silently under-scales every
    // screen-relative size (handles, strokes, labels) on portrait images.
    return Math.max(view.w / rect.w, view.h / rect.h);
  })();

  // Visible dot ≈ 8 screen px on a desktop-sized panel, shrinking with the panel (laptop
  // windows, narrow layouts) where a fixed 8 px would swallow the anatomy — but never
  // below 5 px.
  const rScreen = rect ? clamp(0.012 * Math.min(rect.w, rect.h), 5, 9) : 8;
  const stroke = (rScreen / 8) * 1.5 * scale; // line weights follow the dot's panel scaling

  // Wheel zoom, anchored on the cursor so the point under the pointer stays put.
  //
  // Registered as a NATIVE, non-passive listener rather than via React's onWheel: React
  // attaches wheel handlers as passive, so preventDefault() there is silently ignored and
  // the page scrolls away underneath while you are zooming. This is the standard fix for a
  // zoomable canvas — the wheel belongs to the image while the cursor is over it.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      if (!view || !dims) return;
      const p = toImage(e.clientX, e.clientY);
      if (!p) return;
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      const w = clamp(view.w * factor, dims.w / 20, dims.w);
      const h = w * (view.h / view.w);
      setView({
        w,
        h,
        x: p.x - ((p.x - view.x) * w) / view.w,
        y: p.y - ((p.y - view.y) * h) / view.h,
      });
    };
    svg.addEventListener("wheel", handler, { passive: false });
    return () => svg.removeEventListener("wheel", handler);
  }, [view, dims, toImage, svgRef]);

  /** Start a pan from a pointer-down the viewer decided is not a content gesture. */
  const beginPan = useCallback(
    (clientX: number, clientY: number) => {
      if (view) panRef.current = { x: clientX, y: clientY, box: view };
    },
    [view]
  );

  /** Continue a pan. Returns true when it consumed the move. */
  const panTo = useCallback(
    (clientX: number, clientY: number) => {
      const start = panRef.current;
      if (!start) return false;
      const r = svgRef.current?.getBoundingClientRect();
      if (!r) return false;
      const k = start.box.w / r.width;
      setView({
        ...start.box,
        x: start.box.x - (clientX - start.x) * k,
        y: start.box.y - (clientY - start.y) * k,
      });
      return true;
    },
    [svgRef]
  );

  const endPan = useCallback(() => {
    panRef.current = null;
  }, []);

  const panning = () => panRef.current !== null;

  return { dims, rect, view, setView, toImage, scale, rScreen, stroke, beginPan, panTo, endPan, panning };
}
