"""Per-case event trail for headless batch runs.

One JSONL file per case (`<AUDIT_DIR>/<case_id>.jsonl`), one server-side event per line —
the agent's own record of what the pipeline did to a case (baseline classification,
reference-range changes, landmark recomputes). It exists only for HEADLESS batch runs,
which have no reader and no session stream to fold events into. A recorded interactive
session (a recording extension, see plugins/) gets the same server events — interleaved
with the client-side ones — in its per-session stream instead; a casual interactive run
records nothing at all.

Off by default: a batch runner turns it on and points AUDIT_DIR at the run's own folder, so a
run's events stay with the rest of its output instead of leaking into a shared directory.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

# Set by a batch runner to its per-run folder; unused otherwise.
AUDIT_DIR = os.path.join(os.path.dirname(__file__), "audit")

# Off unless a batch runner enables it — see the module docstring for why recorded and
# casual interactive sessions write elsewhere / nowhere.
enabled = False

_LOCK = threading.Lock()  # concurrent cases append to distinct files, but be safe


def log(case_id: str, event: str, payload: dict) -> None:
    """Append one event to the case's trail. A no-op unless `enabled` (batch runs only). Never
    raises: a broken sink must not take down the run."""
    if not enabled:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case_id": case_id,
        "event": event,
        **payload,
    }
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR, f"{case_id}.jsonl")
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
