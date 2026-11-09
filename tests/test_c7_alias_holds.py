"""C7 — every alias spelling of one provider object is one set of holds (F7-1).

The defect (review F7-1): a store written before the identity fix can hold two
unresolved rows on one repository under two spellings. On reopen the migration
canonicalises the first row and *skips* the second, because the partial unique
index allows one unresolved row per ``object_key``; the second keeps its alias
spelling. Admission looked the canonical key up, found no holder there, and
admitted a new write while the alias row was still pending - one object, two
holds, one of them invisible.

What "legacy" means in the reproducing path, concretely: the store was written
by the code at ``023c403`` and therefore has

  * ``object_key`` exactly as the caller spelled it (no owner/repository case
    folding, no ``refs/heads/`` stripping) - the identity fix did not exist;
  * no ``base_identity`` column and no ``concord_migrations`` table - both were
    added by the fix round, so a real legacy file has neither, and the
    migration runs on first open with the current code.

The fix keeps the equivalence at *admission* time: the holder check compares
canonical identities, so a row in either spelling blocks every alias of that
object, in either settlement order, and after every reopen. Migration still
rewrites what it can; the rewrite is a convenience, not the guard.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

CANONICAL = "github:octo/disposable@main:notes/plan.md"
ALIAS = "github:OCTO/Disposable@main:notes/plan.md"

#: The schema as of 023c403 - the last commit before the identity fix. Quoted
#: from `git show 023c403:plugin/concord_kernel.py`.
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


def legacy_store(path: pathlib.Path, keys: list[str]) -> pathlib.Path:
    """A hand-built pre-fix store: one dispatched-and-uncertain hold per key."""
    path = pathlib.Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO concord_counters (name, value) VALUES ('seq', ?)", (len(keys),))
        for index, key in enumerate(keys, start=1):
            conn.execute(
                "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
                " base_revision, actor, state, holder, attempt_count, receipt, note,"
                " note_chars, created_seq, updated_seq, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'unknown', NULL, 1, NULL, NULL, NULL, ?, ?, ?, ?)",
                (f"op-legacy-{index}", key, f"fp-{index}", "a" * 40, "profile:old",
                 index, index, 1700000000.0 + index, 1700000000.0 + index),
            )
            conn.execute(
                "INSERT INTO concord_attempts (attempt_id, operation_id, ordinal, state,"
                " detail, started_seq, settled_seq, started_at, settled_at)"
                " VALUES (?, ?, 1, 'unknown', 'the response was lost', ?, ?, ?, ?)",
                (f"op-legacy-{index}#1", f"op-legacy-{index}", index, index,
                 1700000000.0 + index, 1700000000.0 + index),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def open_store(store: pathlib.Path) -> K.Coordinator:
    """Open with the current code, which is also where the migration runs."""
    return K.Coordinator(str(store), normalize_object_key=A.canonical_object_key)


def settle(coord: K.Coordinator, operation_id: str, *, state: str = K.CONFIRMED) -> dict:
    """Resolve one hold from fixed evidence (the provider read is exercised above)."""
    def evaluate(**_kwargs):
        return K.EvidenceVerdict(state, "c7: fixed evidence")

    return coord.reconcile(operation_id, {"revision": "a" * 40, "source": "c7"},
                           evaluate=evaluate)


# ---------------------------------------------------- both settlement orders
@pytest.mark.parametrize("label,first_settled", [("canonical", CANONICAL), ("alias", ALIAS)])
def test_a_second_intent_is_refused_after_either_settlement_order(tmp_path, label, first_settled):
    """The acceptance test the owner named: A first, then B - and B first, then A."""
    keys = [CANONICAL, ALIAS] if first_settled == CANONICAL else [ALIAS, CANONICAL]
    store = legacy_store(tmp_path / f"legacy-{label}.db", keys)

    coord = open_store(store)
    spellings = {op: coord.get(op)["object_key"] for op in ("op-legacy-1", "op-legacy-2")}
    assert set(spellings.values()) == {CANONICAL, ALIAS}, (
        "the reproduction premise: one hold per spelling, both unresolved"
    )

    # The settled row is whichever spelling comes first here.
    settle(coord, "op-legacy-1")

    # Reopen before the admission attempt: persistence is part of the finding.
    coord = open_store(store)
    survivor_key = coord.get("op-legacy-2")["object_key"]
    assert coord.holder_of(CANONICAL) == "op-legacy-2", (
        "the surviving hold must be visible under the canonical identity"
    )
    assert coord.holder_of(survivor_key) == "op-legacy-2"

    # Ask through the OTHER spelling - the one the surviving row is not stored under.
    request_key = ALIAS if survivor_key == CANONICAL else CANONICAL
    refused = coord.admit(
        object_key=request_key, operation_id="op-new-intent", fingerprint="fp-new",
        base_revision="a" * 40, actor="profile:new",
    )
    assert refused.kind == K.CONFLICT, refused.as_dict()
    assert refused.holder == "op-legacy-2", (
        "an alias spelling must name the surviving holder, not admit a second write"
    )

    # The object is only free once BOTH holds are resolved.
    settle(coord, "op-legacy-2")
    coord = open_store(store)
    assert coord.holder_of(CANONICAL) is None
    admitted = coord.admit(
        object_key=CANONICAL, operation_id="op-after-both", fingerprint="fp-after",
        base_revision="a" * 40, actor="profile:new",
    )
    assert admitted.kind == K.ADMITTED, admitted.as_dict()


def test_the_alias_spelling_is_refused_after_the_migration_too(tmp_path):
    """The direction the pre-fix code did get right: keep it working."""
    store = legacy_store(tmp_path / "legacy-reverse.db", [CANONICAL, ALIAS])
    coord = open_store(store)
    assert coord.holder_of(ALIAS) == "op-legacy-2"
    refused = coord.admit(object_key=ALIAS, operation_id="op-new", fingerprint="fp",
                          base_revision="a" * 40, actor="profile:new")
    assert refused.kind == K.CONFLICT
    assert refused.holder == "op-legacy-2"


def test_the_collision_is_still_recorded_as_evidence(tmp_path):
    """Enforcement must not become erasure: both rows and the note survive."""
    store = legacy_store(tmp_path / "legacy-note.db", [CANONICAL, ALIAS])
    coord = open_store(store)

    rows = [coord.get("op-legacy-1"), coord.get("op-legacy-2")]
    assert all(row["state"] == K.UNKNOWN for row in rows), "no unresolved row is dropped"
    collided = [row for row in rows if row["object_key"] != CANONICAL]
    assert collided, "the colliding row keeps its own key rather than being merged away"
    note = (collided[0]["note"] or "").lower()
    assert "canonical" in note
    assert "one provider object" in note, (
        "the note must say that both spellings are held as one object"
    )


# -------------------------------------------------- and through the real tool
def test_no_provider_write_lands_while_an_alias_hold_survives(tmp_path, monkeypatch):
    """End to end: the refusal happens before any request reaches the provider."""
    gh = H.setup(monkeypatch, tmp_path)
    store = legacy_store(tmp_path / "legacy-tool.db", [CANONICAL, ALIAS])
    coord = open_store(store)
    settle(coord, "op-legacy-1")
    coord = open_store(store)

    shape = A.guarded_write(
        coord, gh.adapter,
        object_key=CANONICAL, operation_id="op-new-intent",
        value="a second write on a held object\n",
        expected_revision=gh.sha, actor="c7",
    )

    assert shape["status"] == K.CONFLICT, shape
    assert shape["holder"] == "op-legacy-2"
    assert shape.get("second_write") is False
    assert H.writes(gh.root) == 0, "no request may reach the provider"
    assert H.puts(gh.root) == 0
