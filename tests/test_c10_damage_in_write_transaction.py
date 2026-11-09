"""C10 — damage validation shares the write transaction (owner F10).

CONCORD-009 made the damage verdict ONE consistent read snapshot, but that
snapshot was still a *separate* transaction from the schema work (``_init``) and
from admission (``admit``). An OUTSIDE process that damages the store after the
verdict and before the mutation - for example deleting the ``concord_operations``
row that backs a surviving ``unknown`` attempt - is therefore invisible to the
write: a new same-object write is admitted while the evidence that the outcome is
unknown survives.

The contract this module pins:

  * initialization validates, creates and migrates inside ONE ``BEGIN IMMEDIATE``
    span - damage committed in the gap between the open-time verdict and that
    transaction refuses the open, before any row changes;
  * admission validates inside its own ``BEGIN IMMEDIATE`` span before it reads a
    row for a decision or writes one - damage committed in the gap between the
    open-time verdict and the admission refuses the admission with the structured
    damage reason and commits no mutation;
  * the refusal survives close/reopen: it is a property of the file, not of one
    connection;
  * a healthy store still admits and dispatches a same-object write exactly once,
    and a healthy unresolved holder still refuses a second write.

The interleavings here are deterministic control points, not timing bets: an
out-of-band SQLite connection runs one real destructive transaction at a named
point (the same technique CONCORD-009 used for the torn-snapshot defect). The
outside process is modelled as exactly that - a second connection - and the tests
make NO claim about ordinary Concord concurrency causing the damage.
"""
from __future__ import annotations

import sqlite3
import sys

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import concord_store as S
import concord_tool as T
import github_harness as H

CANONICAL = "github:octo/disposable@main:notes/plan.md"
BASE = "a" * 40
VALUE = "line one\n"


# ------------------------------------------------------------------ helpers
def open_store(path) -> K.Coordinator:
    return K.Coordinator(str(path), normalize_object_key=A.canonical_object_key)


def unknown_store(path) -> K.Coordinator:
    """A store with one dispatched-and-lost operation: its attempt outlives the row."""
    coord = open_store(path)
    coord.admit(object_key=CANONICAL, operation_id="op-lost", fingerprint="fp-lost",
                base_revision=BASE, actor="profile:c10")
    coord.begin_dispatch("op-lost")
    coord.mark_unknown("op-lost", "the response was lost")
    assert coord.get("op-lost")["state"] == K.UNKNOWN
    assert len(coord.get("op-lost")["attempts"]) == 1
    return coord


