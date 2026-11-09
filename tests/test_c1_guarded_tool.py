"""C1 — the recommended architecture: a plugin-owned mutation tool.

The admission decision and the provider write live in one function body inside
the tool's own dispatch path. These tests run it through the *same* real
executor as the guarded-by-hook tool, so the comparison is like for like.
"""
from __future__ import annotations

import json

import harness


def _mutate(run, **kwargs):
    args = {"object": "doc-1", "operation_id": "op-1", "value": "after", "expected_revision": 1}
    args.update(kwargs)
    return run(harness.GUARDED_TOOL, args)


def test_guarded_tool_runs_through_real_dispatch_and_writes(probe, run):
    probe.provider.seed("doc-1", {"body": "before"})

    result = _mutate(run)
    payload = json.loads(result.result)

    assert result.dispatched is True and result.blocked is False
    assert payload["status"] == "confirmed"
    assert harness.store_value(str(probe.store), "doc-1") == "after"


def test_guarded_tool_rejects_stale_intent(probe, run):
    """Serialization alone does not refresh a stale plan; the provider check does."""
    probe.provider.seed("doc-1", {"body": "before"})
    probe.provider.update("doc-1", expected_revision=1, value="changed-elsewhere")

    payload = json.loads(_mutate(run, expected_revision=1).result)

    assert payload["status"] == "conflict"
    assert payload["reason"] == "stale_revision"
    assert probe.coordinator.get("op-1")["state"] == "failed_before_execution"
    assert harness.store_revision(str(probe.store), "doc-1") == 2  # only the outside write


def test_guarded_tool_records_unknown_when_the_provider_blips(probe, run, monkeypatch):
    """A provider failure after dispatch is unknown, never 'failed', and holds the object."""
    probe.provider.seed("doc-1", {"body": "before"})
    # An unreadable provider store: the write may or may not have landed.
    probe.store.write_bytes(b"not a database" * 32)

    payload = json.loads(_mutate(run).result)

    assert payload["status"] == "unknown"
    assert probe.coordinator.get("op-1")["state"] == "unknown"
    # A retry cannot proceed while the outcome is unknown.
    retry = json.loads(_mutate(run, operation_id="op-retry", value="after-2").result)
    assert retry["status"] == "conflict"
