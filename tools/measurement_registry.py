"""Curated measurement registry — ONE source of truth for presentation AND selection.

The morphometry tool's canonical English names stay the INTERNAL keys everywhere
(geometry, classification, audit, range matching). This registry curates how a
measurement is PRESENTED: German display name, clinical grouping, decimal places and
number format. UI, report text and exports all draw from here, so the same measurement
can never appear under two different names or formats.

It also doubles as the ALLOWLIST: a measurement the tool offers but which is NOT listed
here is filtered out of the model's view, the report, the chips and the capability panel
(is_supported / filter_supported). To retire a measurement, delete its entry — nothing in
the morphometry tool or anywhere else needs touching. To add one, add its entry here.

Unknown names still fall back to the canonical name and kind-based defaults for the few
places that render a raw value defensively — but the allowlist keeps them out of the
curated surfaces.
"""
from __future__ import annotations

import re

_ROBUST = re.compile(r"\s*\((robust)\)\s*$", re.IGNORECASE)

# canonical tool name -> {de: display name, group: clinical concept, decimals}
REGISTRY: dict[str, dict] = {
    # --- knee, lateral ---
    "Insall Salvati Ratio":             {"de": "Insall-Salvati-Index", "group": "Patellahöhe", "decimals": 1},
    "Modified Insall Salvati Ratio":    {"de": "Modifizierter Insall-Salvati-Index", "group": "Patellahöhe", "decimals": 1},
    "Caton-Deschamps Index":            {"de": "Caton-Deschamps-Index", "group": "Patellahöhe", "decimals": 1},
    "Blackburn-Peel Ratio":             {"de": "Blackburne-Peel-Index", "group": "Patellahöhe", "decimals": 1},
    "Diaphyseal Posterior Tibial Slope": {"de": "Posterior Tibial Slope", "group": "Tibiaplateau", "decimals": 1},
    # --- knee, axial ---
    "Sulcus Angle":                     {"de": "Sulcuswinkel", "group": "Patellofemorale Zentrierung", "decimals": 1},
    "Congruence Angle":                 {"de": "Kongruenzwinkel", "group": "Patellofemorale Zentrierung", "decimals": 1},
    "Patella Tilt Angle":               {"de": "Patella-Tilt-Winkel", "group": "Patellofemorale Zentrierung", "decimals": 1},
    "Lateral Patellofemoral Angle":     {"de": "Lateraler patellofemoraler Winkel", "group": "Patellofemorale Zentrierung", "decimals": 1},
    # --- shoulder, ap --- (grouped as in the reference literature: acromion geometry
    # vs. subacromial space)
    "Critical Shoulder Angle":          {"de": "Kritischer Schulterwinkel", "group": "Akromion", "decimals": 1},
    "Lateral Acromion Angle":           {"de": "Lateraler Akromionwinkel", "group": "Akromion", "decimals": 1},
    "Acromial Index":                   {"de": "Akromionindex", "group": "Akromion", "decimals": 2},
    # Pending the morphometry update (not yet emitted by the tool): keys assume the typo
    # fix lands as "Acromial Slope"/"Acromial Tilt". German terms tentative — confirm.
    "Acromial Slope":                   {"de": "Akromionneigung (Slope)", "group": "Akromion", "decimals": 1},
    "Acromial Tilt":                    {"de": "Akromionkippung (Tilt)", "group": "Akromion", "decimals": 1},
    "Acromiohumeral Interval":          {"de": "Akromiohumeraler Abstand", "group": "Subakromialraum", "decimals": 1},
    # --- feet, ap (Vorfuß) ---
    "Hallux Valgus Angle":              {"de": "Hallux-valgus-Winkel", "group": "Vorfuß", "decimals": 1},
    "First-Second Intermetatarsal Angle": {"de": "I/II-Intermetatarsalwinkel", "group": "Vorfuß", "decimals": 1},
    "First-Fifth Intermetatarsal Angle":  {"de": "I/V-Intermetatarsalwinkel", "group": "Vorfuß", "decimals": 1},
    # --- feet, lateral (Längsgewölbe) --- keys match the tool's exact output; the old
    # "Meary's Angle"/"Calcaneal Inclination" keys never matched (tool: "Talus-First
    # Metatarsal Angle" / "Calcaneal Inclination Angle") and were replaced here.
    "Talus-First Metatarsal Angle":     {"de": "Meary-Winkel (Talus–MT I)", "group": "Fußlängsgewölbe", "decimals": 1},
    "Calcaneal Inclination Angle":      {"de": "Kalkaneus-Inklinationswinkel", "group": "Fußlängsgewölbe", "decimals": 1},
    "Medial Arch Angle":                {"de": "Medialer Bogenwinkel", "group": "Fußlängsgewölbe", "decimals": 1},
    # --- knee, ap (femorotibialer Gelenkspalt) ---
    "Medial Joint Space":               {"de": "Medialer Gelenkspalt", "group": "Femorotibialer Gelenkspalt", "decimals": 1},
    "Lateral Joint Space":              {"de": "Lateraler Gelenkspalt", "group": "Femorotibialer Gelenkspalt", "decimals": 1},
}

