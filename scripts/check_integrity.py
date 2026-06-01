#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "agentplane_hermes_plugin" / "__init__.py"
PLUGIN_ENTRYPOINT = ROOT / "__init__.py"
REGISTRY = ROOT / "registry" / "lane-registry.example.json"
README = ROOT / "README.md"


def fail(message: str) -> None:
    print(f"integrity check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    py_compile.compile(str(PLUGIN), doraise=True)
    py_compile.compile(str(PLUGIN_ENTRYPOINT), doraise=True)

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    lanes = registry.get("lanes", [])
    agentplane_lanes = [
        lane for lane in lanes if lane.get("kind") == "agentplane"
    ]
    if not agentplane_lanes:
        fail("registry has no agentplane lane")

    lane = agentplane_lanes[0]
    if lane.get("match") != "agentplane-*":
        fail("agentplane lane must match agentplane-*")

    spawn = lane.get("spawn") or {}
    if spawn.get("command") != "agentplane":
        fail("agentplane lane command must be agentplane")
    if spawn.get("args") != [
        "hermes",
        "supervise",
        "{agentplane_task_id}",
        "--root",
        "{repo}",
        "--execute-step",
        "--json",
    ]:
        fail("agentplane lane args do not match the supervisor contract")

    plugin_text = PLUGIN.read_text(encoding="utf-8")
    entrypoint = PLUGIN_ENTRYPOINT.read_text(encoding="utf-8")
    if "agentplane_hermes_plugin" not in entrypoint:
        fail("root plugin entrypoint does not re-export the package")
    if "kanban.db" in plugin_text and "never by mutating kanban.db" not in plugin_text:
        fail("plugin appears to reference kanban.db outside the safety note")

    readme = README.read_text(encoding="utf-8")
    for needle in [
        "register_worker_lane",
        "AGENTPLANE_BIN",
        "AGENTPLANE_HERMES_LANE_REGISTRY",
        "hermes agentplane doctor --json",
    ]:
        if needle not in readme:
            fail(f"README is missing {needle}")

    print("integrity ok")


if __name__ == "__main__":
    main()
