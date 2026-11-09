"""C8 — an existing store is validated BEFORE any schema work or migration (F8-A).

The defect (owner F8-A, promoted from residual R1): ``Coordinator._init`` ran
``CREATE TABLE IF NOT EXISTS`` unconditionally, so opening a store whose
``concord_operations`` table was gone recreated it empty. The surviving attempts
were orphaned, the evidence that records ever existed was destroyed, and the very
write that caused the damage was admitted - a silent repair indistinguishable
from a healthy store.

The contract after this fix:

  * **no file, or an empty file** - no history exists, so the schema is created:
    legitimate fresh creation, still healthy;
  * **file exists and a required table, required column or admission-guard index
    is missing** - refuse mutation and admission with a structured error
    (``reason: partial_schema``), changing no bytes;
  * **file exists with every table while attempts name operations that have no
    record, or the durable sequence counter does not cover the rows** - refuse
    with ``reason: damaged_history``;
  * **an older-but-complete store** (no ``base_identity``, no
    ``fingerprint_object``, no migration ledger) - valid, and brought up to date
    additively on open. An older schema is not damage.

Every refusal is also a reopen test: the verdict has to be a property of the
file, not of one connection, so each case opens the store a second time.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

import concord_kernel as K
import concord_status as ST

OBJECT = "github:octo/disposable@main:notes/plan.md"
ALIAS = "github:OCTO/Disposable@main:notes/plan.md"

#: The schema as of 023c403 - the genuine shape of an older store: no
#: ``base_identity``, no ``fingerprint_object``, no ``concord_migrations``.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS concord_counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS concord_operations (
    operation_id  TEXT PRIMARY KEY,
    object_key    TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    actor         TEXT NOT NULL,
    state         TEXT NOT NULL,
    holder        TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    receipt       TEXT,
    note          TEXT,
    note_chars    INTEGER,
    pruned_at     REAL,
    created_seq   INTEGER NOT NULL,
    updated_seq   INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concord_active_object
    ON concord_operations(object_key)
    WHERE state IN ('reserved', 'pending', 'unknown');
CREATE INDEX IF NOT EXISTS ix_concord_object_seq
    ON concord_operations(object_key, created_seq);
CREATE INDEX IF NOT EXISTS ix_concord_state
    ON concord_operations(state);
CREATE TABLE IF NOT EXISTS concord_attempts (
    attempt_id   TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    state        TEXT NOT NULL,
    detail       TEXT,
    started_seq  INTEGER NOT NULL,
    settled_seq  INTEGER,
    started_at   REAL NOT NULL,
    settled_at   REAL,
    UNIQUE(operation_id, ordinal)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concord_open_attempt
    ON concord_attempts(operation_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ix_concord_attempt_op
    ON concord_attempts(operation_id, ordinal);
"""


# ------------------------------------------------------------------ helpers
def healthy_store(path: pathlib.Path) -> pathlib.Path:
    """A real store with one dispatched-and-lost operation and its attempt."""
    coord = K.Coordinator(str(path))
    coord.admit(object_key=OBJECT, operation_id="op-lost", fingerprint="fp-lost",
                base_revision="a" * 40, actor="profile:test")
    coord.begin_dispatch("op-lost")
    coord.mark_unknown("op-lost", "the response was lost")
    return path


def legacy_store(path: pathlib.Path) -> pathlib.Path:
    """A complete, older-schema store: one unresolved legacy hold under the alias."""
    path = pathlib.Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO concord_counters (name, value) VALUES ('seq', 1)")
        conn.execute(
            "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
            " base_revision, actor, state, holder, attempt_count, receipt, note,"
            " note_chars, created_seq, updated_seq, created_at, updated_at)"
            " VALUES ('op-legacy', ?, 'fp-legacy', ?, 'profile:old', 'unknown', NULL, 1,"
            " NULL, NULL, NULL, 1, 1, 1700000000.0, 1700000000.0)",
            (ALIAS, "a" * 40),
        )
        conn.execute(
            "INSERT INTO concord_attempts (attempt_id, operation_id, ordinal, state, detail,"
            " started_seq, settled_seq, started_at, settled_at)"
            " VALUES ('op-legacy#1', 'op-legacy', 1, 'unknown', 'the response was lost',"
            " 1, 1, 1700000000.0, 1700000000.0)",
        )
        conn.commit()
    finally:
        conn.close()
    return path


