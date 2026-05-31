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

