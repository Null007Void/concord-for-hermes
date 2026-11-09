"""C2 — the seven outcome states, their transitions, and what each may claim.

Every state is reached through the public API in one durable store, then read
back through a *fresh* Coordinator instance, because a state that only exists in
memory is not a state.
"""
from __future__ import annotations

import pytest

import concord_kernel as K
from concord_core import LocalProvider


@pytest.fixture
def coord(tmp_path):
    return K.Coordinator(str(tmp_path / "kernel.db"))


@pytest.fixture
def provider(tmp_path):
    p = LocalProvider(str(tmp_path / "store.db"))
    p.seed("doc", {"body": "before"})
    return p


def _fp(material):
    return K.fingerprint(material)


def _args(value="after", base=1, key="doc"):
    return {"object": key, "value": value, "expected_revision": base}


def test_all_seven_states_are_reachable_and_durable(coord, provider):
    # prepared / reserved
    coord.admit(object_key="o-reserved", operation_id="op-reserved",
                fingerprint=_fp(_args(key="o-reserved")), base_revision=1, actor="a")

    # pending
    coord.admit(object_key="o-pending", operation_id="op-pending",
                fingerprint=_fp(_args(key="o-pending")), base_revision=0, actor="a")
    coord.begin_dispatch("op-pending")

    # confirmed
    coord.run(operation_id="op-confirmed", actor="a",
              received_args=_args(), mutate=lambda: provider.update("doc", expected_revision=1, value="after"))

    # failed before execution
    coord.admit(object_key="o-failed", operation_id="op-failed",
                fingerprint=_fp(_args(key="o-failed")), base_revision=0, actor="a")
    coord.begin_dispatch("op-failed")
    coord.fail_before_execution("op-failed", "provider rejected the conditional write")

    # unknown
    coord.admit(object_key="o-unknown", operation_id="op-unknown",
                fingerprint=_fp(_args(key="o-unknown")), base_revision=0, actor="a")
    coord.begin_dispatch("op-unknown")
    coord.mark_unknown("op-unknown", "connection dropped after send")

    # cancelled
    coord.admit(object_key="o-cancelled", operation_id="op-cancelled",
                fingerprint=_fp(_args(key="o-cancelled")), base_revision=0, actor="a")
    coord.cancel("op-cancelled", "operator withdrew the intent")

    # conflict (recorded as a durable outcome, holding the other operation)
    coord.admit(object_key="o-conflict", operation_id="op-holder",
                fingerprint=_fp(_args(key="o-conflict")), base_revision=0, actor="a")
    coord.admit(object_key="o-conflict", operation_id="op-loser",
                fingerprint=_fp(_args(key="o-conflict")), base_revision=0, actor="b")

    fresh = K.Coordinator(coord.path)
    observed = {
        op: fresh.get(op)["state"]
        for op in ("op-reserved", "op-pending", "op-confirmed", "op-failed",
                   "op-unknown", "op-cancelled", "op-loser")
    }
    assert set(observed.values()) == set(K.ALL_STATES), observed
    assert observed["op-loser"] == K.CONFLICT
    assert fresh.get("op-loser")["holder"] == "op-holder"

    stats = fresh.stats()
    assert stats["by_state"][K.CONFLICT] == 1
    assert stats["unresolved"] == 4  # reserved, pending, unknown, and the conflict's holder


def test_cancel_is_only_honest_before_dispatch(coord):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")

    with pytest.raises(K.IllegalTransition):
        coord.cancel("op-1", "too late")
    assert coord.get("op-1")["state"] == K.PENDING

    coord.mark_unknown("op-1", "in flight, outcome not determinable")
    with pytest.raises(K.IllegalTransition):
        coord.cancel("op-1", "still not cancellable")
    assert coord.get("op-1")["state"] == K.UNKNOWN


def test_cancelled_intent_is_closed_and_requires_a_new_operation_id(coord):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.cancel("op-1")

    again = coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args),
                        base_revision=1, actor="a")
    assert again.kind == K.CONFLICT
    assert "cancelled" in again.reason

    fresh = coord.admit(object_key="doc", operation_id="op-2", fingerprint=_fp(args),
                        base_revision=1, actor="a")
    assert fresh.kind == K.ADMITTED, "a cancelled operation must not keep the object held"


def test_unknown_holds_the_object_across_a_restart_and_is_never_retried(coord, tmp_path):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    coord.mark_unknown("op-1", "process died after send")

    restarted = K.Coordinator(str(tmp_path / "kernel.db"))
    record = restarted.get("op-1")
    assert record["state"] == K.UNKNOWN
    assert restarted.holder_of("doc") == "op-1"

    replay = restarted.run(operation_id="op-1", actor="a", received_args=args,
                           mutate=lambda: pytest.fail("an unknown outcome must not be retried"))
    assert replay["status"] == K.CONFLICT

    competitor = restarted.run(operation_id="op-2", actor="b", received_args=args,
                               mutate=lambda: pytest.fail("a held object must not be written"))
    assert competitor["status"] == K.CONFLICT
    assert competitor["holder"] == "op-1"


