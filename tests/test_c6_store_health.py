"""C6 — machine interfaces must not report a corrupt store as healthy (P2-1).

The review's reproduction: a store containing "not a sqlite database" made
``hermes concord status --json`` exit 0 with empty arrays and no error field, and
``hermes concord export --output backup.json`` exit 0 with "exported 0
operation(s)" while the snapshot's own ``error`` was
``DatabaseError: file is not a database``. An empty answer is indistinguishable
from "nothing is held", which is the one thing a coordination tool must never
be vague about.

The contract after this fix:

  * the snapshot carries where the store is, whether it exists, whether it has
    the Concord schema, whether it is readable, and a structured error;
  * a machine interface exits nonzero when the state could not be read, and
    ``export`` refuses to write a deceptively complete backup;
  * a store that is merely *empty* is still a success - nothing is held, and
    that is a fact, not a failure.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess

import pytest

import concord_kernel as K
import concord_status as ST

ROOT = pathlib.Path(__file__).resolve().parents[2]          # .../Concord-for-Hermes
HOME = ROOT / "isolated-home"


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


def _cli(args, *, store, tmp_path, timeout=120):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["CONCORD_DB"] = str(store)
    return subprocess.run(
        [str(CLI), *args], capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=timeout,
    )


def _corrupt(tmp_path) -> pathlib.Path:
    store = tmp_path / "corrupt.db"
    store.write_bytes(b"this is not a database\n")
    return store


def _healthy(tmp_path) -> pathlib.Path:
    store = tmp_path / "coordinator.db"
    coord = K.Coordinator(str(store))
    coord.admit(object_key="github:octo/disposable@main:notes/plan.md",
                operation_id="op-1", fingerprint="fp", base_revision="a" * 40, actor="t")
    coord.begin_dispatch("op-1")
    coord.mark_unknown("op-1", "lost response")
    return store


# ------------------------------------------------------------------ corrupt
def test_status_json_fails_loudly_on_a_corrupt_store(tmp_path):
    store = _corrupt(tmp_path)
    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0, "a corrupt store must not exit 0"
    payload = json.loads(result.stdout)
    assert payload["store"]["readable"] is False
    assert payload["store"]["exists"] is True
    assert "not a database" in (payload["store"]["error"] or "")
    assert payload["operations"] == []
    # The empty arrays are still there, but they can no longer be mistaken for
    # "nothing is held" without reading the store block.
    assert payload["unresolved"] == 0


def test_status_json_is_not_silently_empty(tmp_path):
    """Same call, healthy store: the readable flag distinguishes the two."""
    store = _healthy(tmp_path)
    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["store"]["readable"] is True
    assert payload["store"]["error"] is None
    assert payload["unresolved"] == 1
    assert payload["operations"][0]["operation_id"] == "op-1"


def test_export_refuses_to_write_a_backup_of_an_unreadable_store(tmp_path):
    store = _corrupt(tmp_path)
    target = tmp_path / "backup.json"

    result = _cli(["concord", "export", "--output", str(target)], store=store, tmp_path=tmp_path)

    assert result.returncode != 0, result.stdout
    assert not target.exists(), "a deceptively successful backup must not be written"
    assert "not a database" in result.stderr


def test_human_status_also_exits_nonzero_when_the_state_cannot_be_read(tmp_path):
    """The tightening: exit 0 was itself the defect."""
    store = _corrupt(tmp_path)
    result = _cli(["concord", "status"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0
    assert "STORE UNREADABLE" in result.stdout


# ------------------------------------------------------- empty is not broken
def test_an_empty_store_is_a_success_with_an_honest_report(tmp_path):
    store = tmp_path / "empty.db"
    sqlite3.connect(str(store)).close()

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["store"]["exists"] is True
    assert payload["store"]["readable"] is True
    assert payload["store"]["initialized"] is False
    assert payload["operations"] == []


def test_a_missing_store_is_still_a_success(tmp_path):
    store = tmp_path / "never-created.db"
    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["store"]["exists"] is False
    assert payload["store"]["readable"] is True
    assert not store.exists(), "status must not create the store"


# ------------------------------------------------------------ missing tables
def test_a_store_missing_a_table_is_reported_not_guessed(tmp_path):
    store = _healthy(tmp_path)
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("DROP TABLE concord_attempts")
        conn.commit()
    finally:
        conn.close()

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0, "half a schema is not a healthy store"
    payload = json.loads(result.stdout)
    assert payload["store"]["readable"] is False
    assert "concord_attempts" in (payload["store"]["error"] or "")
    # The operations are still shown: unreadable attempts must not hide them.
    assert [op["operation_id"] for op in payload["operations"]] == ["op-1"]


def test_export_refuses_a_store_it_cannot_read_completely(tmp_path):
    store = _healthy(tmp_path)
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("DROP TABLE concord_attempts")
        conn.commit()
    finally:
        conn.close()

    target = tmp_path / "backup.json"
    result = _cli(["concord", "export", "--output", str(target)], store=store, tmp_path=tmp_path)

    assert result.returncode != 0
    assert not target.exists()


# ------------------------------------------------------------- one snapshot
def test_operations_and_attempts_come_from_one_snapshot(tmp_path):
    store = _healthy(tmp_path)
    snap = ST.read_snapshot(store)

    assert snap.error == "" and snap.attempts_error == ""
    assert [row["operation_id"] for row in snap.rows] == ["op-1"]
    assert [a["operation_id"] for a in snap.attempts] == ["op-1"]
    model = ST.export_model(snap)
    assert model["attempts"] == snap.attempts


def test_read_attempts_does_not_swallow_an_error(tmp_path):
    store = _corrupt(tmp_path)
    with pytest.raises(ST.StoreUnreadable):
        ST.read_attempts(store)


def test_export_of_a_healthy_store_still_works(tmp_path):
    store = _healthy(tmp_path)
    target = tmp_path / "export.json"
    result = _cli(["concord", "export", "--output", str(target)], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["format"] == "concord-export/v1"
    assert payload["store"]["readable"] is True
    assert len(payload["attempts"]) == 1
