"""C1 Q2 — the tool_execution middleware seam.

Q2: an execution-middleware callback failure — does it fall through to the
downstream tool?

hermes_cli/middleware.py::_run_execution_chain catches a callback exception and
`return call_at(index + 1, payload)` — the next frame, which for a single
registered callback is the terminal dispatch.
"""
from __future__ import annotations

import json

import harness


def test_middleware_refusal_prevents_execution(probe, run, monkeypatch):
    """Returning without calling next_call IS fail-closed: the write never happens."""
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_MW", "block")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert harness.store_revision(str(probe.store), "doc-1") == 1
    payload = json.loads(result.result)
    assert payload["status"] == "conflict"
    # A middleware refusal is invisible to executor state: nothing dispatched and
    # nothing blocked, so the call reads as an ordinary successful tool result.
    assert result.dispatched is False
    assert result.blocked is False


def test_q2_raising_middleware_falls_through_and_executes(probe, run, monkeypatch):
    """Q2: the callback raises before calling next_call -> downstream executes.

    This is the central fail-open finding for architecture A's second seam.
    """
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_MW", "raise")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert result.blocked is False
    assert json.loads(result.result)["status"] == "written"
    assert harness.store_revision(str(probe.store), "doc-1") == 2, "middleware failure did not fail open"


def test_q2_raising_middleware_after_dispatch_returns_downstream_result(probe, run, monkeypatch):
    """Raise after next_call: `if next_succeeded: return next_result` swallows it.

    The tool succeeded and the failure is recorded only as a warning; the caller
    sees a clean success.
    """
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_MW", "raise_after_next")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert json.loads(result.result)["status"] == "written"
    assert harness.store_revision(str(probe.store), "doc-1") == 2
