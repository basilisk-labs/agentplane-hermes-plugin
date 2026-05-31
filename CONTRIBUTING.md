# Contributing

Use small, reviewable changes. Keep the plugin free of direct writes to Hermes
storage internals.

Before opening a pull request:

```bash
python scripts/check_integrity.py
python -m pytest
```

For behavior changes, update `README.md` and
`registry/lane-registry.example.json` when the public contract changes.

