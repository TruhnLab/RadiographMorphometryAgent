"""Client for the morphometry service (landmark matching on a GPU, self-hosted).

The reference implementation is the MCP server of RadiographLandmarkMatching
(https://github.com/TruhnLab/RadiographLandmarkMatching, docker/). Any service that speaks
the same protocol works:

  POST /upload                 raw image bytes -> {"image_token"}
  POST /recompute              landmarks -> measurements + construction geometry (optional;
                               without it, landmarks are shown but cannot be dragged)
  MCP  morphometry_health      supported {anatomy, projection, measurements, landmark_names}
  MCP  get_morphometry         landmarks + measurements for an uploaded image
  MCP  visualize_measurement   a rendered measurement (fallback for servers without /recompute)
  MCP  get_processed_image     the prepared image the landmarks refer to

Config (.env): MORPHO_URL, MORPHO_API_KEY (sent as X-API-Key), MORPHO_NUM_REFERENCES,
MORPHO_VERIFY_SSL (falls back to VERIFY_SSL; false for a self-signed certificate).
On any failure or unsupported view, returns a dict with a non-"ok" `status` so the
agent can adapt (it never raises).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import warnings

import requests

import config  # noqa: F401  (loads .env)

warnings.filterwarnings("ignore", message="Unverified HTTPS request")


def _verify() -> bool:
    """TLS verification for the service. The reference deployment serves a self-signed
    certificate, for which MORPHO_VERIFY_SSL (or VERIFY_SSL) must be false."""
    value = os.environ.get("MORPHO_VERIFY_SSL") or os.environ.get("VERIFY_SSL") or "true"
    return value.strip().lower() == "true"


async def _health_async(base: str, key: str) -> list:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(**kw):
        return httpx.AsyncClient(verify=_verify(), **kw)

    async with streamablehttp_client(
        f"{base}/mcp", headers={"X-API-Key": key}, httpx_client_factory=factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("morphometry_health", {})
            return json.loads(res.content[0].text).get("supported", [])


# Millimetres per pixel, per mode. Mirrors the morphometry service's own job config
# (experiment_config_template.json), where mpp is a constant per mode rather than a
# per-image value.
#
# Served with the capabilities so that a client measuring on its own (e.g. with a ruler
# tool) works on the same scale as the service; otherwise a millimetre would not mean the
# same thing in both. Angles and ratios are scale-free and unaffected either way.
#
# Consequence worth stating: absolute millimetres inherit the service's assumption that a
# mode has one pixel spacing. That is the pipeline's own basis; it is not a claim about any
# individual radiograph's true spacing.
MPP_BY_MODE: dict[str, float] = {
    "knee_ap": 0.148,
    "knee_axial": 0.148,
    "knee_lateral": 0.148,
    "feet_ap": 0.15,
    "feet_lateral": 0.15,
    "shoulder_ap": 0.143,
    "shoulder_y": 0.143,
    "hip_ap": 0.143,
}


def mpp_for(anatomy: str, projection: str) -> float | None:
    """Pixel spacing for a combination, or None when the mode is unknown to us.

    None rather than a default: a guessed scale would silently produce plausible, wrong
    millimetres; a client shows "-" instead.
    """
    return MPP_BY_MODE.get(f"{(anatomy or '').strip().lower()}_{(projection or '').strip().lower()}")


def _landmark_names(entry: dict) -> list[str]:
    """Normalize the server's landmark_names to a flat list of display names.

    The pipeline stores them as kpt_names_<mode> = [['1', 'Patella, base'], ...]; the
    server may ship that verbatim, as dicts, or as plain strings. Index = position."""
    names = []
    for item in entry.get("landmark_names") or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.append(str(item.get("name") or item.get("label") or ""))
        elif isinstance(item, (list, tuple)):  # ['1', 'Patella, base (upper border)']
            names.append(str(item[-1]) if item else "")
    return names


# The capability list changes only when the service is redeployed, but run_analysis asks
# for it at the start of EVERY case. That round trip is normally ~200ms — and much worse
# exactly when it hurts most: while the service is busy (e.g. still finishing a landmark
# match from a run the reader just cancelled), the reader is left waiting before the
# pipeline has even reported the region. Cached briefly so a new run does not queue behind
# the old one's GPU work just to re-learn something that did not change.
_CAPS_CACHE: list[dict] = []
_CAPS_TS = 0.0
_CAPS_TTL = float(os.environ.get("MORPHO_CAPS_TTL", "300"))
_CAPS_LOCK = threading.Lock()


def capabilities(force: bool = False) -> list[dict]:
    """Supported {anatomy, projection, measurements, landmark_names} combos from the
    server's morphometry_health. Tolerates the legacy list-of-strings shape.

    Cached for MORPHO_CAPS_TTL seconds (default 300); pass force=True to refresh.

    landmark_names is static per mode, which is why it rides along here rather than on any
    per-case hot path."""
    global _CAPS_CACHE, _CAPS_TS
    with _CAPS_LOCK:
        if not force and _CAPS_CACHE and (time.time() - _CAPS_TS) < _CAPS_TTL:
            return [dict(c) for c in _CAPS_CACHE]

    base = os.environ.get("MORPHO_URL")
    key = os.environ.get("MORPHO_API_KEY")
    if not base or not key:
        return []
    try:
        supported = asyncio.run(_health_async(base, key))
    except Exception:
        # A blip must not erase a good list: keep serving the last one until it expires.
        with _CAPS_LOCK:
            return [dict(c) for c in _CAPS_CACHE]
    out = []
    for entry in supported:
        if isinstance(entry, dict):
            anatomy = entry.get("anatomy", "")
            projection = entry.get("projection", "")
            out.append(
                {"anatomy": anatomy, "projection": projection,
                 "mode": entry.get("mode") or f"{anatomy}_{projection}",
                 "measurements": entry.get("measurements", []),
                 "landmark_names": _landmark_names(entry)}
            )
        elif isinstance(entry, str):  # legacy shape: "knee_lateral"
            anatomy, _, projection = entry.partition("_")
            out.append({"anatomy": anatomy, "projection": projection, "mode": entry,
                        "measurements": [], "landmark_names": []})
    with _CAPS_LOCK:
        _CAPS_CACHE, _CAPS_TS = out, time.time()
    return [dict(c) for c in out]


def _tokens(anatomy_text: str, projection_text: str) -> tuple[str | None, str | None]:
    """Map the assessment's free-text (German) anatomy/projection to service tokens.
    Add a keyword row here when the server gains a new anatomy."""
    a = (anatomy_text or "").lower()
    p = (projection_text or "").lower()
    if any(k in a for k in ("knie", "knee", "patell", "tibia", "femur")):
        anatomy = "knee"
    elif any(k in a for k in ("fuß", "fuss", "foot", "feet", "vorfuß", "mittelfuß",
                              "metatars", "hallux", "calcaneus", "kalkaneus", "talus",
                              "tarsal", "zehe")):
        anatomy = "feet"
    elif any(k in a for k in ("schulter", "shoulder", "humerus", "glenohum", "akromi",
                              "acromi", "klavikula", "clavicula")):
        anatomy = "shoulder"
    else:
        anatomy = None

    if any(k in p for k in ("dorsoplantar", "dorso-plantar", "anteroposterior",
                            "anterior-posterior", "a.p", "a. p")) or p.strip() in ("ap", "dp"):
        projection = "ap"
    elif any(k in p for k in ("seit", "lateral", " lat", "lat.")):
        projection = "lateral"
    elif any(k in p for k in ("axial", "tangential", "skyline", "defil", "défil", "sunrise", "patella")):
        projection = "axial"
    else:
        projection = None
    return anatomy, projection


def supported_combos() -> list[str]:
    """Canonical 'anatomy/projection' tokens the server supports, live.
    e.g. ['knee/lateral', 'knee/axial', 'feet/ap', 'shoulder/ap']."""
    return [f"{c['anatomy']}/{c['projection']}" for c in capabilities()]


def _upload(base: str, key: str, image_path: str) -> str:
    with open(image_path, "rb") as f:
        data = f.read()
    r = requests.post(
        f"{base}/upload", headers={"X-API-Key": key}, data=data, verify=_verify(), timeout=60
    )
    r.raise_for_status()
    return r.json()["image_token"]


async def _get_morphometry(base: str, key: str, anatomy: str, projection: str,
                           image_token: str, num_references: int | None) -> dict:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(**kw):
        return httpx.AsyncClient(verify=_verify(), **kw)

    async with streamablehttp_client(
        f"{base}/mcp", headers={"X-API-Key": key}, httpx_client_factory=factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            args: dict = {"anatomy": anatomy, "projection": projection, "image_token": image_token}
            if num_references:
                args["num_references"] = num_references
            result = await session.call_tool("get_morphometry", args)
            return json.loads(result.content[0].text)


def measure(
    image_path: str, anatomy: str, projection: str, num_references: int | None = None
) -> dict:
    """Compute morphometry. `anatomy`/`projection` are the canonical service tokens
    (e.g. 'knee'/'lateral', chosen by the model from the live supported list).
    `num_references` controls accuracy vs. speed; None -> MORPHO_NUM_REFERENCES."""
    base = os.environ.get("MORPHO_URL")
    key = os.environ.get("MORPHO_API_KEY")
    if not base or not key:
        return {"status": "unavailable", "message": "Morphometrie-Dienst nicht konfiguriert."}

    if not anatomy or not projection:
        return {
            "status": "unsupported",
            "message": (
                "Keine unterstützte Region/Projektion erkannt "
                f"(unterstützt: {', '.join(supported_combos()) or '—'})."
            ),
        }

    num_refs = num_references
    if num_refs is None:
        try:
            num_refs = int(os.environ.get("MORPHO_NUM_REFERENCES", "10") or 0) or None
        except ValueError:
            num_refs = None

    try:
        token = _upload(base, key, image_path)
        raw = asyncio.run(_get_morphometry(base, key, anatomy, projection, token, num_refs))
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    matching = raw.get("matching", {}) or {}
    return {
        "status": raw.get("status", "ok"),
        "anatomy": raw.get("anatomy", anatomy),
        "projection": raw.get("projection", projection),
        "measurements": raw.get("measurements", {}),
        "mean_confidence": matching.get("mean_confidence"),
        "num_references_used": matching.get("num_references_used"),
        # Did matching mirror a working copy to align laterality? Informational only: the
        # service maps the landmarks back, so they stay in the processed image's frame.
        "laterality_corrected": bool(raw.get("laterality_corrected")),
        "landmarks": raw.get("landmarks", []),  # [[x,y],...] in the processed image's pixels
        "result_token": raw.get("result_token"),  # handle for visualize_measurement (~15 min)
        "visualizable_measurements": raw.get("visualizable_measurements", []),
        # mode + mpp make recompute() stateless: measurements are a pure function of
        # (landmarks, mode, mpp), so manual landmark edits never depend on server-side
        # session state and survive an mcp-server restart.
        "mode": raw.get("config_tag") or raw.get("mode") or f"{anatomy}_{projection}",
        "mpp": raw.get("mpp"),
    }


def recompute(
    mode: str, mpp: float | None, landmarks: list[list[float]], result_token: str = ""
) -> dict:
    """Recompute every measurement + its construction geometry from (edited) landmarks.

    Stateless and CPU-only on the server: no GPU, no image, no session state — it just
    re-runs the same validated measurement functions the pipeline used. This is the
    authoritative path; every number that reaches the report or the audit log comes from
    here, never from a client-side reimplementation.

    `result_token` is optional: when the server still holds the result it also refreshes
    the stored landmarks there, so a later visualize_measurement renders the corrected
    construction. An expired token is not an error.

    Returns {"status": "ok", "measurements": {...}, "geometry": {...}} or a non-ok status.
    """
    base = os.environ.get("MORPHO_URL")
    key = os.environ.get("MORPHO_API_KEY")
    if not base or not key:
        return {"status": "unavailable", "message": "Morphometrie-Dienst nicht konfiguriert."}
    if not mode or not landmarks:
        return {"status": "bad_input", "message": "mode und landmarks sind erforderlich."}

    payload: dict = {"mode": mode, "landmarks": landmarks}
    if mpp is not None:
        payload["mpp"] = mpp
    if result_token:
        payload["result_token"] = result_token
    try:
        r = requests.post(
            f"{base}/recompute",
            json=payload,
            headers={"X-API-Key": key},
            verify=_verify(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": data.get("status", "ok"),
        "measurements": data.get("measurements", {}),
        # {name: {value, kind, unit, landmark_indices, points, solid_lines, dotted_lines}}
        # in [x, y], the same space as `landmarks`.
        "geometry": data.get("geometry", {}),
    }


async def _visualize_all_async(base: str, key: str, result_token: str,
                               measurements: list[str]) -> list[dict]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(**kw):
        return httpx.AsyncClient(verify=_verify(), **kw)

    out: list[dict] = []
    async with streamablehttp_client(
        f"{base}/mcp", headers={"X-API-Key": key}, httpx_client_factory=factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for m in measurements:
                res = await session.call_tool(
                    "visualize_measurement", {"result_token": result_token, "measurement": m}
                )
                for item in res.content:
                    if getattr(item, "type", None) == "image":
                        out.append(
                            {"measurement": m,
                             "image": f"data:{item.mimeType};base64,{item.data}"}
                        )
                        break  # skip non-image (status) results silently
    return out


def visualize_all(result_token: str, measurements: list[str]) -> list[dict]:
    """Render every given measurement onto the radiograph in one session.
    Returns a list of {measurement, image(dataURL)}; failures are skipped."""
    base = os.environ.get("MORPHO_URL")
    key = os.environ.get("MORPHO_API_KEY")
    if not base or not key or not result_token or not measurements:
        return []
    try:
        return asyncio.run(_visualize_all_async(base, key, result_token, measurements))
    except Exception:
        return []


async def _processed_image_async(base: str, key: str, result_token: str) -> str | None:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(**kw):
        return httpx.AsyncClient(verify=_verify(), **kw)

    async with streamablehttp_client(
        f"{base}/mcp", headers={"X-API-Key": key}, httpx_client_factory=factory
    ) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("get_processed_image", {"result_token": result_token})
            for item in res.content:
                if getattr(item, "type", None) == "image":
                    return f"data:{item.mimeType};base64,{item.data}"
    return None


def processed_image(result_token: str) -> str | None:
    """The exact radiograph the landmarks/measurements were computed on (the service's
    prepared copy), as a data URL, so the UI can display the image in the same space as
    the overlays without touching local files.

    Returns None if the server doesn't offer `get_processed_image`, or on any error, so
    callers gracefully fall back to the user's uploaded image."""
    base = os.environ.get("MORPHO_URL")
    key = os.environ.get("MORPHO_API_KEY")
    if not base or not key or not result_token:
        return None
    try:
        return asyncio.run(_processed_image_async(base, key, result_token))
    except Exception:
        return None
