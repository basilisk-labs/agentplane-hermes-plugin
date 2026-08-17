# AgentPlane Hermes Plugin

[![CI](https://github.com/basilisk-labs/agentplane-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/basilisk-labs/agentplane-hermes-plugin/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AgentPlane](https://img.shields.io/badge/AgentPlane-0.7.6%2B-111827)](https://github.com/basilisk-labs/agentplane)

Hermes native worker-lane bridge for AgentPlane protocol v2.

The division of responsibility is deliberate:

- Hermes performs every LLM episode and owns the Kanban task, claim, run, heartbeat, retry,
  comments, and dashboard state.
- AgentPlane owns formal engineering task state, PLANNER/EXECUTOR/EVALUATOR roles, authority,
  repository routing, verification, publication, integration, and the terminal decision.
- The plugin is a transport adapter. It never edits AgentPlane task files, reconstructs lifecycle
  transitions from prose, or writes directly to the Hermes database.

## Commands

The plugin registers one native Hermes command group:

```bash
hermes agentplane doctor --json
hermes agentplane run
hermes agentplane supervise --task-id <agentplane-task-id> --root <repo>
hermes agentplane approve --task-id <agentplane-task-id> [--root <repo>]
```

`run` is the AgentPlane custom-runner entrypoint. It reads the runner bundle and bootstrap from the
`AGENTPLANE_RUNNER_*` contract, executes the bounded WorkOrder through Hermes oneshot mode,
validates `AgentSemanticResult v2`, and atomically writes the exact result path.

`supervise` is the normal native worker-lane entrypoint. It requests a fresh
`ap task advance <id> --agent-json` packet, executes only `agent_episode`, writes the typed result
to the exact `exchange.result_path`, and resumes with the exact `exchange.resume_argv`. It stops at
human-input, external-wait, recovery, framework, or terminal boundaries. AgentPlane resolves
repository-policy side effects internally. Formal transitions remain invisible to the Hermes user
but are never delegated to the model.

`approve` is the trusted conversational bridge for a current `approval_required` packet. The same
operation is registered as `/agentplane_approve <task-id> [workspace-root]`, so a user can approve
inside a Hermes conversation without copying a state fingerprint or running a terminal command.
It signs only the packet's exact `operator_action.approval_receipt.request`, substitutes only the
receipt placeholder in the supplied argv, executes it, and requests a fresh packet. Provider merge
packets deliberately have no executable argv and are rejected by this bridge.

## Runtime contract

Hermes must expose `register_worker_lane(match, spawn_fn)` and plugin CLI discovery. The runtime
also needs AgentPlane 0.7.6 or newer and the following configuration:

```bash
export AGENTPLANE_HERMES_LANE_REGISTRY=/opt/agentplane/lane-registry.json
export AGENTPLANE_HERMES_ALLOWED_ROOTS=/workspace/repo-a:/workspace/repo-b
export AGENTPLANE_BIN=/usr/local/bin/agentplane
```

The root allowlist is mandatory. Empty means deny all workspaces.

Configure the non-secret approval identity in Hermes `config.yaml`:

```yaml
plugins:
  entries:
    agentplane:
      settings:
        approval_issuer: hermes-dialog
        approval_subject: owner
        approval_ttl_minutes: 10
```

The trusted Hermes host process also needs an Ed25519 PKCS8 DER key encoded as base64 in the secret
environment variable `AGENTPLANE_HERMES_APPROVAL_PRIVATE_KEY_PKCS8`. This secret is intentionally
absent from the worker environment, so an LLM episode or terminal subprocess cannot invoke the
signer. `hermes agentplane doctor --json` reports the matching `approval_bridge.public_key_spki`;
configure that public key under AgentPlane
`authority.approval_receipts.trusted_issuers[].public_key_spki` with the same issuer id.

The plugin does not inherit the complete parent environment. It forwards a small runtime allowlist,
the AgentPlane runner fields, and the current Hermes claim. Provider credentials that must be
available to a subprocess require an explicit name allowlist:

```bash
export AGENTPLANE_HERMES_FORWARD_ENV=OPENROUTER_API_KEY,ANTHROPIC_API_KEY
```

The values are not logged or included in doctor output.

Every plugin-owned AgentPlane process asserts:

```text
AGENTPLANE_HERMES_PLUGIN_PROTOCOL=agentplane.hermes.plugin.v2
AGENTPLANE_HERMES_NATIVE_WORKER_LANE_API=1
AGENTPLANE_HERMES_APPROVAL_RECEIPT_BRIDGE=1
```

The third assertion is emitted only when the trusted host process loaded a valid Ed25519 key. The
private key variable itself is never forwarded.

## Approval and autonomous side effects

The primary AgentPlane plan always requires an explicit user decision. The user invokes
`/agentplane_approve` from Hermes, and the trusted plugin records the signed, short-lived,
state-bound receipt. The LLM cannot infer approval from prose or manufacture this receipt.

After plan approval, AgentPlane `authority.mode=policy|all` can pass routine side effects without
another Hermes prompt. `policy` allows only `allow_operations`; `all` allows every side effect
except `deny_operations`; the denylist wins. Drift, unconfigured or denied effects, provider merge,
destructive or credential boundaries, and unsafe authority recovery still return to the user.

## Lane registry

Cards must carry an explicit AgentPlane task id in `agentplane_task_id`, `agentplaneTaskId`, or
`metadata.agentplane.task_id`. A Hermes card id is never treated as an AgentPlane task id.

```json
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
          "{repo}"
        ]
      },
      "env": [
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_CLAIM_LOCK"
      ]
    }
  ]
}
```

The spawn callback refuses an incomplete native claim. During an LLM episode, the plugin uses the
Hermes `kanban heartbeat` lifecycle command with the current run id before and after the episode and
periodically while it runs. A rejected heartbeat aborts result submission, preventing a stale run
from writing into a newer AgentPlane exchange.

## Install

Copy or install this package into a Hermes plugin path and enable `agentplane` in Hermes config:

```bash
mkdir -p ~/.hermes/plugins/agentplane
cp -R __init__.py agentplane_hermes_plugin plugin.yaml ~/.hermes/plugins/agentplane/
```

```yaml
plugins:
  enabled:
    - agentplane
```

Then run `hermes agentplane doctor --json`. `ok=true` requires:

- valid lane registry with at least one `kind: agentplane` lane;
- resolvable Hermes and AgentPlane executables;
- native worker-lane registration;
- a valid trusted approval-receipt bridge key;
- a non-empty allowed-root set.

AgentPlane's own `agentplane hermes doctor --json` additionally checks the repository workflow and
the protocol assertion passed by this plugin.

## Completion

The Hermes root card may close only from an AgentPlane
`agentplane.hermes.terminal-attestation.v1` whose canonical route outcome is `done`. A local
`status=DONE` or `verification=ok` check is insufficient because provider, integration, ACR, or
cleanup work may still remain.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/check_integrity.py
python -m pytest
```