def destroy(path: pathlib.Path, statement: str) -> None:
    """Apply one destructive statement out of band, as damage does."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def schema(path: pathlib.Path) -> dict:
    raw = pathlib.Path(path).read_bytes()
    conn = sqlite3.connect(str(path))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = {}
        for table in sorted(tables):
            columns[table] = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        attempts = [tuple(row) for row in conn.execute(
            "SELECT operation_id, state FROM concord_attempts ORDER BY attempt_id"
        )] if "concord_attempts" in tables else []
        counters = {row[0]: row[1] for row in conn.execute(
            "SELECT name, value FROM concord_counters"
        )} if "concord_counters" in tables else {}
    finally:
        conn.close()
    import hashlib

    return {"sha256": hashlib.sha256(raw).hexdigest(), "tables": tables,
            "columns": columns, "attempts": attempts, "counters": counters}


# ---------------------------------------- (a) damage refuses, and stays refused
def test_a_store_missing_operations_refuses_and_is_never_rebuilt(tmp_path):
    store = healthy_store(tmp_path / "partial.db")
    destroy(store, "DROP TABLE concord_operations")
    damaged = schema(store)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "partial_schema", raised.value.reason
    detail = str(raised.value)
    assert "concord_operations" in detail and "is missing" in detail
    assert "concord_attempts still holds 1 row(s)" in detail, detail
    assert "not an empty store" in detail
    assert "nothing will be rebuilt" in detail

    after = schema(store)
    assert "concord_operations" not in after["tables"], "the missing table was recreated"
    assert after["sha256"] == damaged["sha256"], "a refused open must not change the file"
    assert after["attempts"] == damaged["attempts"]
    assert after["counters"] == damaged["counters"]

    # Reopen: the verdict is a property of the file, not of one connection.
    with pytest.raises(K.CoordinatorUnavailable) as second:
        K.Coordinator(str(store))
    assert second.value.reason == "partial_schema"


def test_a_store_missing_attempts_refuses_and_is_never_rebuilt(tmp_path):
    store = healthy_store(tmp_path / "no-attempts.db")
    destroy(store, "DROP TABLE concord_attempts")
    damaged = schema(store)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "partial_schema"
    assert "concord_attempts" in str(raised.value)
    assert schema(store)["sha256"] == damaged["sha256"]

    with pytest.raises(K.CoordinatorUnavailable):
        K.Coordinator(str(store))


def test_a_missing_required_column_is_the_same_refusal(tmp_path):
    """Dropping a column is damage too, not a shorter but writable store."""
    store = healthy_store(tmp_path / "no-column.db")
    destroy(store, "ALTER TABLE concord_operations DROP COLUMN receipt")

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "partial_schema"
    assert "receipt" in str(raised.value)


def test_a_missing_admission_guard_index_is_damage(tmp_path):
    """The partial unique index *is* the admission guard; it is not recreated."""
    store = healthy_store(tmp_path / "no-index.db")
    destroy(store, "DROP INDEX ux_concord_active_object")

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "partial_schema"
    assert "ux_concord_active_object" in str(raised.value)


def test_an_orphaned_unknown_attempt_is_damaged_history_not_emptiness(tmp_path):
    """The operations table is present; one record is gone while its attempt stays."""
    store = healthy_store(tmp_path / "orphan.db")
    destroy(store, "DELETE FROM concord_operations")
    damaged = schema(store)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "damaged_history", raised.value.reason
    detail = str(raised.value)
    assert "op-lost" in detail and "no record" in detail
    assert "not an empty store" in detail
    assert schema(store)["attempts"] == damaged["attempts"]
    assert schema(store)["sha256"] == damaged["sha256"]

    with pytest.raises(K.CoordinatorUnavailable) as second:
        K.Coordinator(str(store))
    assert second.value.reason == "damaged_history"


def test_a_sequence_counter_behind_the_records_is_damaged_history(tmp_path):
    """The durable counter is history: a counter behind the rows would reuse ordinals."""
    store = healthy_store(tmp_path / "stale-counter.db")
    destroy(store, "DELETE FROM concord_counters")

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store))

    assert raised.value.reason == "damaged_history"
    assert "sequence counter" in str(raised.value)


# --------------------------------- (b) no file / empty file are still legitimate
def test_a_new_path_still_creates_a_healthy_store(tmp_path):
    store = tmp_path / "brand-new.db"
    coord = K.Coordinator(str(store))
    admission = coord.admit(object_key=OBJECT, operation_id="op-1", fingerprint="fp",
                            base_revision="a" * 40, actor="profile:test")
    assert admission.kind == K.ADMITTED, admission.as_dict()

    snap = ST.read_snapshot(store)
    assert snap.healthy is True
    assert snap.initialized is True
    assert [row["operation_id"] for row in snap.rows] == ["op-1"]


def test_an_empty_database_file_is_legitimate_fresh_creation(tmp_path):
    """A zero-length file holds no rows, so there is no history to destroy."""
    store = tmp_path / "empty-file.db"
    sqlite3.connect(str(store)).close()
    assert store.stat().st_size == 0

    coord = K.Coordinator(str(store))
    admission = coord.admit(object_key=OBJECT, operation_id="op-1", fingerprint="fp",
                            base_revision="a" * 40, actor="profile:test")
    assert admission.kind == K.ADMITTED
    assert ST.read_snapshot(store).healthy is True


def test_a_database_file_with_no_concord_schema_gets_the_schema_created(tmp_path):
    """A file that exists but holds other tables is not damaged Concord history.

    This is also what a second process sees while the first one is creating the
    schema, so the creation has to be safe: nothing of ours is in the file, so the
    schema is created and the foreign table is left alone.
    """
    store = tmp_path / "someone-elses.db"
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO notes (body) VALUES ('not ours')")
        conn.commit()
    finally:
        conn.close()

    coord = K.Coordinator(str(store))
    admission = coord.admit(object_key=OBJECT, operation_id="op-1", fingerprint="fp",
                            base_revision="a" * 40, actor="profile:test")
    assert admission.kind == K.ADMITTED

    conn = sqlite3.connect(str(store))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"concord_operations", "concord_attempts", "concord_counters"} <= tables
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "not ours"
    finally:
        conn.close()


# ------------------------------------ (c) an older schema is migrated, not refused
def test_an_older_store_is_validated_then_migrated_additively(tmp_path):
    store = legacy_store(tmp_path / "legacy.db")
    before = schema(store)
    assert "base_identity" not in before["columns"]["concord_operations"]
    assert "concord_migrations" not in before["tables"]

    coord = K.Coordinator(str(store), normalize_object_key=lambda key: OBJECT)

    after = schema(store)
    assert "base_identity" in after["columns"]["concord_operations"]
    assert "fingerprint_object" in after["columns"]["concord_operations"]
    assert "concord_migrations" in after["tables"]
    row = coord.get("op-legacy")
    assert row["state"] == K.UNKNOWN, "the legacy hold survives the migration"
    assert row["fingerprint"] == "fp-legacy"
    assert row["object_key"] == OBJECT
    assert row["fingerprint_object"] == ALIAS, (
        "the spelling the fingerprint was computed over is preserved beside the lock key"
    )


def test_an_older_store_that_is_damaged_is_refused_before_any_migration(tmp_path):
    """A migration must not run on a damaged base - and must not invent history."""
    store = legacy_store(tmp_path / "legacy-damaged.db")
    destroy(store, "DROP TABLE concord_operations")
    damaged = schema(store)

    with pytest.raises(K.CoordinatorUnavailable) as raised:
        K.Coordinator(str(store), normalize_object_key=lambda key: OBJECT)

    assert raised.value.reason == "partial_schema"
    after = schema(store)
    assert "concord_migrations" not in after["tables"], "a migration ledger was created anyway"
    assert after["sha256"] == damaged["sha256"]


# ---------------------------------- (d) the write path, not just the constructor
def test_the_write_path_refusal_is_structured_for_callers(tmp_path):
    """``K.refusal`` is what a tool body returns; it must carry the damage class."""
    store = healthy_store(tmp_path / "write-path.db")
    destroy(store, "DROP TABLE concord_operations")

    try:
        K.Coordinator(str(store))
    except K.CoordinatorUnavailable as exc:
        shape = K.refusal(exc)
    else:  # pragma: no cover - the defect would be this branch
        pytest.fail("a damaged store was opened for write")

    assert shape["status"] == "refused"
    assert shape["reason"] == "partial_schema"
    assert "concord_operations" in shape["detail"]
