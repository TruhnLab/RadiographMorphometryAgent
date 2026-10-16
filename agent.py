"""The radiology agent: an autonomous analysis phase followed by interactive chat.

Robust model-agnostic loop: the model replies with a single JSON object choosing
one action. We don't rely on each local model's native tool-calling. Actions:

  {"thought": "...", "action": "call_tool", "tool": "...", "arguments": {...}}
  {"thought": "...", "action": "final_report", "report": {... Report fields ...}}
  {"thought": "...", "action": "message", "message": "..."}   # used in chat
"""
from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from datetime import datetime
from typing import Callable

import audit
from config import Config
from guidelines import store
from llm import LLM, user_message
from report import Report
from tools import measurement_registry as mreg
from tools import morphometry_client
from tools.registry import dispatch, tool_catalogue

MAX_STEPS = 12  # room for per-measurement guideline lookups (grounding)


SYSTEM_PROMPT = """Du bist ein radiologischer Befundassistent. Du erhältst ein
Röntgenbild (eine herunterskalierte Ansicht, nur als Kontext). Quantitative Befunde
MÜSSEN von Tools stammen, nicht aus deiner eigenen Bildbetrachtung.

Dir stehen diese Tools zur Verfügung:
{tools}

Für die Morphometrie aktuell unterstützte Kombinationen:
{supported}

Gehe in dieser festen Reihenfolge vor:
1. assess_image: Bestimme zuerst die abgebildete Anatomie/Körperregion und die
   Projektion/Ebene. Gib in 'anatomy'/'projection' eine kurze DEUTSCHE Beschreibung
   (zur Anzeige). Wähle dann EINE Zeile aus der obigen Liste und übernimm ihre beiden
   Werte GETRENNT: anatomy_key bekommt NUR die Region, projection_key NUR die
   Projektion. Schreibe niemals beides zusammen in ein Feld. Passt keine Zeile, setze
   beide Keys auf null. Erst danach folgt alles Weitere.
   Passt das Bild zu KEINER der oben aufgeführten Kombinationen, dann setze die Keys
   auf null und erstelle KEINEN Bericht: schließe stattdessen direkt mit final_report
   ab, lasse dabei 'report' leer und erkläre im Feld 'message' kurz, welche Region du
   siehst und dass sie derzeit nicht unterstützt wird. Beurteile ein nicht
   unterstütztes Bild NICHT und rate keine Messwerte.
2. morphometry: Rufe das Tool `morphometry` auf (ohne Argumente). Es liefert alle für
   die erkannte Region/Projektion verfügbaren Standardmessungen.
   Bei status != "ok" (z. B. unsupported/no_match) vermerke dies und passe dich an.
3. Schlage für JEDE Messung, die du interpretieren möchtest, GEZIELT ihren
   Normbereich/ihre Interpretation in den Leitlinien nach (search_guidelines,
   z. B. „Insall-Salvati Normbereich"). Nur wenn du einen belegenden Leitlinientext
   findest, darfst du diese Messung im Bericht beurteilen.
{search_language}
   WICHTIG für das Feld 'measurements' im Bericht: Verwende als Schlüssel EXAKT die
   Messungsnamen aus dem morphometry-Ergebnis (unverändert, englisch, z. B.
   "Insall Salvati Ratio") — NICHT übersetzt und NICHT umbenannt. Der Wert ist ein String
   der Form "<Wert> (Referenzbereich: <Bereich aus der Leitlinie>)", z. B.
   "1.07 (Referenzbereich: 0,8 – 1,2)". Ohne belegten Referenzbereich lässt du den
   Klammerzusatz weg. Diese Schlüssel werden maschinell weiterverwendet; abweichende
   Namen führen dazu, dass der Referenzbereich der Messung nicht mehr zugeordnet werden
   kann.
   Gib die Grenzen ZUSÄTZLICH strukturiert als Zahlen im final_report-Feld
   'reference_ranges' an (s. u.). Diese Zahlen — nicht der Fließtext — steuern die
   Einordnung (normal/abnormal) und die Prüfung nach Landmarken-Korrekturen; so muss der
   Bereich nie aus dem Text zurückgelesen werden.
4. final_report: Dieser Assistent ist ein MORPHOMETRIE-MODUL, kein allgemeiner
   Befundassistent. Der Befundentwurf enthält daher AUSSCHLIESSLICH morphometrische
   Inhalte:
   - 'technique': wird maschinell aus Region/Projektion gesetzt — lasse das Feld LEER ("").
   - 'findings': bleibt LEER ([]). KEINE Aussagen zu Frakturen, Gelenkspalt,
     Weichteilen, Knochenstruktur o. Ä. — dafür existiert kein validiertes Modul, und
     ein scheinbar vollständiger Negativbefund wäre gefährlich.
   - 'measurements': exakte Werte (Format s. Schritt 3) — wird maschinell
     nachformatiert und gesperrt.
   - 'impression': die BEURTEILUNG — 1 bis 2 anatomisch präzise Sätze.
     Pathologische/grenzwertige Werte ZUERST benennen, unauffällige zusammengefasst
     (z. B. „übrige erhobene Parameter im Referenzbereich"). KEINE Zahlenwerte
     wiederholen (die stehen maschinell gesichert unter Morphometrie), KEINE
     Konfidenzwerte, KEINE Aussage wie „radiologisch unauffällig" — es wurden nur
     einzelne morphometrische Parameter erhoben. Die Beurteilung wiederholt NICHT die
     Anatomie/Projektion/Technik.
   Gib zusätzlich im Feld 'message' eine kurze Zusammenfassung für den Nutzer (1-3
   Sätze, keine bloße Bestätigung wie „Bericht erstellt").
   - 'reference_ranges' (Feld im final_report, NEBEN 'report'): pro beurteilter Messung
     ein Objekt {{lower, upper, reference}}. 'lower'/'upper' sind ZAHLEN mit Dezimalpunkt
     ODER null. Nur EINE Grenze? Setze die andere auf null — nur Obergrenze („< 10"):
     lower=null, upper=10; nur Untergrenze („> 7"): lower=7, upper=null. 'reference' ist
     der Bereich wörtlich aus der Leitlinie. Nennt der Leitlinientext KEINEN Bereich,
     sondern einen Mittelwert mit Streuung („Normal value: 0,64 +/- 0,06", „86 ± 10°"),
     dann bilde den Bereich daraus: lower = Mittelwert − Streuung, upper = Mittelwert +
     Streuung (genau EINE Streuung, kein anderes Vielfaches). 'reference' nennt in diesem
     Fall BEIDES, den Bereich und die Quellform, z. B. „0,58 – 0,70 (aus 0,64 ± 0,06)".
     Nimm dafür ausschließlich den Wert für Gesunde/Normalbefunde, NIE den Mittelwert
     einer Krankheitsgruppe. Führe NUR belegte Messungen auf; ohne Leitlinienbeleg lasse
     die Messung hier WEG. Schlüssel = kanonischer Messungsname (identisch zu
     'measurements').

Regeln:
- LITERATURTREUE (WICHTIG): Normbereiche, Interpretationen und Schlussfolgerungen zu
  Messwerten stammen AUSSCHLIESSLICH aus den über search_guidelines abgerufenen
  Leitlinien-Texten — NIEMALS aus deinem eigenen medizinischen Vorwissen. Erfinde
  keine Normbereiche und leite keine Schlüsse aus ungesicherten Annahmen ab.
- Eine Messung OHNE Leitlinienbeleg wird im Bericht NICHT bewertet, interpretiert
  oder in die Beurteilung aufgenommen. Du darfst den reinen Messwert
  dem Nutzer im Feld 'message' nennen und darauf hinweisen, dass dafür kein
  freigegebener Referenzbereich hinterlegt ist — aber KEINE Schlussfolgerung dazu in
  den Bericht schreiben.
- DIAGNOSTISCHE ZURÜCKHALTUNG: Aus einem einzelnen morphometrischen Surrogatparameter
  wird KEINE weitergehende Diagnose abgeleitet. Ein auffälliger Wert ist ein
  „Hinweis auf …", nie ein Beweis (z. B. verminderter akromiohumeraler Abstand:
  „Hinweis auf Humeruskopfhochstand", NICHT „Komplettruptur der Rotatorenmanschette").
  Umgekehrt schließt ein normaler Wert eine Erkrankung NICHT aus: formuliere
  „X im Referenzbereich", nie „keine Hinweise auf <Diagnose>".
- RICHTUNGSDIAGNOSEN (z. B. Patellahochstand/alta, Patellatiefstand/baja,
  Humeruskopfhochstand): In ALLEN Textausgaben (Beurteilung UND 'message') NUR
  verwenden, wenn der abgerufene Leitlinientext die Bezeichnung für GENAU diese
  Abweichungsrichtung ausdrücklich nennt. Nennt er sie nicht, formuliere neutral:
  „Wert über/unter dem Referenzbereich" — NIEMALS aus eigenem Wissen ergänzen.
- ANATOMISCHE PRÄZISION: Benenne nur das tatsächlich Gemessene. Patellahöhen-Indizes
  (Insall-Salvati, Caton-Deschamps, Blackburne-Peel) beurteilen die PatellaHÖHE, nicht
  die „Patellaposition". Ein nicht erhöhter Sulcuswinkel belegt nicht das Fehlen einer
  Trochleadysplasie („Sulcuswinkel nicht erhöht", nicht „keine Trochleadysplasie").
  Der Kongruenzwinkel beschreibt einen Teilaspekt der patellofemoralen Zentrierung,
  nicht die Stabilität des Gelenks.
- Verwende in Textausgaben den Begriff „Referenzbereich" (nicht „Normbereich") und
  gib pro Messung nur EINEN Referenzbereich an — keine konkurrierenden Bereiche ohne
  methodischen Kontext.
- MESSUNGSNAMEN: Verwende in deutschem Fließtext (Beurteilung, 'message') AUSSCHLIESSLICH
  die im Morphometrie-Ergebnis unter 'display_names' gelieferten Bezeichnungen — Zeichen
  für Zeichen. Übersetze Messungsnamen NIEMALS selbst, kürze sie nicht ab und erfinde
  keine neuen Namen. (Im Feld 'measurements' gelten weiterhin die kanonischen Namen.)
- NUR FAKTEN: Formuliere den Bericht ausschließlich mit gesicherten Aussagen im
  Indikativ. KEINE hypothetischen, konditionalen oder spekulativen Formulierungen
  (kein Konjunktiv wie „wäre/hätte/könnte/sollte/würde/dürfte", kein „falls/sofern/
  wenn ... dann", kein „möglicherweise/eventuell/vermutlich/gegebenenfalls"). Was
  nicht belegt/festgestellt ist, wird weggelassen, nicht als Möglichkeit angedeutet.
- Rufe immer nur eine Aktion gleichzeitig auf; du siehst das Ergebnis, bevor du
  erneut entscheidest.
- Erfinde keine Messwerte. Gibt ein Tool einen Fehler zurück, vermerke das und
  passe dich an.
- Verfasse alle Textinhalte (Technik, Befunde, Beurteilung) auf DEUTSCH.

Antworte IMMER mit GENAU EINEM JSON-Objekt und nichts anderem. Die JSON-Schlüssel
bleiben englisch, die Werte sind auf Deutsch. Schema:
- Bild beurteilen (immer als allererste Aktion):
  {{"thought": "...", "action": "assess_image", "anatomy": "<deutsche Beschreibung>", "projection": "<deutsche Beschreibung>", "anatomy_key": "<NUR die Region, z. B. knee — oder null>", "projection_key": "<NUR die Projektion, z. B. lateral — oder null>"}}
- Tool aufrufen (morphometry oder search_guidelines):
  {{"thought": "...", "action": "call_tool", "tool": "<name>", "arguments": {{...}}}}
- Abschließen:
  {{"thought": "...", "action": "final_report",
    "message": "<kurze Zusammenfassung/Antwort an den Nutzer, 1-3 Sätze — Zahlen hier erlaubt>",
    "report": {{
       "technique": "",
       "findings": [], "measurements": {{"...": "..."}},
       "impression": "<1-2 Sätze, OHNE Zahlenwerte und OHNE Bereichsangaben; Begriff „Referenzbereich", nie „Normbereich">"}},
    "reference_ranges": {{"<kanonischer Messungsname>": {{"lower": <Zahl oder null>, "upper": <Zahl oder null>, "reference": "<Bereich wörtlich>"}}}}}}

Das Feld "thought" enthält deine kurze Begründung auf Deutsch."""


# The morphometry service (landmark matching on a GPU), appended to the prompt catalogue.
MORPHO_TOOL_SIG = (
    "- morphometry(): Berechnet per Landmarken-Matching (GPU-Dienst) die "
    "morphometrischen Standardmessungen für die erkannte Region/Projektion. Bild, "
    "Anatomie und Projektion werden automatisch aus der Bildbeurteilung übernommen — "
    "keine Argumente nötig. Liefert alle verfügbaren Messwerte."
)


# Agent-callable guideline lookup, appended to the image-tool catalogue in the prompt.
# {query} names the search language (GUIDELINES_LANGUAGE, see guidelines/store.py).
GUIDELINE_TOOL_SIG = (
    '- search_guidelines(query <{query}>, '
    'purpose <Zweck, z. B. "Messparameter", "Interpretation" oder "Befundstandard">): '
    "Schlägt klinische Leitlinien und Messverfahren im Referenzwerk nach."
)


def _repair_json(blob: str) -> str:
    """Best-effort fix for the ONE malformation small/quantised models reliably produce:
    a German quote pair whose CLOSING mark was typed as a straight " (U+0022), which ends
    the JSON string early and breaks the parse (seen with a GGUF Qwen build; other models
    use the curly „…" and parse fine). Both German curly quotes are non-ASCII and therefore
    JSON-safe, so normalise „…" -> „…". Additive and targeted — correct pairs and ordinary
    strings are left untouched."""
    return re.sub(r"„([^\"„]*)\"", "„\\1“", blob)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating code fences and the
    straight-quote-closing-a-German-quote slip (see _repair_json)."""
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in reply")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    # JSONDecodeError subclasses ValueError, so if the repair also fails the
                    # caller's `except ValueError` still handles it exactly as before.
                    return json.loads(_repair_json(blob))
    raise ValueError("unbalanced JSON in reply")


def _round(seconds: float | None) -> float | None:
    return round(seconds, 2) if seconds is not None else None


def _num(v) -> float | None:
    """A number, or None. Tolerates the model writing '0,8' or '1.2°'."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", v)
        if m:
            return float(m.group().replace(",", "."))
    return None


