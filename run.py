"""Start the app.

    python run.py                  development: API on :8000 + Vite dev server on :5173
                                   (open http://localhost:5173; UI edits reload live)
    python run.py serve            build the UI once, then serve UI + API on one port
    python run.py serve --host 0.0.0.0 --port 8000
                                   ... reachable from the network. There is no login, so
                                   only do this on a closed network or behind a proxy
                                   that authenticates.

Run it from the Python environment that has requirements.txt installed. The UI needs
Node.js 18+. Ctrl+C stops everything.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")


def _npm(*args: str) -> list[str]:
    exe = shutil.which("npm")
    if not exe:
        sys.exit("npm not found. Install Node.js 18+ (https://nodejs.org) and try again.")
    return [exe, *args]


def _ensure_node_modules() -> None:
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        subprocess.run(_npm("install"), cwd=FRONTEND, check=True)


def _uvicorn(host: str, port: int, *extra: str) -> list[str]:
    # One worker only: sessions live in memory.
    return [sys.executable, "-m", "uvicorn", "api:app",
            "--host", host, "--port", str(port), "--workers", "1", *extra]


def dev(reload: bool) -> None:
    _ensure_node_modules()
    # Port 8000 is fixed here: the Vite dev server proxies /api to it (vite.config.ts).
    procs = [
        subprocess.Popen(_uvicorn("127.0.0.1", 8000, *(["--reload"] if reload else [])), cwd=ROOT),
        subprocess.Popen(_npm("run", "dev"), cwd=FRONTEND),
    ]
    print("\n  Open http://localhost:5173   (Ctrl+C stops both)\n", flush=True)
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


def serve(host: str, port: int) -> None:
    _ensure_node_modules()
    subprocess.run(_npm("run", "build"), cwd=FRONTEND, check=True)
    print(f"\n  Serving on http://{host}:{port}\n", flush=True)
    try:
        subprocess.run(_uvicorn(host, port), cwd=ROOT)
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["dev", "serve"], default="dev")
    ap.add_argument("--host", default="127.0.0.1", help="serve: interface to bind")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="serve: port")
    ap.add_argument("--reload", action="store_true",
                    help="dev: restart the API on code changes (drops open sessions)")
    args = ap.parse_args()
    if args.mode == "serve":
        serve(args.host, args.port)
    else:
        dev(args.reload)


if __name__ == "__main__":
    main()
