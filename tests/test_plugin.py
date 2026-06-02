from __future__ import annotations

import json
import os
from pathlib import Path

import agentplane_hermes_plugin as plugin


ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    def __init__(self) -> None:
        self.lanes = []
        self.cli_commands = []
        self.commands = []

    def register_worker_lane(self, **kwargs):
        self.lanes.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_command(self, *args):
        self.commands.append(args)


def test_registers_native_worker_lane(monkeypatch):
    monkeypatch.setenv(
        "AGENTPLANE_HERMES_LANE_REGISTRY",
        str(ROOT / "registry" / "lane-registry.example.json"),
    )
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")

    ctx = FakeContext()
    plugin.register(ctx)

    assert len(ctx.lanes) == 1
    assert ctx.lanes[0]["match"] == "agentplane-*"
    assert ctx.lanes[0]["profile_exists"] is True
    assert [command["name"] for command in ctx.cli_commands] == ["agentplane"]
    assert [command[0] for command in ctx.commands] == ["agentplane_doctor"]


def test_doctor_payload_reads_registry(monkeypatch):
    monkeypatch.setenv(
        "AGENTPLANE_HERMES_LANE_REGISTRY",
        str(ROOT / "registry" / "lane-registry.example.json"),
    )
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")

    payload = plugin._doctor_payload()

    assert payload["registry_exists"] is True
    assert payload["agentplane_bin"] == "/usr/local/bin/agentplane"
    assert payload["lanes"][0]["match"] == "agentplane-*"


def test_agentplane_doctor_command_returns_json(monkeypatch):
    monkeypatch.setenv(
        "AGENTPLANE_HERMES_LANE_REGISTRY",
        str(ROOT / "registry" / "lane-registry.example.json"),
    )
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")

    ctx = FakeContext()
    plugin.register(ctx)
    doctor = ctx.commands[0][1]
    payload = json.loads(doctor())

    assert payload["registry_exists"] is True
    assert payload["lanes"][0]["kind"] == "agentplane"


def test_build_command_reads_structured_agentplane_metadata(monkeypatch):
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")
    lane = {
        "spawn": {
            "command": "agentplane",
            "args": [
                "hermes",
                "supervise",
                "{agentplane_task_id}",
                "--root",
                "{repo}",
                "--execute-step",
                "--json",
            ],
        },
    }
    source = {
        "id": "hermes-card-123",
        "workspace": "/workspace/project",
        "metadata": {"agentplane": {"task_id": "202606010001-ABCDEF"}},
    }

    assert plugin._build_command(lane, source) == [
        "/usr/local/bin/agentplane",
        "hermes",
        "supervise",
        "202606010001-ABCDEF",
        "--root",
        "/workspace/project",
        "--execute-step",
        "--json",
    ]


def test_build_command_requires_agentplane_task_id(monkeypatch):
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")
    lane = {
        "spawn": {
            "command": "agentplane",
            "args": ["hermes", "supervise", "{agentplane_task_id}"],
        },
    }
    source = {
        "id": "hermes-card-123",
        "workspace": "/workspace/project",
    }

    try:
        plugin._build_command(lane, source)
    except plugin.AgentPlaneLaneConfigError as exc:
        assert "metadata.agentplane.task_id" in str(exc)
    else:
        raise AssertionError("expected AgentPlaneLaneConfigError")


def test_allowed_roots_blocks_outside_workspace(monkeypatch):
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")
    monkeypatch.setenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", "/workspace/allowed")
    lane = {
        "spawn": {
            "command": "agentplane",
            "args": ["hermes", "supervise", "{agentplane_task_id}", "--root", "{repo}"],
        },
    }
    source = {
        "workspace": "/tmp/outside",
        "metadata": {"agentplane": {"task_id": "202606010001-ABCDEF"}},
    }

    try:
        plugin._build_command(lane, source)
    except plugin.AgentPlaneLaneConfigError as exc:
        assert "AGENTPLANE_HERMES_ALLOWED_ROOTS" in str(exc)
    else:
        raise AssertionError("expected AgentPlaneLaneConfigError")


def test_allowed_roots_allows_child_workspace(monkeypatch):
    monkeypatch.setenv("AGENTPLANE_BIN", "/usr/local/bin/agentplane")
    monkeypatch.setenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", "/workspace")
    lane = {
        "spawn": {
            "command": "agentplane",
            "args": ["hermes", "supervise", "{agentplane_task_id}", "--root", "{repo}"],
        },
    }
    source = {
        "workspace": "/workspace/project",
        "metadata": {"agentplane": {"task_id": "202606010001-ABCDEF"}},
    }

    assert plugin._build_command(lane, source)[-1] == "/workspace/project"


def test_build_env_maps_hermes_card_fields(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "existing-card")
    env = plugin._build_env(
        {
            "id": "hermes-card-123",
            "board": "repo-board",
            "run_id": "run-456",
            "workspace": "/workspace/project",
            "claim_lock": "lock-789",
        }
    )

    assert env["HERMES_KANBAN_TASK"] == "hermes-card-123"
    assert env["HERMES_KANBAN_BOARD"] == "repo-board"
    assert env["HERMES_KANBAN_RUN_ID"] == "run-456"
    assert env["HERMES_KANBAN_WORKSPACE"] == "/workspace/project"
    assert env["HERMES_KANBAN_CLAIM_LOCK"] == "lock-789"


def test_spawn_fn_maps_native_dispatch_task_and_workspace(monkeypatch):
    captured = {}

    class Task:
        id = "hermes-card-123"
        board = "repo-board"
        current_run_id = "run-456"
        workspace_path = "/workspace/from-task"
        claim_lock = "lock-789"
        metadata = {"agentplane": {"task_id": "202606010001-ABCDEF"}}

    class Proc:
        pid = 4242

    def fake_spawn(lane, source):
        captured["lane"] = lane
        captured["source"] = source
        return Proc()

    monkeypatch.setattr(plugin, "_spawn_agentplane", fake_spawn)

    lane = {"name": "agentplane-coder"}
    pid = plugin._spawn_fn_for(lane)(Task(), "/workspace/from-dispatch")

    assert pid == 4242
    assert captured["source"]["id"] == "hermes-card-123"
    assert captured["source"]["current_run_id"] == "run-456"
    assert captured["source"]["workspace_path"] == "/workspace/from-task"
    assert captured["source"]["metadata"]["agentplane"]["task_id"] == "202606010001-ABCDEF"


def test_spawn_fn_uses_positional_workspace_when_task_lacks_workspace(monkeypatch):
    captured = {}

    class Task:
        id = "hermes-card-123"
        current_run_id = "run-456"
        metadata = {"agentplane": {"task_id": "202606010001-ABCDEF"}}

    class Proc:
        pid = 4242

    def fake_spawn(lane, source):
        del lane
        captured.update(source)
        return Proc()

    monkeypatch.setattr(plugin, "_spawn_agentplane", fake_spawn)

    plugin._spawn_fn_for({"name": "agentplane-coder"})(Task(), "/workspace/from-dispatch")

    assert captured["workspace"] == "/workspace/from-dispatch"
