"""C12: carry initialization provenance and guard every mutation transaction.

All destructive actions affect pytest temporary databases only. A second SQLite
connection models external damage at deterministic boundaries, never a timing bet.
"""
from __future__ import annotations

import sqlite3

import pytest

import concord_kernel as K


def admit(coord, operation_id="target", object_key="object:target"):
    return coord.admit(object_key=object_key, operation_id=operation_id,
                       fingerprint="fp", base_revision=0, actor="test:c12")


def damage(path, *, erase=False):
    with sqlite3.connect(str(path)) as conn:
        if erase:
            for table in ("concord_attempts", "concord_operations",
                          "concord_counters", "concord_migrations"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        else:
            conn.execute("DELETE FROM concord_operations WHERE operation_id='lost'")


def dump(path):
    with sqlite3.connect(str(path)) as conn:
        return tuple(conn.iterdump())


def fixture_store(path, *, state=K.PREPARED, journal_mode="WAL"):
    coord = K.Coordinator(str(path), journal_mode=journal_mode)
    admit(coord)
    if state != K.PREPARED:
        coord.begin_dispatch("target")
        if state == K.UNKNOWN:
            coord.mark_unknown("target", "lost response")
        elif state == K.CONFIRMED:
            coord.confirm("target", {"receipt": "retained"})
    admit(coord, "lost", "object:lost")
    coord.begin_dispatch("lost")
    coord.mark_unknown("lost", "lost response")
    return coord


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_initialization_preserves_preflight_schema_provenance(tmp_path, monkeypatch, journal_mode):
    path = tmp_path / "init.db"
    fixture_store(path, journal_mode=journal_mode)
    validate = K.validate_existing_store
    captured = {}

    def validate_then_erase(target, **kwargs):
        verdict = validate(target, **kwargs)
        assert not verdict.creatable
        damage(path, erase=True)
        captured["dump"] = dump(path)
        captured["bytes"] = path.read_bytes()
        return verdict

    monkeypatch.setattr(K, "validate_existing_store", validate_then_erase)
    with pytest.raises(K.CoordinatorUnavailable) as error:
        K.Coordinator(str(path), journal_mode=journal_mode)
    assert error.value.reason == K.DAMAGE_PARTIAL_SCHEMA
    assert dump(path) == captured["dump"]
    assert path.read_bytes() == captured["bytes"]


def test_concurrent_fresh_initializer_may_finish_first(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    validate = K.validate_existing_store
    first = True

    def validate_then_create(target, **kwargs):
        nonlocal first
        verdict = validate(target, **kwargs)
        if first:
            first = False
            assert verdict.creatable
            coord = K.Coordinator(str(path))
            admit(coord, "other", "object:target")
        return verdict

    monkeypatch.setattr(K, "validate_existing_store", validate_then_create)
    coord = K.Coordinator(str(path))
    assert admit(coord).kind == K.CONFLICT
    assert coord.get("other")["state"] == K.PREPARED


@pytest.mark.parametrize("action,state", [
    ("dispatch", K.PREPARED), ("cancel", K.PREPARED),
    ("confirm", K.PENDING), ("fail", K.PENDING),
    ("unknown", K.PENDING), ("reconcile", K.UNKNOWN),
    ("prune", K.CONFIRMED),
])
def test_every_mutation_refuses_new_history_damage(tmp_path, action, state):
    path = tmp_path / "mutation.db"
    coord = fixture_store(path, state=state)
    damage(path)
    before = dump(path)
    operations = {
        "dispatch": lambda: coord.begin_dispatch("target"),
        "cancel": lambda: coord.cancel("target"),
        "confirm": lambda: coord.confirm("target", {"new": "receipt"}),
        "fail": lambda: coord.fail_before_execution("target", "refused"),
        "unknown": lambda: coord.mark_unknown("target", "timeout"),
        "reconcile": lambda: coord.reconcile("target", {"revision": 0}),
        "prune": lambda: coord.prune_resolved(keep_per_object=0),
    }
    with pytest.raises(K.CoordinatorUnavailable) as error:
        operations[action]()
    assert error.value.reason == K.DAMAGE_HISTORY
    assert dump(path) == before
    with pytest.raises(K.CoordinatorUnavailable):
        K.Coordinator(str(path))


def test_damage_between_admission_and_dispatch_never_calls_provider(tmp_path, monkeypatch):
    path = tmp_path / "dispatch.db"
    coord = K.Coordinator(str(path))
    admit(coord, "lost", "object:lost")
    coord.begin_dispatch("lost")
    coord.mark_unknown("lost", "lost response")
    begin = coord.begin_dispatch
    calls = []

    def damage_then_begin(operation_id):
        damage(path)
        return begin(operation_id)

    monkeypatch.setattr(coord, "begin_dispatch", damage_then_begin)
    result = coord.run(operation_id="target", actor="test:c12",
                       received_args={"object": "object:target", "value": "v"},
                       mutate=lambda: calls.append("write") or {"ok": True})
    assert result["status"] == K.UNKNOWN
    assert result["reason"] == "reservation_not_released"
    assert result["storage_reason"] == K.DAMAGE_HISTORY
    assert result["provider_dispatched"] is False
    assert result["settlement_persisted"] is False
    assert calls == []
    assert coord.get("target")["state"] == K.PREPARED


def test_damage_after_provider_write_cannot_report_durable_confirmation(tmp_path):
    path = tmp_path / "settle.db"
    coord = K.Coordinator(str(path))
    admit(coord, "lost", "object:lost")
    coord.begin_dispatch("lost")
    coord.mark_unknown("lost", "lost response")
    calls = []

    def mutate():
        calls.append("write")
        damage(path)
        return {"observed": "provider receipt"}

    result = coord.run(operation_id="target", actor="test:c12",
                       received_args={"object": "object:target", "value": "v"},
                       mutate=mutate)
    assert result["status"] == K.UNKNOWN
    assert result["reason"] == "receipt_not_persisted"
    assert result["observed_receipt"] == {"observed": "provider receipt"}
    assert calls == ["write"]
    assert coord.get("target")["state"] == K.PENDING


@pytest.mark.parametrize("statement,reason", [
    ("DROP INDEX ux_concord_active_object", K.DAMAGE_PARTIAL_SCHEMA),
    ("DELETE FROM concord_operations WHERE operation_id='lost'", K.DAMAGE_HISTORY),
    ("UPDATE concord_counters SET value=0 WHERE name='seq'", K.DAMAGE_HISTORY),
])
def test_status_exposes_same_damage_as_write_gate(tmp_path, statement, reason):
    import concord_status as ST
    path = tmp_path / "status.db"
    fixture_store(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(statement)
    before = path.read_bytes()
    snapshot = ST.read_snapshot(path)
    assert snapshot.damage == reason
    assert not snapshot.healthy
    assert snapshot.rows, "readable surviving operations remain visible"
    assert snapshot.attempts, "surviving attempt evidence remains visible"
    assert path.read_bytes() == before


@pytest.mark.parametrize("outcome", ["rejected", "error", "confirmed"])
def test_settlement_failure_preserves_truthful_recovery_result(tmp_path, monkeypatch, outcome):
    import concord_result as R
    coord = K.Coordinator(str(tmp_path / "result.db"))

    def fail(*args, **kwargs):
        raise K.IllegalTransition("another reconciliation already changed the state")

    def mutate():
        if outcome == "rejected":
            raise K.Rejected("provider refused")
        if outcome == "error":
            raise ConnectionError("response lost")
        return {"blob_sha": "a" * 40}

    monkeypatch.setattr(coord, {
        "rejected": "fail_before_execution", "error": "mark_unknown",
        "confirmed": "confirm",
    }[outcome], fail)
    raw = coord.run(operation_id="target", actor="test:c12",
                    received_args={"object": "object:target", "value": "v"},
                    mutate=mutate)
    shape = R.present(raw, object_key="object:target", operation_id="target")
    assert shape["outcome"] == R.UNKNOWN
    assert shape["recorded"] is True
    assert shape["auto_retry"] is False
    assert shape["settlement_persisted"] is False
    assert shape["last_recorded_state"] == K.PENDING
    assert shape["next_action"]["kind"] == R.RECOVER_STORE
    if outcome == "rejected":
        assert shape["provider_outcome"] == "not_applied"
        assert shape["second_write"] is False


@pytest.mark.parametrize("statement", [
    "UPDATE concord_counters SET value='bad' WHERE name='seq'",
    "UPDATE concord_operations SET created_seq='bad'",
    "UPDATE concord_operations SET updated_seq=-1",
    "UPDATE concord_attempts SET started_seq='bad'",
    "UPDATE concord_attempts SET settled_seq='bad'",
])
def test_malformed_ordering_after_dispatch_is_a_structured_unknown(tmp_path, statement):
    import concord_backup as B
    path = tmp_path / "invalid-sequence.db"
    coord = K.Coordinator(str(path))

    def mutate():
        with sqlite3.connect(path) as conn:
            conn.execute(statement)
        return {"observed": "receipt"}

    result = coord.run(operation_id="target", actor="test:c12",
                       received_args={"object": "object:target", "value": "v"},
                       mutate=mutate)
    assert result["status"] == K.UNKNOWN
    assert result["reason"] == "receipt_not_persisted"
    assert result["recovery_required"] is True
    assert result["observed_receipt"] == {"observed": "receipt"}
    assert not B.inspect_store(path)["is_concord_store"]
    assert K.validate_existing_store(str(path)).damage_reason == K.DAMAGE_HISTORY


@pytest.mark.parametrize("content", ["\ud800", "\udfff"])
def test_unpaired_surrogate_is_refused_before_store_or_provider(tmp_path, monkeypatch, content):
    import concord_store as S
    import concord_tool as T
    path = tmp_path / "must-not-exist.db"
    monkeypatch.setattr(S, "build_adapter", lambda *_: pytest.fail("provider must not be reached"))
    args = dict(repository="octo/disposable", path="file.txt", branch="main",
                operation_id="invalid-utf8", expected_revision="a" * 40, content=content)
    result = T.run_write(args, settings=S.Settings(store_path=path))
    assert result["outcome"] == "refused"
    assert result["reason"] == "invalid_arguments"
    assert result["recorded"] is False
    assert not path.exists()


def test_valid_unicode_content_remains_accepted():
    import concord_tool as T
    args = dict(repository="octo/disposable", path="file.txt", branch="main",
                operation_id="valid-utf8", expected_revision="a" * 40, content="\U0001f31f")
    assert T.parse_arguments(args)["content"] == "\U0001f31f"
