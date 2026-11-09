"""Malformed recovery evidence never becomes permission to dispatch again."""
import sqlite3

import pytest

import concord_backup as B
import concord_kernel as K
import concord_status as ST


DAMAGE = [
    "UPDATE concord_operations SET state='unknwon'",
    "UPDATE concord_attempts SET state='unknwon'",
    "UPDATE concord_operations SET attempt_count='not-a-count'",
    "UPDATE concord_operations SET attempt_count=0",
    "DELETE FROM concord_attempts",
    "UPDATE concord_attempts SET ordinal=2",
    "UPDATE concord_operations SET state='pending'",
    "UPDATE concord_attempts SET state='pending'",
    "UPDATE concord_operations SET receipt='{broken'",
    "UPDATE concord_operations SET receipt='[]'",
]


def held_store(path):
    coord = K.Coordinator(str(path))
    args = {"object": "object", "value": "new", "expected_revision": 0}
    coord.admit(object_key="object", operation_id="held", fingerprint=K.fingerprint(args),
                base_revision=0, actor="test")
    coord.begin_dispatch("held")
    coord.mark_unknown("held", "response lost after possible application")
    return coord, args


@pytest.mark.parametrize("damage", DAMAGE)
def test_damaged_history_refuses_all_recovery_surfaces(tmp_path, damage):
    path = tmp_path / "live.db"
    coord, args = held_store(path)
    with sqlite3.connect(path) as conn:
        conn.execute(damage)
    with sqlite3.connect(path) as conn:
        before = tuple(conn.iterdump())
    writes = []
    result = coord.run(operation_id="held", actor="test", received_args=args,
                       mutate=lambda: writes.append("write") or {})
    assert result["status"] == "refused"
    assert result["reason"] == K.DAMAGE_HISTORY
    assert not writes
    with pytest.raises(K.CoordinatorUnavailable) as error:
        K.Coordinator(str(path))
    assert error.value.reason == K.DAMAGE_HISTORY
    assert not ST.read_snapshot(path).healthy
    assert not B.inspect_store(path)["is_concord_store"]
    target = tmp_path / "backup.db"
    with pytest.raises(B.BackupError):
        B.backup_store(path, target)
    with pytest.raises(B.BackupError):
        B.restore_store(path, target)
    assert not target.exists()
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize("settlement", [K.CONFIRMED, K.FAILED_BEFORE_EXECUTION])
def test_reconciled_unknown_attempt_history_remains_valid(tmp_path, settlement):
    path = tmp_path / "live.db"
    coord, args = held_store(path)
    coord.reconcile("held", {"revision": 1},
                    evaluate=lambda **kwargs: K.EvidenceVerdict(settlement, "test proof"))
    assert not K.validate_existing_store(str(path)).damaged
    assert ST.read_snapshot(path).healthy
    assert B.inspect_store(path)["is_concord_store"]
    # Reconciliation preserves the original unknown attempt, and a proven
    # negative can legitimately be followed by a new attempt of this intent.
    if settlement == K.FAILED_BEFORE_EXECUTION:
        assert coord.run(operation_id="held", actor="test", received_args=args,
                         mutate=lambda: {"revision": 1})["status"] == K.CONFIRMED
