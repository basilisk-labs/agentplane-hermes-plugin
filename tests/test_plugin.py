from __future__ import annotations

import json
import subprocess
from base64 import b64encode, urlsafe_b64decode
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

    def register_command(self, *args, **kwargs):
        self.commands.append((args, kwargs))

    def get_config(self, key, default=None):
        return default


def configure_registry(monkeypatch, tmp_path: Path) -> Path:
    registry = tmp_path / "lane-registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "agentplane.hermes.lane-registry.v2",
                "lanes": [
                    {
                        "name": "agentplane-coder",
                        "match": "agentplane-*",
                        "kind": "agentplane",
                        "spawn": {
                            "command": "hermes",
                            "args": [
                                "agentplane",
                                "supervise",
                                "--task-id",
                                "{agentplane_task_id}",
                                "--root",
                                "{repo}",
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTPLANE_HERMES_LANE_REGISTRY", str(registry))
    monkeypatch.setenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", str(tmp_path))
    return registry


def fake_executable(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def configure_approval_key(monkeypatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    private_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv(
        plugin.APPROVAL_PRIVATE_KEY_ENV, b64encode(private_der).decode("ascii")
    )
    monkeypatch.setattr(
        plugin,
        "_APPROVAL_CONFIG",
        {"issuer": "hermes-dialog", "subject": "denis", "ttl_minutes": 10},
    )
    return key


def semantic(work_order_id: str) -> dict:
    return {
        "schema_version": 2,
        "kind": "agent_semantic_result",
        "work_order_id": work_order_id,
        "status": "completed",
        "summary": "Hermes completed the bounded episode.",
        "findings": [],
        "uncertainty": [],
    }


def test_registers_all_native_surfaces(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_NATIVE_WORKER_LANE_API", False)
    ctx = FakeContext()

    plugin.register(ctx)

    assert len(ctx.lanes) == 1
    assert ctx.lanes[0]["match"] == "agentplane-*"
    assert "profile_exists" not in ctx.lanes[0]
    assert [command["name"] for command in ctx.cli_commands] == ["agentplane"]
    assert ctx.cli_commands[0]["handler_fn"] is plugin._cli_handler
    assert [command[0][0] for command in ctx.commands] == [
        "agentplane_doctor",
        "agentplane_approve",
    ]
    assert plugin._NATIVE_WORKER_LANE_API is True


def test_setup_cli_exposes_approve_doctor_run_and_supervise():
    import argparse

    parser = argparse.ArgumentParser()
    plugin._setup_cli(parser)

    assert parser.parse_args(["doctor", "--json"]).agentplane_command == "doctor"
    assert parser.parse_args(["run"]).agentplane_command == "run"
    assert (
        parser.parse_args(["approve", "--task-id", "TASK"]).agentplane_command
        == "approve"
    )
    args = parser.parse_args(["supervise", "--task-id", "TASK", "--root", "/repo"])
    assert args.agentplane_command == "supervise"
    assert args.task_id == "TASK"


def test_doctor_fails_closed_without_capabilities(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "AGENTPLANE_HERMES_LANE_REGISTRY", str(tmp_path / "missing.json")
    )
    monkeypatch.delenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", raising=False)
    monkeypatch.setattr(plugin, "_NATIVE_WORKER_LANE_API", False)

    payload = plugin._doctor_payload()

    assert payload["ok"] is False
    assert payload["protocol"] == "agentplane.hermes.plugin.v2"
    assert payload["checks"]["registry_exists"] is False
    assert payload["checks"]["allowed_roots_fail_closed"] is False


def test_doctor_proves_protocol_v2_installation(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    agentplane = fake_executable(tmp_path, "agentplane")
    hermes = fake_executable(tmp_path, "hermes")
    monkeypatch.setenv("AGENTPLANE_BIN", str(agentplane))
    monkeypatch.setenv("HERMES_BIN", str(hermes))
    monkeypatch.setattr(plugin, "_NATIVE_WORKER_LANE_API", True)
    configure_approval_key(monkeypatch)

    payload = plugin._doctor_payload()

    assert payload["ok"] is True
    assert payload["schema"] == "agentplane.hermes.plugin-capabilities.v1"
    assert payload["commands"] == [
        "agentplane approve",
        "agentplane doctor",
        "agentplane run",
        "agentplane supervise",
    ]


def test_allowed_roots_is_mandatory(monkeypatch):
    monkeypatch.delenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", raising=False)

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="must contain"):
        plugin._assert_allowed_root("/workspace/project")


def test_allowed_roots_blocks_outside_workspace(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("AGENTPLANE_HERMES_ALLOWED_ROOTS", str(allowed))

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="outside"):
        plugin._assert_allowed_root(str(tmp_path / "outside"))


def test_build_command_uses_native_plugin_supervisor(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    hermes = fake_executable(tmp_path, "hermes")
    monkeypatch.setenv("HERMES_BIN", str(hermes))
    lane = plugin._agentplane_lanes()[0]
    source = {
        "workspace": str(tmp_path),
        "metadata": {"agentplane": {"task_id": "202606010001-ABCDEF"}},
    }

    assert plugin._build_command(lane, source) == [
        str(hermes),
        "agentplane",
        "supervise",
        "--task-id",
        "202606010001-ABCDEF",
        "--root",
        str(tmp_path),
    ]


def test_build_command_requires_agentplane_task_id(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    lane = plugin._agentplane_lanes()[0]

    with pytest.raises(
        plugin.AgentPlaneLaneConfigError, match="metadata.agentplane.task_id"
    ):
        plugin._build_command(lane, {"workspace": str(tmp_path)})


def test_build_env_does_not_inherit_unapproved_secret(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-forward")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "old-card")

    env = plugin._build_env(
        {
            "id": "card-123",
            "board": "repo-board",
            "run_id": "run-456",
            "workspace": str(tmp_path),
            "claim_lock": "lock-789",
        }
    )

    assert env["HERMES_KANBAN_TASK"] == "card-123"
    assert env["AGENTPLANE_HERMES_PLUGIN_PROTOCOL"] == plugin.PROTOCOL
    assert env["AGENTPLANE_HERMES_NATIVE_WORKER_LANE_API"] == "1"
    assert env["AGENTPLANE_HERMES_APPROVAL_RECEIPT_BRIDGE"] == "0"
    assert plugin.APPROVAL_PRIVATE_KEY_ENV not in env
    assert "UNRELATED_SECRET" not in env


def test_build_env_forwards_only_explicit_provider_secret(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "allowed-secret")
    monkeypatch.setenv("OTHER_SECRET", "blocked-secret")
    monkeypatch.setenv("AGENTPLANE_HERMES_FORWARD_ENV", "OPENROUTER_API_KEY")

    env = plugin._minimal_env()

    assert env["OPENROUTER_API_KEY"] == "allowed-secret"
    assert "OTHER_SECRET" not in env


def test_approval_key_stays_out_of_worker_environment(monkeypatch):
    configure_approval_key(monkeypatch)
    monkeypatch.setenv("AGENTPLANE_HERMES_FORWARD_ENV", plugin.APPROVAL_PRIVATE_KEY_ENV)

    env = plugin._minimal_env()

    assert env["AGENTPLANE_HERMES_APPROVAL_RECEIPT_BRIDGE"] == "1"
    assert plugin.APPROVAL_PRIVATE_KEY_ENV not in env


def test_signed_approval_receipt_is_bound_and_verifiable(monkeypatch):
    key = configure_approval_key(monkeypatch)
    request = {
        "approval_type": "side_effect",
        "task_id": "TASK",
        "authority_reference": "authority-1",
        "state_fingerprint": "sha256:" + "a" * 64,
        "operation_id": "pr.open",
        "operation_digest": "sha256:" + "b" * 64,
        "state_scope_digest": "sha256:" + "c" * 64,
    }

    encoded = plugin._signed_approval_receipt(request)
    padded = encoded + "=" * (-len(encoded) % 4)
    receipt = json.loads(urlsafe_b64decode(padded).decode("utf-8"))
    signature = receipt.pop("signature")
    canonical = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    signature_padded = signature + "=" * (-len(signature) % 4)
    key.public_key().verify(
        urlsafe_b64decode(signature_padded), canonical.encode("utf-8")
    )

    assert receipt["task_id"] == "TASK"
    assert receipt["operation_digest"] == request["operation_digest"]
    assert receipt["state_scope_digest"] == request["state_scope_digest"]
    assert receipt["subject"] == "denis"


def test_approve_executes_only_exact_packet_argv_and_fetches_fresh_packet(
    monkeypatch, tmp_path
):
    configure_registry(monkeypatch, tmp_path)
    configure_approval_key(monkeypatch)
    request = {
        "approval_type": "plan_approval",
        "task_id": "TASK",
        "authority_reference": "plan",
        "state_fingerprint": "sha256:" + "a" * 64,
        "operation_id": None,
        "operation_digest": None,
        "state_scope_digest": None,
    }
    issued = {
        "action": {"kind": "approval_required"},
        "operator_action": {
            "kind": "approve_plan",
            "required_role": "USER",
            "cwd": str(tmp_path),
            "argv": [
                "agentplane",
                "task",
                "plan",
                "approve",
                "TASK",
                "--approval-receipt",
                plugin.APPROVAL_RECEIPT_PLACEHOLDER,
            ],
            "approval_receipt": {
                "schema_version": 1,
                "format": "base64url-json+ed25519",
                "request": request,
            },
        },
    }
    fresh = {
        "action": {"kind": "agent_episode"},
        "stop": {"reason": "semantic_boundary"},
    }
    packets = iter([issued, fresh])
    executed = []
    monkeypatch.setattr(plugin, "_advance_command", lambda: ["ap"])
    monkeypatch.setattr(plugin, "_invoke_json", lambda *args, **kwargs: next(packets))

    def run(argv, **kwargs):
        executed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(plugin, "_run_process", run)

    result = plugin._approve("TASK", str(tmp_path))

    assert len(executed) == 1
    argv, invocation = executed[0]
    assert argv[:6] == [
        "agentplane",
        "task",
        "plan",
        "approve",
        "TASK",
        "--approval-receipt",
    ]
    assert len(argv) == 7
    assert argv[6] != plugin.APPROVAL_RECEIPT_PLACEHOLDER
    assert plugin.APPROVAL_PRIVATE_KEY_ENV not in invocation["env"]
    assert result == {
        "schema": "agentplane.hermes.approval-result.v1",
        "task_id": "TASK",
        "approved_kind": "approve_plan",
        "actor": "USER:denis@hermes-dialog",
        "next_action": "agent_episode",
        "next_stop_reason": "semantic_boundary",
    }


def test_approval_bridge_refuses_provider_merge(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    configure_approval_key(monkeypatch)
    packet = {
        "action": {"kind": "approval_required"},
        "operator_action": {
            "kind": "approve_provider_merge",
            "required_role": "USER",
            "cwd": str(tmp_path),
            "argv": None,
            "approval_receipt": {
                "schema_version": 1,
                "format": "base64url-json+ed25519",
                "request": {},
            },
        },
    }
    monkeypatch.setattr(plugin, "_advance_command", lambda: ["ap"])
    monkeypatch.setattr(plugin, "_invoke_json", lambda *args, **kwargs: packet)

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="cannot be executed"):
        plugin._approve("TASK", str(tmp_path))


def test_spawn_requires_complete_native_claim(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    hermes = fake_executable(tmp_path, "hermes")
    monkeypatch.setenv("HERMES_BIN", str(hermes))
    lane = plugin._agentplane_lanes()[0]
    source = {
        "id": "card-123",
        "workspace": str(tmp_path),
        "metadata": {"agentplane": {"task_id": "TASK"}},
    }

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="claim is incomplete"):
        plugin._spawn_agentplane(lane, source)


def test_runner_run_writes_validated_result_atomically(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    run_dir = tmp_path / ".agentplane" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    bundle = run_dir / "bundle.json"
    bootstrap = run_dir / "bootstrap.md"
    result = run_dir / "result.json"
    bundle.write_text(
        json.dumps(
            {
                "repository": {"git_root": str(tmp_path)},
                "work_order": {"work_order_id": "work-order-1", "role": "EXECUTOR"},
            }
        ),
        encoding="utf-8",
    )
    bootstrap.write_text("Perform the bounded implementation.", encoding="utf-8")
    monkeypatch.setenv("AGENTPLANE_RUNNER_BUNDLE_PATH", str(bundle))
    monkeypatch.setenv("AGENTPLANE_RUNNER_BOOTSTRAP_PATH", str(bootstrap))
    monkeypatch.setenv("AGENTPLANE_RUNNER_RUN_DIR", str(run_dir))
    monkeypatch.setenv("AGENTPLANE_RUNNER_RESULT_PATH", str(result))
    monkeypatch.setenv("AGENTPLANE_HERMES_AGENT_COMMAND", '["fake-hermes"]')
    monkeypatch.setattr(
        plugin,
        "_run_process",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(semantic("work-order-1")), stderr=""
        ),
    )

    payload = plugin._run_runner_work_order()

    assert payload["status"] == "completed"
    assert json.loads(result.read_text(encoding="utf-8")) == payload
    assert oct(result.stat().st_mode & 0o777) == "0o600"


def test_runner_rejects_wrong_work_order_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTPLANE_HERMES_AGENT_COMMAND", '["fake-hermes"]')
    monkeypatch.setattr(
        plugin,
        "_run_process",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(semantic("wrong-work-order")), stderr=""
        ),
    )

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="work_order_id"):
        plugin._execute_work_order(
            {"work_order_id": "expected", "role": "EXECUTOR"},
            cwd=tmp_path,
            env={},
        )


def test_supervise_uses_exact_result_path_and_resume_argv(monkeypatch, tmp_path):
    configure_registry(monkeypatch, tmp_path)
    for name, value in {
        "HERMES_KANBAN_TASK": "card-1",
        "HERMES_KANBAN_BOARD": "board-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_WORKSPACE": str(tmp_path),
        "HERMES_KANBAN_CLAIM_LOCK": "lock-1",
    }.items():
        monkeypatch.setenv(name, value)
    exchange_dir = tmp_path / "exchange"
    exchange_dir.mkdir()
    (exchange_dir / "work-order.json").write_text(
        json.dumps({"work_order_id": "wo-1", "role": "EXECUTOR"}), encoding="utf-8"
    )
    result_path = exchange_dir / "result.json"
    resume = [
        "agentplane",
        "task",
        "advance",
        "TASK",
        "--result",
        str(result_path),
        "--agent-json",
    ]
    issued = {
        "task_id": "TASK",
        "transition_id": "tr_123",
        "state_fingerprint": "sha256:abc",
        "action": {"kind": "agent_episode"},
        "exchange": {
            "directory": str(exchange_dir),
            "work_order_ref": "work-order.json",
            "result_path": str(result_path),
            "resume_argv": resume,
        },
    }
    terminal = {
        "action": {"kind": "approval_required"},
        "stop": {"reason": "authority_boundary"},
    }
    calls = []

    class Guard:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def assert_current(self):
            pass

        def __exit__(self, *args):
            pass

    def invoke(argv, **kwargs):
        calls.append(argv)
        return issued if len(calls) == 1 else terminal

    monkeypatch.setattr(plugin, "_advance_command", lambda: ["ap"])
    monkeypatch.setattr(plugin, "_invoke_json", invoke)
    monkeypatch.setattr(plugin, "_HeartbeatGuard", Guard)
    monkeypatch.setattr(
        plugin, "_execute_work_order", lambda *args, **kwargs: semantic("wo-1")
    )

    result = plugin._supervise("TASK", str(tmp_path))

    assert calls == [["ap", "task", "advance", "TASK", "--agent-json"], resume]
    envelope = json.loads(result_path.read_text(encoding="utf-8"))
    assert envelope["transition_id"] == "tr_123"
    assert envelope["state_fingerprint"] == "sha256:abc"
    assert envelope["result"]["work_order_id"] == "wo-1"
    assert result["action"]["kind"] == "approval_required"


def test_heartbeat_rejects_stale_run(monkeypatch, tmp_path):
    hermes = fake_executable(tmp_path, "hermes")
    monkeypatch.setenv("HERMES_BIN", str(hermes))
    env = {
        "HERMES_KANBAN_TASK": "card-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "lock-1",
    }
    monkeypatch.setattr(
        plugin,
        "_run_process",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="not current"
        ),
    )

    with pytest.raises(plugin.AgentPlaneLaneConfigError, match="not current"):
        with plugin._HeartbeatGuard(tmp_path, env):
            pass
