"""C8 — CLI reconcile, then a NEW write, with an ORPHANED UNKNOWN attempt (F8-B).

The scenario the owner named: a dispatched write whose operations record is gone
while its attempts row still says ``unknown`` - an orphaned UNKNOWN. Then, through
the real CLI, ``hermes concord reconcile <op>``; then a NEW write to the same
repository/path.

What the pre-fix tree did (evidence/concord-008/red-f8-b.txt): the reconcile's
``Coordinator`` open recreated the missing table, reconcile answered "no operation
'op-lost' in ..." - the same answer a store that never recorded anything gives -
and the new write was admitted and dispatched to the provider. The orphaned
attempt was quietly joined by a second attempt row.

The contract after the fix:

  * reconcile refuses with the damage named (never "no such operation", never
    healthy-empty), exit nonzero, and the file is byte-identical afterwards;
  * the new write is refused before any provider call;
  * the surviving attempt rows are untouched;
  * all of it survives closing and reopening the store.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

import concord_kernel as K
import concord_status as ST
import concord_store as S
import concord_tool as T
import github_harness as H

ROOT = pathlib.Path(__file__).resolve().parents[2]          # .../Concord-for-Hermes
HOME = ROOT / "isolated-home"
OBJECT = "github:octo/disposable@main:notes/plan.md"


def _find_cli() -> pathlib.Path | None:
    override = (os.environ.get("HERMES_BIN") or "").strip()
    if override and pathlib.Path(override).exists():
        return pathlib.Path(override)
    for candidate in (ROOT / ".venv" / "Scripts" / "hermes.exe",
                      ROOT / ".venv" / "bin" / "hermes"):
        if candidate.exists():
            return candidate
    found = shutil.which("hermes")
    return pathlib.Path(found) if found else None


CLI = _find_cli()
pytestmark = pytest.mark.skipif(CLI is None, reason="no hermes CLI found (set HERMES_BIN)")


def _cli(args, *, store, tmp_path, timeout=180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["CONCORD_DB"] = str(store)
    return subprocess.run([str(CLI), *args], capture_output=True, text=True, env=env,
                          cwd=str(tmp_path), timeout=timeout)


def _lost_store(path: pathlib.Path) -> pathlib.Path:
    """A real store: one dispatched write whose outcome was never recorded."""
    coord = K.Coordinator(str(path))
    coord.admit(object_key=OBJECT, operation_id="op-lost", fingerprint="fp-lost",
                base_revision="a" * 40, actor="profile:test")
    coord.begin_dispatch("op-lost")
    coord.mark_unknown("op-lost", "the response was lost")
    return path


def _drop_operations(store: pathlib.Path) -> None:
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("DROP TABLE concord_operations")
        conn.commit()
    finally:
        conn.close()


def _attempts(store: pathlib.Path) -> list[tuple]:
    conn = sqlite3.connect(str(store))
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT attempt_id, operation_id, ordinal, state, detail FROM concord_attempts"
            " ORDER BY attempt_id")]
    finally:
        conn.close()


def _tables(store: pathlib.Path) -> set[str]:
    conn = sqlite3.connect(str(store))
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _write_tool(store: pathlib.Path, gh) -> dict:
    """The real tool body, pointed at the fake provider and this store."""
    settings = S.Settings(
        allowed_repositories=(("octo", "disposable"),),
        gh_command=(sys.executable, str(H.FAKEGH)),
        timeout_s=30.0,
        store_path=pathlib.Path(store),
    )
    return T.run_write(
        {
            "repository": "octo/disposable",
            "path": "notes/plan.md",
            "branch": "main",
            "operation_id": "op-new-after-reconcile",
            "expected_revision": gh.sha,
            "content": "a new write that must not be sent\n",
        },
        settings=settings,
        actor="profile:c8",
    )


# --------------------------------------------------------------- the finding
def test_cli_reconcile_names_the_damage_and_the_new_write_is_refused(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    store = _lost_store(tmp_path / "orphan.db")
    _drop_operations(store)
    before_attempts = _attempts(store)
    assert len(before_attempts) == 1 and before_attempts[0][3] == K.UNKNOWN
    before_bytes = store.read_bytes()

    result = _cli(["concord", "reconcile", "op-lost", "--json"], store=store, tmp_path=tmp_path)

    # 1. Reconcile is honest about the damage, and does not claim "no operation".
    assert result.returncode != 0, result.stdout
    combined = f"{result.stdout}\n{result.stderr}"
    assert "partial Concord schema" in combined, combined
    assert "concord_operations" in combined, combined
    assert "no operation" not in combined, (
        "damage must not be reported with the answer a store that never recorded"
        f" anything gives: {combined}"
    )
    # 2. Nothing was rebuilt, and the bytes are untouched.
    assert "concord_operations" not in _tables(store)
    assert store.read_bytes() == before_bytes
    assert _attempts(store) == before_attempts

    # 3. The new write to the same repository/path is refused before any provider call.
    shape = _write_tool(store, gh)
    assert shape["outcome"] == "refused", shape
    assert shape["reason"] in ("partial_schema", "damaged_history"), shape
    assert shape["recorded"] is False
    assert shape["second_write"] is False
    assert H.writes(gh.root) == 0, "a damaged store must not let a request reach the provider"
    assert H.puts(gh.root) == 0

    # 4. Close/reopen: the store is still damaged, the orphan is still the only attempt.
    snap = ST.read_snapshot(store)
    assert snap.damage == ST.DAMAGE_PARTIAL_SCHEMA
    assert snap.healthy is False
    assert [row["operation_id"] for row in snap.attempts] == ["op-lost"]
    assert _attempts(store) == before_attempts, "attempt history is evidence; it is not repaired"

    status = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    assert status.returncode != 0
    payload = json.loads(status.stdout)
    assert payload["store"]["damage"] == "partial_schema"
    assert [row["operation_id"] for row in payload["attempts"]] == ["op-lost"]

    # A second reconcile says the same thing - the refusal does not decay.
    again = _cli(["concord", "reconcile", "op-lost"], store=store, tmp_path=tmp_path)
    assert again.returncode != 0
    assert "partial Concord schema" in (again.stdout + again.stderr)
    assert store.read_bytes() == before_bytes


def test_reconcile_still_answers_honestly_for_an_operation_that_never_existed(tmp_path):
    """The damage message must not swallow the ordinary case."""
    store = _lost_store(tmp_path / "healthy.db")

    result = _cli(["concord", "reconcile", "op-missing", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0
    assert "no operation" in result.stderr.lower()
    assert "partial Concord schema" not in result.stderr