def test_conflict_is_recorded_and_retryable_once_the_holder_resolves(coord, provider):
    holder_args = _args(key="other")
    coord.admit(object_key="doc", operation_id="op-holder", fingerprint="h" * 64,
                base_revision=1, actor="a")
    loser = coord.admit(object_key="doc", operation_id="op-loser", fingerprint=_fp(_args()),
                        base_revision=1, actor="b")
    assert loser.kind == K.CONFLICT
    assert coord.get("op-loser")["state"] == K.CONFLICT

    coord.fail_before_execution("op-holder", "never dispatched")
    assert coord.holder_of("doc") is None

    result = coord.run(
        operation_id="op-loser", actor="b", received_args=_args(),
        mutate=lambda: provider.update("doc", expected_revision=1, value="after"),
    )
    assert result["status"] == K.CONFIRMED
    assert result["second_write"] is True
    assert provider.read("doc")["revision"] == 2


def test_failed_before_execution_holds_nothing_and_is_retryable(coord, provider):
    args = _args()

    def stale():
        raise K.Rejected("409: expected revision 1, provider is at 3")

    result = coord.run(operation_id="op-1", actor="a", received_args=args, mutate=stale)
    assert result["status"] == K.CONFLICT
    assert result["state"] == K.FAILED_BEFORE_EXECUTION
    assert coord.holder_of("doc") is None
    assert coord.get("op-1")["attempts"][0]["state"] == K.FAILED_BEFORE_EXECUTION
    assert provider.read("doc")["revision"] == 1


def test_a_pending_operation_is_not_re_admitted_by_a_second_call(coord, provider):
    """The retry rule is state-dependent: pending is ambiguous, so a second call
    on the same id is a conflict, not a re-dispatch."""
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1",
                fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    result = coord.run(operation_id="op-1", actor="a", received_args=args,
                       mutate=lambda: pytest.fail("pending must not be re-dispatched"))
    assert result["status"] == K.CONFLICT
    assert result["state"] == K.PENDING


def test_illegal_transitions_are_refused_not_absorbed(coord, provider):
    args = _args()
    coord.run(operation_id="op-1", actor="a", received_args=args,
              mutate=lambda: provider.update("doc", expected_revision=1, value="after"))
    with pytest.raises(K.IllegalTransition):
        coord.mark_unknown("op-1", "nope")
    with pytest.raises(K.IllegalTransition):
        coord.confirm("op-1", {"revision": 99})
    with pytest.raises(K.IllegalTransition):
        coord.begin_dispatch("op-1")
    assert coord.get("op-1")["state"] == K.CONFIRMED


# ------------------------------------------------------------ reconcile
def test_reconcile_confirms_when_the_provider_matches_our_fingerprint(coord, provider):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    # The write landed, the receipt did not (crash window).
    provider.update("doc", expected_revision=1, value="after")

    evidence = {"revision": 2, "value": "after", "source": "sqlite provider stand-in read"}
    result = coord.reconcile("op-1", evidence)
    assert result["state"] == K.CONFIRMED
    record = coord.get("op-1")
    assert record["receipt"]["reconciled"] is True
    assert record["receipt"]["evidence_sha256"]
    assert coord.holder_of("doc") is None
    assert [a["state"] for a in record["attempts"]] == [K.CONFIRMED]


def test_reconcile_proves_non_execution_when_the_provider_is_still_at_base(coord, provider):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")

    result = coord.reconcile("op-1", {"revision": 1, "value": {"body": "before"}, "source": "read"})
    assert result["state"] == K.FAILED_BEFORE_EXECUTION
    assert coord.holder_of("doc") is None
    assert provider.read("doc")["revision"] == 1


def test_reconcile_refuses_to_claim_a_foreign_change(coord, provider):
    args = _args()
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=_fp(args), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    provider.update("doc", expected_revision=1, value="someone-else")

    result = coord.reconcile("op-1", {"revision": 2, "value": "someone-else", "source": "read"})
    assert result["state"] == K.UNKNOWN, "a value we did not fingerprint must never be claimed as ours"
    assert coord.holder_of("doc") == "op-1"
    assert "not attributable" in coord.get("op-1")["note"]


def test_reconcile_cannot_touch_a_settled_operation(coord, provider):
    args = _args()
    coord.run(operation_id="op-1", actor="a", received_args=args,
              mutate=lambda: provider.update("doc", expected_revision=1, value="after"))
    with pytest.raises(K.IllegalTransition):
        coord.reconcile("op-1", {"revision": 3, "value": "x"})
