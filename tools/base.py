"""Tool convention.

A Tool is just a command-line program that takes named arguments and prints a
single JSON object to stdout. Today these are local stub scripts; later, "run on
the server" means swapping the `command` for an SSH/HTTP invocation here -- the
agent never sees the difference because the contract (args in, JSON out) is the
same.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tool:
    name: str
    description: str
    # parameter name -> human description, shown to the LLM so it knows the args.
    parameters: dict[str, str] = field(default_factory=dict)
    # command template as a list; "{python}" and "{script}" are filled in.
    command: list[str] = field(default_factory=list)

    def signature(self) -> str:
        args = ", ".join(f"{k} <{v}>" for k, v in self.parameters.items())
        return f"- {self.name}({args}): {self.description}"

    def run(self, arguments: dict[str, Any]) -> dict:
        cmd = [part.format(python=sys.executable) for part in self.command]
        for key, value in arguments.items():
            cmd += [f"--{key}", str(value)]
        try:
            # Decode tool output as UTF-8 (results may contain German text); ask
            # Python child tools to emit UTF-8 too, regardless of the OS locale.
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return {"error": "tool timed out"}
        if proc.returncode != 0:
            return {"error": f"tool failed (exit {proc.returncode})", "stderr": proc.stderr.strip()}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"error": "tool did not return valid JSON", "raw": proc.stdout.strip()}
