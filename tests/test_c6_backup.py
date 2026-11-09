"""C6 — a real, restorable backup instead of a misleading export (P2-2).

The review: ``export`` was documented as something to "restore from", but it
omits the material fingerprint and every other persistence column, and there is
no import command. A store rebuilt from it could not tell a replay from a new
intent, so it was an audit artifact wearing a backup's clothes.

Decision (CONCORD-006 §P2-2): implement a real backup/restore rather than only
removing the claim, because the owner's recovery requirement is "preserve
unresolved holds and operation identity across recovery", and only a physical
copy of the store can do that. ``backup`` uses SQLite's online backup API (a
consistent snapshot, safe while a writer is active); ``restore`` verifies the
file really is a Concord store and refuses to overwrite a non-empty one unless
told to. ``export`` stays, documented as an audit dump.

The round-trip test is the acceptance bar: holds, identities, fingerprints,
receipts and attempts must all survive, and a pruned record must still be
identified as pruned.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess

import pytest

import concord_backup as B
import concord_kernel as K
import concord_status as ST

ROOT = pathlib.Path(__file__).resolve().parents[2]
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


def _cli(args, *, store, tmp_path, timeout=120):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["CONCORD_DB"] = str(store)
    return subprocess.run([str(CLI), *args], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path), timeout=timeout)


def _populated_store(tmp_path) -> pathlib.Path:
    """One confirmed op, one unresolved (held) op, one confirmed-then-pruned op.

    That is the state a backup has to preserve: an unresolved hold must not be
    lost, and a pruned record must still be recognisable as pruned - a replay of
    it must still be refused rather than re-executed.
    """
    store = tmp_path / "coordinator.db"
    coord = K.Coordinator(str(store))
    coord.admit(object_key=OBJECT, operation_id="op-confirmed", fingerprint="fp-1",
                base_revision="a" * 40, actor="profile:a", base_identity="c" * 40)
    coord.begin_dispatch("op-confirmed")
    coord.confirm("op-confirmed", {"commit_sha": "d" * 40, "blob_sha": "b" * 40})

    coord.admit(object_key="github:octo/other@main:x.md", operation_id="op-held",
                fingerprint="fp-2", base_revision="e" * 40, actor="profile:b")
    coord.begin_dispatch("op-held")
    coord.mark_unknown("op-held", "the response was lost")

    coord.prune_resolved(keep_per_object=0)
    return store


def _state(path) -> dict:
    snap = ST.read_snapshot(path)
    assert snap.error == "", snap.error
    return {
        "rows": snap.rows,
        "attempts": snap.attempts,
        "unresolved": snap.unresolved,
    }


# ------------------------------------------------------------------ module API
def test_a_backup_round_trip_preserves_holds_identity_and_fingerprints(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"

    made = B.backup_store(store, target)
    assert made["operations"] == 2
    assert target.exists()

    before = _state(store)
    store.unlink()                      # "the disk died"
    restored = B.restore_store(target, store)
    after = _state(store)

    assert restored["operations"] == 2
    assert after["rows"] == before["rows"], "every column, including the fingerprint"
    assert after["attempts"] == before["attempts"]
    assert after["unresolved"] == 1, "the unresolved hold survives recovery"

    held = {row["operation_id"]: row for row in after["rows"]}["op-held"]
    assert held["state"] == "unknown"
    pruned = [row for row in after["rows"] if row["pruned"]]
    assert pruned and pruned[0]["operation_id"] == "op-confirmed"
    conn = sqlite3.connect(str(store))
    try:
        fingerprint = conn.execute(
            "SELECT fingerprint FROM concord_operations WHERE operation_id = ?",
            ("op-confirmed",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert fingerprint == "fp-1", "the material fingerprint survives a backup (export drops it)"
    coord = K.Coordinator(str(store))
    replay = coord.admit(object_key=OBJECT, operation_id="op-confirmed", fingerprint="fp-1",
                         base_revision="a" * 40, actor="profile:a")
    assert replay.kind == K.REPLAY_PRUNED, "a pruned identity still refuses a second write"


def test_the_backup_is_a_consistent_snapshot_while_a_writer_holds_the_store(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"
    writer = sqlite3.connect(str(store), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE concord_operations SET note = 'mid-transaction' WHERE 1 = 0")
        made = B.backup_store(store, target)
        assert made["operations"] == 2
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert ST.read_snapshot(target).unresolved == 1


def test_backup_refuses_an_unreadable_store(tmp_path):
    store = tmp_path / "corrupt.db"
    store.write_bytes(b"not a database\n")
    with pytest.raises(B.BackupError):
        B.backup_store(store, tmp_path / "backup.db")
    assert not (tmp_path / "backup.db").exists()


def test_backup_refuses_to_overwrite_an_existing_file(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"
    target.write_bytes(b"keep me\n")
    with pytest.raises(B.BackupError):
        B.backup_store(store, target)
    assert target.read_bytes() == b"keep me\n"


def test_restore_refuses_a_file_that_is_not_a_concord_store(tmp_path):
    store = tmp_path / "coordinator.db"
    stranger = tmp_path / "stranger.db"
    sqlite3.connect(str(stranger)).execute("CREATE TABLE t (x)").connection.close()

    with pytest.raises(B.BackupError) as excinfo:
        B.restore_store(stranger, store)
    assert "concord" in str(excinfo.value).lower()
    assert not store.exists()


def test_restore_refuses_to_clobber_a_non_empty_store_without_replace(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"
    B.backup_store(store, target)
    original = store.read_bytes()

    with pytest.raises(B.BackupError):
        B.restore_store(target, store)
    assert store.read_bytes() == original, "a refused restore changes nothing"


def test_restore_with_replace_replaces_a_non_empty_store(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"
    B.backup_store(store, target)

    # Change the live store, then roll it back from the backup.
    coord = K.Coordinator(str(store))
    coord.admit(object_key="github:octo/new@main:y.md", operation_id="op-new",
                fingerprint="fp-3", base_revision="f" * 40, actor="t")
    assert len(_state(store)["rows"]) == 3

    B.restore_store(target, store, replace=True)
    assert len(_state(store)["rows"]) == 2


# -------------------------------------------------------------------- the CLI
@pytest.mark.skipif(CLI is None, reason="no hermes CLI found (set HERMES_BIN)")
def test_backup_and_restore_through_the_cli(tmp_path):
    store = _populated_store(tmp_path)
    target = tmp_path / "backup.db"

    made = _cli(["concord", "backup", "--output", str(target)], store=store, tmp_path=tmp_path)
    assert made.returncode == 0, made.stderr
    assert "2 operation(s)" in made.stdout
    assert target.exists()

    store.unlink()
    restored = _cli(["concord", "restore", "--input", str(target)], store=store, tmp_path=tmp_path)
    assert restored.returncode == 0, restored.stderr

    status = _cli(["concord", "status", "--json"], store=store, tmp_path=tmp_path)
    payload = json.loads(status.stdout)
    assert payload["unresolved"] == 1
    assert [op["operation_id"] for op in payload["operations"]].count("op-held") == 1


@pytest.mark.skipif(CLI is None, reason="no hermes CLI found (set HERMES_BIN)")
def test_the_cli_refuses_a_backup_of_an_unreadable_store(tmp_path):
    store = tmp_path / "corrupt.db"
    store.write_bytes(b"not a database\n")
    target = tmp_path / "backup.db"
    result = _cli(["concord", "backup", "--output", str(target)], store=store, tmp_path=tmp_path)
    assert result.returncode != 0
    assert not target.exists()


@pytest.mark.skipif(CLI is None, reason="no hermes CLI found (set HERMES_BIN)")
def test_export_is_documented_as_an_audit_dump_not_a_backup(tmp_path):
    """The README's restore claim is what made the old export dangerous."""
    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "restore from an export" not in readme
    assert "concord backup" in readme and "concord restore" in readme
