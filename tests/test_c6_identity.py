"""C6 — one provider object has one identity (review P1-2).

GitHub treats `owner/repo` as case-insensitive, and `refs/heads/main` and `main`
are the same ref. The allowlist already compared owner/repository case-
insensitively, but the SQLite object key preserved the caller's spelling, so
`OCTO/Disposable` was admitted while `octo/disposable` sat `unknown` and held -
two writes to one file, one unresolved hold.

The fix canonicalises the provider identity before validation, fingerprinting,
admission and migration. What is deliberately NOT canonicalised: path case and
branch case, because both are case-sensitive in git. Only the owner/repository
case is folded, and only the `refs/heads/` prefix is stripped from a branch.

CONCORD-007 (review F7-1) made the equivalence hold at *admission*, not only at
migration: the holder check compares canonical identities, so a legacy row the
migration could not rewrite still blocks every alias spelling of its object -
in either settlement order, and after the store is reopened. See
``test_c7_alias_holds``.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

TOOL = "concord_github_write"
CANONICAL = "github:octo/disposable@main:notes/plan.md"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    repo = H.setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    return repo


def _args(**over):
    args = {
        "repository": "octo/disposable",
        "path": H.PATH,
        "branch": H.BRANCH,
        "operation_id": "op-1",
        "expected_revision": "",
        "content": "line one\nline two\n",
    }
    args.update(over)
    return args


def _call(run, **over):
    return json.loads(run(TOOL, _args(**over)).result)


def _store() -> pathlib.Path:
    return pathlib.Path(os.environ["CONCORD_DB"])


def _keys() -> list[str]:
    path = _store()
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT object_key FROM concord_operations")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ------------------------------------------------------- canonical identity
def test_alias_forms_of_one_object_share_one_identity():
    assert A.canonical_key("OCTO", "Disposable", "refs/heads/main", "notes/plan.md") == CANONICAL
    assert A.canonical_key("octo", "disposable", "main", "notes/plan.md") == CANONICAL
    assert A.parse_object_key("github:Octo/Disposable@refs/heads/main:notes/plan.md").key == CANONICAL


def test_path_and_branch_case_are_not_folded():
    """git refs and paths are case-sensitive: folding them would merge two files."""
    assert A.canonical_key("octo", "disposable", "MAIN", "notes/plan.md") == (
        "github:octo/disposable@MAIN:notes/plan.md"
    )
    assert A.canonical_key("octo", "disposable", "main", "Notes/Plan.md") == (
        "github:octo/disposable@main:Notes/Plan.md"
    )


def test_the_tool_stores_the_canonical_key(run, gh):
    shape = _call(run, repository="OCTO/Disposable", branch="refs/heads/main",
                  expected_revision=gh.sha)
    assert shape["outcome"] == "confirmed", shape
    assert shape["object_key"] == CANONICAL
    assert _keys() == [CANONICAL]


# ------------------------------------------------------ the hold is not bypassed
def test_a_repository_alias_cannot_bypass_an_unresolved_hold(run, gh, monkeypatch):
    H.set_config(fixture="write_ambiguous")
    first = _call(run, repository="octo/disposable", operation_id="op-held",
                  expected_revision=gh.sha)
    assert first["outcome"] == "unknown", first
    writes_after_ambiguous = H.writes(gh.root)

    H.set_config(fixture="write_ok")
    alias = _call(run, repository="OCTO/Disposable", operation_id="op-alias",
                  expected_revision=gh.sha)

    assert alias["outcome"] == "conflict", alias
    assert alias["holder"] == "op-held"
    assert alias["second_write"] is False
    assert H.writes(gh.root) == writes_after_ambiguous, "the alias must not write a second time"
    assert set(_keys()) == {CANONICAL}, "both intents address one canonical object"


@pytest.mark.parametrize("state", ["reserved", "pending"])
def test_an_alias_cannot_bypass_a_reserved_or_pending_operation(run, gh, tmp_path, state):
    store = _store()
    coordinator = K.Coordinator(str(store))
    coordinator.admit(
        object_key=CANONICAL, operation_id="op-early", fingerprint="fp",
        base_revision=gh.sha, actor="profile:test",
    )
    if state == "pending":
        coordinator.begin_dispatch("op-early")

    shape = _call(run, repository="Octo/DisPOSABLE", operation_id="op-later",
                  expected_revision=gh.sha)

    assert shape["outcome"] == "conflict", shape
    assert shape["holder"] == "op-early"
    assert shape["second_write"] is False
    # The holder's own state is untouched: the alias was refused, not merged in.
    assert coordinator.get("op-early")["state"] == state
    assert H.writes(gh.root) == 0


# ---------------------------------------------------------------- migration
def _legacy_store(tmp_path, *, keys):
    store = tmp_path / "legacy.db"
    coordinator = K.Coordinator(str(store))
    for index, key in enumerate(keys):
        coordinator.admit(
            object_key=key, operation_id=f"op-legacy-{index}", fingerprint=f"fp-{index}",
            base_revision="a" * 40, actor="profile:old",
        )
        coordinator.begin_dispatch(f"op-legacy-{index}")
    return store


def test_a_legacy_key_is_migrated_and_its_hold_preserved(tmp_path):
    store = _legacy_store(tmp_path, keys=["github:OCTO/Disposable@main:notes/plan.md"])

    migrated = K.Coordinator(str(store), normalize_object_key=A.canonical_object_key)

    row = migrated.get("op-legacy-0")
    assert row["object_key"] == CANONICAL
    assert row["state"] == K.PENDING
    assert migrated.holder_of(CANONICAL) == "op-legacy-0", (
        "the unresolved hold must survive the identity migration"
    )
    # Idempotent: opening again changes nothing and still reports the hold.
    again = K.Coordinator(str(store), normalize_object_key=A.canonical_object_key)
    assert again.get("op-legacy-0")["object_key"] == CANONICAL
    assert again.holder_of(CANONICAL) == "op-legacy-0"


def test_migration_never_destroys_a_hold_when_two_keys_collide(tmp_path):
    """Two unresolved rows, two spellings, one object: both records survive.

    Collapsing them is not an option - the partial unique index allows one
    unresolved row per object, and silently picking a winner would drop evidence.
    The migration leaves the second row as it is and records why.
    """
    store = _legacy_store(
        tmp_path,
        keys=["github:octo/disposable@main:notes/plan.md",
              "github:OCTO/Disposable@main:notes/plan.md"],
    )

    migrated = K.Coordinator(str(store), normalize_object_key=A.canonical_object_key)

    rows = {row["operation_id"]: row for row in
            (migrated.get("op-legacy-0"), migrated.get("op-legacy-1"))}
    assert len(rows) == 2
    assert all(row["state"] == K.PENDING for row in rows.values()), "no unresolved row is dropped"
    assert migrated.holder_of(CANONICAL) in {"op-legacy-0", "op-legacy-1"}
    collided = [row for row in rows.values() if row["object_key"] != CANONICAL]
    assert collided, "the colliding row keeps its own key rather than being merged away"
    assert "canonical" in (collided[0]["note"] or "").lower()
