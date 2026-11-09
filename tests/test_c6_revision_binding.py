"""C6 — content is bound to the revision the caller actually read (review P1-1).

The review's reproduction: agents A and B both prepare an edit from the same
original. A writes first. B then submits content prepared from the original,
and the tool reads a *fresh* base at dispatch, so B's write lands on top of A's
and A's change is destroyed while both report ``confirmed``.

Contract change implemented here, and the reason it is a change rather than a
tweak: the tool now REQUIRES ``expected_revision`` - the blob SHA the caller
read before composing the content. The base is no longer refreshed at dispatch.
A draft prepared from a superseded revision conflicts and requires a fresh
read; it is never silently rebased onto the current blob.

The overlapping intervals are deterministic: the read happens once, both
editors compose from it, and the dispatches run one after the other in this
process. The two-process race in ``test_c4_tool.py`` still covers the other
failure mode (concurrent dispatch of the same object).
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3

import pytest

import github_harness as H

TOOL = "concord_github_write"


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
        "operation_id": "op-a",
        "content": H.VALUE + "A's line\n",
        "expected_revision": over.pop("expected_revision", None),
    }
    args = {k: v for k, v in args.items() if v is not None}
    args.update(over)
    return args


def _call(run, **over):
    return json.loads(run(TOOL, _args(**over)).result)


def _store() -> pathlib.Path:
    return pathlib.Path(os.environ["CONCORD_DB"])


def _row(operation_id):
    path = _store()
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT state, base_revision, fingerprint FROM concord_operations"
            " WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


# ------------------------------------------------- the lost update, reproduced
def test_a_draft_prepared_from_an_older_revision_conflicts_instead_of_overwriting(run, gh):
    """Both editors read the same blob; only the first write may land."""
    observed = gh.sha                       # both A and B read this revision
    original = H.content_at(gh.root)

    first = _call(run, operation_id="op-a", content=original + "A's line\n",
                  expected_revision=observed)
    assert first["outcome"] == "confirmed", first

    second = _call(run, operation_id="op-b", content=original + "B's line\n",
                   expected_revision=observed)
    assert second["outcome"] == "conflict", second
    assert second["state"] == "failed_before_execution"
    assert second["second_write"] is False

    # The decisive assertion: A's edit still exists and B did not overwrite it.
    assert H.content_at(gh.root) == original + "A's line\n"
    assert H.writes(gh.root) == 1
    assert "B's line" not in H.content_at(gh.root)


def test_the_stale_conflict_is_recorded_and_names_a_new_operation(run, gh):
    observed = gh.sha
    _call(run, operation_id="op-a", expected_revision=observed)
    stale = _call(run, operation_id="op-b", expected_revision=observed,
                  content=H.VALUE + "stale draft\n")

    assert stale["recorded"] is True
    assert stale["held"] is False
    assert stale["next_action"]["kind"] == "create-new-reviewed-operation"
    row = _row("op-b")
    assert row is not None and row[0] == "failed_before_execution"


def test_a_draft_bound_to_the_current_revision_is_confirmed(run, gh):
    """Positive control: the same call, with the revision the caller just read."""
    shape = _call(run, operation_id="op-a", expected_revision=H.repo_state(gh.root)["files"][H.PATH]["sha"])
    assert shape["outcome"] == "confirmed"
    assert H.writes(gh.root) == 1


# --------------------------------------------------- the binding is an argument
def test_a_call_without_expected_revision_is_refused_before_any_provider_call(run, gh):
    args = _args(operation_id="op-a", expected_revision=gh.sha)
    del args["expected_revision"]
    shape = json.loads(run(TOOL, args).result)

    assert shape["outcome"] == "refused"
    assert shape["reason"] == "invalid_arguments"
    assert shape["recorded"] is False
    assert H.captures(gh.root) == [], "nothing may reach the provider before validation"
    assert _row("op-a") is None


def test_the_expected_revision_is_part_of_the_operation_identity(run, gh):
    """Same id, same content, different observed base: a different intent."""
    first = _call(run, operation_id="op-a", expected_revision=gh.sha)
    assert first["outcome"] == "confirmed"

    again = _call(run, operation_id="op-a", expected_revision=gh.sha)
    assert again["outcome"] == "replayed", "identical material must still replay"

    rebound = _call(run, operation_id="op-a",
                    expected_revision=H.repo_state(gh.root)["files"][H.PATH]["sha"])
    assert rebound["outcome"] == "conflict"
    assert rebound["state"] == "argument_mismatch"
    assert H.writes(gh.root) == 1
