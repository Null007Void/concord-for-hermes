"""C2 — explicit outcomes for storage faults and clock anomalies, all tested.

Each fault is produced by the real storage engine, not by mocking the kernel:

  * corruption      — a file that is not a database            (DatabaseError)
  * full storage    — SQLITE_FULL from a hard page cap          (OperationalError)
  * permissions     — the database file made read-only          (OperationalError)
  * lock contention — another connection holding a write lock   (OperationalError)

The full-storage case uses the kernel's own ``max_pages`` bound. That is the same
SQLITE_FULL error a full volume produces, but the trigger is a configured cap
rather than an exhausted disk — stated here so the evidence is not overread.

The clock tests prove the kernel never consults wall time for a correctness
decision: timestamps are diagnostics, ordering is a durable counter.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import concord_kernel as K
from concord_core import LocalProvider, StaleRevision


@pytest.fixture
def provider(tmp_path):
    p = LocalProvider(str(tmp_path / "store.db"))
    p.seed("doc", {"body": "before"})
    return p


def _args(value="after", base=1):
    return {"object": "doc", "value": value, "expected_revision": base}


# ------------------------------------------------------------- storage faults
def test_corruption_refuses_and_never_reaches_the_provider(tmp_path):
    db = tmp_path / "k.db"
    coord = K.Coordinator(str(db))
    db.write_bytes(b"this is not a sqlite database" * 64)
    calls = []

    result = coord.run(operation_id="op-1", actor="a", received_args=_args(),
                       mutate=lambda: calls.append(1) or {"revision": 2})
    assert result["status"] == "refused"
    assert result["reason"] == "storage_corrupt"
    assert calls == [], "a corrupt coordinator must not fall through to the mutation"


def test_a_corrupt_file_is_refused_at_construction(tmp_path):
    db = tmp_path / "k.db"
    db.write_bytes(b"\x00\xffnot sqlite" * 200)
    with pytest.raises(K.CoordinatorUnavailable) as excinfo:
        K.Coordinator(str(db))
    assert excinfo.value.reason == "storage_corrupt"


def test_lock_contention_refuses_within_the_busy_budget(tmp_path):
    db = tmp_path / "k.db"
    coord = K.Coordinator(str(db), busy_ms=100)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    calls = []
    try:
        result = coord.run(operation_id="op-1", actor="a", received_args=_args(),
                           mutate=lambda: calls.append(1) or {"revision": 2})
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result["status"] == "refused"
    assert result["reason"] == "storage_locked"
    assert calls == []


def test_full_storage_refuses_with_a_named_reason(tmp_path):
    db = tmp_path / "k.db"
    K.Coordinator(str(db), journal_mode="DELETE")
    conn = sqlite3.connect(str(db), isolation_level=None)
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.close()

    coord = K.Coordinator(str(db), journal_mode="DELETE", max_pages=pages,
                          max_unresolved_records=10**9, max_resolved_per_object=10**9)
    error = None
    for i in range(500):
        try:
            coord.admit(object_key=f"obj-{i}", operation_id=f"op-{i}",
                        fingerprint="f" * 64, base_revision=0, actor="a")
            coord.fail_before_execution(f"op-{i}", "fill the database")
        except K.CoordinatorUnavailable as exc:
            error = exc
            break
    assert error is not None, "the page cap never produced SQLITE_FULL"
    assert error.reason == "storage_full"

    calls = []
    result = coord.run(operation_id="op-after", actor="a",
                       received_args={"object": "obj-after", "value": "x", "expected_revision": 0},
                       mutate=lambda: calls.append(1) or {"revision": 1})
    assert result["status"] == "refused"
    assert result["reason"] == "storage_full"
    assert calls == []


def test_permission_failure_refuses_and_leaves_the_provider_untouched(tmp_path, provider):
    db = tmp_path / "k.db"
    coord = K.Coordinator(str(db))
    os.chmod(db, 0o444)
    calls = []
    try:
        result = coord.run(operation_id="op-1", actor="a", received_args=_args(),
                           mutate=lambda: calls.append(1) or provider.update(
                               "doc", expected_revision=1, value="after"))
    finally:
        os.chmod(db, 0o666)
    assert result["status"] == "refused"
    assert result["reason"] == "storage_permissions"
    assert calls == []
    assert provider.read("doc")["revision"] == 1


# ------------------------------------------------------------------- clock
class _BackwardsClock:
    def __init__(self, start: float = 1_800_000_000.0, step: float = 3600.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value -= self.step
        return self.value


class _JumpClock:
    def __init__(self) -> None:
        self.value = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value

    def jump(self, seconds: float) -> None:
        self.value += seconds


def test_a_backwards_clock_changes_no_decision(tmp_path, provider):
    clock = _BackwardsClock()
    coord = K.Coordinator(str(tmp_path / "k.db"), clock=clock)

    coord.admit(object_key="doc", operation_id="op-1",
                fingerprint=K.fingerprint_from_arguments(_args()), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    coord.mark_unknown("op-1", "lost after send")
    coord.admit(object_key="other", operation_id="op-2", fingerprint="g" * 64,
                base_revision=0, actor="a")

    first = coord.get("op-1")
    second = coord.get("op-2")
    # Durable ordering is a counter and is unaffected by the clock.
    assert first["created_seq"] < second["created_seq"]
    assert first["created_seq"] < first["updated_seq"]
    # The recorded wall clock did go backwards — and nothing depended on it.
    assert first["updated_at"] < first["created_at"]
    # The ambiguous operation still holds its object.
    assert first["state"] == K.UNKNOWN
    assert coord.holder_of("doc") == "op-1"

    # A replay is still a replay, and a fresh operation still writes once.
    replay = coord.run(operation_id="op-1", actor="a", received_args=_args(),
                       mutate=lambda: pytest.fail("unknown must not retry"))
    assert replay["status"] == K.CONFLICT


def test_a_forward_clock_jump_does_not_release_an_unknown(tmp_path, provider):
    clock = _JumpClock()
    coord = K.Coordinator(str(tmp_path / "k.db"), clock=clock)
    coord.admit(object_key="doc", operation_id="op-1",
                fingerprint=K.fingerprint_from_arguments(_args()), base_revision=1, actor="a")
    coord.begin_dispatch("op-1")
    coord.mark_unknown("op-1", "ambiguous")

    clock.jump(3600 * 24 * 365 * 5)
    coord.prune_resolved(older_than_seconds=0)

    assert coord.get("op-1")["state"] == K.UNKNOWN
    assert coord.get("op-1")["pruned"] is False
    assert coord.holder_of("doc") == "op-1"
    blocked = coord.run(operation_id="op-2", actor="b", received_args=_args(),
                        mutate=lambda: pytest.fail("a leap forward in time releases nothing"))
    assert blocked["status"] == K.CONFLICT


# --------------------------------------------------- dispatch-side ambiguity
def test_a_landed_write_with_an_unrecorded_receipt_reports_unknown(tmp_path, provider, monkeypatch):
    coord = K.Coordinator(str(tmp_path / "k.db"))

    def boom(self, operation_id, receipt):
        raise K.CoordinatorUnavailable("disk fell over during the receipt write",
                                       reason="storage_full")

    monkeypatch.setattr(K.Coordinator, "confirm", boom)
    result = coord.run(
        operation_id="op-1", actor="a", received_args=_args(),
        mutate=lambda: provider.update("doc", expected_revision=1, value="after"),
    )
    assert result["status"] == K.UNKNOWN
    assert result["reason"] == "receipt_not_persisted"
    assert result["observed_receipt"]["revision"] == 2
    assert provider.read("doc")["revision"] == 2

    record = coord.get("op-1")
    assert record["state"] == K.PENDING, "the durable record must not claim confirmed"
    assert coord.holder_of("doc") == "op-1"

    monkeypatch.undo()
    settled = coord.reconcile("op-1", {"revision": 2, "value": "after", "source": "read"})
    assert settled["state"] == K.CONFIRMED


def test_an_unexpected_dispatch_error_is_unknown_not_failed(tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))

    def explode():
        raise ConnectionError("connection reset after send")

    result = coord.run(operation_id="op-1", actor="a", received_args=_args(), mutate=explode)
    assert result["status"] == K.UNKNOWN
    assert result["reason"] == "dispatch_error"
    record = coord.get("op-1")
    assert record["state"] == K.UNKNOWN
    assert coord.holder_of("doc") == "op-1", "an ambiguous failure holds the object"
    assert "ConnectionError" in record["note"]


def test_a_stale_revision_is_a_proof_not_a_guess(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    provider.seed("doc", {"body": "moved"})  # revision 2, caller expects 1

    def stale():
        try:
            return provider.update("doc", expected_revision=1, value="after")
        except StaleRevision as exc:
            raise K.Rejected(str(exc)) from exc

    result = coord.run(operation_id="op-1", actor="a", received_args=_args(), mutate=stale)
    assert result["status"] == K.CONFLICT
    assert result["state"] == K.FAILED_BEFORE_EXECUTION
    assert coord.holder_of("doc") is None
    assert provider.read("doc")["revision"] == 2
