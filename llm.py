"""Thin OpenAI-compatible chat client with multimodal (image) support.

Talks directly to the chat/completions endpoint defined by the active profile.
Kept deliberately small and dependency-light (requests) so behaviour is the same
across every model in .env, regardless of each model's native tool-calling
implementation.
"""
from __future__ import annotations

import base64
import io
import time
import warnings

import requests
from PIL import Image

from config import Config

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# A shared model server occasionally hangs or drops a connection; retry transient
# network failures a few times before giving up.
_RETRIES = 3
_RETRY_ON = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


def encode_image(path: str, max_side: int = 1024) -> str:
    """Load an image, downsample it for triage, return a base64 data URL.

    The orchestrator only needs a low-res view for routing decisions; the
    quantitative work is done by the tools on the full-resolution file.
    """
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def user_message(text: str, image_path: str | None = None) -> dict:
    """Build a (possibly multimodal) user message."""
    if not image_path:
        return {"role": "user", "content": text}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": encode_image(image_path)}},
        ],
    }


class LLM:
    def __init__(self, config: Config):
        self.config = config
        # Cumulative accounting over this instance's lifetime (one instance per
        # AgentSession, so per case). The server reports usage per request; zeros stay
        # zeros if a backend omits the field.
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.seconds = 0.0  # wall time inside complete(), retries included

    def complete(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        enable_thinking: bool | None = None,
    ) -> str:
        """`enable_thinking=False` asks a reasoning model to skip its chain-of-thought
        for this call. Best-effort and model-agnostic by design, so any vLLM-served
        model stays plug-and-play:
          - sent via `chat_template_kwargs`, a GENERIC vLLM field (not model-specific);
          - models whose chat template has no such variable silently ignore it;
          - backends that reject the field get the identical request without it (see
            the 4xx fallback below).
        Worst case is therefore the backend's default behaviour — never an error.
        Measured on the recheck rewrite (a transcription-style call whose answer is
        fully determined by the prompt): ~45-80s / 5-9k thinking tokens with thinking
        on, ~2s / ~270 tokens with it off, identical JSON. Leave it None wherever
        deliberation can help."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if enable_thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        last_exc: Exception | None = None
        t0 = time.perf_counter()
        for attempt in range(_RETRIES):
            try:
                resp = requests.post(
                    self.config.endpoint,
                    json=payload,
                    headers=headers,
                    verify=self.config.verify_ssl,
                    timeout=self.config.request_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                self.calls += 1
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                self.seconds += time.perf_counter() - t0
                return data["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError as exc:
                # A backend that doesn't know chat_template_kwargs may 4xx on it. The
                # switch is an optimisation, never a requirement — drop it and retry
                # immediately rather than failing the caller.
                status = exc.response.status_code if exc.response is not None else 0
                if "chat_template_kwargs" in payload and 400 <= status < 500:
                    payload.pop("chat_template_kwargs")
                    last_exc = exc
                    continue
                raise
            except _RETRY_ON as exc:
                last_exc = exc
                if attempt < _RETRIES - 1:
                    time.sleep(2 * (attempt + 1))  # brief backoff, then retry
        raise RuntimeError(f"LLM request failed after {_RETRIES} attempts: {last_exc}")
