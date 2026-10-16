"""FastAPI backend wrapping the (unchanged) radiology agent.

The agent core lives in agent.py / tools / llm / config and is UI-agnostic.
This layer just holds AgentSession objects in memory and exposes them over HTTP so
the React frontend can drive them.

Endpoints:
  - GET  /api/profiles                    -> list .env profiles + which are live
  - POST /api/analyze_stream              -> Server-Sent Events; each agent step
  - POST /api/chat_stream                    is pushed as it happens, then a
                                             final "done" event with the report

Optional extensions in plugins/ can add their own routes at startup (plugins/__init__.py).

Run:  uvicorn api:app --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
import warnings

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import plugins
from agent import AgentSession
from config import Config
from tools import measurement_registry, morphometry_client

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

app = FastAPI(title="Radiology Report Assistant API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local dev; tighten for any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOADS = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOADS, exist_ok=True)

# Live AgentSessions keyed by an opaque session id handed to the frontend.
SESSIONS: dict[str, AgentSession] = {}

# Cleanup: sweep once a day, drop sessions + their uploaded images idle > TTL.
# Bounds both memory (SESSIONS) and disk (uploads/); keeps patient data from
# lingering on the host. Tunable via env.
_REAP_INTERVAL_S = int(os.environ.get("REAP_INTERVAL_HOURS", "24")) * 3600
_UPLOAD_TTL_S = int(os.environ.get("UPLOAD_TTL_HOURS", "12")) * 3600


def _reap_loop() -> None:
    while True:
        time.sleep(_REAP_INTERVAL_S)
        cutoff = time.time() - _UPLOAD_TTL_S
        # idle sessions -> remove session + its image
        for sid, session in list(SESSIONS.items()):
            if getattr(session, "last_active", 0) < cutoff:
                # ONLY EVER FROM uploads/. A session's image_path is a copy made by
                # analyze_stream, so this is true today by construction — but the same
                # AgentSession is also built in the headless runner directly on a dataset
                # file, and the cost of those two ever meeting is deleted patient data.
                # The dataset is read-only to this codebase; that has to be enforced where
                # the deleting happens, not remembered where the sessions are made.
                path = os.path.abspath(getattr(session, "image_path", "") or "")
                if path and os.path.commonpath([path, os.path.abspath(UPLOADS)]) == os.path.abspath(UPLOADS):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                SESSIONS.pop(sid, None)
        # orphan upload files (no live session) older than the TTL
        for name in os.listdir(UPLOADS):
            path = os.path.join(UPLOADS, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


threading.Thread(target=_reap_loop, daemon=True).start()


# The full case payload lives on the session (AgentSession.to_payload), shared with
# headless batch runs — one definition for both the UI and batch output.
def _report_payload(session: AgentSession) -> dict:
    return session.to_payload()


_CAPABILITIES_CACHE: dict | None = None
_CAPABILITIES_TS = 0.0
_CAPABILITIES_TTL = 600  # re-fetch at most every 10 min -> new server modes appear w/o restart


@app.get("/api/capabilities")
def capabilities() -> dict:
    """What the morphometry service currently supports (anatomy/projection + measurement
    names), live from the server. Cached with a short TTL so new modes surface on their own."""
    global _CAPABILITIES_CACHE, _CAPABILITIES_TS
    now = time.time()
    if _CAPABILITIES_CACHE is None or now - _CAPABILITIES_TS > _CAPABILITIES_TTL:
        combos = morphometry_client.capabilities()
        # Never cache a failure. capabilities() swallows connection errors and returns [],
        # which is indistinguishable here from "the service genuinely offers nothing" — and
        # caching it would keep the region/projection chips empty (and unusable) for the
        # full TTL after the service comes back. Serve the last good list if we have one and
        # leave the timestamp alone, so the very next request retries.
        if combos:
            _CAPABILITIES_CACHE = {"morphometry": combos}
            _CAPABILITIES_TS = now
        elif _CAPABILITIES_CACHE is None:
            # nothing known yet; don't poison the cache — but the (static) registry is
            # always available
            return {"morphometry": [], "registry": measurement_registry.ui_registry()}
    # Each combo's measurement list is passed through the registry allowlist and mapped to
    # curated German display names (robust variants collapsed), so the "Unterstützt" panel
    # shows exactly the measurements the pipeline actually uses — not everything the tool
    # happens to offer. Done at RESPONSE time so the cache keeps the raw service list.
    combos = [
        {
            **c,
            "measurements": measurement_registry.supported_display(c.get("measurements", [])),
            # Pixel spacing rides along so a client measuring MANUALLY (e.g. a ruler tool) can
            # convert its distances to millimetres on the same scale the pipeline uses,
            # without ever calling the morphometry service.
            "mpp": morphometry_client.mpp_for(c.get("anatomy", ""), c.get("projection", "")),
        }
        for c in _CAPABILITIES_CACHE.get("morphometry", [])
    ]
    # The curated measurement registry rides along: display names, clinical grouping and
    # decimal places for the frontend (single source of truth with the report renderer).
    return {"morphometry": combos, "registry": measurement_registry.ui_registry()}


@app.get("/api/profiles")
def list_profiles() -> dict:
    """Profiles from .env, each flagged with whether its model is currently served.

    Liveness comes from each endpoint's OpenAI-style /models listing, asked once per
    distinct endpoint, so profiles may point at different servers."""
    names = Config.list_profiles()
    cfgs: dict[str, Config | None] = {}
    for name in names:
        try:
            cfgs[name] = Config.load(name)
        except Exception:
            cfgs[name] = None

    served: dict[tuple, set] = {}
    for cfg in cfgs.values():
        if cfg is None:
            continue
        key = (cfg.endpoint, cfg.api_key, cfg.verify_ssl)
        if key in served:
            continue
        served[key] = set()
        try:
            base = cfg.endpoint.rsplit("/chat/completions", 1)[0]
            resp = requests.get(
                base + "/models",
                headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
                verify=cfg.verify_ssl,
                timeout=10,
            )
            served[key] = {m.get("id") for m in resp.json().get("data", [])}
        except Exception:
            pass  # endpoint unreachable -> its profiles just aren't flagged live

    profiles = []
    for name in names:
        cfg = cfgs[name]
        live = cfg is not None and cfg.model in served.get(
            (cfg.endpoint, cfg.api_key, cfg.verify_ssl), set()
        )
        profiles.append({
            "name": name,
            "model": cfg.model if cfg else "",
            "label": cfg.label if cfg else "",
            "live": live,
        })
    return {"profiles": profiles, "active": os.environ.get("ACTIVE_PROFILE", "").lower()}


class ChatIn(BaseModel):
    session_id: str
    message: str
    # Denkmodus at the moment of sending; None = leave the session's setting alone.
    thinking: bool | None = None


# --- Streaming endpoints (Server-Sent Events) ------------------------------
# Each agent step is pushed as it happens, so the UI can show reasoning live.

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _stream_run(session: AgentSession, work, first: dict | None = None) -> StreamingResponse:
    """Run `work(session)` in a thread, streaming its steps then a final event.

    `work` returns the dict to send as the final "done" payload. `first`, if given, is sent
    before any step — analyze uses it to hand over the session id up front, so the reader can
    already drag landmarks (which needs the id) while the agent is still writing the report.
    """
    events: "queue.Queue" = queue.Queue()
    session.on_step = lambda step: events.put({"event": "step", "step": step})

    def worker() -> None:
        # The reader is blocked on the model for exactly this span. When the session is
        # recorded, bracketing it server-side rather than in the browser keeps the boundary
        # on the clock that also timestamps the agent's own events — no offset correction
        # between a wait and the steps inside it.
        if session.recorder is not None:
            session.recorder.log("model_wait_start", {})
        try:
            payload = work(session)
            events.put({"event": "done", "payload": payload})
        except Exception as exc:
            events.put({"event": "error", "detail": str(exc)})
        finally:
            if session.recorder is not None:
                session.recorder.log("model_wait_end", {})
            session.snapshot("turn_complete")
            session.on_step = None
            events.put(None)  # sentinel: stream complete

    def generate():
        if first is not None:
            yield _sse(first)
        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = events.get()
            if item is None:
                break
            yield _sse(item)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/analyze_stream")
def analyze_stream(
    image: UploadFile = File(...),
    profile: str = Form(...),
    num_references: int = Form(0),  # 0 -> server default (MORPHO_NUM_REFERENCES)
    # Reader-supplied region/projection (canonical keys, e.g. "knee"/"lateral"). Empty ->
    # the model assesses the image itself. Set when the reader corrected the selection and
    # asked for a re-run; the agent then skips its own assessment.
    anatomy: str = Form(""),
    projection: str = Form(""),
    # Denkmodus (header toggle): "false" disables the reasoning models' chain-of-thought
    # for this session's pipeline calls. Default on = native model behaviour.
    thinking: str = Form("true"),
    # Recorded session to bind this run to, when an extension provides one
    # (plugins.Host.recorder_lookup). Passed HERE rather than in a follow-up call because
    # the analysis worker starts at once: a later bind would miss the first turn's
    # model_wait_start, so the longest wait of the case would go unmeasured.
    recorder_session: str = Form(""),
) -> StreamingResponse:
    sid = uuid.uuid4().hex
    ext = os.path.splitext(image.filename or "")[1] or ".png"
    path = os.path.join(UPLOADS, sid + ext)
    with open(path, "wb") as f:
        shutil.copyfileobj(image.file, f)

    try:
        cfg = Config.load(profile)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session = AgentSession(cfg, path, num_references or None, anatomy.strip(), projection.strip())
    session.thinking = thinking.strip().lower() != "false"
    uid = recorder_session.strip()
    recorder = _HOST.recorder_lookup(uid) if uid and _HOST.recorder_lookup else None
    if recorder is not None:
        session.attach_recorder(recorder, sid)
    SESSIONS[sid] = session

    def work(s: AgentSession) -> dict:
        s.run_analysis()
        return {"session_id": sid, "reply": s.last_reply, **_report_payload(s)}

    return _stream_run(session, work, first={"event": "session", "session_id": sid})


@app.get("/api/report.docx")
def report_docx(session: str):
    """Download the session's current report as a Word (.docx) file."""
    s = SESSIONS.get(session)
    if s is None or s.report is None:
        raise HTTPException(status_code=404, detail="kein Bericht für diese Sitzung")
    return Response(
        content=s.report.to_docx_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="Radiologiebericht_{s.case_id}.docx"'
        },
    )


