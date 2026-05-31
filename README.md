# AgentPlane Hermes Plugin

[![CI](https://github.com/basilisk-labs/agentplane-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/basilisk-labs/agentplane-hermes-plugin/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AgentPlane](https://img.shields.io/badge/AgentPlane-0.6%2B-111827)](https://github.com/basilisk-labs/agentplane)

Hermes plugin for spawning AgentPlane as an external worker lane.

This package makes Hermes Kanban assignees matching `agentplane-*` resolve to an
external supervisor command instead of a Hermes profile.

## Status

Compatibility plugin for current Hermes images, with a native registration path
for Hermes runtimes that expose:

```python
register_worker_lane(match, spawn_fn)
```

Without that hook, the plugin still provides doctor/registry checks and does not
mutate `kanban.db` directly.

## Runtime contract

Hermes image/runtime must provide:

- `PATH` containing `/opt/hermes/bin`
- Node.js 24+
- `agentplane` on `PATH` or `AGENTPLANE_BIN=/path/to/agentplane`
- `AGENTPLANE_HERMES_LANE_REGISTRY=/opt/agentplane/lane-registry.json`
- Hermes plugin loader pointed at this plugin

Example lane registry:

```json
{
  "schema": "agentplane.hermes.lane-registry.v1",
  "lanes": [
    {
      "name": "agentplane-coder",
      "match": "agentplane-*",
      "kind": "agentplane",
      "spawn": {
        "command": "agentplane",
        "args": ["hermes", "supervise", "{agentplane_task_id}", "--root", "{repo}", "--json"]
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

## Install

Copy this repository into a Hermes plugin path, for example:

```bash
mkdir -p ~/.hermes/plugins/agentplane
cp -R __init__.py agentplane_hermes_plugin plugin.yaml ~/.hermes/plugins/agentplane/
```

Enable it in Hermes config:

```yaml
plugins:
  enabled:
    - agentplane
```

Set runtime variables:

```bash
export AGENTPLANE_HERMES_LANE_REGISTRY=/opt/agentplane/lane-registry.json
export AGENTPLANE_BIN=/usr/local/bin/agentplane
```

## Doctor

When Hermes exposes plugin CLI registration, run:

```bash
hermes agentplane doctor --json
```

Expected signal:

- registry exists
- AgentPlane binary resolves
- at least one `kind: agentplane` lane exists
- native worker-lane API is reported when Hermes exposes it

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/check_integrity.py
python -m pytest
```

## Boundaries

The plugin must not write directly to `~/.hermes/kanban.db`. AgentPlane should
complete, block, heartbeat, and reclaim work only through Hermes lifecycle APIs
or CLI surfaces.