FALLBACK_GROUP = "Weitere Messwerte"

# Display name -> canonical. Lets a record that already uses the reported names be read
# back through the registry (see _canonical_of).
_BY_DISPLAY = {e["de"]: k for k, e in REGISTRY.items()}


def _split_robust(canonical: str) -> tuple[str, bool]:
    """'Hallux Valgus Angle (robust)' -> ('Hallux Valgus Angle', True)."""
    base = _ROBUST.sub("", canonical or "")
    return base, base != (canonical or "")


def entry(canonical: str) -> dict | None:
    base, _ = _split_robust(canonical)
    return REGISTRY.get(base)


def is_supported(canonical: str) -> bool:
    """Is this measurement on the allowlist? True iff its base (robust suffix stripped) is
    in the curated registry. The registry IS the allowlist — delete an entry to retire the
    measurement everywhere, add one to extend support, without touching the tool."""
    base, _ = _split_robust(canonical)
    return base in REGISTRY


def filter_supported(measurements: dict) -> dict:
    """Drop measurements the registry does not list (robust variants of an allowed base are
    kept). Applied to the tool's output wherever it feeds the model or the UI, so an
    excluded measurement never reaches the report, the chips, the arrows or the model."""
    return {k: v for k, v in measurements.items() if is_supported(k)}


def supported_display(measurements: list[str]) -> list[str]:
    """A tool combo's raw measurement names -> curated German display names, keeping ONLY
    allowlisted ones, collapsing robust variants to their base, deduped, in registry order.
    This is what the 'Unterstützt' panel shows — the panel and the pipeline read the same
    allowlist, so they can never disagree about which measurements are in use."""
    order = {k: i for i, k in enumerate(REGISTRY)}
    seen: list[str] = []
    for m in measurements:
        base, _ = _split_robust(m)
        if base in REGISTRY and base not in seen:
            seen.append(base)
    seen.sort(key=lambda b: order.get(b, len(order)))
    return [REGISTRY[b]["de"] for b in seen]


def display_name(canonical: str) -> str:
    """Curated German display name. The '(robust)' suffix is an implementation detail
    the reader never sees — the robust variant is displayed under the base name."""
    base, _ = _split_robust(canonical)
    e = REGISTRY.get(base)
    if not e:
        return base or canonical
    return e["de"]


def preferred_measurements(measurements: dict) -> dict:
    """Collapse (X, X (robust)) pairs to ONE entry under the BASE name, preferring the
    robust value. Applied wherever measurements are PRESENTED or handed to the model —
    the raw tool result (audit, geometry, session state) keeps every variant."""
    robust_bases = {
        _split_robust(k)[0] for k in measurements if _split_robust(k)[1]
    }
    out: dict = {}
    for k, v in measurements.items():
        base, robust = _split_robust(k)
        if robust:
            out[base] = v  # robust wins
        elif base not in robust_bases:
            out[base] = v  # no robust twin -> the plain value stands
    return out


def _canonical_of(name: str) -> str:
    """The registry key for a measurement named either canonically or by its German
    display name. Unknown names come back unchanged, so they can still be filtered out."""
    if _split_robust(name)[0] in REGISTRY:
        return name
    return _BY_DISPLAY.get((name or "").strip(), name)


