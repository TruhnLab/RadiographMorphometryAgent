"""Configuration loading.

Resolves the active model profile from the .env file. Each profile defines an
OpenAI-compatible endpoint, an API key and a model name. Switch profiles by
changing ACTIVE_PROFILE in .env (or by passing a profile name in the UI).

The .env next to this file is read by default; RRA_ENV_FILE points elsewhere (e.g. a
secrets file kept outside the code tree). Importing this module loads it, which is why
the service clients import it too.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import dotenv_values, load_dotenv

ENV_FILE = os.environ.get("RRA_ENV_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".env"
)

load_dotenv(ENV_FILE)
# Keys the .env file provided at the last read. load_dotenv only ever adds/overrides —
# it cannot delete — so to make a profile REMOVED from the file disappear too, we track
# what the file contributed and unset whatever vanished on the next refresh.
_FILE_KEYS: set[str] = set(dotenv_values(ENV_FILE))


@dataclass
class Config:
    profile: str
    api_key: str
    endpoint: str
    model: str
    # Optional display name from .env (<PREFIX>_LABEL, e.g. "Qwen 35B – lokale
    # Verarbeitung"). The technical model id stays internal; the UI shows this alias.
    label: str = ""
    # TLS verification for the model endpoint. Set VERIFY_SSL=false for a self-signed
    # certificate (e.g. a local proxy on an IP address).
    verify_ssl: bool = True
    request_timeout: int = 180

    @staticmethod
    def _refresh_env() -> None:
        """Re-read .env so edits show up WITHOUT restarting the backend — additions,
        changes AND removals: a profile added to the file appears on the next
        /api/profiles fetch (page reload), edited values win over the stale first read
        (override=True), and keys the file no longer contains are unset again, so a
        deleted profile also disappears. Loaded Config instances hold copies, so running
        sessions are unaffected. One edge: a key defined both in the real environment
        and in .env cannot fall back to its environment value after removal from the
        file — the first override already replaced it (irrelevant for profile keys,
        which live only in .env)."""
        global _FILE_KEYS
        current = dotenv_values(ENV_FILE)
        for stale in _FILE_KEYS - set(current):
            os.environ.pop(stale, None)
        load_dotenv(ENV_FILE, override=True)
        _FILE_KEYS = set(current)

    @staticmethod
    def list_profiles() -> list[str]:
        """Every env var ending in _ENDPOINT marks a profile."""
        Config._refresh_env()
        profiles = []
        for key in os.environ:
            if key.endswith("_ENDPOINT"):
                profiles.append(key[: -len("_ENDPOINT")].lower())
        return sorted(profiles)

    @classmethod
    def load(cls, profile: str | None = None) -> "Config":
        cls._refresh_env()
        profile = (profile or os.environ.get("ACTIVE_PROFILE", "")).strip()
        if not profile:
            raise RuntimeError("No ACTIVE_PROFILE set in .env and none provided.")
        prefix = profile.upper()

        def need(suffix: str) -> str:
            val = os.environ.get(f"{prefix}_{suffix}")
            if not val:
                raise RuntimeError(
                    f"Profile '{profile}' is missing {prefix}_{suffix} in .env"
                )
            return val

        return cls(
            profile=profile,
            # Optional: local servers (Ollama, a bare vLLM) usually need no key.
            api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
            endpoint=need("ENDPOINT"),
            model=need("MODEL"),
            label=os.environ.get(f"{prefix}_LABEL", "").strip(),
            verify_ssl=os.environ.get("VERIFY_SSL", "true").lower() == "true",
        )
