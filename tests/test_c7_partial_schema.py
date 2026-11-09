"""C7 — partial schema damage is loud; a genuinely empty store is not (F7-3).

The defect (review F7-3): a store whose ``concord_operations`` table was gone
while ``concord_attempts`` still held rows was read as "exists, no Concord
schema". ``concord status --json`` therefore exited 0 with empty arrays,
``healthy: true`` and no error, and ``concord export --output`` wrote a
successful-looking dump. Both answers are indistinguishable from a store that
never recorded anything - while the surviving attempt rows are positive evidence
that it did.

The three states are now distinct, and this module tests all three:

  (a) **genuinely empty** - no Concord table at all, or all of them empty. That
      is healthy, healthy-empty, exit 0. It must keep working.
  (b) **partial schema** - some of the schema survives while a required table (or
      a required column) is gone: nonzero exit in both interfaces, a structured
      ``damage: "partial_schema"`` in the JSON, ``export`` refused, and the
      surviving attempts still shown so the damage is legible.
  (c) **fully corrupt** - the pre-existing ``StoreUnreadable`` behaviour, with
      ``damage: "unreadable"``.
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

OBJECT = "github:octo/disposable@main:notes/plan.md"


def _cli(args, *, store, tmp_path, timeout=180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["CONCORD_DB"] = str(store)
    return subprocess.run([str(CLI), *args], capture_output=True, text=True, env=env,
                          cwd=str(tmp_path), timeout=timeout)


def _held_store(path: pathlib.Path) -> pathlib.Path:
    """A real store with one unknown operation and its attempt."""
    coord = K.Coordinator(str(path))
    coord.admit(object_key=OBJECT, operation_id="op-1", fingerprint="fp",
                base_revision="a" * 40, actor="profile:test")
    coord.begin_dispatch("op-1")
    coord.mark_unknown("op-1", "the response was lost")
    return path


def _drop_operations(store: pathlib.Path) -> int:
    conn = sqlite3.connect(str(store))
    try:
        attempts = conn.execute("SELECT COUNT(*) FROM concord_attempts").fetchone()[0]
        conn.execute("DROP TABLE concord_operations")
        conn.commit()
    finally:
        conn.close()
    return int(attempts)


# ------------------------------------- (a) genuinely empty is still a success
def test_an_empty_database_is_healthy_empty_and_exits_zero(tmp_path):
    store = tmp_path / "empty.db"
    sqlite3.connect(str(store)).close()

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["store"]["exists"] is True
    assert payload["store"]["initialized"] is False
    assert payload["store"]["healthy"] is True
    assert payload["store"]["damage"] is None
    assert payload["operations"] == [] and payload["attempts"] == []


def test_a_store_with_all_its_tables_and_no_rows_is_healthy_empty(tmp_path):
    store = tmp_path / "schema-only.db"
    K.Coordinator(str(store))
    snap = ST.read_snapshot(store)

    assert snap.initialized is True
    assert snap.damage == ST.DAMAGE_NONE
    assert snap.healthy is True
    assert snap.rows == [] and snap.attempts == []


# ------------------------ (b) a required table missing while attempts survive
def test_status_json_fails_loudly_when_operations_is_missing(tmp_path):
    store = _held_store(tmp_path / "partial.db")
    remaining = _drop_operations(store)
    assert remaining == 1

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0, "partial schema damage must not exit 0"
    payload = json.loads(result.stdout)
    assert payload["store"]["healthy"] is False
    assert payload["store"]["readable"] is False
    assert payload["store"]["damage"] == "partial_schema"
    assert payload["store"]["initialized"] is False
    error = payload["store"]["error"] or ""
    assert "concord_operations" in error, error
    assert "not an empty store" in error, error
    # The damage is legible: the surviving rows are still shown.
    assert [a["operation_id"] for a in payload["attempts"]] == ["op-1"]


def test_the_human_status_names_the_damage(tmp_path):
    store = _held_store(tmp_path / "partial-human.db")
    _drop_operations(store)

    result = _cli(["concord", "status"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0
    assert "STORE DAMAGED - PARTIAL SCHEMA" in result.stdout
    assert "surviving attempt rows: 1" in result.stdout


def test_export_refuses_a_partially_damaged_store(tmp_path):
    store = _held_store(tmp_path / "partial-export.db")
    _drop_operations(store)
    target = tmp_path / "export.json"

    result = _cli(["concord", "export", "--output", str(target)], store=store, tmp_path=tmp_path)

    assert result.returncode != 0, result.stdout
    assert not target.exists(), "no successful-looking dump may be written"
    assert "partial" in result.stderr.lower()


def test_a_missing_required_column_is_the_same_signature(tmp_path):
    """Dropping a column is damage too, not a shorter but healthy store."""
    store = _held_store(tmp_path / "partial-column.db")
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("ALTER TABLE concord_operations DROP COLUMN receipt")
        conn.commit()
    finally:
        conn.close()

    snap = ST.read_snapshot(store)
    assert snap.damage == ST.DAMAGE_PARTIAL_SCHEMA
    assert snap.healthy is False
    assert "receipt" in snap.error

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    assert result.returncode != 0
    assert json.loads(result.stdout)["store"]["damage"] == "partial_schema"


def test_read_attempts_raises_but_the_snapshot_still_shows_the_rows(tmp_path):
    """A partially damaged store is not readable *as a store* - and the surviving
    attempt history is still surfaced by the read-only snapshot, which is what
    makes the damage legible."""
    store = _held_store(tmp_path / "partial-read.db")
    _drop_operations(store)

    with pytest.raises(ST.StoreUnreadable) as raised:
        ST.read_attempts(store)
    assert "concord_operations" in str(raised.value)

    snap = ST.read_snapshot(store)
    assert [a["operation_id"] for a in snap.attempts] == ["op-1"]


def test_the_damage_verdict_survives_reopen_and_a_second_reader(tmp_path):
    """Close the store, damage it, and read it again from a fresh process.

    The verdict must be a property of the file, not of one connection: the same
    `partial_schema` answer from a second reader, and no silent healing by the
    read-only path (a reader that recreated the missing table would turn the
    damage into a healthy-looking empty store).
    """
    store = _held_store(tmp_path / "partial-reopen.db")
    _drop_operations(store)

    first = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    second = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    export = _cli(["concord", "export", "--output", str(tmp_path / "again.json")],
                  store=store, tmp_path=tmp_path)

    for result in (first, second):
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["store"]["damage"] == "partial_schema"
        assert [a["operation_id"] for a in payload["attempts"]] == ["op-1"]
    assert export.returncode != 0
    assert not (tmp_path / "again.json").exists()

    # Nothing recreated the table behind the readers' backs.
    conn = sqlite3.connect(str(store))
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "concord_operations" not in tables


# ------------------------------- (c) the corrupt file is unchanged, and named
def test_a_corrupt_file_keeps_the_existing_unreadable_path(tmp_path):
    store = tmp_path / "corrupt.db"
    store.write_bytes(b"this is not a database\n")

    snap = ST.read_snapshot(store)
    assert snap.damage == ST.DAMAGE_UNREADABLE
    assert snap.healthy is False
    assert "not a database" in snap.error

    result = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["store"]["damage"] == "unreadable"
    assert payload["store"]["readable"] is False


def test_a_missing_store_is_not_damage(tmp_path):
    store = tmp_path / "never-created.db"
    snap = ST.read_snapshot(store)

    assert snap.exists is False
    assert snap.damage == ST.DAMAGE_NONE
    assert snap.healthy is True
    assert not store.exists()
