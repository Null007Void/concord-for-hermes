"""C1 Q4 — coordinator failure.

Q4: when the coordinator is locked or corrupt, is the mutation refused cleanly?

Tested at both candidate seams, because the answer differs: the self-coordinating
tool (architecture B) converts its own dependency failure into a refusal; a
pre-hook guard (architecture A) relies on the framework's per-callback isolation.
"""
from __future__ import annotations

import json
import sqlite3

import harness


def _corrupt(path) -> None:
    path.write_bytes(b"this is not a sqlite database" * 64)


def test_q4_corrupt_coordinator_refuses_guarded_tool(probe, run, monkeypatch):
    """Architecture B: coordinator unreadable -> explicit refusal, no write."""
    probe.provider.seed("doc-1", {"body": "before"})
    _corrupt(probe.db)

    result = run(
        harness.GUARDED_TOOL,
        {"object": "doc-1", "operation_id": "op-1", "value": "after", "expected_revision": 1},
    )

    payload = json.loads(result.result)
    assert payload["status"] == "refused"
    assert payload["reason"] == "coordinator_unavailable"
    assert harness.store_revision(str(probe.store), "doc-1") == 1


def test_q4_locked_coordinator_refuses_guarded_tool(probe, run, monkeypatch):
    """Architecture B: coordinator locked by another connection -> refusal, no write."""
    probe.provider.seed("doc-1", {"body": "before"})
    monkeypatch.setenv("CONCORD_PROBE_BUSY_MS", "200")

    holder = sqlite3.connect(str(probe.db), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = run(
            harness.GUARDED_TOOL,
            {"object": "doc-1", "operation_id": "op-1", "value": "after", "expected_revision": 1},
        )
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    payload = json.loads(result.result)
    assert payload["status"] == "refused"
    assert payload["reason"] == "coordinator_unavailable"
    assert harness.store_revision(str(probe.store), "doc-1") == 1


def test_q4_corrupt_coordinator_blocks_via_prehook(probe, run, monkeypatch):
    """Architecture A: a guard whose own dependency raises is vetoed by isolation.

    Correct here — and only here: the same outage becomes a fail-open the moment
    the *dispatch machinery* (rather than the callback) is what fails
    (see test_c1_prehook.py::test_q1_outer_hook_dispatch_failure_executes_tool).
    """
    probe.provider.seed("doc-1", {"body": "before"})
    _corrupt(probe.db)
    monkeypatch.setenv("CONCORD_PROBE_HOOK", "coord_fail")

    result = run(harness.TOOL_NAME, {"object": "doc-1", "value": "after", "expected_revision": 1})

    assert result.blocked is True
    assert "CoordinatorUnavailable" in result.result
    assert harness.store_revision(str(probe.store), "doc-1") == 1
