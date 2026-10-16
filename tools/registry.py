"""The tool registry: declares local CLI tools (args in, JSON out) and dispatches.

Currently empty — morphometry and guideline search are served by remote services
(see agent.py: _execute_tool). This stays as the seam for any future local tools:
add a CLI script and register a Tool here; nothing else in the agent changes.
"""
from __future__ import annotations

from tools.base import Tool

TOOLS: dict[str, Tool] = {}


def tool_catalogue() -> str:
    return "\n".join(t.signature() for t in TOOLS.values())


def dispatch(name: str, arguments: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool '{name}'", "available": list(TOOLS)}
    return tool.run(arguments)
