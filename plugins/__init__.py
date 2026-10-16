"""Optional extensions, discovered at startup.

Every module in this package that defines ``install(app, host)`` is imported by api.py and
called once, after the core routes and before the built frontend is mounted. `host` hands
an extension what it may need from the core:

    host.sessions          live AgentSessions, keyed by session id
    host.session_or_404    look a session up (HTTP 404 if unknown) and mark it active
    host.dist_dir          the built frontend, for extensions that serve client routes
    host.recorder_lookup   set to a callable(uid) -> recorder | None, so that
                           POST /api/analyze_stream can attach an event recorder to a new
                           run (see agent.AgentSession.attach_recorder)

The core runs unchanged with this package empty. The frontend counterpart is
frontend/src/plugins/.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Host:
    sessions: dict
    session_or_404: Callable[[str], Any]
    dist_dir: str
    recorder_lookup: Callable[[str], Any] | None = None


def install_all(app, host: Host) -> list[str]:
    """Import every module in this package and call its install(app, host).

    Import errors are NOT swallowed: an extension that is present but broken should stop
    the server, not quietly disappear."""
    installed = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        install = getattr(module, "install", None)
        if callable(install):
            install(app, host)
            installed.append(info.name)
    return installed