@app.get("/api/report.csv")
def report_csv(session: str):
    """Download the session's measurement table as CSV (German Excel conventions).

    One row per displayed measurement — value, verdict, reference range and its source —
    with the Fall-ID on every row, so files from several cases concatenate cleanly."""
    s = SESSIONS.get(session)
    if s is None or not (s.measurements or s.measurements_predicted):
        raise HTTPException(status_code=404, detail="keine Messwerte für diese Sitzung")
    s.last_active = time.time()
    # Ranges/verdicts come from the background baseline classification — wait for it
    # briefly so an early export doesn't ship a table full of "unbekannt".
    s.wait_for_baseline(timeout=60)
    return Response(
        content=s.to_csv_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="Morphometrie_{s.case_id}.csv"'
        },
    )


# --- Reader-in-the-loop landmark refinement --------------------------------
# These are deliberately LLM-free and synchronous: they sit inside a drag loop, so they
# must be fast. Every value they return is recomputed server-side by the morphometry
# pipeline's own (validated) measurement code — never by the browser.

class SessionIn(BaseModel):
    session_id: str


class LandmarksIn(BaseModel):
    session_id: str
    landmarks: list[list[float]]
    # True on pointer-up (the reader settled on this geometry) -> writes an audit event.
    # False for the intermediate frames of a drag.
    commit: bool = False


