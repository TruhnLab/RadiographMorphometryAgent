"""Guideline retrieval: the agent's evidence base.

Every reference range and every interpretation in a report must come from text returned
here (see the grounding rules in agent.py). Which retrieval system answers is a matter of
configuration (.env), so any RAG setup can be plugged in:

    GUIDELINES_BACKEND = mcp | http | local | none

    mcp    an MCP server (Streamable HTTP) exposing a search tool.
           GUIDELINES_MCP_URL, GUIDELINES_MCP_TOKEN (optional bearer token),
           GUIDELINES_MCP_TOOL (default "search"), GUIDELINES_MCP_QUERY_ARG (default "query")
    http   a plain JSON endpoint, called as POST {"query": ..., "top_k": ...}.
           GUIDELINES_HTTP_URL, GUIDELINES_HTTP_TOKEN (optional bearer token)
    local  Markdown/text files in GUIDELINES_DIR, split into paragraphs and ranked with
           BM25. No server and no index to build. Meant for small curated corpora and for
           trying the pipeline out.
    none   no retrieval. The agent then reports bare values and interprets nothing.

Without GUIDELINES_BACKEND, the backend follows from what is configured: mcp if
GUIDELINES_MCP_URL is set, else http if GUIDELINES_HTTP_URL, else local if GUIDELINES_DIR,
else none. (CLINICAL_MCP_URL / CLINICAL_MCP_TOKEN, the older names, are still read.)

Result shapes differ between RAG systems. The mcp and http backends parse a JSON object,
take the result list from GUIDELINES_RESULTS_PATH (default "results"; empty means the
response itself is the list) and read each field from the first of several dotted paths
that exists. The defaults cover common shapes, and each can be overridden with a
comma-separated list of paths:

    GUIDELINES_FIELD_TITLE   title, document.name, document.title, source, name
    GUIDELINES_FIELD_TEXT    text, content, chunk_text, chunk.chunk_text, page_content
    GUIDELINES_FIELD_SCORE   score, similarity, relevance
    GUIDELINES_FIELD_ID      id, document_id, document.id
    GUIDELINES_FIELD_PAGES   pages, metadata.pages, chunk.metadata.pages

GUIDELINES_LANGUAGE (de | en | mixed, default de) tells the agent which language to
search in.

retrieve() never raises. A failed lookup returns [] (and logs a warning), which the agent
treats like a lookup without a hit: the measurement stays uninterpreted.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import re
from collections import Counter

import requests

import config  # noqa: F401  (loads .env)

log = logging.getLogger(__name__)

_DEFAULT_FIELDS = {
    "title": "title,document.name,document.title,source,name",
    "text": "text,content,chunk_text,chunk.chunk_text,page_content",
    "score": "score,similarity,relevance",
    "id": "id,document_id,document.id",
    "pages": "pages,metadata.pages,chunk.metadata.pages",
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _verify_ssl() -> bool:
    return _env("GUIDELINES_VERIFY_SSL", default="true").lower() == "true"


def backend() -> str:
    explicit = _env("GUIDELINES_BACKEND").lower()
    if explicit:
        return explicit
    if _env("GUIDELINES_MCP_URL", "CLINICAL_MCP_URL"):
        return "mcp"
    if _env("GUIDELINES_HTTP_URL"):
        return "http"
    if _env("GUIDELINES_DIR"):
        return "local"
    return "none"


# --- response mapping (mcp + http) -------------------------------------------

def _dig(obj, path: str):
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _field(result: dict, name: str):
    paths = _env(f"GUIDELINES_FIELD_{name.upper()}", default=_DEFAULT_FIELDS[name])
    for path in (p.strip() for p in paths.split(",")):
        value = _dig(result, path) if path else None
        if value not in (None, ""):
            return value
    return None


def _normalize(results: list, k: int) -> list[dict]:
    out = []
    for r in results[:k]:
        if isinstance(r, str):  # a bare passage
            r = {"text": r}
        if not isinstance(r, dict):
            continue
        pages = _field(r, "pages") or []
        out.append({
            "title": str(_field(r, "title") or "Leitlinie"),
            "text": str(_field(r, "text") or ""),
            "score": _field(r, "score"),
            "document_id": _field(r, "id"),
            # Page number(s) of the source document, for a precise citation ("S. 27").
            "pages": pages if isinstance(pages, list) else [pages],
        })
    return [h for h in out if h["text"]]


def _results_from(payload) -> list:
    path = _env("GUIDELINES_RESULTS_PATH", default="results")
    if path and isinstance(payload, dict):
        payload = _dig(payload, path)
    return payload if isinstance(payload, list) else []


# --- backends ------------------------------------------------------------------

async def _search_mcp(query: str, k: int) -> list[dict]:
    # Imported lazily so a missing 'mcp' package degrades gracefully to [].
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = _env("GUIDELINES_MCP_URL", "CLINICAL_MCP_URL")
    token = _env("GUIDELINES_MCP_TOKEN", "CLINICAL_MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    verify = _verify_ssl()

    def factory(**kw):
        return httpx.AsyncClient(verify=verify, **kw)

    tool = _env("GUIDELINES_MCP_TOOL", default="search")
    arg = _env("GUIDELINES_MCP_QUERY_ARG", default="query")
    async with streamablehttp_client(url, headers=headers, httpx_client_factory=factory) as (
        read, write, _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, {arg: query})
    texts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
    if not texts:
        return []
    try:
        return _normalize(_results_from(json.loads(texts[0])), k)
    except ValueError:
        # Not JSON: a server that returns one passage per content block.
        return _normalize(texts, k)


def _search_http(query: str, k: int) -> list[dict]:
    token = _env("GUIDELINES_HTTP_TOKEN")
    resp = requests.post(
        _env("GUIDELINES_HTTP_URL"),
        json={"query": query, "top_k": k},
        headers={"Authorization": f"Bearer {token}"} if token else {},
        verify=_verify_ssl(),
        timeout=30,
    )
    resp.raise_for_status()
    return _normalize(_results_from(resp.json()), k)


# Local corpus: paragraphs under their nearest heading, ranked with BM25 (k1=1.5, b=0.75).
_INDEX: dict = {"key": None}


def _chunks(path: str) -> list[dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    doc_title = os.path.splitext(os.path.basename(path))[0]
    heading, chunks = "", []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            first, _, rest = block.partition("\n")
            if first.startswith("# ") and not chunks and not heading:
                doc_title = first.lstrip("# ").strip()
            heading = first.lstrip("# ").strip()
            block = rest.strip()
            if not block:
                continue
        title = f"{doc_title} · {heading}" if heading and heading != doc_title else doc_title
        chunks.append({"title": title, "text": block, "document_id": os.path.basename(path)})
    return chunks


def _local_index(root: str) -> dict:
    if not os.path.isabs(root):  # relative to the repository, not the working directory
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), root)
    files = sorted(
        p
        for ext in ("md", "txt")
        for p in glob.glob(os.path.join(root, "**", f"*.{ext}"), recursive=True)
        if not os.path.basename(p).lower().startswith("readme")  # describes the folder
    )
    key = (root, tuple((p, os.path.getmtime(p)) for p in files))
    if _INDEX.get("key") != key:
        docs = [c for p in files for c in _chunks(p)]
        toks = [re.findall(r"\w+", (d["title"] + " " + d["text"]).lower()) for d in docs]
        _INDEX.update(
            key=key,
            docs=docs,
            tf=[Counter(ts) for ts in toks],
            lens=[len(ts) for ts in toks],
            avg=(sum(len(ts) for ts in toks) / len(toks)) if toks else 0.0,
            df=Counter(t for ts in toks for t in set(ts)),
            n=len(docs),
        )
    return _INDEX


def _search_local(query: str, k: int) -> list[dict]:
    idx = _local_index(_env("GUIDELINES_DIR"))
    terms = set(re.findall(r"\w+", query.lower()))
    if not idx["n"] or not terms:
        return []
    scored = []
    for i, tf in enumerate(idx["tf"]):
        s = 0.0
        for t in terms:
            if t not in tf:
                continue
            idf = math.log(1 + (idx["n"] - idx["df"][t] + 0.5) / (idx["df"][t] + 0.5))
            norm = tf[t] + 1.5 * (1 - 0.75 + 0.75 * idx["lens"][i] / (idx["avg"] or 1))
            s += idf * tf[t] * 2.5 / norm
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    best = scored[0][0] if scored else 1.0
    # Scores are relative to the best hit (1.0), so they read like the other backends'.
    return [{**idx["docs"][i], "score": round(s / best, 3), "pages": []} for s, i in scored[:k]]


def retrieve(query: str, k: int = 4) -> list[dict]:
    """Return up to k relevant guideline passages for the query:
    [{title, text, score, document_id, pages}]."""
    kind = backend()
    try:
        if kind == "mcp":
            # run_analysis runs in a worker thread with no event loop, so a fresh
            # asyncio.run() per retrieval is safe.
            return asyncio.run(_search_mcp(query, k))
        if kind == "http":
            return _search_http(query, k)
        if kind == "local":
            return _search_local(query, k)
    except Exception as exc:  # never break a run over retrieval
        log.warning("guideline search via %s failed: %s", kind, exc)
    return []


# --- prompt helpers ----------------------------------------------------------------

def _language() -> str:
    return _env("GUIDELINES_LANGUAGE", default="de").lower()


def query_terms_hint() -> str:
    """What the search_guidelines tool signature asks the model for."""
    return {"de": "präzise deutsche Suchbegriffe", "en": "präzise englische Suchbegriffe"}.get(
        _language(), "präzise Suchbegriffe"
    )


def search_language_hint() -> str:
    """The system prompt's paragraph on which language to search in."""
    lang = _language()
    if lang == "de":
        return (
            "   SUCHSPRACHE: Das Referenzwerk ist derzeit DEUTSCH — formuliere die Suchbegriffe\n"
            "   vorzugsweise deutsch und nutze den deutschen Messungsnamen aus 'display_names'\n"
            "   (z. B. „Medialer Gelenkspalt\"). Die englische/deutsche Entsprechung wird maschinell\n"
            "   ergänzt; wähle also inhaltlich die treffendsten Begriffe."
        )
    if lang == "en":
        return (
            "   SUCHSPRACHE: Das Referenzwerk ist ENGLISCH — formuliere die Suchbegriffe\n"
            "   englisch und nutze den englischen Messungsnamen aus dem morphometry-Ergebnis\n"
            "   (z. B. „Insall Salvati Ratio\"). Die deutsche/englische Entsprechung wird maschinell\n"
            "   ergänzt; wähle also inhaltlich die treffendsten Begriffe."
        )
    return (
        "   SUCHSPRACHE: Das Referenzwerk ist mehrsprachig — formuliere die Suchbegriffe\n"
        "   deutsch oder englisch. Die jeweils andere Entsprechung des Messungsnamens wird\n"
        "   maschinell ergänzt; wähle also inhaltlich die treffendsten Begriffe."
    )


def format_for_prompt(guidelines: list[dict]) -> str:
    if not guidelines:
        return (
            "(Leitlinien-Server nicht erreichbar — "
            "allgemeine radiologische Best Practice anwenden)"
        )
    return "\n\n".join(f"### {g['title']}\n{g['text']}" for g in guidelines)