def as_recorded(measurements: dict) -> dict:
    """Measurements as a SESSION RECORD should hold them: allowlisted only, robust
    variants collapsed onto their base, keyed by the curated German display name, rounded
    to the reported precision.

    The same three steps the report and the UI already apply, in one call, so a recorded
    session lists exactly the measurements the reader was shown, under the names they were
    shown under. Without it the log carried every raw pipeline output — including the
    '(robust)' twins and measurements the allowlist excludes — so a session appeared to
    contain values that never reached the report, under English names that no report or
    manual reading ever uses.

    That last point is what keeps records comparable: a manual reading records the reader's
    chosen German label, so an AI-assisted record keyed 'Hallux Valgus Angle' could never be
    joined to 'Hallux-valgus-Winkel' at analysis.
    """
    # Idempotent: a dict that has already been through here is keyed by display names,
    # which the registry does not recognise as canonical. Mapping them back first means
    # applying this twice — once when writing, once when reading an older session — is the
    # same as applying it once, instead of silently emptying the dict.
    measurements = {_canonical_of(k): v for k, v in measurements.items()}

    out: dict = {}
    for canonical, value in preferred_measurements(filter_supported(measurements)).items():
        out[display_name(canonical)] = (
            round_value(canonical, value) if isinstance(value, (int, float)) and
            not isinstance(value, bool) else value
        )
    return out


def keys_as_recorded(by_measurement: dict) -> dict:
    """The same renaming for anything else keyed by measurement — classifications,
    reference ranges — so one record never mixes naming schemes."""
    out: dict = {}
    for name, value in by_measurement.items():
        canonical = _canonical_of(name)
        if is_supported(canonical):
            out[display_name(canonical)] = value
    return out


def group_for(canonical: str) -> str:
    e = entry(canonical)
    return e["group"] if e else FALLBACK_GROUP


def decimals_for(canonical: str, kind: str | None = None) -> int:
    """One decimal is the house standard (two decimals would suggest a precision the
    pipeline does not have — repeatability CV reaches ~12%). The per-measurement field
    stays so an individual measurement can deviate deliberately."""
    del kind  # kept for call-site compatibility; the default no longer depends on it
    e = entry(canonical)
    if e:
        return int(e["decimals"])
    return 1


def round_value(canonical: str, value: float, kind: str | None = None) -> float:
    """Round to the REPORTED precision. Classification uses this same rounding
    (laboratory convention: a value is compared to the reference interval at the
    precision it is reported with), so display and verdict can never contradict each
    other at a range boundary."""
    return round(value, decimals_for(canonical, kind))


def format_value(canonical: str, value: float, kind: str | None = None, unit: str | None = None) -> str:
    """German clinical number format: decimal comma; '°' without, ' mm' with a space;
    per-measurement decimal places. E.g. 1,07 · 8,8° · 10,3 mm."""
    dec = decimals_for(canonical, kind)
    num = f"{value:.{dec}f}".replace(".", ",")
    u = unit if unit is not None else ("°" if kind == "angle" else (" mm" if kind == "distance" else ""))
    u = u.strip()
    if not u:
        return num
    return f"{num}{u}" if u == "°" else f"{num} {u}"


def ui_registry() -> dict:
    """The registry as served to the frontend (display names, groups, decimals)."""
    return {k: {"de": v["de"], "group": v["group"], "decimals": v["decimals"]} for k, v in REGISTRY.items()}


def bilingual_query(query: str) -> str:
    """Ensure every registry measurement named in a guideline-search query is present in
    BOTH its English canonical and its German display form, so the search hits sources in
    either language. The reference corpus is German today, so an English-named query
    gains the German term; adding the mirror direction means English sources added later are
    matched too, without changing this call site. Additive — the model's own terms stay,
    the query is returned unchanged when no measurement is recognised."""
    ql = query.lower()
    added: list[str] = []
    for canonical, e in REGISTRY.items():
        de = e["de"]
        has_en, has_de = canonical.lower() in ql, de.lower() in ql
        if has_en and not has_de:
            added.append(de)
        elif has_de and not has_en:
            added.append(canonical)
    return f"{query} {' '.join(added)}" if added else query


def alias_norms() -> dict[str, str]:
    """norm(display name) -> canonical, for matching report text back to tool keys."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    return {norm(v["de"]): k for k, v in REGISTRY.items()}
