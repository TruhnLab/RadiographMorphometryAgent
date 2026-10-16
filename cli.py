"""Headless runner — analyse one case, then drop into a terminal chat.

Usage:  python cli.py --image path/to/xray.png
"""
from __future__ import annotations

import argparse
import sys

from agent import AgentSession
from config import Config

# Windows consoles default to cp1252 and choke on the trace arrows / em-dashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--profile", default=None, help="override ACTIVE_PROFILE")
    args = ap.parse_args()

    config = Config.load(args.profile)
    print(f"Using profile '{config.profile}' -> {config.model}\n")

    session = AgentSession(config, args.image)
    report = session.run_analysis()

    print("\n".join(session.log))
    print("\n" + "=" * 60)
    # None -> the agent declined to assess this image (e.g. unsupported region).
    print(report.to_markdown() if report else session.last_reply)
    print("=" * 60)
    print("\nChat with the agent (blank line to quit).")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            break
        print("\nassistant> " + session.chat(text))


if __name__ == "__main__":
    main()