def _session_or_404(session_id: str) -> AgentSession:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown or expired session")
    session.last_active = time.time()
    return session


class ImpressionIn(BaseModel):
    session_id: str
    impression: str


@app.post("/api/report/impression")
def edit_impression(body: ImpressionIn) -> dict:
    """Reader-edited Beurteilung (editable Befundentwurf). Audited; marks the impression
    user-authored, which later model rewrites must preserve (minimal-diff contract)."""
    session = _session_or_404(body.session_id)
    if session.report is None:
        raise HTTPException(status_code=400, detail="Kein Bericht vorhanden.")
    session.set_impression(body.impression)
    return _report_payload(session)


@app.post("/api/cancel")
def cancel(body: SessionIn) -> dict:
    """Request cooperative cancellation of the session's running analysis/chat turn.
    Takes effect at the next step boundary; the UI detaches immediately regardless."""
    session = _session_or_404(body.session_id)
    session.cancel_requested = True
    return {"status": "ok"}


@app.post("/api/landmarks")
def update_landmarks(body: LandmarksIn) -> dict:
    """Recompute measurements + construction geometry from reader-edited landmarks."""
    session = _session_or_404(body.session_id)
    result = session.update_landmarks(body.landmarks, commit=body.commit)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("message", "recompute failed"))
    return {**result, "recheck": session.recheck_outcome}