_RANGE_RE = re.compile(
    # Units may sit between number and separator ("-20° bis 10°", "0° – 4°",
    # "10 mm bis 12 mm") — without the optional unit group such ranges silently
    # failed to parse and were dropped.
    r"(-?\d+(?:[.,]\d+)?)\s*(?:°|mm)?\s*(?:–|—|-|bis|to)\s*(-?\d+(?:[.,]\d+)?)", re.I
)
_UPPER_RE = re.compile(r"(?:<|≤|\\leq|kleiner)\s*=?\s*(-?\d+(?:[.,]\d+)?)", re.I)
_LOWER_RE = re.compile(r"(?:>|≥|\\geq|größer|groesser)\s*=?\s*(-?\d+(?:[.,]\d+)?)", re.I)


def _parse_range(text: str) -> dict | None:
    """Pull a numeric reference range out of the report's own measurement line, e.g.
    '1.05 (Normbereich: 0,8 – 1,2)' -> {lower: 0.8, upper: 1.2}.

    The agent already looked every range up in the guidelines during the run and wrote it
    here. Re-deriving it with a second LLM call costs minutes on a slow server and can only
    ever reproduce what this string already says — so parse it instead, and fall back to the
    model only for what genuinely cannot be read."""
    if not text:
        return None
    # Only look AFTER a "Norm…" marker when there is one, so the measured value itself
    # ('1.05') is never mistaken for a bound.
    m = re.search(r"(?:norm|referenz)\w*\s*[:=]?\s*(.*)$", text, re.I | re.S)
    seg = m.group(1) if m else text
    ref = seg.strip(" ()")

    r = _RANGE_RE.search(seg)
    if r:
        lo, hi = _num(r.group(1)), _num(r.group(2))
        if lo is not None and hi is not None and lo <= hi:
            return {"lower": lo, "upper": hi, "reference": ref}
    hi = _UPPER_RE.search(seg)
    if hi:
        return {"lower": None, "upper": _num(hi.group(1)), "reference": ref}
    lo = _LOWER_RE.search(seg)
    if lo:
        return {"lower": _num(lo.group(1)), "upper": None, "reference": ref}
    return None


def _norm_numeric_text(text: str) -> str:
    """Lowercased text with German decimal commas turned into dots, so '0,8' and '0.8'
    compare equal when looking for a threshold inside a guideline passage."""
    return re.sub(r"(?<=\d),(?=\d)", ".", (text or "").lower())


def _number_positions(text: str, value: float) -> list[int]:
    """Offsets where `text` states `value` as a standalone number (already normalized)."""
    return [
        m.start()
        for m in re.finditer(r"-?\d+(?:\.\d+)?", text)
        if abs(float(m.group()) - value) < 1e-9
    ]


_GENERIC_NAME_TOKENS = {  # too common to prove a passage is about THIS measurement
    "ratio", "index", "angle", "winkel", "value", "wert", "modified", "robust",
    "posterior", "anterior", "medial", "lateral", "first", "second", "fifth",
}


def _name_tokens(name: str) -> set[str]:
    """Distinctive words of a measurement name ('Insall Salvati Ratio' -> insall, salvati)."""
    words = re.split(r"[^a-zA-ZäöüßÄÖÜ]+", (name or "").lower())
    return {w for w in words if len(w) >= 4 and w not in _GENERIC_NAME_TOKENS}


# A mean with its spread, as the corpus states it: "Normal value: 0.64 +/- 0.06",
# "$0.73 \pm 0.06$", "Normal AGA 86 +/- 10°", "$78.9^\circ \pm 8^\circ$". The junk class
# swallows the unit and the LaTeX left over from ingestion between the number and the sign.
_PM_JUNK = r"(?:[\s$°]|\^|\\?circ|[{}]|\\,|mm|%)*"
_MEAN_SD_RE = re.compile(
    rf"(-?\d+(?:\.\d+)?){_PM_JUNK}(?:\+/-|\+-|±|\\pm){_PM_JUNK}(-?\d+(?:\.\d+)?)"
)

# Which multiples of the stated spread may be read as a reference range. 1 SD is what the
# prompt asks for (it keeps the printed bounds exactly as wide as the passage supports);
# 2 SD is recognised too, because a model that reaches for the 95% interval is still doing
# literature arithmetic and should be citable rather than silently untraceable. The factor
# is recorded, so the audit can tell the two apart instead of averaging them.
_SD_FACTORS = (1, 2)


def _close(value: float, expected: float) -> bool:
    """Is `value` the rounded form of `expected`? Tolerance is 0.5% of the magnitude (never
    below 0.05), which absorbs the rounding of a derived bound ("70.9" written as "71")
    without being loose enough to match a different number."""
    return abs(value - expected) <= max(0.05, abs(expected) * 0.005) + 1e-9


def _mean_sd_derivation(text: str, lower: float | None, upper: float | None) -> dict | None:
    """Does this passage state a mean ± spread that the range was computed from?

    Many corpus entries give no interval at all, only "Normal value: 0.64 +/- 0.06". The
    range in the report is then one subtraction and one addition away from the literature,
    and refusing to cite it would mark a properly grounded threshold as untraceable. So the
    derivation is reconstructed and reported explicitly (mean, sd, factor), never smoothed
    into looking like a quotation.

    Both bounds are required: a one-sided range cannot come from a symmetric spread, and
    accepting it would let a single stray number in a passage "explain" an arbitrary bound.
    """
    if lower is None or upper is None:
        return None
    for m in _MEAN_SD_RE.finditer(text):
        mean, sd = float(m.group(1)), float(m.group(2))
        if sd <= 0:
            continue
        for k in _SD_FACTORS:
            if _close(lower, mean - k * sd) and _close(upper, mean + k * sd):
                return {"mean": mean, "sd": sd, "k": k}
    return None


def _range_source(rng: dict, hits: list[dict], name: str = "") -> dict | None:
    """Which retrieved passage states this reference range?

    Grounding here means per-threshold traceability, so that every number can be checked:
    a bound in the report must be attributable to a specific retrieved SPAN, not merely to
    "the guidelines". A passage qualifies when it states every bound of the range, or when
    it states a mean ± spread the range follows from arithmetically (see
    `_mean_sd_derivation`) — the corpus states plenty of norms only in that form.

    Ranking, in order: a passage that QUOTES the bounds beats one they were derived from
    (a quotation needs no reasoning to check), then one that also NAMES the measurement
    beats one that merely happens to contain the same numbers — different measurements
    share ranges (0,8 – 1,2 is both Insall-Salvati and Caton-Deschamps), so numeric
    agreement alone is weak evidence — then retrieval score. `names_measurement` and
    `derived_from` record which kind of match this was, so the audit can report the strong
    and weak cases apart instead of averaging them together.

    Returns None when no retrieved passage states or implies the range — itself the finding
    (an untraceable threshold), never to be smoothed over by fuzzy matching.
    """
    bounds = [b for b in (rng.get("lower"), rng.get("upper")) if b is not None]
    if not bounds or not hits:
        return None

    wanted = _name_tokens(name)
    best = best_text = None
    best_rank: tuple = ()
    best_derived: dict | None = None
    for h in hits:
        text = _norm_numeric_text(h.get("text", ""))
        quoted = all(_number_positions(text, b) for b in bounds)
        derived = None if quoted else _mean_sd_derivation(text, rng.get("lower"), rng.get("upper"))
        if not quoted and not derived:
            continue
        named = bool(wanted) and any(w in text for w in wanted)
        rank = (quoted, named, h.get("score") or 0)
        if best is None or rank > best_rank:
            best, best_text, best_rank, best_derived = h, text, rank, derived

    if best is None:
        return None
    # A short window around the anchor number: the human-checkable span. For a derived range
    # the anchor is the MEAN, since the bounds themselves are nowhere in the text. The full
    # chunk stays available in the guideline steps, so this stays a pointer, not a copy.
    anchor = best_derived["mean"] if best_derived else bounds[0]
    pos = _number_positions(best_text, anchor)[0]
    # Slice the ORIGINAL text (not the lowercased/number-normalised copy): _norm_numeric_text
    # only lowercases and swaps decimal commas, both length-preserving, so the offsets align
    # — this keeps the displayed excerpt in its real casing.
    original = best.get("text", "")
    start, end = max(0, pos - 120), min(len(original), pos + 120)
    return {
        "document_id": best.get("document_id"),
        "title": best.get("title", ""),
        "score": best.get("score"),
        "pages": best.get("pages") or [],  # PDF page(s) of the passage, for the citation
        # False -> the passage states the numbers but never names this measurement: the
        # threshold is present in the retrieved literature, but not demonstrably ITS
        # threshold. Weak evidence, reported separately (only a manual check settles it).
        "names_measurement": best_rank[1],
        # None -> the bounds are quoted verbatim. Otherwise {mean, sd, k}: the passage states
        # the mean and spread and the bounds are mean ∓ k·sd, shown as such to the reader.
        "derived_from": best_derived,
        "excerpt": ("…" if start else "") + original[start:end].strip() + ("…" if end < len(original) else ""),
    }


def _match_report_line(name: str, report_measurements: dict) -> str:
    """The report's line for this measurement — EXACT match only (modulo punctuation/case).

    Deliberately not fuzzy. Fuzzy matching on names looks helpful and is quietly dangerous:
    "Modified Insall Salvati Ratio" happily matches the report's "Insall-Salvati-Index" and
    inherits ITS normal range, which is a different range — a wrong normal/abnormal call
    dressed up as a correct one. The system prompt therefore requires the report to key its
    measurements by the exact names morphometry returned. Anything we cannot match exactly is
    reported as "no reference range" (visible to the reader), never guessed at.
    """
    if name in report_measurements:
        return str(report_measurements[name])
    want = _norm_key(name)
    aliases = mreg.alias_norms()  # curated display name -> canonical (still exact, not fuzzy)
    for key, line in report_measurements.items():
        kn = _norm_key(key)
        if kn == want or aliases.get(kn) == name:
            return str(line)
    return ""


def _verdict(value: float, rng: dict) -> str:
    """Is `value` inside the reference range? Pure arithmetic — the LLM extracted the range
    once; applying it is not a job for a language model, and doing it in code is both
    instant and exactly reproducible (so a label change is never mere sampling noise)."""
    lo, hi = rng.get("lower"), rng.get("upper")
    if lo is None and hi is None:
        return "unbekannt"
    if lo is not None and value < lo:
        return "abnormal"
    if hi is not None and value > hi:
        return "abnormal"
    return "normal"


def _norm_key(name: str) -> str:
    """Loose key for matching a measurement name across the model's phrasings
    ('Hallux Valgus Angle' / 'hallux-valgus-winkel' -> 'halluxvalgus...')."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _landmark_names_for(mode: str) -> list[str]:
    """Display names for each landmark index of a mode (e.g. 'Patella, base (upper
    border)'), from the server's static capability list. [] if unknown — the UI then
    falls back to showing the index."""
    for combo in morphometry_client.capabilities():
        if combo.get("mode") == mode:
            return combo.get("landmark_names") or []
    return []


# Re-check after a landmark correction. Deliberately a small, self-contained call rather
# than a turn of the main chat loop: chat() replays the whole conversation — including the
# base64 radiograph in the first message, every tool result and every retrieved guideline —
# on every LLM call, and can iterate several times. For a task that is "compare these few
# numbers against ranges we already have", that was costing minutes for seconds of work.
# Everything this needs is passed in explicitly below.
# Framed as TRANSCRIPTION, not verification: the labels were recomputed in code and are
# binding — the model's only job is making the prose match them. The old "Prüfe, ob der
# Bericht geändert werden muss" phrasing invited the reasoning model to re-derive every
# verdict against the full guideline corpus: measured 5-9k chain-of-thought tokens
# (~45-80s) for a ~270-token answer. With the task pinned down (and thinking disabled at
# the call site) the same rewrite takes ~2s with identical output.
RECHECK_PROMPT = """Der Nutzer hat die Landmarken manuell korrigiert. Messwerte und deren
Einordnung (normal/abnormal) wurden bereits NEU BERECHNET und stehen FEST. Deine Aufgabe
ist NUR, Befund und Beurteilung des Berichts an die feststehende Einordnung anzupassen —
KEINE erneute Prüfung, KEINE eigene Herleitung.

Aktueller Bericht:
{report}
{user_note}
Feststehende Messwerte:
{measurements}

Feststehende Einordnung (verbindlich, nicht neu bewerten):
{classification}

Geänderte Einordnung gegenüber der letzten Prüfung — der Grund für die Anpassung:
{changes}

Leitlinientexte (bereits abgerufen; die EINZIGE zulässige Quelle für Normbereiche und
Interpretationen):
{guidelines}

Regeln:
- Übernimm Messwerte und Einordnung EXAKT wie oben; erfinde nichts, bewerte nichts neu.
- MESSUNGSNAMEN: Verwende AUSSCHLIESSLICH die oben aufgeführten Bezeichnungen, Zeichen
  für Zeichen. KEINE eigenen Übersetzungen, Abkürzungen oder neuen Namen.
- Messungen mit Einordnung "unbekannt" (kein freigegebener Referenzbereich) erscheinen
  NUR in der Morphometrie-Liste (die steht bereits fest): Erwähne oder bewerte sie in
  Befund und Beurteilung NICHT.
- Der Bericht ist ein eigenständiges klinisches Dokument: KEINE Erwähnung von
  Korrekturen, Aktualisierungen, Landmarken, Neuberechnungen oder sonstigen
  Verarbeitungsschritten in "findings"/"impression" — formuliere neutral, als wäre der
  Bericht so erstellt worden. Was sich geändert hat, gehört AUSSCHLIESSLICH in "message".
- "findings" bleibt LEER ([]): keine nicht-morphometrischen Aussagen (Frakturen,
  Gelenkspalt, Weichteile o. Ä.).
