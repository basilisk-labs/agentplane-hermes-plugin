"""AgentPlane external worker-lane compatibility plugin.

This plugin is intentionally conservative. When Hermes exposes a native
register_worker_lane API, it registers AgentPlane lanes through that public
surface. Until then, it provides `hermes agentplane doctor --json` and keeps all
Kanban writes outside the plugin; AgentPlane must close cards through Hermes'
own lifecycle API/CLI, never by mutating kanban.db directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY = "/opt/agentplane/lane-registry.json"
REQUIRED_ENV = [
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_CLAIM_LOCK",
]


def _registry_path() -> Path:
    return Path(os.environ.get("AGENTPLANE_HERMES_LANE_REGISTRY") or DEFAULT_REGISTRY)


def _agentplane_bin() -> str | None:
    configured = os.environ.get("AGENTPLANE_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("agentplane")


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.is_file():
        return {"schema": "agentplane.hermes.lane-registry.v1", "lanes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _agentplane_lanes() -> list[dict[str, Any]]:
    registry = _load_registry()
    lanes = registry.get("lanes", [])
    return [
        lane
        for lane in lanes
        if isinstance(lane, dict) and lane.get("kind") == "agentplane"
    ]


def _value(source: dict[str, Any], *names: str) -> str:
    for name in names:
        value = source.get(name)
        if value is not None:
            return str(value)
    return ""


def _metadata_value(source: dict[str, Any], *names: str) -> str:
    metadata = source.get("metadata")
    if not isinstance(metadata, dict):
        return ""

    agentplane = metadata.get("agentplane")
    if not isinstance(agentplane, dict):
        return ""

    for name in names:
        value = agentplane.get(name)
        if value is not None:
            return str(value)
    return ""


def _build_env(source: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    mappings = {
        "HERMES_KANBAN_TASK": ("HERMES_KANBAN_TASK", "hermes_kanban_task", "card_id", "id"),
        "HERMES_KANBAN_BOARD": ("HERMES_KANBAN_BOARD", "hermes_kanban_board", "board"),
        "HERMES_KANBAN_RUN_ID": (
            "HERMES_KANBAN_RUN_ID",
            "hermes_kanban_run_id",
            "run_id",
            "current_run_id",
        ),
        "HERMES_KANBAN_WORKSPACE": (
            "HERMES_KANBAN_WORKSPACE",
            "hermes_kanban_workspace",
            "workspace",
            "workspace_path",
            "repo",
            "root",
        ),
        "HERMES_KANBAN_CLAIM_LOCK": (
            "HERMES_KANBAN_CLAIM_LOCK",
            "hermes_kanban_claim_lock",
            "claim_lock",
        ),
    }
    for name, aliases in mappings.items():
        value = _value(source, *aliases)
        if value:
            env[name] = value
    return env


def _build_command(lane: dict[str, Any], source: dict[str, Any]) -> list[str]:
    spawn = lane.get("spawn") or {}
    command = os.environ.get("AGENTPLANE_BIN") or spawn.get("command") or "agentplane"
    if command == "agentplane":
        command = _agentplane_bin() or command

    task_id = _value(
        source,
        "agentplane_task_id",
        "agentplaneTaskId",
    ) or _metadata_value(
        source,
        "task_id",
        "taskId",
        "id",
    ) or _value(
        source,
        "task_id",
        "id",
        "HERMES_KANBAN_TASK",
    )
    repo = _value(
        source,
        "repo",
        "root",
        "workspace",
        "HERMES_KANBAN_WORKSPACE",
    )

    replacements = {
        "{agentplane_task_id}": task_id,
        "{task_id}": task_id,
        "{repo}": repo,
        "{root}": repo,
        "{workspace}": repo,
    }

    args = []
    for raw in spawn.get("args", []):
        text = str(raw)
        for key, value in replacements.items():
            text = text.replace(key, value)
        args.append(text)

    return [str(command), *args]


def _spawn_agentplane(lane: dict[str, Any], source: dict[str, Any]) -> subprocess.Popen:
    command = _build_command(lane, source)
    workspace = _value(source, "workspace", "repo", "root", "HERMES_KANBAN_WORKSPACE")
    cwd = workspace if workspace and Path(workspace).is_dir() else None
    env = _build_env(source)
    return subprocess.Popen(command, cwd=cwd, env=env)


def _spawn_fn_for(lane: dict[str, Any]):
    def spawn_fn(*args, **kwargs):
        source: dict[str, Any] = {}
        for index, item in enumerate(args):
            if isinstance(item, dict):
                source.update(item)
            elif isinstance(item, (str, os.PathLike)):
                if index == 1 and not _value(source, "workspace", "workspace_path", "repo", "root"):
                    source["workspace"] = os.fspath(item)
            else:
                for attr in [
                    "id",
                    "card_id",
                    "task_id",
                    "agentplane_task_id",
                    "workspace",
                    "workspace_path",
                    "repo",
                    "root",
                    "board",
                    "run_id",
                    "current_run_id",
                    "claim_lock",
                    "metadata",
                ]:
                    if hasattr(item, attr):
                        source[attr] = getattr(item, attr)
        source.update(kwargs)
        proc = _spawn_agentplane(lane, source)
        return getattr(proc, "pid", None)

    return spawn_fn


def _doctor_payload() -> dict[str, Any]:
    registry_path = _registry_path()
    agentplane_bin = _agentplane_bin()
    lanes = _agentplane_lanes()
    return {
        "ok": bool(registry_path.is_file() and agentplane_bin and lanes),
        "registry": str(registry_path),
        "registry_exists": registry_path.is_file(),
        "agentplane_bin": agentplane_bin,
        "lanes": lanes,
        "native_worker_lane_api": False,
    }


def _doctor_handler(args=None, **kwargs):
    del args, kwargs
    payload = _doctor_payload()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def _setup_cli(parser):
    subcommands = parser.add_subparsers(dest="agentplane_command")
    doctor = subcommands.add_parser("doctor", help="Show AgentPlane integration status")
    doctor.add_argument("--json", action="store_true", help="Emit JSON output")


def _cli_handler(args=None, **kwargs):
    command = getattr(args, "agentplane_command", None)
    if command in (None, "doctor"):
        return _doctor_handler(args=args, **kwargs)
    raise SystemExit(f"Unsupported agentplane command: {command}")


def register(ctx):
    lanes = _agentplane_lanes()
    native_register = getattr(ctx, "register_worker_lane", None)

    if callable(native_register):
        for lane in lanes:
            match = lane.get("match") or lane.get("name")
            if not match:
                continue
            native_register(match=match, spawn_fn=_spawn_fn_for(lane), profile_exists=True)

        # Surface this in doctor output when the runtime supports it.
        def native_doctor(args=None, **kwargs):
            payload = _doctor_payload()
            payload["native_worker_lane_api"] = True
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return payload

        doctor = native_doctor
    else:
        doctor = _cli_handler

    register_cli_command = getattr(ctx, "register_cli_command", None)
    if callable(register_cli_command):
        register_cli_command(
            name="agentplane",
            help="AgentPlane worker-lane integration helpers",
            setup_fn=_setup_cli,
            handler_fn=doctor,
        )

    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(
            "agentplane_doctor",
            lambda *args, **kwargs: json.dumps(_doctor_payload(), ensure_ascii=False),
            "Show AgentPlane worker-lane integration status as JSON.",
        )
