"""C6 — the workflow boundary is enforced in code, not just promised (P2-3).

The skill said "do not use it ... to change a workflow - those calls are
refused", and nothing refused them: a write to ``.github/workflows/test.yml`` in
an allowlisted repository returned ``confirmed`` and performed the write.

Decision (CONCORD-006 §P2-3): ENFORCE. The owner's intent is that agents are
safe on our own infrastructure, and a promise the code does not keep is worse
than no promise - an agent that has been told workflows are refused will not
re-check. Enforcement is a refusal before any record exists, plus a
defence-in-depth check in the adapter so a hand-built key cannot address a
workflow either.

The boundary is deliberately narrow: the ``.github/workflows/`` directory.
Everything else under ``.github/`` (CODEOWNERS, issue templates, dependabot.yml)
stays writable, because those are ordinary single-file text edits.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3

import pytest

import concord_gh_adapter as A
import github_harness as H

TOOL = "concord_github_write"
WORKFLOW = ".github/workflows/test.yml"
CODEOWNERS = ".github/CODEOWNERS"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    repo = H.setup(monkeypatch, tmp_path, files={
        H.PATH: H.VALUE,
        WORKFLOW: "name: test\non: push\n",
        CODEOWNERS: "* @octo\n",
    })
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    return repo


def _sha(root, path: str) -> str:
    return H.repo_state(root)["files"][path]["sha"]


def _call(run, gh, path: str, **over):
    args = {
        "repository": "octo/disposable",
        "path": path,
        "branch": H.BRANCH,
        "operation_id": "op-1",
        "expected_revision": _sha(gh.root, path) if path in H.repo_state(gh.root)["files"]
        else "a" * 40,
        "content": "changed\n",
    }
    args.update(over)
    return json.loads(run(TOOL, args).result)


def _store() -> pathlib.Path:
    return pathlib.Path(os.environ["CONCORD_DB"])


def _rows() -> list:
    if not _store().exists():
        return []
    conn = sqlite3.connect(str(_store()))
    try:
        return conn.execute("SELECT operation_id, state FROM concord_operations").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_a_workflow_file_is_refused_before_anything_is_recorded(run, gh):
    shape = _call(run, gh, WORKFLOW)

    assert shape["outcome"] == "refused", shape
    assert shape["reason"] == "workflow_change_not_supported"
    assert shape["recorded"] is False
    assert not any(c["op"] == "put" for c in H.captures(gh.root)), "nothing may be sent"
    assert H.captures(gh.root) == [], "not even a read: the refusal is local"
    assert _rows() == []
    assert H.content_at(gh.root, WORKFLOW) == "name: test\non: push\n"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/test.yml",
        ".GITHUB/WORKFLOWS/test.yml",
        ".github/Workflows/release/publish.yaml",
    ],
)
def test_the_workflow_boundary_does_not_depend_on_spelling(run, gh, path):
    """Case variants must not walk around the check.

    In git these are different paths, so such a write would 404 rather than
    land - but refusing them is free and keeps the rule one rule.
    """
    shape = _call(run, gh, path, operation_id="op-case")
    assert shape["outcome"] == "refused", shape
    assert shape["reason"] == "workflow_change_not_supported"
    assert H.writes(gh.root) == 0


def test_an_ordinary_dot_github_file_is_still_writable(run, gh):
    """The check must not become "nothing under .github"."""
    shape = _call(run, gh, CODEOWNERS)
    assert shape["outcome"] == "confirmed", shape
    assert H.content_at(gh.root, CODEOWNERS) == "changed\n"


def test_the_adapter_refuses_a_hand_built_workflow_target(gh):
    """Defence in depth: the key parser is not the only guard."""
    target = A.GithubTarget(owner="octo", repo="disposable", branch="main", path=WORKFLOW)
    with pytest.raises(A.AdapterMisconfigured) as excinfo:
        gh.adapter.validate_target(target.key)
    assert excinfo.value.reason == "workflow_change_not_supported"
    with pytest.raises(A.AdapterMisconfigured):
        gh.adapter.write(target, expected_revision=_sha(gh.root, WORKFLOW), value="x\n",
                         operation_id="op-hand")
    assert not any(c["op"] == "put" for c in H.captures(gh.root))


def test_the_skill_and_the_code_now_say_the_same_thing():
    """The promise in the agent-facing skill is the behaviour above."""
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "plugin" / "skills" / "write-file" / "SKILL.md").read_text(encoding="utf-8")
    assert ".github/workflows/" in text, "the skill names the enforced boundary"
    assert "refused" in text
