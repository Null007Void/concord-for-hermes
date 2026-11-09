"""Shared fixtures for the Concord C1 integration proof.

HERMES_HOME is pinned to the workspace's isolated home *before* any hermes
module is imported, so every path the runtime resolves during this process is
inside the assignment's tree. No test writes to the real ~/.hermes.
"""
from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]          # .../Concord-for-Hermes
HOME = ROOT / "isolated-home"
PLUGIN_DIR = HOME / "plugins" / "concord_probe"
PRODUCT_DIR = HOME / "plugins" / "concord-for-hermes"

# Must precede the first `import hermes_cli.*` anywhere in the session.
os.environ["HERMES_HOME"] = str(HOME)
os.environ.setdefault("HERMES_RUNTIME_DIR", str(HOME / "runtime"))
# Keep the runtime from reaching for a real provider/key on any lazy path.
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("HERMES_INFERENCE_PROVIDER", "")

for path in (PLUGIN_DIR, PRODUCT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(scope="session", autouse=True)
def loaded_plugin_manager():
    """Load the probe plugin once through the real PluginManager for the session."""
    import hermes_cli.plugins as P

    manager = P.PluginManager()
    manager.discover_and_load()
    assert "concord_probe" in manager._plugins, (
        f"probe plugin did not load; loaded={sorted(manager._plugins)}"
    )
    assert "concord-for-hermes" in manager._plugins, (
        f"product plugin did not load; loaded={sorted(manager._plugins)}"
    )
    # The manager get_plugin_manager()/_delivery_manager() resolve is adopted from
    # this slot, so hook + middleware delivery in this process uses it.
    P._plugin_manager = manager
    yield manager


@pytest.fixture(scope="session", autouse=True)
def kernel_matches_the_installed_copy():
    """The C2 tests import ``concord_kernel`` from the isolated home, which is a
    copy of ``concord-for-hermes/plugin``. A stale copy would produce a false
    green, so fail loudly with the fix instead.

    C3 added the adapter and C4 adds the product modules (result shapes, store,
    tool, CLI, status view). Every one is guarded the same way: a stale copy
    would be exactly as misleading as a stale kernel.
    """
    import concord_backup
    import concord_cli
    import concord_gh_adapter
    import concord_kernel
    import concord_result
    import concord_status
    import concord_store
    import concord_tool

    for module in (
        concord_kernel, concord_gh_adapter, concord_result, concord_store,
        concord_tool, concord_status, concord_cli, concord_backup,
    ):
        source = pathlib.Path(__file__).resolve().parents[1] / "plugin" / f"{module.__name__}.py"
        installed = pathlib.Path(module.__file__)
        assert installed.read_bytes() == source.read_bytes(), (
            f"installed {module.__name__} at {installed} does not match {source}; "
            "run scripts/install_plugin.py before the suite"
        )
    yield


@pytest.fixture(autouse=True)
def _clear_hook_timeout_state(loaded_plugin_manager):
    """Reset the manager's per-callback timeout suppression between tests.

    ``plugins_dispatch`` remembers a timed-out callback process-wide
    (``_hook_timeout_suppressed_until``) and skips it - fail-closed - for the
    suppression window. That is correct product behaviour and must not leak from the Q3 timeout test into
    every later test in this session.
    """
    manager = loaded_plugin_manager
    for attr in ("_hook_timeout_suppressed_until", "_hook_abandoned", "_hook_running_callbacks"):
        state = getattr(manager, attr, None)
        if state is not None:
            state.clear()
    yield


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """Per-test coordinator/provider/trace paths, with fault knobs defaulted off."""
    db = tmp_path / "coordinator.db"
    store = tmp_path / "provider.db"
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("CONCORD_PROBE_DB", str(db))
    monkeypatch.setenv("CONCORD_PROBE_STORE", str(store))
    monkeypatch.setenv("CONCORD_PROBE_TRACE", str(trace))
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "off")
    monkeypatch.setenv("CONCORD_PROBE_MW", "off")
    monkeypatch.delenv("CONCORD_PROBE_HANG_SECONDS", raising=False)
    monkeypatch.delenv("CONCORD_PROBE_HANG_MARKER", raising=False)

    import concord_core

    provider = concord_core.LocalProvider(str(store))
    coordinator = concord_core.Coordinator(str(db))
    return SimpleNamespace(
        db=db,
        store=store,
        trace=trace,
        provider=provider,
        coordinator=coordinator,
        core=concord_core,
    )


@pytest.fixture
def agent(loaded_plugin_manager):
    import harness

    return harness.make_agent()


@pytest.fixture
def run(agent, loaded_plugin_manager):
    """Callable: run(tool_name, args) -> managed executor result."""
    import harness

    scope = loaded_plugin_manager.scope_key

    def _run(tool_name, args):
        return harness.run_tool(agent, tool_name, args, scope=scope)

    return _run
