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
    agentplane_lanes = [lane for lane in lanes if lane.get("kind") == "agentplane"]
    if not agentplane_lanes:
        fail("registry has no agentplane lane")

    lane = agentplane_lanes[0]
    if lane.get("match") != "agentplane-*":
        fail("agentplane lane must match agentplane-*")

    spawn = lane.get("spawn") or {}
    if spawn.get("command") != "hermes":
        fail("agentplane lane command must be hermes")
    if spawn.get("args") != [
        "agentplane",
        "supervise",
        "--task-id",
        "{agentplane_task_id}",
        "--root",
        "{repo}",
    ]:
        fail("agentplane lane args do not match the supervisor contract")

    plugin_text = PLUGIN.read_text(encoding="utf-8")
    entrypoint = PLUGIN_ENTRYPOINT.read_text(encoding="utf-8")
    if "agentplane_hermes_plugin" not in entrypoint:
        fail("root plugin entrypoint does not re-export the package")
    if "AgentPlaneLaneConfigError" not in plugin_text:
        fail("plugin does not fail closed on invalid AgentPlane lane config")
    if "AGENTPLANE_HERMES_ALLOWED_ROOTS" not in plugin_text:
        fail("plugin does not enforce the mandatory workspace allowlist")
    if "kanban.db" in plugin_text:
        fail("plugin must not reference the Hermes database")
    for needle in [
        "agentplane.hermes.plugin.v2",
        "AGENTPLANE_RUNNER_RESULT_PATH",
        "resume_argv",
        "_HeartbeatGuard",
        "AGENTPLANE_HERMES_APPROVAL_PRIVATE_KEY_PKCS8",
        "approval_receipt_bridge",
    ]:
        if needle not in plugin_text:
            fail(f"plugin is missing protocol surface {needle}")

    readme = README.read_text(encoding="utf-8")
    for needle in [
        "register_worker_lane",
        "AGENTPLANE_BIN",
        "AGENTPLANE_HERMES_LANE_REGISTRY",
        "AGENTPLANE_HERMES_ALLOWED_ROOTS",
        "metadata.agentplane.task_id",
        "hermes agentplane doctor --json",
        "hermes agentplane run",
        "hermes agentplane supervise",
        "hermes agentplane approve",
        "/agentplane_approve",
        "agentplane.hermes.plugin.v2",
    ]:
        if needle not in readme:
            fail(f"README is missing {needle}")

    print("integrity ok")


if __name__ == "__main__":
    main()