- Die Beurteilung ("impression"): 1-2 anatomisch präzise Sätze, pathologische Werte
  zuerst, unauffällige zusammengefasst; KEINE Zahlenwerte (die stehen maschinell
  gesichert in der Morphometrie-Liste), keine Konfidenzwerte. Aus einem einzelnen
  Surrogatparameter keine Diagnose ableiten ("Hinweis auf …", nie Beweis; normaler
  Wert schließt nichts aus).
- Formulierungen zu Referenzbereichen/Interpretationen AUSSCHLIESSLICH aus den
  Leitlinientexten; Begriff "Referenzbereich" verwenden. RICHTUNGSDIAGNOSEN
  (Hochstand/Tiefstand, alta/baja o. Ä.) NUR, wenn der Leitlinientext sie für GENAU
  diese Abweichungsrichtung ausdrücklich nennt — sonst neutral „Wert über/unter dem
  Referenzbereich", NIEMALS aus eigenem Wissen.
- NUR gesicherte Aussagen im Indikativ, kein Konjunktiv, keine Spekulation.
- Alle Textinhalte auf DEUTSCH.

Schreibe NUR die inhaltliche Beurteilung neu. Technik und Messwerte werden NICHT von dir
übernommen (die stehen bereits fest) — gib sie nicht aus.

Antworte mit GENAU EINEM JSON-Objekt, ohne weiteren Text:
{{"message": "<kurz: was hat sich geändert und warum, 1-3 Sätze — Zahlen hier erlaubt>",
  "findings": [], "impression": "<1-2 Sätze, OHNE Zahlenwerte und OHNE Bereichsangaben; Begriff „Referenzbereich", nie „Normbereich"; Richtungsdiagnosen (alta/baja, Hoch-/Tiefstand) NUR mit ausdrücklichem Leitlinienbeleg, sonst neutral ‚über/unter dem Referenzbereich'>"}}"""


# Grounded, structured normal/abnormal call per measurement. Deliberately a SEPARATE
# LLM call rather than something parsed out of the report prose: the question that matters
# is whether a landmark correction *flips the label*, so the label has to be a stable
# artefact we can diff, not a sentence that gets rewritten every run.
CLASSIFY_PROMPT = """Ordne jeden der folgenden Messwerte als normal oder abnormal ein.

Messwerte:
{measurements}

Leitlinientexte (die EINZIGE zulässige Quelle für Normbereiche):
{guidelines}

Regeln:
- Stütze dich AUSSCHLIESSLICH auf die obigen Leitlinientexte. Nutze KEIN eigenes
  medizinisches Vorwissen und erfinde keine Normbereiche.
- Findest du für eine Messung keinen belegten Normbereich, ist das Urteil "unbekannt".
- "reference" enthält den Normbereich wörtlich aus dem Leitlinientext, sonst "".
- Nennt der Leitlinientext statt eines Bereichs einen Mittelwert mit Streuung ("0.64 +/- 0.06"),
  bilde den Bereich als Mittelwert − Streuung bis Mittelwert + Streuung (genau EINE Streuung)
  und nimm dafür den Wert für Gesunde, nie den einer Krankheitsgruppe; "reference" nennt dann
  Bereich und Quellform, z. B. "0,58 - 0,70 (aus 0,64 +/- 0,06)".

- Gib den Normbereich ZUSÄTZLICH als Zahlen an ("lower"/"upper"), damit er später ohne
  erneute Modellabfrage angewendet werden kann. Fehlt eine Grenze (z. B. "< 10°"), setze
  die andere auf null. Ohne belegten Normbereich: beide null.

