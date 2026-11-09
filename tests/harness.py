"""Drive the real Hermes dispatch path without a model.

What is REAL in these tests (all imported from the pinned checkout):
  * ``hermes_cli.plugins.PluginManager`` discovery + load of the plugin
  * ``hermes_cli.plugins_dispatch`` hook invocation, per-callback isolation,
    fail-closed block directives and the bounded timeout worker
  * ``hermes_cli.middleware.run_tool_execution_middleware`` chain
  * ``agent.tool_executor._run_agent_tool_execution_middleware`` ->
    ``_dispatch_authorized_once`` -> ``_pre_tool_block`` -> guardrail gate
  * ``tools.registry`` handler invocation

What is STUBBED (and only this): the ``AIAgent`` object itself. Everything it
needs for a tool call is supplied by ``make_agent`` below, mirroring the stub
the upstream suite uses (tests/agent/test_tool_activity_heartbeat.py:27).
The guardrail manager is a permissive double so the *coordination* question is
isolated from unrelated policy; it is not the seam under test.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

TOOL_NAME = "concord_probe_write"
GUARDED_TOOL = "concord_probe_mutate"


def make_agent(session_id: str = "probe-session") -> object:
    """Minimal AIAgent-shaped stub — enough for one tool call through the executor."""
    class _Stub:
        _interrupt_requested = False
        _interrupt_message = None
        log_prefix = ""
        quiet_mode = True
        verbose_logging = False
        log_prefix_chars = 200
        _checkpoint_mgr = MagicMock(enabled=False)
        tool_progress_callback = None
        tool_start_callback = None
        tool_complete_callback = None
        tool_progress_mode = "off"
        _todo_store = MagicMock()
        _session_db = None
        valid_tool_names = set()
        _turns_since_memory = 0
        _iters_since_skill = 0
        _current_tool = None
        _last_activity = 0.0
        _current_turn_id = ""
        _current_api_request_id = ""

        def __init__(self) -> None:
            self.session_id = session_id
            self._tool_worker_threads: set = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children_lock = threading.Lock()
            # Permissive guardrail double: allows execution, records nothing.
            self._tool_guardrails = MagicMock(
                before_call=lambda name, args: MagicMock(allows_execution=True)
            )

        def _touch_activity(self, desc: str) -> None:
            self._last_activity = time.time()

        def _vprint(self, msg, force=False) -> None:
            pass

        def _safe_print(self, msg) -> None:
            pass

        def _should_emit_quiet_tool_messages(self) -> bool:
            return False

        def _should_start_quiet_spinner(self) -> bool:
            return False

        def _has_stream_consumers(self) -> bool:
            return False

        def _tool_result_content_for_active_model(self, name, result):
            return result

        def _record_file_mutation_result(self, *a, **kw) -> None:
            pass

        def _apply_pending_steer_to_tool_results(self, *a, **kw) -> None:
            pass

    stub = _Stub()
    stub._subdirectory_hints = MagicMock()
    stub._subdirectory_hints.check_tool_call = lambda *a, **kw: None
    stub._flush_messages_to_session_db = lambda *a, **kw: None
    stub._append_guardrail_observation = lambda name, result, *a, **kw: result
    stub._guardrail_block_result = lambda d: json.dumps({"error": "blocked"})
    return stub


def load_manager(scope_hint: str = ""):
    """Load the probe plugin through the real PluginManager."""
    from hermes_cli import plugins

    manager = plugins.PluginManager()
    manager.discover_and_load()
    return manager


def dispatch_via_registry(tool_name: str, args: dict, scope: str | None = None) -> object:
    """Invoke the tool implementation the way the registry does."""
    from tools.registry import registry

    entry = registry.get_entry(tool_name, scope=scope)
    if entry is None:
        raise RuntimeError(f"tool {tool_name!r} is not registered (scope={scope!r})")
    return entry.handler(args)


def run_tool(agent, tool_name: str, args: dict, *, scope: str | None = None, call_id: str = "call-1"):
    """One full real-dispatch tool call. Returns the executor's managed result."""
    import agent.tool_executor as te

    return te._run_agent_tool_execution_middleware(
        agent,
        function_name=tool_name,
        function_args=args,
        effective_task_id="task-1",
        tool_call_id=call_id,
        execute=lambda final_args: dispatch_via_registry(tool_name, final_args, scope),
        display_index=1,
    )


def trace_records(path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def store_revision(path, key: str) -> int:
    """Read the synthetic provider's revision directly (test-side observation)."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT revision FROM objects WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def store_value(path, key: str):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM objects WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None
