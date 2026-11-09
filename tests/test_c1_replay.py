"""C1 Q6 — replay and durable identity.

Q6: is the same operation ID replayed after confirmed completion answered from
a durable receipt, with no second write? And does reusing that ID with different
material content fail rather than become a new external action?
"""
from __future__ import annotations

import json

import harness


def _mutate(run, **kwargs):
    args = {"object": "doc-1", "operation_id": "op-1", "value": "after", "expected_revision": 1}
    args.update(kwargs)
    return json.loads(run(harness.GUARDED_TOOL, args).result)


def test_q6_replay_returns_durable_receipt_without_second_write(probe, run):
    probe.provider.seed("doc-1", {"body": "before"})

    first = _mutate(run)
    assert first["status"] == "confirmed"
    assert harness.store_revision(str(probe.store), "doc-1") == 2

    second = _mutate(run)

    assert second["status"] == "replayed"
    assert second["second_write"] is False
    assert second["receipt"] == first["receipt"], "replay did not return the original receipt"
    assert harness.store_revision(str(probe.store), "doc-1") == 2, "replay performed a second write"


def test_q6_replay_survives_a_fresh_coordinator_instance(probe, run):
    """Durability: the receipt is in the store, not in process memory."""
    probe.provider.seed("doc-1", {"body": "before"})
    first = _mutate(run)

    reopened = probe.core.Coordinator(str(probe.db))
    record = reopened.get("op-1")

    assert record["state"] == "confirmed"
    assert record["receipt"] == first["receipt"]
    assert harness.store_revision(str(probe.store), "doc-1") == 2


def test_q6_same_operation_id_with_changed_args_is_rejected(probe, run):
    probe.provider.seed("doc-1", {"body": "before"})
    assert _mutate(run)["status"] == "confirmed"

    changed = _mutate(run, value="something else")

    assert changed["status"] == "rejected_argument_mismatch"
    assert harness.store_revision(str(probe.store), "doc-1") == 2


def test_q6_unresolved_operation_blocks_a_competitor(probe, run):
    """An unresolved operation on the object refuses a different operation."""
    probe.provider.seed("doc-1", {"body": "before"})
    probe.coordinator.admit(
        object_key="doc-1",
        operation_id="op-holder",
        fingerprint="held",
        actor="actor-a",
    )

    blocked = _mutate(run, operation_id="op-other")

    assert blocked["status"] == "conflict"
    assert blocked["holder"] == "op-holder"
    assert harness.store_revision(str(probe.store), "doc-1") == 1


def test_q6_unknown_outcome_is_not_released_by_time(probe, run):
    """Unknown stays unknown: elapsed time must not make a repeat safe."""
    probe.provider.seed("doc-1", {"body": "before"})
    probe.coordinator.admit(
        object_key="doc-1", operation_id="op-unknown", fingerprint="fp", actor="actor-a"
    )
    probe.coordinator.mark_unknown("op-unknown", "provider timed out after dispatch")

    later = _mutate(run, operation_id="op-retry", value="after-again")

    assert later["status"] == "conflict"
    assert probe.coordinator.get("op-unknown")["state"] == "unknown"
    assert harness.store_revision(str(probe.store), "doc-1") == 1