Antworte mit GENAU EINEM JSON-Objekt, ohne weiteren Text:
{{"classifications": {{"<Messungsname>": {{"verdict": "normal"|"abnormal"|"unbekannt",
  "reference": "<Normbereich aus der Leitlinie oder \\"\\">",
  "lower": <Zahl oder null>, "upper": <Zahl oder null>}}}}}}"""


class AgentSession:
    """Holds the running conversation, image and report for one case."""

    def __init__(
        self,
        config: Config,
        image_path: str,
        num_references: int | None = None,
        anatomy_key: str = "",
        projection_key: str = "",
        case_id: str | None = None,
    ):
        self.config = config
        self.llm = LLM(config)
        self.image_path = image_path
        self.num_references = num_references  # morphometry accuracy (refs); None -> default
        # Caller-supplied ids let a batch run key cases by dataset name; the timestamp
        # default is second-resolution and collides for sessions started in parallel.
        self.case_id = case_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        # Optional event recorder, attached by an extension (see plugins/) when a session is
        # being recorded; None in ordinary use. Any object with `log(event, payload)` works.
        # Purely observational: nothing in the pipeline reads it, it only receives the
        # audit events and state snapshots.
        self.recorder = None
        self.last_active = time.time()  # bumped on chat; used by the cleanup reaper
        self.report: Report | None = None
        self.anatomy = ""            # German display, from assess_image
        self.projection = ""
        self.anatomy_key = ""        # canonical service token (e.g. "knee"), from assess_image
        self.projection_key = ""
        # A reader-supplied anatomy/projection overrides the model's own assessment: the
        # keys are seeded here and assess_image is skipped entirely. Which of the two chose
        # the combination is a run condition (it changes the input to morphometry), so it
        # is carried on the session and written to the audit trail — not just displayed.
        self.anatomy_source = "model"  # "model" | "user"
        if anatomy_key or projection_key:
            self.anatomy_key = anatomy_key.strip().lower()
            self.projection_key = projection_key.strip().lower()
            self.anatomy = self.anatomy_key
            self.projection = self.projection_key
            self.anatomy_source = "user"
        # Is the current combination one the morphometry service actually offers? Recorded
        # for the UI badge and the audit trail ONLY — it deliberately does not gate anything.
        # What to do about an unsupported region is the model's call (see the prompt), so
        # that it can still explain itself to the reader instead of hitting a silent wall.
        self.supported: bool | None = None
        # -- morphometry landmark state ------------------------------------
        # `landmarks_predicted` is what the pipeline matched and never changes; `landmarks`
        # is what the reader currently sees (identical until they drag one). Keeping both
        # apart is what lets us offer "reset to prediction" and keeps the AI's geometry
        # comparable against the corrected geometry afterwards.
        self.landmarks: list[list[float]] = []
        self.landmarks_predicted: list[list[float]] = []
        self.landmarks_edited = False
        self.landmark_names: list[str] = []      # per-index display names, from capabilities
        # Construction geometry per measurement, in the same [x, y] space as the landmarks:
        # {name: {value, kind, unit, landmark_indices, points, solid_lines, dotted_lines}}.
        # Recomputed server-side on every edit; the UI draws it as SVG.
        self.geometry: dict = {}
        self.measurements: dict = {}             # current values (corrected, if edited)
        self.measurements_predicted: dict = {}   # values as the pipeline first computed them
        self.mode = ""                           # e.g. "knee_lateral" — for stateless recompute
        self.mpp: float | None = None            # mm per pixel — ditto
        # Outcome of the last morphometry call. When it is not "ok" the agent still writes a
        # report — just one without measurements — which reads perfectly plausible. Surfacing
        # the status lets the UI say so out loud, so a case that was never actually measured
        # can't be mistaken for a normal one.
        self.morpho_status = ""
        self.morpho_message = ""
        self.morpho_result_token = ""            # handle for visualize_measurement
        # Structured normal/abnormal call per measurement, grounded ONLY in this case's
        # retrieved guideline passages. This is the case's "label"; the report's prose is
        # not a reliable place to read it from.
        # Who LAST wrote the current Beurteilung: "user" only while the text verbatim IS
        # the reader's saved wording (text comparison, never intent), else "model". The
        # UI badges only the user state — a model rewrite is already announced by the
        # status chip. Protection against free rewrites is SEPARATE and sticky: it keys
        # on user_impression_text (has the reader ever authored), not on the display
        # state. Reset only by a fresh analysis.
        self.impression_author = "model"
        self.user_impression_text = ""
        # Cooperative cancellation (UI stop button): checked at every step boundary of the
        # analysis/chat loops. A step already in flight finishes server-side; the UI has
        # long since detached. Threads cannot be killed — this is the honest alternative.
        self.cancel_requested = False
        # Reader-facing "Denkmodus" toggle (header switch): False disables the reasoning
        # model's chain-of-thought for the pipeline's LLM calls — much faster, terser
        # deliberation. Default True = the models' native behaviour. The recheck rewrite
        # ignores this and stays hard-off (a transcription task, nothing to deliberate).
        self.thinking = True
        self.classification: dict = {}           # baseline, from the predicted landmarks
        # Classification at the LAST re-check — what the current report narrative was
        # written against. Rewrites are driven by "changed since the last check", NOT by
        # "changed vs the AI baseline": with the baseline as anchor, correcting a value
        # back to its original band silently skipped the rewrite and left the narrative
        # asserting the intermediate (now wrong) state. The frozen baseline stays what it
        # is — the AI's own label, before any correction — this is a separate cursor.
        self.last_checked_classification: dict | None = None
        # {measurement: {lower, upper, reference}} — extracted from the guideline text ONCE,
        # then applied in code. Re-checking a corrected value is then arithmetic, not another
        # LLM round trip.
        self.reference_ranges: dict = {}
        # The verdict for the landmarks as they stand right now, or None if they have been
        # moved since the last check. The UI badge reads this rather than tracking it
        # itself — otherwise "already checked" and "just edited again" can drift apart, and
        # the reader is told their correction was incorporated when it wasn't.
        self.recheck_outcome: dict | None = None
        # The baseline classification is an extra LLM call that nobody is waiting for, so it
        # runs in the background once the report is out. reclassify() joins this before
        # comparing, so the reader can never race it.
        self._baseline_thread: threading.Thread | None = None
        self.guideline_hits: list[dict] = []     # every passage retrieved this case (deduped)
        # When the server corrected laterality (mirrored the image), the radiograph it
        # actually measured on, as a data URL — the UI displays this so the image matches
        # the landmark/measurement space. Empty when no mirror happened (upload already
        # matches) or the server can't supply it. The user's uploaded file is never modified.
        self.processed_image = ""
        self.laterality_corrected = False  # did the server mirror the image for laterality?
        # Measurement visualizations generated THIS turn (for the UI image-panel tabs).
        self.new_visualizations: list[dict] = []
        self.last_reply = ""  # the model's conversational reply for the latest turn
        self.log: list[str] = []          # human-readable trace of tool calls
        self.messages: list[dict] = []    # full LLM conversation
        # Structured reasoning of the MOST RECENT turn (for the UI "thinking" view).
        # Reset at the start of each run_analysis() / chat() call.
        self.last_steps: list[dict] = []
        # Optional callback fired with each step as it happens (for live streaming).
        self.on_step: Callable[[dict], None] | None = None

    # What a step is, without what it produced: which tool, what it was asked, how long it
    # took. Enough to follow what the agent did and in what order — the results are large
    # (landmark arrays, guideline passages) and already recorded where they belong, in the
    # baseline and reclassify events and the snapshots.
    _STEP_DIGEST = ("type", "tool", "purpose", "query", "count", "outcome", "mode", "seconds")

    def _emit(self, step: dict) -> None:
        self.last_steps.append(step)
        # The agent's own trace, into the recorded stream. Without it a recording showed a
        # reader waiting 19 s with nothing to say what the agent was doing, so a slow turn
        # could not be told apart from a turn that did more work. Written here because
        # _emit is the single funnel every step type passes through.
        if self.recorder is not None:
            digest = {k: step[k] for k in self._STEP_DIGEST if k in step}
            if step.get("type") == "guideline":
                digest["query"] = str(step.get("query", ""))[:200]
            # A step whose digest is nothing but its type says only "something happened",
            # which model_wait_start/end already says. Thoughts and status lines produced
            # dozens of those per turn, burying the tool calls that carry the answer.
            if len(digest) > 1:
                self.recorder.log("agent_step", digest)
        if self.on_step is not None:
            try:
                self.on_step(step)
            except Exception:
                pass  # a broken listener must never break the agent

    def _record_thought(self, action: dict, seconds: float | None = None) -> None:
        thought = (action.get("thought") or "").strip()
        if thought:
            self._emit({"type": "thought", "text": thought, "seconds": _round(seconds)})

    def _record_tool(
        self, name: str, arguments: dict, result: dict, seconds: float | None = None
    ) -> None:
        # Drop the (fixed) image path from the displayed args to reduce noise.
        shown = {k: v for k, v in arguments.items() if k != "image"}
        self._emit(
            {
                "type": "tool",
                "tool": name,
                "arguments": shown,
                "result": result,
                "seconds": _round(seconds),
            }
        )

    def _emit_guideline(self, purpose: str, query: str, hits: list[dict]) -> None:
        """Record a (visible) guideline retrieval as its own step type. Includes the
        source passage text so the UI can show, on demand, exactly which guideline
        excerpt informed the agent."""
        # Keep every passage this case saw (deduped by text), so a later re-classification
        # is judged against exactly the same evidence as the original one.
        seen = {h.get("text") for h in self.guideline_hits}
        self.guideline_hits += [h for h in hits if h.get("text") not in seen]
        self._emit(
            {
                "type": "guideline",
                "purpose": purpose or "Leitlinien-Suche",
                "query": query,
                "count": len(hits),
                "results": [
                    {
                        "title": h.get("title", ""),
                        "score": h.get("score"),
                        "text": h.get("text", ""),
                        "document_id": h.get("document_id"),
                    }
                    for h in hits
                ],
            }
        )

    def attach_recorder(self, recorder, session_id: str) -> None:
        """Route this session's audit events to `recorder` from now on. A recorder that
        knows the case's own id (e.g. a dataset name) replaces the timestamp default."""
        self.recorder = recorder
        if getattr(recorder, "case_id", None):
            self.case_id = recorder.case_id
        recorder.log("agent_session_bound", {"session_id": session_id})

    def _audit(self, event: str, payload: dict) -> None:
        """Route one server-side event to the single log that owns this run: the attached
        recorder when the session is being recorded, the per-case audit trail when that is
        enabled (headless batch runs), and nowhere for a casual one-off. One destination,
        never two."""
        if self.recorder is not None:
            self.recorder.log(event, payload)
        else:
            audit.log(self.case_id, event, payload)  # a no-op unless audit.enabled

    def snapshot(self, reason: str) -> None:
        """Checkpoint the full case state into the recorder's stream.

        Milestones only. Replay applies deltas, but a snapshot OVERWRITES, so a gap in the
        deltas cannot propagate past one — which is what keeps a single dropped event from
        invalidating a whole session."""
        if self.recorder is None:
            return
        payload = self.to_payload()
        # The pixels live in the dataset; a base64 blob per snapshot would dwarf the log.
        for key in ("processed_image", "visualizations", "steps", "trace"):
            payload.pop(key, None)

        # THE RECORD MATCHES WHAT THE READER SAW. to_payload carries the raw tool result —
        # every '(robust)' twin and the measurements the allowlist excludes — because the
        # UI and the batch runner want it. A snapshot must not: it is the state the reader
        # worked in, so it lists the allowlisted measurements under the same German names
        # the report uses. That also makes it joinable with records that carry a reader's
        # chosen label, where nothing would have matched 'Hallux Valgus Angle'.
        # The unfiltered tool output is NOT lost: it stays in the `baseline` and
        # `landmarks_corrected` events, which are the morphometry record.
        for key in ("measurements", "measurements_predicted"):
            if isinstance(payload.get(key), dict):
                payload[key] = mreg.as_recorded(payload[key])
        for key in ("classification", "reference_ranges"):
            if isinstance(payload.get(key), dict):
                payload[key] = mreg.keys_as_recorded(payload[key])

        # WHAT THE MODEL SAID BACK. The stream recorded every word the reader wrote and
        # none of the model's: a reader could be seen asking a question and then rewriting
        # their Beurteilung, with the answer that prompted it recorded nowhere. Taken here
        # because one place brackets every model turn (api._stream_run), so analysis, chat
        # and re-check are all covered without a call at each return.
        payload["message"] = self.last_reply

        self.recorder.log("state_snapshot", {"reason": reason, **payload})

    def to_payload(self) -> dict:
        """Everything known about this case, as one serializable dict.

        The single source of truth for exporting a session — the API's analyze/chat
        responses and headless batch runs both use it, so a field added here
        reaches the UI and the batch output at the same time.
        """
        report = self.report
        return {
            "report_markdown": report.to_markdown() if report else "",
            "report": dataclasses.asdict(report) if report else None,
            "impression_author": self.impression_author,  # last writer (text-derived)
            # Sticky: the reader has authored the Beurteilung at some point, so model
            # rewrites run under the minimal-diff contract. Together with the author
            # field this reconstructs the full provenance (user / revised-user / model)
            # for eval and audit consumers, while the UI shows only the user badge.
            "impression_protected": bool(self.user_impression_text),
            "trace": self.log,
            "steps": self.last_steps,  # structured reasoning of the latest turn
            "landmarks": self.landmarks,  # current (possibly reader-corrected) landmarks
            "landmarks_predicted": self.landmarks_predicted,  # the AI's geometry, for "reset"
            "landmark_names": self.landmark_names,  # per-index labels for the drag handles
            "landmarks_edited": self.landmarks_edited,
            "geometry": self.geometry,  # per-measurement construction, drawn as SVG
            "measurements": self.measurements,
            # Non-"ok" -> the case was never actually measured; the UI says so explicitly
            # rather than letting a measurement-free report look like a normal one.
            # None -> the current landmarks have not been checked yet (the badge reads this).
            "recheck": self.recheck_outcome,
            "morpho_status": self.morpho_status,
            "morpho_message": self.morpho_message,
            # The anatomy/projection the case actually ran with, as canonical service keys,
            # plus who chose them. Drives the editable chips in the UI.
            "anatomy_key": self.anatomy_key,
            "projection_key": self.projection_key,
            "anatomy_source": self.anatomy_source,  # "model" | "user"
            "supported": self.supported,
            "processed_image": self.processed_image,  # mirrored image the server measured on ("" if no mirror)
            "laterality_corrected": self.laterality_corrected,  # did the server mirror for laterality?
            "visualizations": self.new_visualizations,  # static PNGs; only when /recompute is absent
            "case_id": self.case_id,
            # -- grounding trace -----------------------------------------------------
            # The label, the thresholds behind it, and every passage this case saw.
            # Carried here rather than derived later so a report's numbers and bounds can be
            # audited against their sources without re-running anything.
            "classification": self.classification,
            "reference_ranges": self.reference_ranges,  # {name: {lower, upper, reference, source}}
            "guideline_hits": self.guideline_hits,      # deduped passages, with scores
        }

    def wait_for_baseline(self, timeout: float | None = None) -> None:
        """Block until the background baseline classification has finished.

        The API never needs this (reclassify() joins on demand), but a headless batch
        run serializes the session right after run_analysis() and would otherwise
        race the thread and dump an empty classification.
        """
        if self._baseline_thread is not None:
            self._baseline_thread.join(timeout)

    def _execute_tool(self, name: str, arguments: dict, user_requested: bool = False) -> dict:
        """Run one tool call. Guideline lookups go to the guideline search and
        produce a 'guideline' step; image tools hit the registry and produce a
        'tool' step. Returns the observation dict fed back to the model."""
        tag = " (vom Nutzer angefordert)" if user_requested else ""
        if name == "search_guidelines":
            query = str(arguments.get("query", "")).strip()
            purpose = str(arguments.get("purpose", "")).strip()
            # Add each named measurement in both languages, so recall does not depend on
            # the query's language (German corpus today, English sources possible later).
            search_query = mreg.bilingual_query(query)
            hits = store.retrieve(search_query) if search_query else []
            if search_query != query:
                self.log.append(f"Leitlinie: '{query}' (Begriffe ergänzt) → {len(hits)} Treffer{tag}")
            else:
                self.log.append(f"Leitlinie: '{query}' → {len(hits)} Treffer{tag}")
            self._emit_guideline(purpose, query, hits)
            return {
                "count": len(hits),
                "results": [
                    {"title": h["title"], "text": h["text"], "score": h["score"]} for h in hits
                ],
            }

        if name == "morphometry":
            # Canonical tokens from the model's assessment (chosen from the live
            # supported list); fall back to best-effort keyword mapping if missing.
            anatomy_key, projection_key = self.anatomy_key, self.projection_key
            if not (anatomy_key and projection_key):
                fa, fp = morphometry_client._tokens(self.anatomy, self.projection)
                anatomy_key = anatomy_key or (fa or "")
                projection_key = projection_key or (fp or "")
            # Announce the call BEFORE making it. The `tool` step reports how long it took,
            # so it cannot be sent until the call returns — which leaves the longest silent
            # stretch of the whole run (~15s at 10 references, up to a minute at 40)
            # showing nothing at all. The reader sees the region, then a dead pause, and
            # reads it as the model being slow to start. The pipeline already narrates when
            # the geometry ARRIVES; this is the missing other half.
            self._emit(
                {
                    "type": "thought",
                    "text": (
                        f"Morphometrie läuft: Landmarken-Matching für "
                        f"{anatomy_key or '?'}/{projection_key or '?'} auf dem GPU-Dienst"
                        + (f" ({self.num_references} Referenzen)" if self.num_references else "")
                        + "…"
                    ),
                }
            )
            t1 = time.perf_counter()
            result = morphometry_client.measure(
                self.image_path, anatomy_key, projection_key, self.num_references
            )
            secs = time.perf_counter() - t1
            # Keep landmarks (UI overlay) and the result_token (for visualization) out
            # of what the model/reasoning step see; visualizable_measurements stays so
            # the model knows the exact names it may visualize.
            self.landmarks = result.pop("landmarks", []) or []
            self.landmarks_predicted = [list(p) for p in self.landmarks]  # immutable baseline
            self.landmarks_edited = False
            self.morpho_result_token = result.pop("result_token", "") or ""
            # mode/mpp are plumbing for stateless recompute, not something to reason about.
            self.mode = result.pop("mode", "") or ""
            self.mpp = result.pop("mpp", None)
            # Allowlist: measurements absent from the curated registry are dropped here, at
            # the tool boundary, so nothing downstream (model, report, chips, arrows) ever
            # sees a measurement we chose not to use.
            self.measurements = mreg.filter_supported(dict(result.get("measurements") or {}))
            self.measurements_predicted = dict(self.measurements)
            self.landmark_names = _landmark_names_for(self.mode)
            self.morpho_status = str(result.get("status") or "")
            self.morpho_message = str(result.get("message") or result.get("detail") or "")
            # Landmarks and geometry are in the PROCESSED image's pixel frame, so that is the
            # image the reader must drag on — always fetch it, not just when laterality was
            # corrected. Making the overlay's correctness depend on a flag staying in sync
            # invites a silent misalignment; fetching unconditionally cannot drift.
            self.laterality_corrected = bool(result.get("laterality_corrected"))
            if self.morpho_result_token:
                self.processed_image = (
                    morphometry_client.processed_image(self.morpho_result_token) or ""
                )
            self.log.append(
                f"→ morphometry({self.anatomy}/{self.projection}) [{secs:.1f}s] "
                f"status={result.get('status')} conf={result.get('mean_confidence')}{tag}"
            )
            self._record_tool(
                "morphometry",
                {"anatomy": self.anatomy, "projection": self.projection},
                result,
                secs,
            )
            # Quality metrics stay OUT of the model's view entirely: they belong to quality
            # assurance (trace + recorded step above), not to the generation. The model
            # cannot mention what it never saw. Kept on the session for a future quality gate.
            self.morpho_confidence = result.pop("mean_confidence", None)
            result.pop("matching", None)
            # Sanctioned German names for prose, from the curated registry. Without them
            # the model only ever sees English tool names and translates on its own —
            # which produced invented terms ("Tibiaschuhwinkel"). Whatever it may write,
            # it has now been GIVEN, character for character.
            if result.get("measurements"):
                # (X, X (robust)) pairs collapse to the base name with the robust value —
                # the model never learns two near-identical variants exist, and the
                # session/audit keep the full raw set.
                result["measurements"] = {
                    k: (mreg.round_value(k, v) if isinstance(v, (int, float)) else v)
                    for k, v in mreg.preferred_measurements(
                        mreg.filter_supported(result["measurements"])
                    ).items()
                }
                result["display_names"] = {
                    k: mreg.display_name(k) for k in result["measurements"]
                }
            if result.get("visualizable_measurements"):
                result["visualizable_measurements"] = [
                    m for m in result["visualizable_measurements"] if mreg.is_supported(m)
                ]
            if result.get("available_measurements"):
                result["available_measurements"] = [
                    m for m in result["available_measurements"] if mreg.is_supported(m)
                ]
            # Bootstrap the construction geometry from the PREDICTED landmarks, so the UI
            # can draw (and then drag) every measurement as SVG. One cheap CPU call.
            if self.landmarks and self.mode:
                geo = morphometry_client.recompute(
                    self.mode, self.mpp, self.landmarks, self.morpho_result_token
                )
                if geo.get("status") == "ok" and geo.get("geometry"):
                    self.geometry = mreg.filter_supported(geo["geometry"])
                    # Take the baseline values from /recompute too, not from the pipeline's
                    # own run. They agree to ~1e-15, but every later value comes from
                    # /recompute, and "did this number change?" must not be answerable by
                    # floating-point noise — both in the UI's edited-highlight and in the
                    # recorded before/after comparison.
                    if geo.get("measurements"):
                        self.measurements = mreg.filter_supported(geo["measurements"])
                        self.measurements_predicted = dict(self.measurements)
                    self.log.append(f"Geometrie geladen: {len(self.geometry)} Messungen")
                else:
                    # Say so, loudly. Everything interactive hangs off this one call, and
                    # the fallback below is a silent, plausible-looking downgrade: the
                    # reader just gets static overlays at the end of the run and no way to
                    # correct anything. A morphometry server deployed without /recompute
                    # once caused exactly this downgrade, and it went unnoticed because
                    # nothing anywhere said it out loud.
                    detail = geo.get("message") or geo.get("status") or "unbekannt"
                    self.log.append(
                        f"Geometrie NICHT verfügbar ({detail}) — der Morphometrie-Dienst "
                        "beantwortet /recompute nicht. Landmarken sind nur statisch "
                        "sichtbar und nicht korrigierbar."
                    )
                    self._emit(
                        {
                            "type": "thought",
                            "text": (
                                "Der Morphometrie-Dienst liefert keine Konstruktionsgeometrie "
                                "(/recompute nicht verfügbar). Die Landmarken werden statisch "
                                "angezeigt und können nicht korrigiert werden."
                            ),
                        }
                    )

            # Hand the geometry to the UI RIGHT NOW, as its own step, instead of letting it
            # ride along on the final payload. The measurements are ready here; everything
            # that follows (guideline lookups, report writing, classification) is LLM work on
            # a sometimes-slow model server. Waiting for it would leave the reader staring at
            # a spinner for minutes with the image already measured.
            if self.geometry:
                # A visible narration line (the geometry step itself is UI-state, no text).
                self._emit(
                    {
                        "type": "thought",
                        "text": (
                            f"{len(self.geometry)} Messungen berechnet; Landmarken und "
                            "Konstruktionslinien geladen — zur manuellen Korrektur bereit."
                        ),
                    }
                )
                self._emit(
                    {
                        "type": "geometry",
                        "landmarks": self.landmarks,
                        "landmarks_predicted": self.landmarks_predicted,
                        "landmark_names": self.landmark_names,
                        "geometry": self.geometry,
                        "measurements": self.measurements,
                        "processed_image": self.processed_image,
                        "laterality_corrected": self.laterality_corrected,
                    }
                )

            # Fallback for a morphometry server that predates /recompute: no live geometry,
            # so fall back to the old behaviour and render each measurement as a static PNG.
            if not self.geometry:
                visualizable = result.get("visualizable_measurements", [])
                if self.morpho_result_token and visualizable:
                    t2 = time.perf_counter()
                    self.new_visualizations = morphometry_client.visualize_all(
                        self.morpho_result_token, visualizable
                    )
                    self.log.append(
                        f"Visualisierungen erzeugt: {len(self.new_visualizations)} "
                        f"[{time.perf_counter() - t2:.1f}s] (statisch — /recompute fehlt)"
                    )
                    if self.new_visualizations:
                        self._emit(
                            {"type": "visualization",
                             "text": f"{len(self.new_visualizations)} Messungen visualisiert"}
                        )
            return result

        arguments["image"] = self.image_path  # fixed per case; model can't override
        self.log.append(f"→ {name}({arguments}){tag}")
        t1 = time.perf_counter()
        result = dispatch(name, arguments)
        tool_secs = time.perf_counter() - t1
        self.log.append(f"← [{tool_secs:.2f}s] {json.dumps(result)[:300]}")
        self._record_tool(name, arguments, result, tool_secs)
        return result

    def _baseline_classification(self) -> None:
        """Freeze the label the AI's own geometry produced, before any reader correction —
        the 'before' half of the label-change comparison. Recorded even when the
        reader never touches a landmark (that's the `accepted` outcome)."""
        if not self.measurements_predicted:
            return

        # The report already carries each range the agent looked up ("… (Normbereich: 0,8 –
        # 1,2)"). Read them from there: it is instant, it is grounded in exactly what the
        # agent asserted, and it means the reader's first re-check does not have to sit and
        # wait for an LLM call that can take minutes on a slow model server.
        if self.report:
            for name in self.measurements_predicted:
                if name in self.reference_ranges:
                    continue
                line = _match_report_line(name, self.report.measurements)
                rng = _parse_range(line)
                if rng:
                    self.reference_ranges[name] = rng
            got = len(self.reference_ranges)
            self.log.append(
                f"Normbereiche aus dem Bericht übernommen: {got}/{len(self.measurements_predicted)}"
            )

        # Only what could not be read falls back to the model (usually nothing).
        self.classification = self.classify(self.measurements_predicted)
        self._attach_range_sources()
        # Re-lock the Morphometrie lines with the final classification: references the
        # LLM extraction just added become visible in the report list too.
        if self.report:
            self.report.measurements = self._measurement_lines(self.classification)
        self._audit(
            "baseline",
            {
                "mode": self.mode,
                "landmarks_predicted": self.landmarks_predicted,
                "measurements_predicted": self.measurements_predicted,
                "classification": self.classification,
                "reference_ranges": self.reference_ranges,  # incl. the source span per bound
            },
        )

    def _ingest_reference_ranges(self, raw: dict | None) -> None:
        """Take the model's STRUCTURED reference ranges (final_report's 'reference_ranges')
        into the numeric store — the authoritative source for verdicts, the locked lines,
        the re-check and the out-of-range arrows.

        Numbers, not prose: this is what frees everything downstream from re-reading the
        model's ever-varying phrasing ('> 7', 'pathologisch ≤ 7 mm', 'bis 11'), which the
        text parser cannot disambiguate (and which silently inverted 'pathologisch'). A
        one-sided range is normal here — the absent bound is null and never triggers a
        verdict on that side. Stored BEFORE the prose fallback runs, so the model's numbers
        win and _parse_range only fills what the model did not ground. Provenance is
        literature (this case's own guideline hits); an unmatched bound keeps source=None,
        which the faithfulness audit counts as an untraceable threshold."""
        if not isinstance(raw, dict):
            return
        for name, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            canonical = self._resolve_measurement(name) or name  # exact-or-key, never fuzzy
            lower, upper = _num(spec.get("lower")), _num(spec.get("upper"))
            if lower is None and upper is None:
                continue  # no bound to classify against -> leave the measurement ungrounded
            reference = str(spec.get("reference") or "").strip() or " – ".join(
                f"{b:g}" for b in (lower, upper) if b is not None
            )
            rng = {"lower": lower, "upper": upper, "reference": reference}
            rng["source"] = _range_source(rng, self.guideline_hits, canonical)
            self.reference_ranges[canonical] = rng

    def _attach_range_sources(self) -> None:
        """Link every reference range to the retrieved passage that states it.

        Idempotent (an entry with a 'source' key is left alone), so it can be called after
        each path that can add a range. A range whose source stays None is an untraceable
        threshold — kept as such, because that is exactly what the faithfulness audit counts.
        """
        for name, rng in self.reference_ranges.items():
            if "source" not in rng:
                rng["source"] = _range_source(rng, self.guideline_hits, name)

    # -- landmark refinement (reader in the loop) --------------------------
    @property
    def _enable_thinking(self) -> bool | None:
        """What the pipeline's LLM calls pass as enable_thinking: None = backend default
        (thinking on) while the Denkmodus toggle is on, False when the reader switched
        deliberation off."""
        return None if self.thinking else False

    def classify(self, measurements: dict, allow_llm: bool = True) -> dict:
        """Normal/abnormal per measurement, grounded ONLY in this case's retrieved
        guideline passages. Returns {name: {value, verdict, reference}}; {} if there is
        nothing to judge or the model's reply is unusable (never raises).

        The LLM is used ONCE, to extract each measurement's reference range from the
        guideline text. After that the range is a pair of numbers, and deciding whether a
        value falls inside it is arithmetic — so every later call (i.e. every re-check after
        a landmark correction) is instant and perfectly reproducible: the label can no
        longer wobble with the model's mood.

        Values are rounded to their REPORTED precision (registry) before classification —
        laboratory convention: the verdict belongs to the number the reader sees, so a
        displayed "1,2" can never be labelled outside a 0,8–1,2 range. NOTE: this coarsens
        the label-flip granularity to the display precision — sub-rounding jitter can no
        longer produce a label flip.
        """
        values = {
            k: mreg.round_value(k, v)
            for k, v in mreg.preferred_measurements(measurements or {}).items()
            if v is not None
        }
        if not values:
            return {}

        # Per measurement, not all-or-nothing. A case has 8 measurements but the report only
        # interprets the ones with guideline support, so requiring EVERY range to be known
        # meant this fast path never fired and every re-check fell back to the model.
        # A measurement without a range is simply not assessable — that is an answer
        # ("unbekannt"), not a reason to go ask an LLM to invent one.
        if self.reference_ranges or not allow_llm:
            out = {}
            for name, value in values.items():
                rng = self.reference_ranges.get(name)
                out[name] = {
                    "value": value,
                    "verdict": _verdict(value, rng) if rng else "unbekannt",
                    "reference": (rng or {}).get("reference", ""),
                }
            return out

        prompt = CLASSIFY_PROMPT.format(
            measurements="\n".join(f"- {k}: {v}" for k, v in values.items()),
            guidelines=store.format_for_prompt(self.guideline_hits) or "(keine)",
        )
        try:
            reply = self.llm.complete(
                [{"role": "user", "content": prompt}],
                temperature=0,
                enable_thinking=self._enable_thinking,
            )
            parsed = _extract_json(reply).get("classifications", {}) or {}
        except Exception:
            # Classification is an adjunct: it must never take down a run that has already
            # produced a report (it is called after final_report). An empty result degrades
            # to "no baseline label", not to a lost case.
            self.log.append("Einordnung fehlgeschlagen (Modell nicht erreichbar).")
            return {}
        # Match the model's keys loosely. It will happily answer with "Hallux-Valgus-Winkel"
        # for "Hallux Valgus Angle", and an exact-key lookup would then silently record every
        # measurement as "unbekannt" — which later reads as a label change when the next pass
        # happens to use the exact name. That would fabricate correction-induced label
        # changes out of nothing but LLM phrasing, straight into the recorded outcomes.
        loose = {_norm_key(k): v for k, v in parsed.items()}

        out = {}
        for name, value in values.items():
            entry = parsed.get(name) or loose.get(_norm_key(name)) or {}
            verdict = str(entry.get("verdict", "unbekannt")).strip().lower()
            rng = {
                "lower": _num(entry.get("lower")),
                "upper": _num(entry.get("upper")),
                "reference": str(entry.get("reference", "")).strip(),
            }
            # Cache the extracted range so no later check has to ask the model again.
            if rng["lower"] is not None or rng["upper"] is not None:
                self.reference_ranges[name] = rng
                verdict = _verdict(value, rng)  # trust the numbers over the model's own word
            out[name] = {
                "value": value,
                "verdict": verdict if verdict in ("normal", "abnormal") else "unbekannt",
                "reference": rng["reference"],
            }
        self._attach_range_sources()  # ranges the model just extracted also need a source
        return out

    def update_landmarks(self, landmarks: list[list[float]], commit: bool = False) -> dict:
        """Recompute measurements + geometry from reader-edited landmarks.

        Server-authoritative and LLM-free, so it is fast enough to sit inside a drag loop.
        `commit` marks the end of a drag: only then is an audit event written, so the
        audit log holds the positions the reader settled on, not every intermediate frame.
        """
        if not self.mode or not self.landmarks_predicted:
            return {"status": "error", "message": "Keine Morphometrie-Ergebnisse in dieser Sitzung."}
        if len(landmarks) != len(self.landmarks_predicted):
            return {
                "status": "bad_input",
                "message": (
                    f"{len(landmarks)} Landmarken erhalten, "
                    f"{len(self.landmarks_predicted)} erwartet."
                ),
            }

        result = morphometry_client.recompute(
            self.mode, self.mpp, landmarks, self.morpho_result_token
        )
        if result.get("status") != "ok":
            return result

        self.landmarks = [list(p) for p in landmarks]
        # Same allowlist as the initial ingestion, so a re-compute after a landmark edit
        # cannot re-introduce a measurement the registry excludes.
        self.measurements = mreg.filter_supported(result.get("measurements") or {})
        self.geometry = mreg.filter_supported(result.get("geometry") or {})
        self.landmarks_edited = self.landmarks != self.landmarks_predicted
        self.recheck_outcome = None  # the geometry moved: any previous verdict is stale
        self.last_active = time.time()

        if commit:
            self._audit(
                "landmarks_reset" if not self.landmarks_edited else "landmarks_corrected",
                {
                    "mode": self.mode,
                    "landmarks_predicted": self.landmarks_predicted,
                    "landmarks_corrected": self.landmarks,
                    "measurements_predicted": self.measurements_predicted,
                    "measurements_corrected": self.measurements,
                },
            )
        return {
            "status": "ok",
            "measurements": self.measurements,
            "geometry": self.geometry,
            "landmarks_edited": self.landmarks_edited,
        }

    def reset_landmarks(self) -> dict:
        """Discard the reader's corrections and go back to the predicted geometry."""
        return self.update_landmarks(self.landmarks_predicted, commit=True)

    def reclassify(self) -> dict:
        """Re-check the corrected measurements against the guidelines already retrieved
        for this case, and report whether the diagnostic label actually moved.

        This separates corrections that matter from cosmetic ones: the outcome is one of
        accepted / corrected-no-label-change / corrected-label-change. The report text is
        deliberately NOT rewritten here — the reader decides whether to update it, so a
        cosmetic landmark nudge doesn't churn the prose and add noise to any later review
        of the report text."""
        # Everything below runs BEFORE the chat turn and makes its own LLM calls, so without
        # these steps the reader sits through a silent gap and then sees a single short
        # "thought" — which looks like the model idling when it is actually working.
        t0 = time.perf_counter()
        n = len([v for v in (self.measurements or {}).values() if v is not None])
        known = sum(1 for k in self.reference_ranges if k in (self.measurements or {}))
        self._emit(
            {
                "type": "thought",
                "text": (
                    f"Die Landmarken wurden korrigiert. Ich prüfe die {n} neu berechneten "
                    f"Messwerte gegen die {known} Normbereiche, die bereits aus den Leitlinien "
                    "extrahiert wurden — ein reiner Zahlenvergleich, ohne neue Suche und ohne "
                    "erneute Modellabfrage."
                ),
            }
        )

        # The baseline may still be computing in the background; a label comparison against a
        # half-filled 'before' would silently invent label changes, so wait for it.
        if self._baseline_thread is not None and self._baseline_thread.is_alive():
            self._emit(
                {
                    "type": "thought",
                    "text": (
                        "Die ursprüngliche Einordnung (vor der Korrektur) läuft noch im "
                        "Hintergrund — ich warte darauf, um sauber vergleichen zu können."
                    ),
                }
            )
            self._baseline_thread.join(timeout=20)
        if not self.classification:
            # Do NOT run the extraction inline — that is the call that took minutes. Fall back
            # to whatever ranges we have; anything missing is reported as not assessable.
            self.classification = self.classify(self.measurements_predicted, allow_llm=False)

        # Anchor on the last CHECKED state (falling back to the baseline for the first
        # check): the question this answers is "does the report need rewriting?", and the
        # report reflects the last check, not the AI's original geometry.
        before_is_baseline = self.last_checked_classification is None
        before = self.last_checked_classification or self.classification or {}
        # allow_llm=False: the re-check is arithmetic. If a range is missing the answer is
        # "not assessable", which we show — never a minutes-long round trip to find out.
        after = self.classify(self.measurements, allow_llm=False)
        secs = time.perf_counter() - t0

        # A label change is a crossing of a DECISION threshold: normal <-> abnormal. A
        # measurement moving to or from "unbekannt" (no reference range found in the
        # literature) is not a diagnostic flip — it is the classifier finding, or failing to
        # find, a citation. Counting those would inflate the measured rate of label changes
        # with what is really retrieval noise, so they are recorded but kept out of `changes`.
        LABELS = ("normal", "abnormal")
        changes, unresolved = [], []
        for name, new in after.items():
            old = before.get(name, {})
            before_v, after_v = old.get("verdict", "unbekannt"), new["verdict"]
            if before_v == after_v:
                continue
            entry = {
                "measurement": name,
                "value_before": old.get("value"),
                "value_after": new["value"],
                "verdict_before": before_v,
                "verdict_after": after_v,
                "reference": new["reference"],
            }
            if before_v in LABELS and after_v in LABELS:
                changes.append(entry)  # a real flip
            else:
                unresolved.append(entry)  # gained/lost a reference range; not a flip

        # Outcome by what happened to the LABELS since the last check — not by whether the
        # current landmarks differ from the prediction. A reset back to the AI geometry
        # after a checked correction flips labels BACK; that is a label change the report
        # must incorporate, not an "accepted" no-op. ("accepted" = checked while never
        # deviating from the prediction; the audit's landmarks_edited field disambiguates.)
        if changes:
            outcome = "corrected_label_change"
        elif self.landmarks_edited:
            outcome = "corrected_no_label_change"
        else:
            outcome = "accepted"

        moved = [
            f"{name}: {before.get(name, {}).get('value'):.2f} → {new['value']:.2f}"
            for name, new in after.items()
            if isinstance(before.get(name, {}).get("value"), (int, float))
            and abs(before[name]["value"] - new["value"]) > 1e-9
        ]
        # Measurements with no reference range cannot be checked at all. Saying nothing would
        # let them pass as "no label change", which is a silent false negative — exactly the
        # kind of thing that quietly deflates the measured rate of label changes.
        no_range = [n for n, c in after.items() if c["verdict"] == "unbekannt"]
        self._emit(
            {
                "type": "reclassify",
                "outcome": outcome,
                "changes": changes,
                "unresolved": unresolved,
                "no_range": no_range,
                "moved": moved,  # measurements whose VALUE moved (with or without a flip)
                "seconds": _round(secs),
            }
        )

        self.last_checked_classification = after  # the next check diffs against THIS state
        # And the session's own classification, which every state snapshot exports. Without
        # this the snapshot kept the AI's BASELINE verdicts for the whole session: after a
        # landmark correction the recorded classification disagreed with the measurements
        # and with the report the reader signed. Harmless only while a correction changes
        # no verdict, which is exactly the case that hides it. The baseline is not lost —
        # it is carried by the separate `baseline` event.
        self.classification = after

        self._audit(
            "reclassify",
            {
                "outcome": outcome,
                "landmarks_edited": self.landmarks_edited,
                # NOTE for analysis: since 2026-08-13, `classification_before` is the state
                # at the LAST check (report-rewrite semantics), not necessarily the AI
                # baseline; `before_is_baseline` disambiguates. The session-level
                # baseline-vs-final comparison is reconstructable from the separate
                # "baseline" event and the last reclassify event's classification_after.
                "before_is_baseline": before_is_baseline,
                "classification_before": before,
                "classification_after": after,
                "changes": changes,
                # transitions to/from "unbekannt": kept for analysis, deliberately NOT
                # counted as label changes (see above)
                "unresolved": unresolved,
            },
        )

        # Make the corrected numbers visible to the model, so any later chat turn or report
        # update reasons about what the reader actually sees rather than the stale values.
        if self.landmarks_edited:
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        "Der Nutzer hat die Landmarken manuell korrigiert. Die "
                        "Morphometrie wurde neu berechnet; ab jetzt gelten AUSSCHLIESSLICH "
                        "diese Messwerte:\n"
                        f"{json.dumps(self._rounded(), ensure_ascii=False)}\n"
                        + (
                            "Die Einordnung hat sich dadurch geändert: "
                            f"{json.dumps(changes, ensure_ascii=False)}"
                            if changes
                            else "Die Einordnung (normal/abnormal) ändert sich dadurch nicht."
                        )
                    ),
                }
            )

        return {
            "outcome": outcome,
            "changes": changes,
            "unresolved": unresolved,
            "classification": after,
            "measurements": self.measurements,
        }

    # -- anatomy / projection selection ------------------------------------
    def _note_assessment(self, combos: list[str]) -> None:
        """Record the current anatomy/projection: log it, flag whether the morphometry
        service supports it, and push it to the UI as an `assessment` step.

        The support flag is informational. Deciding what to do about an unsupported region
        belongs to the model (it can say so, and the reader can correct the selection);
        gating it here would replace an explanation with a dead end.
        """
        combo = f"{self.anatomy_key}/{self.projection_key}"
        if not combos:
            # The service is unreachable, so its capability list is empty. That is not the
            # same as "this region is unsupported" — claiming so would tell the reader their
            # perfectly ordinary knee film is out of scope. Leave the verdict unknown.
            self.supported = None
        else:
            self.supported = bool(self.anatomy_key and self.projection_key and combo in combos)
        self.log.append(
            f"Bildbeurteilung ({'Nutzerwahl' if self.anatomy_source == 'user' else 'Modell'}): "
            f"{self.anatomy or self.anatomy_key or '?'}, "
            f"{self.projection or self.projection_key or '?'} [{combo}]"
            + ("" if self.supported is not False else " — nicht unterstützt")
        )
        self._emit(
            {
                "type": "assessment",
                "anatomy": self.anatomy,
                "projection": self.projection,
                "anatomy_key": self.anatomy_key,
                "projection_key": self.projection_key,
                "supported": self.supported,
                "source": self.anatomy_source,
            }
        )
        self._audit(
            "assessment",
            {
                "anatomy_key": self.anatomy_key,
                "projection_key": self.projection_key,
                "anatomy_source": self.anatomy_source,  # run condition: model vs. reader
                "supported": self.supported,
            },
        )

    def _phase_hint(self) -> str:
        """What the pipeline is about to spend time on, for the UI's wait indicator.

        The pipeline cannot know that the NEXT model call is the report — the model
        chooses its own action — so this reports the phase rather than the step, and every
        wording stays true whichever action follows. Without it the reader watches a
        counter tick without knowing why: the report call alone runs ~22s and announces
        nothing, which is the same complaint the morphometry hint fixed.
        """
        if not self.geometry:
            return "wertet die Messungen aus"
        looked_up = sum(1 for st in self.last_steps if st.get("type") == "guideline")
        if looked_up < len(self.geometry):
            return (
                f"prüft die Referenzbereiche in den Leitlinien "
                f"({looked_up}/{len(self.geometry)} Messwerte)"
            )
        # Every measurement has been looked up, so the outstanding work is the report —
        # true even if the model decides to retrieve one more passage on the way.
        return "verfasst den Bericht"

    def _assessment_followup(self) -> str:
        """The message handed back after the anatomy/projection is fixed."""
        if self.anatomy_source == "user":
            lead = (
                f"Region und Projektion sind vom Nutzer vorgegeben: "
                f"{self.anatomy_key}/{self.projection_key}. Übernimm diese Auswahl "
                "unverändert und beurteile sie nicht neu."
            )
        else:
            lead = (
                f"Bildbeurteilung erfasst (Anatomie: {self.anatomy or self.anatomy_key or '?'}, "
                f"Projektion: {self.projection or self.projection_key or '?'})."
            )
        # None -> capabilities unknown (service down); proceed as usual and let the
        # morphometry call itself report the problem.
        if self.supported is not False:
            # The pipeline runs morphometry itself on BOTH paths, so this announces the
            # result rather than requesting the call — the transcript must not show the
            # model being asked for something it was never given the chance to answer.
            return f"{lead}\n\nDie Morphometrie wurde ausgeführt; das Ergebnis folgt."
        # Unsupported: state the fact, leave the response to the model.
        return (
            f"{lead}\n\nDiese Kombination gehört NICHT zu den unterstützten Kombinationen "
            f"({', '.join(morphometry_client.supported_combos()) or '—'}). Es sind daher "
            "keine Messungen möglich. Entscheide, wie du weiter vorgehst."
        )

    # -- phase 1: autonomous analysis -------------------------------------
    def _cancelled(self, what: str = "Analyse", clear_report: bool = True) -> None:
        """Wind down a cancelled run: visible in trace and steps. Only an ANALYSIS drops
        its (unfinished) report — a cancelled chat turn must keep the existing one."""
        self.log.append(f"— {what} auf Nutzerwunsch abgebrochen.")
        self._emit({"type": "final", "text": f"{what} abgebrochen."})
        self.last_reply = f"Die {what} wurde abgebrochen."
        if clear_report:
            self.report = None

    def _resolve_measurement(self, name: str) -> str | None:
        """Map a (canonical, display or robust-variant) name onto the BASE canonical key
        the classification uses. Exact modulo punctuation/case — never fuzzy."""
        n = _norm_key(name)
        aliases = mreg.alias_norms()
        for k in (self.measurements or self.measurements_predicted or {}):
            base = k and mreg._split_robust(k)[0]
            if not base:
                continue
            if _norm_key(k) == n or _norm_key(base) == n or aliases.get(n) == base:
                return base
        return None

    def set_reference_range(
        self,
        measurement: str,
        lower: float | None,
        upper: float | None,
        reference: str,
        source_type: str = "user",
    ) -> dict:
        """Commit a reference range as plain numbers into the DETERMINISTIC machinery
        (chat). Two provenances, kept distinct because the grounding audit depends on it:

          source_type='user'       reader-stated ('setze den Referenzbereich für X auf A–B').
                                    Reader authority — committed without question. Marked
                                    'Angabe Befunder'.
          source_type='literature' a range the model FOUND via search_guidelines for a
                                    previously ungrounded measurement, and the reader has
                                    CONFIRMED (see the chat prompt). Provenance is the
                                    retrieved passage, never 'Angabe Befunder'.

        Like a landmark correction, this drives classification and the locked Morphometrie
        lines, so the change is real and consistent everywhere. A one-sided range (only
        lower or only upper) is allowed. Audited."""
        canonical = self._resolve_measurement(measurement)
        if canonical is None:
            known = ", ".join(sorted({mreg._split_robust(k)[0] for k in (self.measurements or {})}))
            return {"status": "unknown_measurement", "known": known}
        if lower is None and upper is None:
            return {"status": "bad_input", "message": "lower und/oder upper erforderlich"}
        # The marker is OURS to append — strip it if the model helpfully included it in
        # its argument (it doubled otherwise).
        ref = re.sub(r"[\s(—–-]*Angabe\s+Befunder\)?", "", reference or "", flags=re.I).strip()
        ref = ref or " – ".join(str(x) for x in (lower, upper) if x is not None)
        if source_type == "literature":
            rng = {"lower": lower, "upper": upper, "reference": ref}
            # Attribute it to the passage that states it (falls back to a bare literature
            # marker if no retrieved chunk matches — still not 'Angabe Befunder').
            rng["source"] = _range_source(rng, self.guideline_hits, canonical) or {"type": "literature"}
        else:
            rng = {
                "lower": lower,
                "upper": upper,
                "reference": f"{ref} — Angabe Befunder",
                "source": {"type": "user"},
            }
        self.reference_ranges[canonical] = rng
        self._audit(
            "reference_range_user" if source_type != "literature" else "reference_range_literature",
            {"measurement": canonical, "lower": lower, "upper": upper, "reference": ref},
        )
        self._lock_report_measurements()  # re-render lines + verdict with the new range
        verdict = (
            self.classify(self.measurements, allow_llm=False).get(canonical, {}).get("verdict")
        )
        return {"status": "ok", "measurement": canonical, "verdict": verdict}

    def clear_reference_range(self, measurement: str) -> dict:
        """Remove a measurement's reference range entirely — back to 'kein freigegebener
        Referenzbereich' (verdict 'unbekannt'). For retracting a wrong entry (e.g. a range
        added to the wrong measurement). Re-locks the report + re-verdicts. Audited."""
        canonical = self._resolve_measurement(measurement)
        if canonical is None:
            known = ", ".join(sorted({mreg._split_robust(k)[0] for k in (self.measurements or {})}))
            return {"status": "unknown_measurement", "known": known}
        existed = canonical in self.reference_ranges
        self.reference_ranges.pop(canonical, None)
        self._audit(
            "reference_range_cleared",
            {"measurement": canonical, "existed": existed},
        )
        self._lock_report_measurements()  # re-render the line without a range, verdict -> unbekannt
        return {"status": "ok", "measurement": canonical, "cleared": existed}

    def set_impression(self, text: str) -> None:
        """Reader-authored Beurteilung (editable Befundentwurf). Audited — reader edits
        are part of the record — and sticky: from now on model rewrites must preserve this
        wording (see impression_author)."""
        if self.report is None:
            raise ValueError("Kein Bericht vorhanden.")
        before = self.report.impression
        self.report.impression = text.strip()
        self.impression_author = "user"
        self.user_impression_text = self.report.impression  # the wording to compare against
        self.last_active = time.time()
        self._audit(
            "impression_edited",
            {"before": before, "after": self.report.impression},
        )

    def _update_impression_provenance(self) -> None:
        """After a model rewrite: last-writer semantics by text comparison. "user" only
        while the impression IS the reader's saved text (whitespace-insensitive) — the
        flag can never claim reader authorship for text the model altered."""
        if not self.user_impression_text or self.report is None:
            return
        same = " ".join((self.report.impression or "").split()) == " ".join(
            self.user_impression_text.split()
        )
        self.impression_author = "user" if same else "model"

    def run_analysis(self) -> Report | None:
        self.cancel_requested = False  # a fresh run starts unencumbered
        self.impression_author = "model"  # a fresh analysis writes a fresh draft
        self.user_impression_text = ""
        self.last_steps = []
        self.new_visualizations = []
        # The Denkmodus is a run condition (like anatomy_source) — make it visible in the
        # trace so no run's latency/quality is compared blind to it.
        self.log.append(f"Denkmodus: {'an' if self.thinking else 'aus'}")
        combos = morphometry_client.supported_combos()
        tool_lines = [
            s for s in (
                tool_catalogue(),
                MORPHO_TOOL_SIG,
                GUIDELINE_TOOL_SIG.format(query=store.query_terms_hint()),
            ) if s.strip()
        ]
        # Rendered as explicit pairs, not "knee/ap, knee/axial, …". The joined form handed
        # the model a single token to copy, and a weaker backbone duly put the whole thing
        # into anatomy_key ("knee/ap") — which matches no supported combination and ends
        # the case. Naming both fields on every line removes the ambiguity.
        supported_lines = "\n".join(
            f"  - anatomy_key={c.split('/', 1)[0]}, projection_key={c.split('/', 1)[1]}"
            for c in combos if "/" in c
        )
        system = SYSTEM_PROMPT.format(
            tools="\n".join(tool_lines),
            supported=supported_lines or "  (keine — Dienst nicht erreichbar)",
            search_language=store.search_language_hint(),
        )
        self.messages = [{"role": "system", "content": system}]
        if self.anatomy_source == "user":
            # The reader already fixed the region/projection (they corrected the model, or
            # confirmed it before a re-run), so step 1 has no decision left to make. Skipping
            # it keeps the model from quietly re-deriving — and contradicting — the selection,
            # and saves an LLM round trip, which on the slower profiles is most of a minute.
            self._note_assessment(combos)
            self.messages.append(
                user_message(self._assessment_followup(), image_path=self.image_path)
            )
            # ...and run morphometry here rather than spending a round trip asking the
            # model to. With the region fixed there is no decision left: the only possible
            # reply is "call morphometry". That reply costs a full VISION call (the image
            # rides along) — median 17s in our runs — purely to relay an instruction we
            # just gave, and it is the first thing a reader waits through after correcting
            # a misrouted case.
            # Not done when the combination is unsupported: there the model genuinely has
            # something to decide, and _assessment_followup asks it to.
            if self.supported is not False and not self.cancel_requested:
                result = self._execute_tool("morphometry", {})
                self.messages.append(
                    {
                        "role": "user",
                        "content": f"Ergebnis von morphometry:\n"
                                   f"{json.dumps(result, ensure_ascii=False)}",
                    }
                )
        else:
            self.messages.append(
                user_message(
                    "Beginne mit der Bildbeurteilung (assess_image).",
                    image_path=self.image_path,
                )
            )

        for _ in range(MAX_STEPS):
            if self.cancel_requested:  # step boundary: before the next expensive call
                self._cancelled("Analyse")
                return None
            # Say what the coming wait is for. Transient: the UI shows it as the label of
            # its "arbeitet…" counter and never as a list entry, so the step list stays a
            # record of what happened rather than of what was expected to happen.
            self._emit({"type": "status", "text": self._phase_hint()})
            t0 = time.perf_counter()
            reply = self.llm.complete(self.messages, enable_thinking=self._enable_thinking)
            llm_secs = time.perf_counter() - t0
            self.messages.append({"role": "assistant", "content": reply})
            if self.cancel_requested:  # the call that was in flight when Stop was pressed
                self._cancelled("Analyse")
                return None
            try:
                action = _extract_json(reply)
            except ValueError:
                self.messages.append(
                    {"role": "user", "content": "Antworte mit GENAU EINEM gültigen JSON-Objekt."}
                )
                continue

            self.log.append(f"überlegt in {llm_secs:.1f}s")
            self._record_thought(action, llm_secs)
            kind = action.get("action")
            if kind == "assess_image":
                self.anatomy = str(action.get("anatomy", "")).strip()
                self.projection = str(action.get("projection", "")).strip()
                self.anatomy_key = str(action.get("anatomy_key") or "").strip().lower()
                self.projection_key = str(action.get("projection_key") or "").strip().lower()
                self.anatomy_source = "model"
                self._note_assessment(combos)
                # No region-level guideline lookup here: the corpus is organized by
                # measurement method, so a generic region query returns off-target passages
                # (wrist/hip for a knee), and every useful hit is re-retrieved by the targeted
                # per-measurement lookups below. Morphometry needs only the anatomy/projection
                # keys, so we hand straight off to it; grounding happens per measurement.
                self.messages.append({"role": "user", "content": self._assessment_followup()})
                # Run it here rather than spending a round trip asking the model to. Once
                # the keys are known the call is fully determined, exactly as on the
                # reader-set path — and doing it in ONLY one of the two paths would make a
                # reader-corrected run structurally cheaper than a model-routed one, so a
                # misroute could measure as costing no time at all. Both paths now go
                # straight to morphometry, and the only remaining difference is the
                # assess_image call itself, which is real work the reader-set path does
                # not need.
                if self.supported is not False and not self.cancel_requested:
                    result = self._execute_tool("morphometry", {})
                    self.messages.append(
                        {
                            "role": "user",
                            "content": f"Ergebnis von morphometry:\n"
                                       f"{json.dumps(result, ensure_ascii=False)}",
                        }
                    )
            elif kind == "call_tool":
                name = action.get("tool", "")
                arguments = action.get("arguments", {}) or {}
                result = self._execute_tool(name, arguments)
                self.messages.append(
                    {"role": "user",
                     "content": f"Ergebnis von {name}:\n{json.dumps(result, ensure_ascii=False)}"}
                )
            elif kind == "final_report":
                report = Report.from_dict(action.get("report", {}))
                # An empty report is how the model declines to assess (an unsupported
                # region, say). Rendering it anyway would produce a "Radiologiebericht"
                # heading over a column of dashes, which looks like a finished report of a
                # normal case — exactly the reading we must not invite. Keep it as no
                # report at all and let the message carry the explanation.
                empty = not (report.technique or report.findings or report.measurements
                             or report.impression)
                self.report = None if empty else report
                if not empty:
                    # Structured bounds first, then the model's prose as a one-time fallback
                    # for anything it grounded only in text — both BEFORE _lock renders.
                    self._ingest_reference_ranges(action.get("reference_ranges"))
                    self._ingest_prose_ranges()
                self._lock_report_measurements()
                self.last_reply = (action.get("message") or "").strip() or (
                    "Für dieses Bild wurde kein Bericht erstellt." if empty
                    else "Der Bericht wurde erstellt."
                )
                self.log.append("— kein Bericht erstellt." if empty else "✓ Finaler Bericht erstellt.")
                self._emit(
                    {"type": "final", "text": "Kein Bericht erstellt." if empty else "Bericht erstellt."}
                )
                if not empty:
                    # Off the critical path: the report goes to the reader immediately, the
                    # baseline label is computed behind it.
                    self._baseline_thread = threading.Thread(
                        target=self._baseline_classification, daemon=True
                    )
                    self._baseline_thread.start()
                return self.report
            else:
                self.messages.append(
                    {"role": "user", "content": "Unbekannte Aktion. Nutze call_tool oder final_report."}
                )

        # Ran out of steps -> ask for a report with what we have.
        self.messages.append(
            {"role": "user", "content": "Schrittlimit erreicht. Gib jetzt deinen final_report aus."}
        )
        reply = self.llm.complete(self.messages, enable_thinking=self._enable_thinking)
        try:
            action = _extract_json(reply)
            self.report = Report.from_dict(action.get("report", {}))
            self._ingest_reference_ranges(action.get("reference_ranges"))
            self._ingest_prose_ranges()
            self.last_reply = (action.get("message") or "").strip() or "Der Bericht wurde erstellt."
        except ValueError:
            self.report = Report(impression=reply)
            self.last_reply = "Der Bericht wurde erstellt."
        self._lock_report_measurements()
        return self.report

    def _rounded(self) -> dict:
        """Measurements at reporting precision (registry). Handing the model
        1.0793588215482 is how that many decimals end up in the report — it copies
        faithfully what it is given."""
        return {
            k: (mreg.round_value(k, v) if isinstance(v, (int, float)) else v)
            for k, v in mreg.preferred_measurements(self.measurements or {}).items()
        }

    def _measurement_lines(self, classification: dict) -> dict:
        """The report's Morphometrie section, written from the numbers we already hold — no
        LLM. Asking a model to retype values it was just given is a pure waste of a round
        trip and an opportunity for it to mistype one. Names, decimal places and number
        format (German comma, unit spacing) come from the curated measurement registry, so
        UI, report and export can never drift apart."""
        out = {}
        for name, c in classification.items():
            g = self.geometry.get(name) or {}
            text = mreg.format_value(name, c["value"], g.get("kind"), g.get("unit"))
            if c.get("reference"):
                text += f" (Referenzbereich: {c['reference']})"
            elif c.get("verdict") == "unbekannt":
                text += " — kein freigegebener Referenzbereich hinterlegt"
            out[mreg.display_name(name)] = text
        return out

    # Canonical keys -> German display, same vocabulary as the UI chips. Small on purpose:
    # unknown keys fall back to the key itself rather than to model prose.
    _ANATOMY_DE = {"knee": "Kniegelenk", "feet": "Fuß", "shoulder": "Schulter", "hip": "Hüfte"}
    _PROJECTION_DE = {"lateral": "lateral", "ap": "a.p.", "axial": "axial"}

    def _technique_line(self) -> str:
        """Deterministic Technik line from the canonical assessment keys — brief facts
        only. The model used to write it and embellished: invented purpose statements
        ('zur morphometrischen Beurteilung …'), unverified laterality ('rechtes Knie'),
        and off-vocabulary terms ('Seitenaufnahme' instead of lateral)."""
        if not (self.anatomy_key and self.projection_key):
            return ""
        anat = self._ANATOMY_DE.get(self.anatomy_key, self.anatomy_key)
        proj = self._PROJECTION_DE.get(self.projection_key, self.projection_key)
        line = f"Röntgen {anat}, {proj}"
        return line if line.endswith(".") else line + "."  # "a.p." brings its own period

    def to_csv_bytes(self) -> bytes:
        """The Morphometrie as a CSV table — one row per displayed measurement, same
        collapsing (robust variants), rounding, names and verdicts as report and UI.

        Format targets German Excel: semicolon delimiter, decimal comma, UTF-8 with BOM,
        CRLF. Fall-ID and Untersuchung repeat on every row so exported files from several
        cases can be concatenated into one combined sheet without losing provenance."""
        import csv
        import io

        values = self.measurements or self.measurements_predicted or {}
        cls = self.classify(values, allow_llm=False)
        predicted = {
            k: mreg.round_value(k, v)
            for k, v in mreg.preferred_measurements(self.measurements_predicted or {}).items()
            if v is not None
        }
        # Stable clinical order: registry order (which already groups by concept),
        # unknown names after that.
        order = {k: i for i, k in enumerate(mreg.REGISTRY)}
        names = sorted(cls, key=lambda n: (order.get(n, len(order)), n))

        technique = self._technique_line().rstrip(".")

        def num(name: str, v) -> str:
            # Number only, decimal comma — the unit has its own column.
            return f"{v:.{mreg.decimals_for(name)}f}".replace(".", ",") if v is not None else ""

        def unit_of(name: str) -> str:
            g = self.geometry.get(name) or {}
            u = (g.get("unit") or "").strip()
            if u:
                return u
            kind = g.get("kind")
            return "°" if kind == "angle" else ("mm" if kind == "distance" else "")

        def einordnung(name: str, c: dict) -> str:
            if c.get("verdict") == "normal":
                return "im Referenzbereich"
            if c.get("verdict") == "abnormal":
                rng = self.reference_ranges.get(name) or {}
                v, hi, lo = c.get("value"), rng.get("upper"), rng.get("lower")
                if isinstance(v, (int, float)):
                    if hi is not None and v > hi:
                        return "über dem Referenzbereich"
                    if lo is not None and v < lo:
                        return "unter dem Referenzbereich"
                return "außerhalb des Referenzbereichs"
            return "kein freigegebener Referenzbereich"

        def quelle(name: str) -> str:
            rng = self.reference_ranges.get(name)
            if not rng:
                return ""
            src = rng.get("source")
            if isinstance(src, dict):
                if src.get("type") == "user":
                    return "Angabe Befunder"
                if src.get("title"):
                    return src["title"]
            # A range exists but no retrieved passage states it — an untraceable
            # threshold. Said plainly, never smoothed over.
            return "kein Textbeleg in den abgerufenen Passagen"

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow([
            "Fall-ID", "Untersuchung", "Gruppe", "Messung", "Wert", "Einheit",
            "Einordnung", "Referenzbereich", "Quelle Referenzbereich",
            "KI-Vorhersage", "Manuell korrigiert",
        ])
        for name in names:
            c = cls[name]
            pred = predicted.get(name)
            corrected = "" if pred is None else ("ja" if c.get("value") != pred else "nein")
            w.writerow([
                self.case_id,
                technique,
                mreg.group_for(name),
                mreg.display_name(name),
                num(name, c.get("value")),
                unit_of(name),
                einordnung(name, c),
                c.get("reference", ""),
                quelle(name),
                num(name, pred),
                corrected,
            ])
        # BOM so Excel detects UTF-8 (umlauts, "°") instead of falling back to ANSI.
        return ("﻿" + buf.getvalue()).encode("utf-8")

    def _ingest_prose_ranges(self) -> None:
        """Fallback for the INITIAL report only: capture a reference range the model wrote in
        its measurement prose but did not put in the structured 'reference_ranges' field.
        Reads the model's own lines ONCE, before _lock_report_measurements code-renders them.

        It must NOT be folded into _lock (as it once was): _lock runs again after every
        set/clear/recheck, and re-reading a code-rendered '(Referenzbereich: …)' line there
        re-derived a range the reader had just cleared — the range came back on the next
        lock. Kept here, run once on the model's prose, that circularity is gone."""
        if not self.report or not (self.measurements or self.measurements_predicted):
            return
        for name in (self.measurements or self.measurements_predicted):
            if name in self.reference_ranges:
                continue
            rng = _parse_range(_match_report_line(name, self.report.measurements))
            if rng:
                self.reference_ranges[name] = rng
        self._attach_range_sources()

    def _lock_report_measurements(self) -> None:
        """Replace the model-written Morphometrie lines with code-rendered ones: locked
        values, curated names, uniform German formats. Called after every path that lets
        the model (re)write the report — the number the reader sees must come from exactly
        one place, the morphometry tool (a value once differed between prose and
        list). Pure rendering: reference ranges are ingested BEFORE this (structured field
        + _ingest_prose_ranges at the initial report; set_reference_range/clear thereafter) —
        this method never DERIVES a range from a line, so a clear cannot be undone by it.
        Classification is code-only here (no LLM latency)."""
        if self.report is not None:
            # Technik is locked like the numbers: deterministic, never model prose.
            self.report.technique = self._technique_line() or self.report.technique
        if not self.report or not (self.measurements or self.measurements_predicted):
            return
        values = self.measurements or self.measurements_predicted
        self.report.measurements = self._measurement_lines(
            self.classify(values, allow_llm=False)
        )

    def _range_direction(self, name: str, c: dict) -> str:
        """' — Wert ÜBER/UNTER dem Normbereich' for an abnormal value, else ''.

        Computed in code and handed to the rewrite prompt, because the DIRECTION is where
        the model slipped when only told 'abnormal': a high Insall-Salvati was narrated as
        Patella baja (low) — the verdict was right, the anatomy inverted. The arithmetic
        already knows which bound was crossed; saying so costs nothing and removes the
        model's only degree of freedom to get it backwards."""
        if c.get("verdict") != "abnormal":
            return ""
        rng = self.reference_ranges.get(name) or {}
        value = c.get("value")
        if not isinstance(value, (int, float)):
            return ""
        hi, lo = rng.get("upper"), rng.get("lower")
        if hi is not None and value > hi:
            return " — Wert ÜBER dem Normbereich"
        if lo is not None and value < lo:
            return " — Wert UNTER dem Normbereich"
        return ""

    def recheck_report(self) -> dict:
        """Re-check the report after a landmark correction.

        The expensive part used to be an LLM call that did nothing but compare numbers to
        ranges. It no longer exists. What happens now:

          1. the labels are recomputed in code, against ranges extracted once (instant);
          2. the report's measurement values are rewritten from the numbers we hold (instant);
          3. the narrative is regenerated ONLY if a label actually flipped — because that is
             the only case where the findings/impression can be wrong. One small call.

        So a correction that moves the numbers without crossing a threshold now returns in
        milliseconds and still updates the report's values.
        """
        outcome = self.reclassify()  # emits its own comparison steps + writes the audit log
        after = outcome.get("classification") or {}
        changes = outcome.get("changes") or []

        # (2) Values first: always current, never invented.
        if self.report and after:
            self.report.measurements = self._measurement_lines(after)

        if not changes:
            self.last_reply = (
                "Die korrigierten Messwerte ändern die Beurteilung nicht (alle Werte bleiben "
                "innerhalb bzw. außerhalb derselben Normbereiche). Die Messwerte im Bericht "
                "wurden aktualisiert; Befund und Beurteilung bleiben gültig."
            )
            self._emit({"type": "final", "text": "Messwerte im Bericht aktualisiert."})
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        "Die Landmarken wurden manuell korrigiert. Gültige Messwerte: "
                        f"{json.dumps(self._rounded(), ensure_ascii=False)}. Die Einordnung "
                        "(normal/abnormal) ändert sich dadurch nicht."
                    ),
                }
            )
            self.messages.append({"role": "assistant", "content": self.last_reply})
            self.recheck_outcome = outcome
            return outcome

        # (3) A label flipped -> the prose really is wrong now. This is the only LLM call.
        self._emit(
            {
                "type": "thought",
                "text": (
                    f"Die Beurteilung ändert sich ({len(changes)} Messung(en)). Ich schreibe "
                    "Befund und Beurteilung entsprechend neu."
                ),
            }
        )
        # Provenance contract: a reader-authored Beurteilung is not the model's draft to
        # rewrite freely — only what the label flip made factually wrong may change.
        user_note = (
            "\nWICHTIG: Die aktuelle Beurteilung wurde vom BEFUNDER manuell formuliert. "
            "Ändere AUSSCHLIESSLICH, was durch die geänderte Einordnung sachlich falsch "
            "geworden ist; übernimm alle übrigen Formulierungen WÖRTLICH und unverändert.\n"
            if self.user_impression_text  # protection is sticky: ever reader-authored
            else ""
        )
        prompt = RECHECK_PROMPT.format(
            user_note=user_note,
            report=self.report.to_markdown() if self.report else "(kein Bericht)",
            measurements="\n".join(f"- {k}: {v}" for k, v in self._measurement_lines(after).items())
            or "(keine)",
            # Display names throughout: mixing canonical English keys into these lines made
            # the model translate names on its own ("Tibiaschuhwinkel"). Every name the
            # model sees is one it may repeat verbatim.
            classification="\n".join(
                f"- {mreg.display_name(k)}: {c['verdict']}"
                + (f" (Referenzbereich: {c['reference']})" if c.get("reference") else "")
                + self._range_direction(k, c)
                for k, c in after.items()
            )
            or "(keine)",
            changes="\n".join(
                f"- {mreg.display_name(c['measurement'])}: {c['verdict_before']} → {c['verdict_after']}"
                for c in changes
            ),
            guidelines=store.format_for_prompt(self.guideline_hits) or "(keine)",
        )

        t0 = time.perf_counter()
        try:
            # Thinking off: this is a transcription task — the verdicts are computed and
            # binding, there is nothing to deliberate. See RECHECK_PROMPT + llm.complete.
            reply = self.llm.complete(
                [{"role": "user", "content": prompt}], enable_thinking=False
            )
            action = _extract_json(reply)
        except Exception as exc:
            self.last_reply = (
                f"Die Beurteilung ändert sich, aber der Bericht konnte nicht neu geschrieben "
                f"werden ({exc}). Die Messwerte im Bericht sind aktuell."
            )
            self._emit({"type": "thought", "text": self.last_reply})
            # The CHECK ran and its verdict stands — only the prose rewrite failed. Returning
            # without recording it left the badge stuck on "noch nicht geprüft" even though
            # the reader's correction had in fact been evaluated.
            self.recheck_outcome = outcome
            return outcome
        secs = time.perf_counter() - t0
        self._record_thought(action, secs)

        # The model returns only the narrative — technique and measurements are ours and were
        # never sent back for it to retype. (Tolerate a nested "report" object too, in case
        # it falls back on the shape from the main loop's prompt.)
        body = action.get("report") if isinstance(action.get("report"), dict) else action
        if self.report:
            self.report.findings = list(body.get("findings", self.report.findings))
            self.report.impression = body.get("impression", self.report.impression)
            self.report.measurements = self._measurement_lines(after)
            self._update_impression_provenance()  # "user" only if the wording survived
        self._emit({"type": "final", "text": "Bericht aktualisiert."})
        self.last_reply = (action.get("message") or "").strip() or (
            "Ich habe den Bericht anhand der korrigierten Messwerte aktualisiert."
        )

        self.messages.append(
            {
                "role": "user",
                "content": (
                    "Die Landmarken wurden manuell korrigiert. Gültige Messwerte: "
                    f"{json.dumps(self._rounded(), ensure_ascii=False)}. Geänderte "
                    f"Einordnung: {json.dumps(changes, ensure_ascii=False)}"
                ),
            }
        )
        self.messages.append({"role": "assistant", "content": self.last_reply})
        self.recheck_outcome = outcome
        return outcome

    # -- phase 2: interactive chat ----------------------------------------
    def chat(self, user_text: str, reset_steps: bool = True) -> str:
        """Discuss/refine the report. The agent may call tools mid-conversation.

        `reset_steps=False` keeps whatever steps the caller already emitted for this turn —
        the re-check flow emits its label comparison BEFORE handing over to the chat loop,
        and clearing here would erase it from the trace the reader sees.
        """
        if reset_steps:
            self.last_steps = []
        self.new_visualizations = []
        current = self.report.to_markdown() if self.report else "(no report yet)"
        authored = (
            "\nHINWEIS: Die Beurteilung des Berichts wurde vom BEFUNDER manuell "
            "formuliert. Bewahre ihre Formulierungen wörtlich, sofern der Nutzer nicht "
            "ausdrücklich eine Änderung daran verlangt.\n"
            if self.user_impression_text  # protection is sticky: ever reader-authored
            else ""
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"Der Nutzer sagt: {user_text}\n\n"
                    f"Aktueller Bericht:\n{current}\n{authored}\n"
                    "Antworte dem Nutzer inhaltlich und hilfreich:\n"
                    "- Stellt der Nutzer eine Frage oder möchte eine Erklärung, BEANTWORTE sie "
                    "direkt und konkret mit einer 'message'-Aktion "
                    '({"thought": "...", "action": "message", "message": "..."}). '
                    "Schlage bei Bedarf zuerst mit search_guidelines nach und nenne dann das "
                    "konkrete Ergebnis (z. B. den Referenzbereich). Stütze Aussagen zu "
                    "Referenzbereichen/Interpretationen NUR auf die Leitlinien-Texte, nicht auf "
                    "eigenes Vorwissen; findest du keinen Beleg, sage das offen (kein "
                    "Referenzbereich in der Literatur). Ändere den Bericht dafür NICHT.\n"
                    "- FINDEST du dabei per search_guidelines für eine Messung, die BISHER KEINEN "
                    "Referenzbereich hatte, einen belegten Bereich: übernimm ihn NICHT eigenmächtig. "
                    "SCHLAGE ihn dem Nutzer per 'message' vor (nenne Bereich UND Leitlinienbeleg) und "
                    "frage, ob du ihn übernehmen sollst. ERST wenn der Nutzer ausdrücklich zustimmt, "
                    "übernimm ihn mit set_reference_range und \"source\": \"literature\".\n"
                    "- Soll der Bericht tatsächlich (in der Beurteilung) geändert werden, nutze "
                    "final_report und erkläre im Feld 'message' kurz, was du gefunden und geändert "
                    "hast (nicht bloß „Bericht aktualisiert\").\n"
                    "  WICHTIG: Technik und die Morphometrie-Zeilen sind maschinell GESPERRT — "
                    "direkte Änderungen daran über final_report werden verworfen. Behaupte "
                    "niemals, sie geändert zu haben.\n"
                    "- Nennt der Nutzer SELBST einen Referenzbereich für eine Messung (z. B. „setze "
                    "den Referenzbereich für X auf A–B\"), übernimm ihn OHNE Rückfrage mit "
                    '{"thought": "...", "action": "set_reference_range", '
                    '"measurement": "<kanonischer Name>", "lower": <Zahl oder null>, '
                    '"upper": <Zahl oder null>, "reference": "<Bereich wörtlich, OHNE Zusatz>", '
                    '"source": "user"}. Bei nur einer Grenze setze die andere auf null. Die '
                    "Morphometrie-Zeile und die Einordnung werden dann maschinell aktualisiert "
                    "(Kennzeichnung „Angabe Befunder\"). Stelle eine ausdrückliche Nutzer-Vorgabe "
                    "NICHT in Frage.\n"
                    "- Möchte der Nutzer einen Referenzbereich WIEDER ENTFERNEN (z. B. versehentlich "
                    "der falschen Messung zugeordnet), lösche ihn mit "
                    '{"thought": "...", "action": "clear_reference_range", '
                    '"measurement": "<kanonischer Name>"}. Die Messung steht danach wieder ohne '
                    "Referenzbereich da (Einordnung „unbekannt\").\n"
                    "- Ist eine neue Messung/Analyse nötig, nutze call_tool.\n"
                    "Verfasse alle Nachrichten und Berichtsinhalte auf Deutsch."
                ),
            }
        )

        self.cancel_requested = False
        retried_bad_json = False
        for _ in range(MAX_STEPS):
            if self.cancel_requested:
                self._cancelled("Verarbeitung", clear_report=False)
                return self.last_reply
            t0 = time.perf_counter()
            reply = self.llm.complete(self.messages, enable_thinking=self._enable_thinking)
            llm_secs = time.perf_counter() - t0
            self.messages.append({"role": "assistant", "content": reply})
            try:
                action = _extract_json(reply)
            except ValueError:
                # A reply that is clearly an INTENDED action but malformed (a stray unescaped
                # quote, a trailing comma) must not be dumped to the reader as raw JSON.
                # Nudge once for valid JSON, then fall through to treating it as prose.
                if not retried_bad_json and '"action"' in reply:
                    retried_bad_json = True
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "Deine letzte Antwort war kein gültiges JSON (vermutlich ein nicht "
                            "maskiertes gerades Anführungszeichen im Text). Antworte erneut mit "
                            "GENAU EINEM gültigen JSON-Objekt und verwende in Fließtext deutsche "
                            "Anführungszeichen „…“ statt gerader."
                        ),
                    })
                    continue
                self.last_reply = reply  # a genuine plain-prose answer is fine in chat
                return reply

            self.log.append(f"überlegt in {llm_secs:.1f}s")
            self._record_thought(action, llm_secs)
            kind = action.get("action")
            if kind == "call_tool":
                name = action.get("tool", "")
                arguments = action.get("arguments", {}) or {}
                result = self._execute_tool(name, arguments, user_requested=True)
                self.messages.append(
                    {"role": "user",
                     "content": f"Ergebnis von {name}:\n{json.dumps(result, ensure_ascii=False)}"}
                )
            elif kind == "set_reference_range":
                src = str(action.get("source", "user")).strip().lower()
                rr = self.set_reference_range(
                    str(action.get("measurement", "")),
                    _num(action.get("lower")),
                    _num(action.get("upper")),
                    str(action.get("reference", "")),
                    source_type="literature" if src == "literature" else "user",
                )
                self._record_tool(
                    "set_reference_range",
                    {k: action.get(k) for k in ("measurement", "lower", "upper", "reference")},
                    rr,
                    0.0,
                )
                self.messages.append(
                    {"role": "user",
                     "content": (
                         f"Ergebnis von set_reference_range:\n{json.dumps(rr, ensure_ascii=False)}\n"
                         "Die Morphometrie-Zeile wurde maschinell aktualisiert. Passe bei Bedarf "
                         "die Beurteilung an (final_report) oder antworte dem Nutzer (message)."
                     )}
                )
            elif kind == "clear_reference_range":
                rr = self.clear_reference_range(str(action.get("measurement", "")))
                self._record_tool(
                    "clear_reference_range", {"measurement": action.get("measurement")}, rr, 0.0
                )
                self.messages.append(
                    {"role": "user",
                     "content": (
                         f"Ergebnis von clear_reference_range:\n{json.dumps(rr, ensure_ascii=False)}\n"
                         "Die Messung hat nun keinen Referenzbereich mehr (Einordnung „unbekannt“). "
                         "Passe bei Bedarf die Beurteilung an (final_report) oder antworte (message)."
                     )}
                )
            elif kind == "final_report":
                self.report = Report.from_dict(action.get("report", {}))
                self._lock_report_measurements()  # chat edits must not unlock the numbers
                self._update_impression_provenance()  # "user" only if the wording survived
                self._emit({"type": "final", "text": "Bericht aktualisiert."})
                self.last_reply = (
                    (action.get("message") or "").strip() or "Ich habe den Bericht aktualisiert."
                )
                return self.last_reply
            elif kind == "message":
                self.last_reply = action.get("message", "")
                return self.last_reply
            else:
                self.last_reply = reply
                return reply
        self.last_reply = "Ich konnte zu keiner Antwort kommen; könntest du es anders formulieren?"
        return self.last_reply
