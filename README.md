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
```

`run` is the AgentPlane custom-runner entrypoint. It reads the runner bundle and bootstrap from the
`AGENTPLANE_RUNNER_*` contract, executes the bounded WorkOrder through Hermes oneshot mode,
validates `AgentSemanticResult v2`, and atomically writes the exact result path.

`supervise` is the normal native worker-lane entrypoint. It requests a fresh
`ap task advance <id> --agent-json` packet, executes only `agent_episode`, writes the typed result
to the exact `exchange.result_path`, and resumes with the exact `exchange.resume_argv`. It stops at
approval, human-input, external-wait, recovery, framework, or terminal boundaries. Formal
AgentPlane transitions remain invisible to the Hermes user but are never delegated to the model.

## Runtime contract

Hermes must expose `register_worker_lane(match, spawn_fn)` and plugin CLI discovery. The runtime
also needs AgentPlane 0.7.6 or newer and the following configuration:

```bash
export AGENTPLANE_HERMES_LANE_REGISTRY=/opt/agentplane/lane-registry.json
export AGENTPLANE_HERMES_ALLOWED_ROOTS=/workspace/repo-a:/workspace/repo-b
export AGENTPLANE_BIN=/usr/local/bin/agentplane
```

The root allowlist is mandatory. Empty means deny all workspaces.

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
```

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
