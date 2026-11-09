"""C8 — the ORIGINAL fingerprint identity survives the canonical lock rewrite (F8-C).

``fingerprint`` is ``sha256`` over ``{"object": <key as spelled>, "value": ...,
"expected_revision": ...}``. The store written before the identity fix (023c403)
holds keys exactly as the caller spelled them, so its digests are digests of those
spellings. The legacy-alias migration then rewrote ``object_key`` to the canonical
lock identity - which silently invalidated every one of those digests:

  * a replay of an already-confirmed operation was refused as
    ``rejected_argument_mismatch`` ("already recorded with different material");
  * reconciliation could no longer attribute the operation's *own* value, so a
    hold whose write had landed stayed unknown forever.

The fix records the spelling each digest was derived from in
``fingerprint_object``, beside the canonical lock identity and never overwritten,
and uses it for exactly two decisions: replay equivalence at admission, and the
content fingerprint in the evidence evaluator.

The fingerprints below are computed over the same material the 023c403 tool
handed the kernel (``object_key = f"github:{repository}@{branch}:{path}"`` from
the repository string as typed). ``concord_kernel.material_from_arguments`` and
``concord_kernel.fingerprint`` are byte-identical between 023c403 and this tree;
``evidence/concord-008/repro_f8_c_legacy_fingerprint_identity.py`` derives the
same strings by importing the 023c403 source itself.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import concord_store as S
import concord_tool as T
import github_harness as H

CANONICAL = "github:octo/disposable@main:notes/plan.md"
#: What the pre-canonical tool built from repository="OCTO/Disposable".
ALIAS = "github:OCTO/Disposable@main:notes/plan.md"

CONFIRMED_VALUE = "the confirmed legacy edit\n"
UNCERTAIN_VALUE = "the uncertain legacy edit\n"

#: The schema as of 023c403: no ``base_identity``, no ``fingerprint_object``,
#: no ``concord_migrations``.
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


def legacy_fingerprint(object_key: str, value: str, base_revision: str) -> str:
    """The digest the 023c403 code path produced for this material."""
    return K.fingerprint(
        {"object": object_key, "value": value, "expected_revision": base_revision}
    )


def legacy_store(path: pathlib.Path, base_revision: str) -> pathlib.Path:
    """One confirmed and one unresolved operation, both under the legacy spelling."""
    path = pathlib.Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO concord_counters (name, value) VALUES ('seq', 2)")
        receipt = {"blob_sha": "b" * 40, "commit_sha": "c" * 40,
                   "operation_id": "op-replay", "source": "legacy"}
        conn.execute(
            "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
            " base_revision, actor, state, holder, attempt_count, receipt, note,"
            " note_chars, created_seq, updated_seq, created_at, updated_at)"
            " VALUES ('op-replay', ?, ?, ?, 'profile:old', 'confirmed', NULL, 1, ?,"
            " NULL, NULL, 1, 1, 1700000000.0, 1700000000.0)",
            (ALIAS, legacy_fingerprint(ALIAS, CONFIRMED_VALUE, base_revision),
             base_revision, json.dumps(receipt)),
        )
        conn.execute(
            "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
            " base_revision, actor, state, holder, attempt_count, receipt, note,"
            " note_chars, created_seq, updated_seq, created_at, updated_at)"
            " VALUES ('op-unknown', ?, ?, ?, 'profile:old', 'unknown', NULL, 1, NULL,"
            " NULL, NULL, 2, 2, 1700000001.0, 1700000001.0)",
            (ALIAS, legacy_fingerprint(ALIAS, UNCERTAIN_VALUE, base_revision), base_revision),
        )
        for index, (op, state, ordinal) in enumerate(
                (("op-replay", "confirmed", 1), ("op-unknown", "unknown", 1)), start=1):
            conn.execute(
                "INSERT INTO concord_attempts (attempt_id, operation_id, ordinal, state,"
                " detail, started_seq, settled_seq, started_at, settled_at)"
                " VALUES (?, ?, ?, ?, 'legacy attempt', ?, ?, ?, ?)",
                (f"{op}#{ordinal}", op, ordinal, state, index, index,
                 1700000000.0 + index, 1700000000.0 + index),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def open_store(store: pathlib.Path) -> K.Coordinator:
    """Open the way the tool and the CLI do - which is where the migration runs."""
    return K.Coordinator(str(store), normalize_object_key=A.canonical_object_key)


def _never_dispatch():
    raise AssertionError("a recorded operation must not dispatch a write")


def _settings(store: pathlib.Path) -> S.Settings:
    return S.Settings(
        allowed_repositories=(("octo", "disposable"),),
        gh_command=(sys.executable, str(H.FAKEGH)),
        timeout_s=30.0,
        store_path=pathlib.Path(store),
    )


# ------------------------------------------------------------------ identity
def test_the_original_fingerprint_identity_is_kept_beside_the_canonical_key(tmp_path):
    base = "a" * 40
    store = legacy_store(tmp_path / "legacy.db", base)
    stored_digest = legacy_fingerprint(ALIAS, CONFIRMED_VALUE, base)

    coord = open_store(store)

    row = coord.get("op-replay")
    assert row["object_key"] == CANONICAL, "the lock identity is canonical"
    assert row["fingerprint"] == stored_digest, "the digest is never rewritten"
    assert row["fingerprint_object"] == ALIAS, (
        "the spelling the digest was computed over is preserved as its own identity"
    )
    assert row["fingerprint_object"] != row["object_key"]


def test_a_confirmed_legacy_operation_replays_after_the_migration(tmp_path):
    base = "a" * 40
    store = legacy_store(tmp_path / "replay.db", base)
    material = {"object": CANONICAL, "value": CONFIRMED_VALUE, "expected_revision": base}

    coord = open_store(store)
    first = coord.run(operation_id="op-replay", actor="c8", received_args=dict(material),
                      revision_model="opaque", mutate=_never_dispatch)

    assert first["status"] == "replayed", first
    assert first["second_write"] is False
    assert first["receipt"]["commit_sha"] == "c" * 40

    # Reopen: the replay answer is a property of the store, not of one connection.
    reopened = open_store(store)
    second = reopened.run(operation_id="op-replay", actor="c8",
                          received_args=dict(material), revision_model="opaque",
                          mutate=_never_dispatch)
    assert second["status"] == "replayed", second
    assert second["receipt"] == first["receipt"]


def test_the_real_tool_replays_a_legacy_operation_without_a_second_write(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    store = legacy_store(tmp_path / "tool-replay.db", gh.sha)

    shape = T.run_write(
        {
            "repository": "OCTO/Disposable",
            "path": "notes/plan.md",
            "branch": "main",
            "operation_id": "op-replay",
            "expected_revision": gh.sha,
            "content": CONFIRMED_VALUE,
        },
        settings=_settings(store),
        actor="profile:c8",
    )

    assert shape["outcome"] == "replayed", shape
    assert shape["second_write"] is False
    assert shape["receipt"]["commit_sha"] == "c" * 40
    assert H.writes(gh.root) == 0, "a replay must not touch the provider"
    assert H.puts(gh.root) == 0


# --------------------------------------------------------- reconciliation
def test_reconciliation_attributes_the_legacy_operations_own_write(tmp_path, monkeypatch):
    """A write that landed under the legacy digest is still attributable."""
    gh = H.setup(monkeypatch, tmp_path)
    store = legacy_store(tmp_path / "reconcile.db", gh.sha)
    target = gh.adapter.validate_target(CANONICAL)
    # The provider-side event: the operation's own content, attributed to it.
    gh.adapter.write(target, expected_revision=gh.sha, value=UNCERTAIN_VALUE,
                     operation_id="op-unknown", summary="the uncertain edit")
    evidence = gh.adapter.read(target)

    coord = open_store(store)
    settled = coord.reconcile("op-unknown", evidence, evaluate=A.opaque_evidence_verdict,
                              require_release_proof=True)

    assert settled["state"] == K.CONFIRMED, coord.get("op-unknown")["note"]
    assert settled["evidence"]["commit_sha"] == evidence["commit_sha"]
    assert coord.holder_of(CANONICAL) is None, "a confirmed operation releases the object"

    reopened = open_store(store)
    row = reopened.get("op-unknown")
    assert row["state"] == K.CONFIRMED
    assert row["receipt"]["reconciled"] is True


def test_a_different_value_under_a_legacy_record_is_still_refused(tmp_path):
    """The equivalence is exact: a different intent under the same id still fails."""
    base = "a" * 40
    store = legacy_store(tmp_path / "mismatch.db", base)
    coord = open_store(store)

    shape = coord.run(
        operation_id="op-replay",
        actor="c8",
        received_args={"object": CANONICAL, "value": "someone else's edit\n",
                       "expected_revision": base},
        revision_model="opaque",
        mutate=_never_dispatch,
    )

    assert shape["status"] == "refused", shape
    assert shape["reason"] == "argument_mismatch"


# ------------------------------------------------------------ equivalence
def test_the_alias_spelling_after_the_migration_is_the_same_lock_identity(tmp_path):
    """Both identities address one provider object, before and after settling."""
    base = "a" * 40
    store = legacy_store(tmp_path / "lock.db", base)

    coord = open_store(store)
    refused = coord.admit(object_key=ALIAS, operation_id="op-alias-intent",
                          fingerprint="fp-alias", base_revision=base, actor="profile:c8")
    assert refused.kind == K.CONFLICT, refused.as_dict()
    assert refused.holder == "op-unknown"
    assert coord.holder_of(ALIAS) == "op-unknown"
    assert coord.holder_of(CANONICAL) == "op-unknown"

    # Settle the legacy hold from fixed evidence, reopen, then the canonical
    # spelling is free while the alias still resolves to the same object.
    def evaluate(**_kwargs):
        return K.EvidenceVerdict(K.CONFIRMED, "c8: fixed evidence")

    coord.reconcile("op-unknown", {"revision": base, "source": "c8"}, evaluate=evaluate)
    reopened = open_store(store)
    assert reopened.holder_of(CANONICAL) is None
    admitted = reopened.admit(object_key=CANONICAL, operation_id="op-after",
                              fingerprint="fp-after", base_revision=base, actor="profile:c8")
    assert admitted.kind == K.ADMITTED, admitted.as_dict()
