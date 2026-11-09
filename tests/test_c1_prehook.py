"""C1 Q1 + Q3 — the pre_tool_call seam.

Q1: a pre-hook callback that RAISES — is the downstream tool executed anyway?
Q3: a pre-hook that TIMES OUT — does execution continue, and does the
    abandoned worker keep running?
"""
from __future__ import annotations

import json
import time

import harness


def _blocked(result) -> bool:
    try:
        payload = json.loads(result.result)
    except (TypeError, json.JSONDecodeError):
        return False
    return "error" in payload and result.blocked


def test_baseline_tool_executes_when_guard_is_off(probe, run):
    """Control: the real path reaches the tool implementation."""
    probe.provider.seed("doc-1", {"body": "before"})

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert result.dispatched is True
    assert result.blocked is False
    assert json.loads(result.result)["status"] == "written"
    assert harness.store_revision(str(probe.store), "doc-1") == 2
    assert harness.store_value(str(probe.store), "doc-1") == "after"


def test_hook_block_directive_prevents_execution(probe, run, monkeypatch):
    """A well-behaved guard: block directive, no downstream dispatch, no write."""
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "block")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert result.dispatched is True          # the executor reached its dispatch gate
    assert result.blocked is True             # ... and it was vetoed there
    assert "concord: object held" in result.result
    assert harness.store_revision(str(probe.store), "doc-1") == 1


def test_q1_raising_hook_does_not_execute_tool(probe, run, monkeypatch):
    """Q1: the callback raises -> per-callback isolation turns it into a veto.

    Fail-closed at this layer. Recorded because it is the *only* layer that is.
    """
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "raise")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert result.blocked is True
    assert "plugin callback" in result.result and "raised RuntimeError" in result.result
    assert harness.store_revision(str(probe.store), "doc-1") == 1, "tool executed despite raising hook"


def test_q1_outer_hook_dispatch_failure_executes_tool(probe, run, monkeypatch):
    """Q1, outer layer: the hook *machinery* fails, not the callback.

    agent/tool_executor.py::_pre_tool_block wraps the whole dispatch in
    `except Exception: return None, ref.args` ("Hook failures never block").
    Fault-injecting the dispatch function itself — not a callback — is the
    reachable form of that path.
    """
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "block")   # guard *wants* to veto

    import hermes_cli.plugins as P

    def _explode(*args, **kwargs):
        raise RuntimeError("hook subsystem unavailable")

    monkeypatch.setattr(P, "_dispatch_pre_tool_call_hooks", _explode)

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    # OBSERVED FAIL-OPEN: the guard never got to speak and the write happened.
    assert result.blocked is False
    assert json.loads(result.result)["status"] == "written"
    assert harness.store_revision(str(probe.store), "doc-1") == 2


def test_q3_hook_timeout_blocks_but_worker_keeps_running(probe, run, monkeypatch, tmp_path):
    """Q3: timeout vetoes the call AND the abandoned worker still finishes.

    Both halves matter: the tool is blocked (fail-closed) while the hook body
    runs to completion after the block was issued. A mutation placed inside a
    pre-hook would therefore still land, after the framework already refused
    the call.
    """
    probe.provider.seed("doc-1", {"body": "before"})
    marker = tmp_path / "hook-worker-finished"
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "hang")
    monkeypatch.setenv("CONCORD_PROBE_HANG_SECONDS", "3")
    monkeypatch.setenv("CONCORD_PROBE_HANG_MARKER", str(marker))

    started = time.time()
    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})
    elapsed = time.time() - started

    # 1. The call was refused on the configured 1.5s cap, not on the 3s sleep.
    assert result.blocked is True
    assert "timed out" in result.result.lower()
    assert elapsed < 3.0, f"call waited for the hook instead of capping it ({elapsed:.2f}s)"
    assert harness.store_revision(str(probe.store), "doc-1") == 1

    # 2. The abandoned worker outlived the refusal and completed its own work.
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "abandoned hook worker never completed — timeout would be safe"
    assert "worker_completed" in json.dumps(harness.trace_records(probe.trace))
