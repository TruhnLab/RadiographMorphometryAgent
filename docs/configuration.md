# Configuration

All settings live in a `.env` file in the repository root (start from `.env.example`).
The file is re-read while the app runs, so a new model profile shows up after a page
reload. `RRA_ENV_FILE=/path/to/file` makes the app read a different file instead.

The assistant needs three services. Each is configured independently, so you can swap
any of them for your own.

| Service | What it provides | Configure |
|---|---|---|
| Language model | reasoning, image triage, report writing | `<NAME>_ENDPOINT`, `_MODEL`, `_API_KEY`, `_LABEL` |
| Guideline search | reference ranges and their source passages | `GUIDELINES_*` |
| Morphometry service | landmarks and measurements | `MORPHO_*` |

## Language model

Any endpoint that speaks the OpenAI `/chat/completions` protocol works: Ollama, vLLM,
LiteLLM, llama.cpp's server, or a hosted API. Each model is a *profile*, a group of
variables that share a prefix:

```
MYMODEL_ENDPOINT=http://localhost:11434/v1/chat/completions   # required
MYMODEL_MODEL=qwen2.5vl:7b                                    # required
MYMODEL_API_KEY=...                                           # optional
MYMODEL_LABEL=Qwen2.5-VL 7B (lokal)                           # optional, shown in the UI
ACTIVE_PROFILE=mymodel                                        # the default selection
```

Define as many profiles as you like. The model dropdown lists all of them and marks the
ones the endpoint currently serves (read from its `/models` listing).

Some notes on choosing a model:

- **Vision.** The model first looks at a downscaled copy of the radiograph to decide the
  body region and projection, so it must accept images. All numbers still come from the
  morphometry service, never from the model's view of the image.
- **Instruction following.** The agent talks to the model in a strict JSON action
  protocol rather than native tool calling, so any model works in principle. Larger
  models follow the grounding rules more reliably.
- **Reasoning models.** The *Denkmodus* switch in the header turns their chain of thought
  off for a session (sent as `chat_template_kwargs.enable_thinking`, which vLLM
  understands). Servers that reject the field get the request without it.
- **Privacy.** The radiograph is sent to the endpoint. Keep real patient data on a local
  or institutional server.

`VERIFY_SSL=false` disables certificate checks for a server with a self-signed
certificate.

## Guideline search

Every reference range and every interpretation in a report must come from a passage
this search returns. The agent formulates its own queries, and the UI shows each
retrieved passage. Pick one backend:

| `GUIDELINES_BACKEND` | Use it for | Settings |
|---|---|---|
| `local` | a folder of Markdown or text files, ranked with BM25. No server needed | `GUIDELINES_DIR` |
| `mcp` | an MCP server (Streamable HTTP) that exposes a search tool | `GUIDELINES_MCP_URL`, `GUIDELINES_MCP_TOKEN`, `GUIDELINES_MCP_TOOL` (default `search`), `GUIDELINES_MCP_QUERY_ARG` (default `query`) |
| `http` | any JSON endpoint, called as `POST {"query": "...", "top_k": 4}` | `GUIDELINES_HTTP_URL`, `GUIDELINES_HTTP_TOKEN` |
| `none` | no retrieval. Measurements are reported but not interpreted | |

You can leave `GUIDELINES_BACKEND` unset. The backend then follows from what is
configured, in the order mcp, http, local.

**Result format (mcp and http).** The response should be JSON with a list of results
under `results` (`GUIDELINES_RESULTS_PATH` changes the key, an empty value means the
response itself is the list). Each result needs at least a text field. The fields are
looked up under common names, and each can be pointed elsewhere with a comma-separated
list of dotted paths:

| Setting | Default lookup |
|---|---|
| `GUIDELINES_FIELD_TEXT` | `text, content, chunk_text, chunk.chunk_text, page_content` |
| `GUIDELINES_FIELD_TITLE` | `title, document.name, document.title, source, name` |
| `GUIDELINES_FIELD_SCORE` | `score, similarity, relevance` |
| `GUIDELINES_FIELD_ID` | `id, document_id, document.id` |
| `GUIDELINES_FIELD_PAGES` | `pages, metadata.pages, chunk.metadata.pages` |

**Local files.** See `guidelines/example_corpus/README.md` for the format. The demo corpus
in that folder is for trying the pipeline only.

**Language.** `GUIDELINES_LANGUAGE` (`de`, `en` or `mixed`) tells the agent which language
to search in. Measurement names in a query are added in both English and German
automatically, so either works for recall.

`GUIDELINES_VERIFY_SSL=false` disables certificate checks for the mcp and http backends.

## Morphometry service

The measurements come from a separate GPU service. The reference implementation is the
MCP server in [RadiographLandmarkMatching](https://github.com/TruhnLab/RadiographLandmarkMatching)
(folder `docker/`). It needs a set of annotated reference radiographs for each
anatomy and projection you want to support. Which combinations exist is read from the
service at runtime, so the UI and the agent adapt to whatever it offers.

```
MORPHO_URL=https://localhost         # base URL of the service
MORPHO_API_KEY=...                   # sent as X-API-Key
MORPHO_NUM_REFERENCES=10             # references matched per image (-1 = all)
MORPHO_VERIFY_SSL=false              # the reference deployment uses a self-signed certificate
```

A different service works if it speaks the same protocol:

| Call | Purpose |
|---|---|
| `POST /upload` | raw image bytes in, `{"image_token"}` out |
| MCP `morphometry_health` | supported `{anatomy, projection, measurements, landmark_names}` |
| MCP `get_morphometry` | landmarks, measurements, `mode`, `mpp` and a `result_token` for an uploaded image |
| MCP `get_processed_image` | the image the landmarks refer to |
| `POST /recompute` | measurements and construction geometry from edited landmarks. Optional: without it, landmarks are shown but cannot be dragged |
| MCP `visualize_measurement` | a rendered measurement, used only when `/recompute` is missing |

Measurement names that the UI and the report show are curated in
`tools/measurement_registry.py` (German display name, group, decimals). A measurement the
service returns but the registry does not list is left out everywhere, so add an entry
there when you add one to the service.

## Other settings

| Variable | Default | Meaning |
|---|---|---|
| `UPLOAD_TTL_HOURS` | `12` | idle sessions and their uploaded images are deleted after this |
| `REAP_INTERVAL_HOURS` | `24` | how often that cleanup runs |
| `MORPHO_CAPS_TTL` | `300` | seconds the service's capability list is cached |

## Extensions

Code in `plugins/` (backend) and `frontend/src/plugins/` (UI) is loaded automatically
when present. An extension can add API routes, record interaction events and replace or
extend parts of the UI. This is how we run reader studies on top of the assistant without
changing its core. See `plugins/__init__.py` and `frontend/src/lib/plugins.ts`.
