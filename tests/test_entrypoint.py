from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_loads_as_hermes_directory_plugin() -> None:
    """Exercise the same package layout used by Hermes' directory loader."""
    parent_name = "hermes_plugins"
    module_name = f"{parent_name}.agentplane_test"
    previous_parent = sys.modules.get(parent_name)
    parent = previous_parent or types.ModuleType(parent_name)
    if previous_parent is None:
        parent.__path__ = []
        parent.__package__ = parent_name
        sys.modules[parent_name] = parent

    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(ROOT)]
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
        assert module.VERSION == "0.2.2"
        assert callable(module.register)
    finally:
        prefix = f"{module_name}."
        for name in [
            candidate
            for candidate in sys.modules
            if candidate == module_name or candidate.startswith(prefix)
        ]:
            del sys.modules[name]
        if previous_parent is None:
            del sys.modules[parent_name]