def damage_operations_row(path) -> None:
    """What an OUTSIDE process does: one real transaction, out of band.

    It removes the operations row behind the surviving UNKNOWN attempt, so the
    store becomes ``damaged_history``: an attempt row explains a dispatch whose
    operation record is gone.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM concord_operations WHERE operation_id = 'op-lost'")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def damage_index(path) -> None:
    """A partial-schema form of the same out-of-band damage."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP INDEX ux_concord_active_object")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def rows(path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        operations = [tuple(row) for row in conn.execute(
            "SELECT operation_id, state FROM concord_operations ORDER BY operation_id")]
        attempts = [tuple(row) for row in conn.execute(
            "SELECT attempt_id, state FROM concord_attempts ORDER BY attempt_id")]
        orphans = [row[0] for row in conn.execute(
            "SELECT a.attempt_id FROM concord_attempts a WHERE NOT EXISTS"
            " (SELECT 1 FROM concord_operations o WHERE o.operation_id = a.operation_id)")]
    finally:
        conn.close()
    return {"operations": operations, "attempts": attempts, "orphans": orphans}


def admit_new(coord: K.Coordinator) -> K.Admission:
    return coord.admit(object_key=CANONICAL, operation_id="op-new", fingerprint="fp-new",
                       base_revision=BASE, actor="profile:c10")


def _settings(store) -> S.Settings:
    return S.Settings(
        allowed_repositories=(("octo", "disposable"),),
        gh_command=(sys.executable, str(H.FAKEGH)),
        timeout_s=30.0,
        store_path=store,
    )


def _tool_args(base: str, operation_id: str = "op-new") -> dict:
    return {
        "repository": "octo/disposable", "path": "notes/plan.md", "branch": "main",
        "operation_id": operation_id, "expected_revision": base, "content": VALUE,
    }


# ------------------------------------------------- admission shares the txn
def test_damage_after_the_open_time_verdict_refuses_the_admission(tmp_path):
    """The finding itself: damage in the gap is refused, not admitted."""
    store = tmp_path / "admission-gap.db"
    coord = unknown_store(store)          # open-time verdict: healthy
    damage_operations_row(store)          # outside process, after that verdict

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        admit_new(coord)

    assert raised.value.reason == K.DAMAGE_HISTORY, raised.value.reason
    assert "no record" in str(raised.value)
    assert "op-lost" in str(raised.value)


def test_the_refused_admission_committed_nothing_and_still_refuses_after_reopen(tmp_path):
    store = tmp_path / "admission-no-mutation.db"
    coord = unknown_store(store)
    damage_operations_row(store)
    before = rows(store)

    with pytest.raises(K.CoordinatorUnavailable):
        admit_new(coord)

    after = rows(store)
    assert after == before, "a refused admission must not write a row"
    assert [op for op, _state in after["operations"]] == [], after["operations"]
    assert len(after["attempts"]) == 1
    assert after["orphans"], "the surviving attempt is still orphaned"

    # Close/reopen: the verdict is a property of the file, not of one connection.
    del coord
    with pytest.raises(K.CoordinatorUnavailable) as reopened:
        open_store(store)
    assert reopened.value.reason == K.DAMAGE_HISTORY


def test_partial_schema_damage_in_the_gap_is_refused_the_same_way(tmp_path):
    """The other damage class is refused by the same transaction, not only history."""
    store = tmp_path / "admission-partial.db"
    coord = open_store(store)
    coord.admit(object_key=CANONICAL, operation_id="op-existing", fingerprint="fp",
                base_revision=BASE, actor="profile:c10")
    coord.begin_dispatch("op-existing")
    coord.confirm("op-existing", {"blob_sha": "b" * 40})
    damage_index(store)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        admit_new(coord)

    assert raised.value.reason == K.DAMAGE_PARTIAL_SCHEMA, raised.value.reason
    assert "ux_concord_active_object" in str(raised.value)
    assert [op for op, _state in rows(store)["operations"]] == ["op-existing"]


def test_a_store_emptied_after_open_refuses_admission_instead_of_recreating_it(tmp_path):
    """Initialization is the only place ``creatable`` is legitimate."""
    store = tmp_path / "admission-emptied.db"
    coord = unknown_store(store)
    # Replace the Concord schema with an empty SQLite database: what a file
    # restored from nothing - or replaced by a foreign tool - looks like.
    conn = sqlite3.connect(str(store), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("concord_attempts", "concord_operations", "concord_counters",
                      "concord_migrations"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        admit_new(coord)

    assert raised.value.reason == K.DAMAGE_PARTIAL_SCHEMA, raised.value.reason
    assert "no longer holds the Concord schema" in str(raised.value)
    conn = sqlite3.connect(str(store))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "concord_operations" not in tables, "admission must not recreate the store"


# -------------------------------------- initialization shares the transaction
def test_damage_in_the_initialization_gap_refuses_the_open(tmp_path, monkeypatch):
    """Damage between the read-only verdict and the schema transaction refuses."""
    store = tmp_path / "init-gap.db"
    unknown_store(store)

    real_validate = K.validate_existing_store
    state = {"fired": False}

    def verdict_then_damage(target, **kwargs):
        verdict = real_validate(target, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            damage_operations_row(store)
        return verdict

    monkeypatch.setattr(K, "validate_existing_store", verdict_then_damage)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        open_store(store)

    assert state["fired"], "the control point did not run"
    assert raised.value.reason == K.DAMAGE_HISTORY, raised.value.reason
    assert [op for op, _state in rows(store)["operations"]] == []


# ------------------------------------------------------ healthy stores still work
def test_a_healthy_store_still_admits_the_same_object_write(tmp_path):
    store = tmp_path / "healthy-admit.db"
    coord = open_store(store)

    admission = admit_new(coord)

    assert admission.kind == K.ADMITTED, admission.as_dict()
    assert coord.get("op-new")["state"] == K.PREPARED


def test_a_healthy_unresolved_holder_still_refuses_a_second_write(tmp_path):
    """The damage gate did not replace the older, narrower hold."""
    store = tmp_path / "healthy-hold.db"
    coord = unknown_store(store)

    admission = admit_new(coord)

    assert admission.kind == K.CONFLICT, admission.as_dict()
    assert admission.holder == "op-lost"


# ------------------------------------------------ the real tool, fake provider
def test_the_real_tool_refuses_a_same_object_write_after_the_gap_damage(tmp_path, monkeypatch):
    """End-to-end: no provider write is dispatched while a hold's record is gone."""
    gh = H.setup(monkeypatch, tmp_path)
    store = tmp_path / "tool-gap.db"
    unknown_store(store)

    real_run = K.Coordinator.run
    state = {"fired": False}

    def damaging_run(self, **kwargs):
        # The tool has constructed and validated its coordinator, read the base
        # from the provider, and is about to admit. This is the gap.
        if not state["fired"]:
            state["fired"] = True
            damage_operations_row(store)
        return real_run(self, **kwargs)

    monkeypatch.setattr(K.Coordinator, "run", damaging_run)

    shape = T.run_write(_tool_args(gh.sha), settings=_settings(store), actor="profile:c10")

    assert state["fired"], "the control point did not run"
    assert shape["outcome"] == "refused", shape
    assert shape["reason"] == K.DAMAGE_HISTORY, shape
    assert H.puts(gh.root) == 0, "damage observed at admission must not reach the provider"
    assert H.writes(gh.root) == 0
    assert [op for op, _state in rows(store)["operations"]] == []


def test_the_real_tool_still_writes_once_to_a_healthy_store(tmp_path, monkeypatch):
    """Keep-working control: the same call on a healthy store still lands."""
    gh = H.setup(monkeypatch, tmp_path)
    store = tmp_path / "tool-healthy.db"
    open_store(store).stats()             # create the store, no operations

    shape = T.run_write(_tool_args(gh.sha), settings=_settings(store), actor="profile:c10")

    assert shape["outcome"] == "confirmed", shape
    assert shape["second_write"] is True
    assert H.writes(gh.root) == 1
    assert H.content_at(gh.root) == VALUE
    assert open_store(store).get("op-new")["state"] == K.CONFIRMED