@app.post("/api/landmarks/reset")
def reset_landmarks(body: SessionIn) -> dict:
    """Discard the reader's corrections; restore the predicted geometry."""
    session = _session_or_404(body.session_id)
    result = session.reset_landmarks()
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("message", "reset failed"))
    return {**result, "recheck": session.recheck_outcome}


@app.post("/api/reclassify_stream")
def reclassify_stream(body: ChatIn) -> StreamingResponse:
    """Re-check the report after the reader corrected landmarks.

    Two things happen, in order, and both matter:
      1. reclassify() — the deterministic, guideline-grounded label comparison that tells
         whether the correction changed the diagnostic label (and that goes to the audit log).
      2. a normal chat turn, so the agent visibly reasons about the corrected numbers and
         updates the report if they warrant it. Streaming it means the reader watches this
         happen rather than waiting on a silent request.
    """
    session = _session_or_404(body.session_id)
    if body.thinking is not None:
        session.thinking = body.thinking

    def work(s: AgentSession) -> dict:
        s.last_steps = []  # one turn: the label comparison AND the report decision
        outcome = s.recheck_report()
        return {"reply": s.last_reply, "reclassify": outcome, **_report_payload(s)}

    return _stream_run(session, work)


@app.post("/api/chat_stream")
def chat_stream(body: ChatIn) -> StreamingResponse:
    session = SESSIONS.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown or expired session")
    session.last_active = time.time()
    if body.thinking is not None:
        session.thinking = body.thinking  # toggle applies from this turn on

    def work(s: AgentSession) -> dict:
        reply = s.chat(body.message)
        return {"reply": reply, **_report_payload(s)}

    return _stream_run(session, work)


# --- Serve the built frontend (production) ------------------------------------
# When frontend/dist exists (after `npm run build`), serve the SPA from the same
# origin as the API. In dev this dir is absent and the Vite server is used instead.
# Mounted LAST so /api/* routes take precedence.
_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")


class _SpaStatics(StaticFiles):
    """The built SPA, with cache headers that survive a redeploy.

    Vite content-hashes every bundle, so /assets/* can be cached forever. index.html must
    NOT be: it is the only file whose name stays the same across builds, and a browser that
    reuses a stale copy keeps pointing at the PREVIOUS build's hashed assets, which it also
    still holds — so the reader runs the old app against the new backend with nothing to
    show that anything is wrong. Without an explicit header the browser is free to invent a
    freshness lifetime from Last-Modified, which is exactly what it does here (the file's
    mtime is the image build, days old), so a redeploy can go unnoticed for a day.

    "no-cache" does not forbid caching, it forbids reusing the copy without asking: the
    ETag still turns the check into a 304 and nothing is re-downloaded when the build is
    unchanged.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        path = str(getattr(response, "path", "")).replace("\\", "/")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if "/assets/" in path else "no-cache"
        )
        return response


# --- Extensions -----------------------------------------------------------------
# Installed after the core routes and BEFORE the SPA mount, so an extension's routes (and
# any client-side route it serves) take precedence over the static files.
_HOST = plugins.Host(sessions=SESSIONS, session_or_404=_session_or_404, dist_dir=_DIST)
plugins.install_all(app, _HOST)

if os.path.isdir(_DIST):
    app.mount("/", _SpaStatics(directory=_DIST, html=True), name="frontend")
