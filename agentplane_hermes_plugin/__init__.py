"""Hermes native worker-lane bridge for the AgentPlane supervisor protocol."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from base64 import b64decode, b64encode, urlsafe_b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VERSION = "0.2.0"
PROTOCOL = "agentplane.hermes.plugin.v2"
CAPABILITY_SCHEMA = "agentplane.hermes.plugin-capabilities.v1"
DEFAULT_REGISTRY = "/opt/agentplane/lane-registry.json"
TERMINAL_ACTIONS = {
    "approval_required",
    "human_input_required",
    "external_wait",
    "framework_transition",
    "terminal",
}
APPROVAL_RECEIPT_PLACEHOLDER = "<base64url-receipt>"
APPROVAL_PRIVATE_KEY_ENV = "AGENTPLANE_HERMES_APPROVAL_PRIVATE_KEY_PKCS8"
RUNNER_ENV = {
    "AGENTPLANE_RUNNER_ADAPTER",
    "AGENTPLANE_RUNNER_API_VERSION",
    "AGENTPLANE_RUNNER_BOOTSTRAP_PATH",
    "AGENTPLANE_RUNNER_BUNDLE_PATH",
    "AGENTPLANE_RUNNER_DANGER_AUTHORIZED",
    "AGENTPLANE_RUNNER_ENFORCEMENT_MODE",
    "AGENTPLANE_RUNNER_ENFORCEMENT_PLATFORM",
    "AGENTPLANE_RUNNER_EVENTS_PATH",
    "AGENTPLANE_RUNNER_MODE",
    "AGENTPLANE_RUNNER_RESULT_PATH",
    "AGENTPLANE_RUNNER_RUN_DIR",
    "AGENTPLANE_RUNNER_SANDBOX_ENFORCEMENT",
    "AGENTPLANE_RUNNER_SANDBOX_REQUESTED",
    "AGENTPLANE_RUNNER_STATE_PATH",
    "AGENTPLANE_RUNNER_TARGET",
    "AGENTPLANE_RUNNER_TASK_ID",
    "AGENTPLANE_RUNNER_WORK_ORDER_ID",
}
BASE_ENV = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "TMPDIR",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "HERMES_HOME",
    "HERMES_INFERENCE_MODEL",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_BIN",
    "AGENTPLANE_BIN",
    "AGENTPLANE_BIN_ARGS",
    "AP_BIN",
    "AGENTPLANE_HERMES_AGENT_COMMAND",
    "AGENTPLANE_HERMES_FORWARD_ENV",
    "AGENTPLANE_HERMES_HEARTBEAT_SECONDS",
    "AGENTPLANE_HERMES_LANE_REGISTRY",
    "AGENTPLANE_HERMES_ALLOWED_ROOTS",
}
CLAIM_ENV = {
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_CLAIM_LOCK",
}

_NATIVE_WORKER_LANE_API = False
_APPROVAL_CONFIG: dict[str, Any] = {
    "issuer": "hermes-dialog",
    "subject": "owner",
    "ttl_minutes": 10,
}


class AgentPlaneLaneConfigError(RuntimeError):
    """Raised when the bridge cannot prove a safe AgentPlane execution contract."""


def _registry_path() -> Path:
    return Path(os.environ.get("AGENTPLANE_HERMES_LANE_REGISTRY") or DEFAULT_REGISTRY)


def _resolved_executable(configured: str | None, fallback: str) -> str | None:
    value = (configured or "").strip()
    if value:
        candidate = Path(value).expanduser()
        if candidate.parent != Path("."):
            return (
                str(candidate.resolve())
                if candidate.is_file() and os.access(candidate, os.X_OK)
                else None
            )
        return shutil.which(value)
    return shutil.which(fallback)


def _agentplane_bin() -> str | None:
    return _resolved_executable(os.environ.get("AGENTPLANE_BIN"), "agentplane")


def _hermes_bin() -> str | None:
    return _resolved_executable(os.environ.get("HERMES_BIN"), "hermes")


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.is_file():
        return {"schema": "agentplane.hermes.lane-registry.v2", "lanes": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentPlaneLaneConfigError(
            f"Invalid AgentPlane lane registry {path}: {exc}"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("lanes"), list):
        raise AgentPlaneLaneConfigError(
            f"Invalid AgentPlane lane registry shape: {path}"
        )
    return value


def _agentplane_lanes() -> list[dict[str, Any]]:
    return [
        lane
        for lane in _load_registry().get("lanes", [])
        if isinstance(lane, dict) and lane.get("kind") == "agentplane"
    ]


def _value(source: dict[str, Any], *names: str) -> str:
    for name in names:
        value = source.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _metadata_value(source: dict[str, Any], *names: str) -> str:
    metadata = source.get("metadata")
    agentplane = metadata.get("agentplane") if isinstance(metadata, dict) else None
    if not isinstance(agentplane, dict):
        return ""
    return _value(agentplane, *names)


def _agentplane_task_id(source: dict[str, Any]) -> str:
    task_id = _value(
        source, "agentplane_task_id", "agentplaneTaskId"
    ) or _metadata_value(source, "task_id", "taskId", "id")
    if not task_id:
        raise AgentPlaneLaneConfigError(
            "AgentPlane lane requires metadata.agentplane.task_id or explicit agentplane_task_id"
        )
    return task_id


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("AGENTPLANE_HERMES_ALLOWED_ROOTS", "").strip()
    return [
        Path(entry.strip()).expanduser().resolve()
        for entry in raw.split(os.pathsep)
        if entry.strip()
    ]


def _assert_allowed_root(path_value: str) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise AgentPlaneLaneConfigError(
            "AGENTPLANE_HERMES_ALLOWED_ROOTS must contain at least one workspace root"
        )
    if not path_value:
        raise AgentPlaneLaneConfigError(
            "AgentPlane lane requires an explicit workspace root"
        )
    candidate = Path(path_value).expanduser().resolve()
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise AgentPlaneLaneConfigError(
            f"AgentPlane lane workspace is outside AGENTPLANE_HERMES_ALLOWED_ROOTS: {candidate}"
        )
    return candidate


def _base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _approval_private_key() -> Ed25519PrivateKey:
    encoded = os.environ.get(APPROVAL_PRIVATE_KEY_ENV, "").strip()
    if not encoded:
        raise AgentPlaneLaneConfigError(
            f"{APPROVAL_PRIVATE_KEY_ENV} is required in the trusted Hermes host process"
        )
    try:
        raw = b64decode(encoded, validate=True)
        key = serialization.load_der_private_key(raw, password=None)
    except (TypeError, ValueError) as exc:
        raise AgentPlaneLaneConfigError(
            f"{APPROVAL_PRIVATE_KEY_ENV} must be base64 PKCS8 DER"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AgentPlaneLaneConfigError("Hermes approval key must be Ed25519")
    return key


def _approval_bridge_snapshot() -> dict[str, Any]:
    try:
        key = _approval_private_key()
        public_der = key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            "ready": True,
            "issuer": str(_APPROVAL_CONFIG["issuer"]),
            "subject": str(_APPROVAL_CONFIG["subject"]),
            "ttl_minutes": int(_APPROVAL_CONFIG["ttl_minutes"]),
            "public_key_spki": b64encode(public_der).decode("ascii"),
            "error": None,
        }
    except (AgentPlaneLaneConfigError, KeyError, TypeError, ValueError) as exc:
        return {
            "ready": False,
            "issuer": str(_APPROVAL_CONFIG.get("issuer") or ""),
            "subject": str(_APPROVAL_CONFIG.get("subject") or ""),
            "ttl_minutes": _APPROVAL_CONFIG.get("ttl_minutes"),
            "public_key_spki": None,
            "error": str(exc),
        }


def _iso_timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _signed_approval_receipt(request: dict[str, Any]) -> str:
    required = {
        "approval_type",
        "task_id",
        "authority_reference",
        "state_fingerprint",
        "operation_id",
        "operation_digest",
        "state_scope_digest",
    }
    if set(request) != required:
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval request has a non-exact field set"
        )
    approval_type = request.get("approval_type")
    if approval_type not in {"plan_approval", "side_effect"}:
        raise AgentPlaneLaneConfigError(
            f"Hermes approval bridge cannot approve {approval_type!r}"
        )
    side_effect_fields = (
        request.get("operation_id"),
        request.get("operation_digest"),
        request.get("state_scope_digest"),
    )
    if approval_type == "side_effect" and not all(side_effect_fields):
        raise AgentPlaneLaneConfigError(
            "Side-effect approval request is missing state binding"
        )
    if approval_type == "plan_approval" and any(
        value is not None for value in side_effect_fields
    ):
        raise AgentPlaneLaneConfigError(
            "Plan approval request contains side-effect fields"
        )

    now = datetime.now(UTC)
    ttl = int(_APPROVAL_CONFIG.get("ttl_minutes") or 0)
    if ttl < 1 or ttl > 60:
        raise AgentPlaneLaneConfigError("approval ttl_minutes must be between 1 and 60")
    receipt = {
        "schema_version": 1,
        "kind": "agentplane.user_approval_receipt",
        "receipt_id": f"receipt-{uuid.uuid4()}",
        "issuer": str(_APPROVAL_CONFIG.get("issuer") or ""),
        "subject": str(_APPROVAL_CONFIG.get("subject") or ""),
        "decision": "approved",
        "approval_type": approval_type,
        "task_id": request.get("task_id"),
        "authority_reference": request.get("authority_reference"),
        "state_fingerprint": request.get("state_fingerprint"),
        "operation_id": request.get("operation_id"),
        "operation_digest": request.get("operation_digest"),
        "state_scope_digest": request.get("state_scope_digest"),
        "issued_at": _iso_timestamp(now),
        "expires_at": _iso_timestamp(now + timedelta(minutes=ttl)),
        "signature": "",
    }
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", receipt["issuer"]):
        raise AgentPlaneLaneConfigError("approval issuer is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}", receipt["subject"]):
        raise AgentPlaneLaneConfigError("approval subject is invalid")
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    receipt["signature"] = _base64url(
        _approval_private_key().sign(canonical.encode("utf-8"))
    )
    return _base64url(
        json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def _forwarded_env_names() -> set[str]:
    raw = os.environ.get("AGENTPLANE_HERMES_FORWARD_ENV", "")
    return {name for name in re.split(r"[,\s:]+", raw) if name}


def _minimal_env(extra_names: set[str] | None = None) -> dict[str, str]:
    names = (
        BASE_ENV
        | CLAIM_ENV
        | RUNNER_ENV
        | _forwarded_env_names()
        | (extra_names or set())
    )
    names.discard(APPROVAL_PRIVATE_KEY_ENV)
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["AGENTPLANE_HERMES_PLUGIN_PROTOCOL"] = PROTOCOL
    env["AGENTPLANE_HERMES_NATIVE_WORKER_LANE_API"] = "1"
    env["AGENTPLANE_HERMES_APPROVAL_RECEIPT_BRIDGE"] = (
        "1" if _approval_bridge_snapshot()["ready"] else "0"
    )
    return env


def _build_env(
    source: dict[str, Any], lane: dict[str, Any] | None = None
) -> dict[str, str]:
    lane_env = lane.get("env", []) if isinstance(lane, dict) else []
    explicit = {str(name) for name in lane_env if str(name).strip()}
    env = _minimal_env(explicit)
    mappings = {
        "HERMES_KANBAN_TASK": (
            "HERMES_KANBAN_TASK",
            "hermes_kanban_task",
            "card_id",
            "id",
        ),
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
    spawn = lane.get("spawn") if isinstance(lane.get("spawn"), dict) else {}
    command = str(spawn.get("command") or "hermes")
    if command == "hermes":
        command = _hermes_bin() or command
    task_id = _agentplane_task_id(source)
    repo = _value(
        source, "repo", "root", "workspace", "workspace_path", "HERMES_KANBAN_WORKSPACE"
    )
    _assert_allowed_root(repo)
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
    if not args:
        args = ["agentplane", "supervise", "--task-id", task_id, "--root", repo]
    return [command, *args]


def _spawn_agentplane(lane: dict[str, Any], source: dict[str, Any]) -> subprocess.Popen:
    command = _build_command(lane, source)
    workspace = _value(
        source, "workspace", "workspace_path", "repo", "root", "HERMES_KANBAN_WORKSPACE"
    )
    cwd = _assert_allowed_root(workspace)
    if not cwd.is_dir():
        raise AgentPlaneLaneConfigError(
            f"AgentPlane workspace is not a directory: {cwd}"
        )
    env = _build_env(source, lane)
    missing = [name for name in CLAIM_ENV if not env.get(name)]
    if missing:
        raise AgentPlaneLaneConfigError(
            f"Hermes native worker claim is incomplete: {', '.join(sorted(missing))}"
        )
    return subprocess.Popen(command, cwd=cwd, env=env)


def _spawn_fn_for(lane: dict[str, Any]):
    def spawn_fn(*args, **kwargs):
        source: dict[str, Any] = {}
        for index, item in enumerate(args):
            if isinstance(item, dict):
                source.update(item)
            elif isinstance(item, (str, os.PathLike)):
                if index == 1 and not _value(
                    source, "workspace", "workspace_path", "repo", "root"
                ):
                    source["workspace"] = os.fspath(item)
            else:
                for attr in (
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
                ):
                    if hasattr(item, attr):
                        source[attr] = getattr(item, attr)
        source.update(kwargs)
        proc = _spawn_agentplane(lane, source)
        return getattr(proc, "pid", None)

    return spawn_fn


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentPlaneLaneConfigError(
            f"Hermes did not return one JSON object: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise AgentPlaneLaneConfigError("Hermes result must be a JSON object")
    return value


def _validate_semantic_result(
    value: dict[str, Any], work_order_id: str
) -> dict[str, Any]:
    expected = {
        "schema_version": 2,
        "kind": "agent_semantic_result",
        "work_order_id": work_order_id,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise AgentPlaneLaneConfigError(
                f"Hermes semantic result {key} must equal {expected_value!r}"
            )
    if value.get("status") not in {"completed", "blocked", "needs_context", "failed"}:
        raise AgentPlaneLaneConfigError(
            "Hermes semantic result has an unsupported status"
        )
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise AgentPlaneLaneConfigError(
            "Hermes semantic result summary must be a non-empty string"
        )
    for field in ("findings", "uncertainty"):
        if not isinstance(value.get(field), list):
            raise AgentPlaneLaneConfigError(
                f"Hermes semantic result {field} must be an array"
            )
    return value


def _atomic_write_json(
    path_value: str, value: dict[str, Any], allowed_parent: Path
) -> None:
    target = Path(path_value).expanduser()
    parent = target.parent.resolve()
    allowed = allowed_parent.resolve()
    if parent != allowed and allowed not in parent.parents:
        raise AgentPlaneLaneConfigError(
            f"Result path escapes its AgentPlane exchange directory: {target}"
        )
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )


def _agent_command() -> list[str]:
    raw = os.environ.get("AGENTPLANE_HERMES_AGENT_COMMAND", "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentPlaneLaneConfigError(
                "AGENTPLANE_HERMES_AGENT_COMMAND must be a JSON argv array"
            ) from exc
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise AgentPlaneLaneConfigError(
                "AGENTPLANE_HERMES_AGENT_COMMAND must be a non-empty JSON argv array"
            )
        return value
    hermes = _hermes_bin()
    if not hermes:
        raise AgentPlaneLaneConfigError("Hermes executable is unavailable")
    return [hermes, "-z"]


def _work_order_prompt(work_order: dict[str, Any], bootstrap: str | None = None) -> str:
    return (
        "Execute this bounded AgentPlane WorkOrder as the Hermes agent. AgentPlane owns all formal "
        "task transitions; perform only the semantic objective and authority in the WorkOrder. "
        "Return exactly one JSON object conforming to AgentSemanticResult v2, with no prose or code fence.\n\n"
        f"WORK_ORDER:\n{json.dumps(work_order, ensure_ascii=False, indent=2)}\n\n"
        + (f"BOOTSTRAP:\n{bootstrap}\n" if bootstrap else "")
    )


class _HeartbeatGuard:
    def __init__(self, cwd: Path, env: dict[str, str]) -> None:
        self.cwd = cwd
        self.env = env
        self.failed: str | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def _heartbeat(self) -> bool:
        task = self.env.get("HERMES_KANBAN_TASK")
        run_id = self.env.get("HERMES_KANBAN_RUN_ID")
        claim = self.env.get("HERMES_KANBAN_CLAIM_LOCK")
        hermes = _hermes_bin()
        if not task or not run_id or not claim or not hermes:
            self.failed = "Hermes current-run guard is incomplete"
            return False
        argv = [hermes, "kanban"]
        board = self.env.get("HERMES_KANBAN_BOARD")
        if board:
            argv.extend(["--board", board])
        argv.extend(
            ["heartbeat", task, "--note", f"AgentPlane protocol v2 run {run_id}"]
        )
        completed = _run_process(argv, cwd=self.cwd, env=self.env)
        if completed.returncode != 0:
            self.failed = (
                completed.stderr.strip() or "Hermes rejected the current-run heartbeat"
            )
            return False
        return True

    def __enter__(self):
        if not self._heartbeat():
            raise AgentPlaneLaneConfigError(self.failed or "Hermes heartbeat failed")
        interval = max(
            5, int(os.environ.get("AGENTPLANE_HERMES_HEARTBEAT_SECONDS", "30"))
        )

        def loop() -> None:
            while not self.stop.wait(interval):
                if not self._heartbeat():
                    self.stop.set()

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def assert_current(self) -> None:
        if self.failed:
            raise AgentPlaneLaneConfigError(self.failed)

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.failed:
            raise AgentPlaneLaneConfigError(self.failed)


def _execute_work_order(
    work_order: dict[str, Any],
    *,
    cwd: Path,
    env: dict[str, str],
    bootstrap: str | None = None,
    guard_current_run: bool = True,
) -> dict[str, Any]:
    work_order_id = str(work_order.get("work_order_id") or "")
    if not work_order_id:
        raise AgentPlaneLaneConfigError("AgentPlane WorkOrder is missing work_order_id")
    prompt = _work_order_prompt(work_order, bootstrap)
    argv = [*_agent_command(), prompt]
    claim_guard = guard_current_run and all(env.get(name) for name in CLAIM_ENV)
    if claim_guard:
        with _HeartbeatGuard(cwd, env) as guard:
            completed = _run_process(argv, cwd=cwd, env=env)
            guard.assert_current()
    else:
        completed = _run_process(argv, cwd=cwd, env=env)
    if completed.returncode != 0:
        raise AgentPlaneLaneConfigError(
            f"Hermes WorkOrder failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return _validate_semantic_result(_json_object(completed.stdout), work_order_id)


def _run_runner_work_order() -> dict[str, Any]:
    raw_bundle_path = os.environ.get("AGENTPLANE_RUNNER_BUNDLE_PATH", "").strip()
    result_path = os.environ.get("AGENTPLANE_RUNNER_RESULT_PATH", "")
    raw_run_dir = os.environ.get("AGENTPLANE_RUNNER_RUN_DIR", "").strip()
    bundle_path = Path(raw_bundle_path)
    run_dir = Path(raw_run_dir)
    if (
        not raw_bundle_path
        or not bundle_path.is_file()
        or not result_path
        or not raw_run_dir
    ):
        raise AgentPlaneLaneConfigError(
            "AgentPlane runner bundle, run directory, and result path are required"
        )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    work_order = bundle.get("work_order") if isinstance(bundle, dict) else None
    repository = bundle.get("repository") if isinstance(bundle, dict) else None
    if not isinstance(work_order, dict) or not isinstance(repository, dict):
        raise AgentPlaneLaneConfigError(
            "AgentPlane runner bundle is missing repository or WorkOrder"
        )
    cwd = _assert_allowed_root(str(repository.get("git_root") or ""))
    bootstrap_path = Path(os.environ.get("AGENTPLANE_RUNNER_BOOTSTRAP_PATH", ""))
    bootstrap = (
        bootstrap_path.read_text(encoding="utf-8") if bootstrap_path.is_file() else None
    )
    result = _execute_work_order(
        work_order, cwd=cwd, env=_minimal_env(), bootstrap=bootstrap
    )
    _atomic_write_json(result_path, result, run_dir)
    return result


def _advance_command() -> list[str]:
    ap = _resolved_executable(os.environ.get("AP_BIN"), "ap")
    if ap:
        return [ap]
    agentplane = _agentplane_bin()
    if not agentplane:
        raise AgentPlaneLaneConfigError(
            "Neither ap nor agentplane executable is available"
        )
    prefix: list[str] = []
    raw_prefix = os.environ.get("AGENTPLANE_BIN_ARGS", "").strip()
    if raw_prefix:
        value = json.loads(raw_prefix)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise AgentPlaneLaneConfigError(
                "AGENTPLANE_BIN_ARGS must be a JSON argv array"
            )
        prefix = value
    return [agentplane, *prefix]


def _invoke_json(argv: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    completed = _run_process(argv, cwd=cwd, env=env)
    if completed.returncode != 0:
        raise AgentPlaneLaneConfigError(
            f"Command failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return _json_object(completed.stdout)


def _approval_root(root: str | None) -> Path:
    if root and root.strip():
        return _assert_allowed_root(root)
    roots = _allowed_roots()
    if len(roots) != 1:
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval requires --root unless exactly one allowed root is configured"
        )
    return roots[0]


def _approve(task_id: str, root: str | None = None) -> dict[str, Any]:
    cwd = _approval_root(root)
    env = _minimal_env()
    packet = _invoke_json(
        [*_advance_command(), "task", "advance", task_id, "--agent-json"],
        cwd=cwd,
        env=env,
    )
    if packet.get("action", {}).get("kind") != "approval_required":
        raise AgentPlaneLaneConfigError(
            "Current AgentPlane packet does not require user approval"
        )
    operator_action = packet.get("operator_action")
    if (
        not isinstance(operator_action, dict)
        or operator_action.get("required_role") != "USER"
    ):
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval packet has no USER operator action"
        )
    argv = operator_action.get("argv")
    if not isinstance(argv, list) or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval cannot be executed by the receipt bridge"
        )
    if sum(item == APPROVAL_RECEIPT_PLACEHOLDER for item in argv) != 1:
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval argv has an invalid receipt placeholder"
        )
    approval = operator_action.get("approval_receipt")
    request = approval.get("request") if isinstance(approval, dict) else None
    if (
        not isinstance(approval, dict)
        or approval.get("schema_version") != 1
        or approval.get("format") != "base64url-json+ed25519"
        or not isinstance(request, dict)
    ):
        raise AgentPlaneLaneConfigError(
            "AgentPlane approval receipt contract is invalid"
        )
    receipt = _signed_approval_receipt(request)
    exact_argv = [
        receipt if item == APPROVAL_RECEIPT_PLACEHOLDER else item for item in argv
    ]
    action_cwd = operator_action.get("cwd")
    execution_cwd = _assert_allowed_root(str(action_cwd or cwd))
    completed = _run_process(exact_argv, cwd=execution_cwd, env=env)
    if completed.returncode != 0:
        raise AgentPlaneLaneConfigError(
            "AgentPlane rejected the signed approval receipt: "
            + (completed.stderr.strip() or f"exit {completed.returncode}")
        )
    fresh = _invoke_json(
        [*_advance_command(), "task", "advance", task_id, "--agent-json"],
        cwd=cwd,
        env=env,
    )
    return {
        "schema": "agentplane.hermes.approval-result.v1",
        "task_id": task_id,
        "approved_kind": operator_action.get("kind"),
        "actor": f"USER:{_APPROVAL_CONFIG['subject']}@{_APPROVAL_CONFIG['issuer']}",
        "next_action": fresh.get("action", {}).get("kind"),
        "next_stop_reason": fresh.get("stop", {}).get("reason"),
    }


def _slash_approve(raw_args: str) -> str:
    args = shlex.split(raw_args)
    if len(args) not in {1, 2}:
        raise AgentPlaneLaneConfigError(
            "Usage: /agentplane_approve <agentplane-task-id> [workspace-root]"
        )
    result = _approve(args[0], args[1] if len(args) == 2 else None)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _supervise(task_id: str, root: str, max_episodes: int = 32) -> dict[str, Any]:
    cwd = _assert_allowed_root(root)
    env = _minimal_env()
    if not all(env.get(name) for name in CLAIM_ENV):
        missing = sorted(name for name in CLAIM_ENV if not env.get(name))
        raise AgentPlaneLaneConfigError(
            f"Hermes native worker claim is incomplete: {', '.join(missing)}"
        )
    packet = _invoke_json(
        [*_advance_command(), "task", "advance", task_id, "--agent-json"],
        cwd=cwd,
        env=env,
    )
    episodes = 0
    while packet.get("action", {}).get("kind") == "agent_episode":
        if episodes >= max_episodes:
            raise AgentPlaneLaneConfigError(
                f"AgentPlane supervisor exceeded {max_episodes} semantic episodes"
            )
        exchange = packet.get("exchange")
        if not isinstance(exchange, dict):
            raise AgentPlaneLaneConfigError(
                "AgentPlane agent_episode packet is missing exchange"
            )
        directory = Path(str(exchange.get("directory") or ""))
        work_order_path = directory / str(exchange.get("work_order_ref") or "")
        result_path = str(exchange.get("result_path") or "")
        resume_argv = exchange.get("resume_argv")
        if (
            not work_order_path.is_file()
            or not result_path
            or not isinstance(resume_argv, list)
        ):
            raise AgentPlaneLaneConfigError("AgentPlane exchange is incomplete")
        if not all(isinstance(item, str) and item for item in resume_argv):
            raise AgentPlaneLaneConfigError(
                "AgentPlane resume_argv must be an exact argv array"
            )
        work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
        if not isinstance(work_order, dict):
            raise AgentPlaneLaneConfigError("AgentPlane WorkOrder must be an object")
        with _HeartbeatGuard(cwd, env) as guard:
            semantic = _execute_work_order(
                work_order, cwd=cwd, env=env, guard_current_run=False
            )
            guard.assert_current()
            envelope = {
                "schema_version": 1,
                "kind": "agent_action_result",
                "task_id": packet.get("task_id"),
                "transition_id": packet.get("transition_id"),
                "state_fingerprint": packet.get("state_fingerprint"),
                "role": work_order.get("role"),
                "result": semantic,
            }
            _atomic_write_json(result_path, envelope, directory)
            guard.assert_current()
        packet = _invoke_json(resume_argv, cwd=cwd, env=env)
        episodes += 1
    action = packet.get("action", {}).get("kind")
    if action not in TERMINAL_ACTIONS:
        raise AgentPlaneLaneConfigError(
            f"Unsupported AgentPlane supervisor action: {action!r}"
        )
    return packet


def _doctor_payload() -> dict[str, Any]:
    registry_path = _registry_path()
    try:
        lanes = _agentplane_lanes()
        registry_error = None
    except AgentPlaneLaneConfigError as exc:
        lanes = []
        registry_error = str(exc)
    approval_bridge = _approval_bridge_snapshot()
    checks = {
        "registry_exists": registry_path.is_file(),
        "registry_valid": registry_error is None,
        "agentplane_lane_registered": bool(lanes),
        "agentplane_bin_available": _agentplane_bin() is not None,
        "hermes_bin_available": _hermes_bin() is not None,
        "native_worker_lane_api": _NATIVE_WORKER_LANE_API,
        "approval_receipt_bridge": approval_bridge["ready"],
        "allowed_roots_fail_closed": bool(_allowed_roots()),
    }
    return {
        "schema": CAPABILITY_SCHEMA,
        "version": VERSION,
        "protocol": PROTOCOL,
        "commands": [
            "agentplane approve",
            "agentplane doctor",
            "agentplane run",
            "agentplane supervise",
        ],
        "native_worker_lane_api": _NATIVE_WORKER_LANE_API,
        "approval_receipt_bridge": approval_bridge["ready"],
        "approval_bridge": approval_bridge,
        "ok": all(checks.values()),
        "checks": checks,
        "registry": str(registry_path),
        "registry_error": registry_error,
        "agentplane_bin": _agentplane_bin(),
        "hermes_bin": _hermes_bin(),
        "allowed_roots": [str(root) for root in _allowed_roots()],
        "lanes": lanes,
    }


def _setup_cli(parser) -> None:
    subcommands = parser.add_subparsers(dest="agentplane_command", required=True)
    doctor = subcommands.add_parser(
        "doctor", help="Validate the AgentPlane bridge contract"
    )
    doctor.add_argument("--json", action="store_true", help="Emit JSON output")
    subcommands.add_parser(
        "run", help="Execute the current AgentPlane runner WorkOrder"
    )
    approve = subcommands.add_parser(
        "approve",
        help="Approve the current AgentPlane packet from a trusted user action",
    )
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--root")
    supervise = subcommands.add_parser(
        "supervise", help="Drive compact AgentPlane supervisor packets"
    )
    supervise.add_argument("--task-id", required=True)
    supervise.add_argument("--root", required=True)
    supervise.add_argument("--max-episodes", type=int, default=32)
    supervise.add_argument("--json", action="store_true", help="Emit JSON output")


def _cli_handler(args=None, **kwargs):
    del kwargs
    command = getattr(args, "agentplane_command", None)
    if command == "doctor":
        payload = _doctor_payload()
    elif command == "run":
        payload = _run_runner_work_order()
    elif command == "approve":
        payload = _approve(args.task_id, args.root)
    elif command == "supervise":
        payload = _supervise(args.task_id, args.root, args.max_episodes)
    else:
        raise SystemExit(f"Unsupported agentplane command: {command}")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def register(ctx) -> None:
    global _NATIVE_WORKER_LANE_API, _APPROVAL_CONFIG
    get_config = getattr(ctx, "get_config", None)
    if callable(get_config):
        _APPROVAL_CONFIG = {
            "issuer": str(get_config("approval_issuer", "hermes-dialog")),
            "subject": str(get_config("approval_subject", "owner")),
            "ttl_minutes": int(get_config("approval_ttl_minutes", 10)),
        }
    try:
        lanes = _agentplane_lanes()
    except AgentPlaneLaneConfigError:
        lanes = []
    native_register: Callable[..., Any] | None = getattr(
        ctx, "register_worker_lane", None
    )
    _NATIVE_WORKER_LANE_API = callable(native_register)
    if native_register:
        for lane in lanes:
            match = lane.get("match") or lane.get("name")
            if match:
                native_register(match=match, spawn_fn=_spawn_fn_for(lane))
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if callable(register_cli_command):
        register_cli_command(
            name="agentplane",
            help="AgentPlane native worker-lane bridge",
            description="Execute AgentPlane WorkOrders with Hermes while AgentPlane owns task control.",
            setup_fn=_setup_cli,
            handler_fn=_cli_handler,
        )
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(
            "agentplane_doctor",
            lambda *args, **kwargs: json.dumps(_doctor_payload(), ensure_ascii=False),
            "Show AgentPlane worker-lane integration status as JSON.",
        )
        register_command(
            "agentplane_approve",
            _slash_approve,
            "Approve the current AgentPlane plan or protected effect as the configured owner.",
            args_hint="<agentplane-task-id> [workspace-root]",
        )
