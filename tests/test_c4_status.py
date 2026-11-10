"""C4 — the human surface, exercised as a real CLI process, plus its behaviour under lock.

The CLI here is the *pinned* Hermes (``.venv/Scripts/hermes.exe``, commit
c5f347e), so plugin discovery, manifest parsing, ``register(ctx)`` and argparse
wiring are the same code the in-process tests import. Nothing is patched: each
test spawns ``hermes concord ...`` with an isolated ``HERMES_HOME``, a store in
``tmp_path`` and the process-level fake ``gh``.

Two things these tests are deliberately strict about:

  * ``status`` must never create, lock or modify the store.
  * disabling the plugin must stop protection while leaving every unresolved
    record readable and reconcilable once the plugin is back.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

import concord_kernel as K
import github_harness as H

ROOT = pathlib.Path(__file__).resolve().parents[2]          # .../Concord-for-Hermes
REPO = pathlib.Path(__file__).resolve().parents[1]
HOME = ROOT / "isolated-home"

#: The revision the seeded repository starts at (set by the ``gh`` fixture).
_BASE_SHA = ""


def _find_cli() -> pathlib.Path | None:
    """The Hermes CLI these tests must drive: explicit, then this workspace, then PATH.

    A clean archive has no ``.venv`` beside it, so without this the whole file
    skipped and the fresh-install run reported green on 140 of 152 tests. A
    stranger's activated ``hermes`` is a valid CLI for the same contract.
    """
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

pytestmark = pytest.mark.skipif(CLI is None,
                                reason="no hermes CLI found (set HERMES_BIN)")


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake repository (process-level gh) and a fresh store for this test."""
    global _BASE_SHA
    repo = H.setup(monkeypatch, tmp_path)
    _BASE_SHA = repo.sha
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    return repo


def _current(root) -> str:
    """The blob SHA a caller would read right now."""
    return H.repo_state(root)["files"][H.PATH]["sha"]


def _tool(run, **over):
    args = {
        "repository": "octo/disposable",
        "path": H.PATH,
        "branch": H.BRANCH,
        "operation_id": "op-1",
        "expected_revision": _BASE_SHA,
        "content": "line one\nline two\n",
    }
    args.update(over)
    return json.loads(run("concord_github_write", args).result)


def _cli(args, *, store, tmp_path, home=None, timeout=120):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home or HOME)
    env["CONCORD_DB"] = str(store)
    return subprocess.run(
        [str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=timeout,
    )


def _state(store, operation_id):
    conn = sqlite3.connect(str(store))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT state, receipt, note FROM concord_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ------------------------------------------------------------------ status
def test_status_on_a_fresh_install_says_so_and_creates_nothing(tmp_path):
    store = tmp_path / "nothing-here.db"
    result = _cli(["concord", "status"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "no coordinator store yet" in result.stdout
    assert "NOT PROTECTED" in result.stdout
    assert not store.exists(), "read-only status must not create the store"


def test_status_shows_the_holder_and_the_safe_next_action(run, gh, tmp_path, monkeypatch):
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    H.set_config(fixture="write_ambiguous")
    assert _tool(run, operation_id="op-held")["outcome"] == "unknown"

    result = _cli(["concord", "status"], store=tmp_path / "coordinator.db", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert H.OBJECT in result.stdout
    assert "holder: op-held" in result.stdout
    assert "state=unknown" in result.stdout
    assert "hermes concord reconcile op-held" in result.stdout
    assert "NOT PROTECTED" in result.stdout

    machine = _cli(
        ["concord", "status", "--json"], store=tmp_path / "coordinator.db", tmp_path=tmp_path
    )
    payload = json.loads(machine.stdout)
    assert payload["allowlist"] == ["octo/disposable"]
    assert payload["objects"][0]["holder"] == "op-held"
    assert payload["unresolved"] == 1


def test_status_reports_an_unreadable_store_instead_of_guessing(tmp_path):
    """Contract change (CONCORD-006, review P2-1): the human view already said
    "STORE UNREADABLE", but it exited 0. An unreadable store is a failure, and a
    script that checks the exit code must see one."""
    store = tmp_path / "corrupt.db"
    store.write_bytes(b"this is not a database\n")

    result = _cli(["concord", "status"], store=store, tmp_path=tmp_path)

    assert result.returncode != 0
    assert "STORE UNREADABLE" in result.stdout


# --------------------------------------------------------------- reconcile
def test_reconcile_settles_an_unknown_from_evidence_and_releases_the_object(
    run, gh, tmp_path, monkeypatch
):
    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    H.set_config(fixture="write_ambiguous")
    assert _tool(run, operation_id="op-amb")["outcome"] == "unknown"
    writes_after_ambiguous = H.writes(gh.root)

    H.set_config(fixture="write_ok")
    result = _cli(["concord", "reconcile", "op-amb"], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "confirmed from provider evidence" in result.stdout
    row = _state(store, "op-amb")
    assert row["state"] == "confirmed"
    assert json.loads(row["receipt"])["reconciled"] is True
    assert H.writes(gh.root) == writes_after_ambiguous, "reconcile must not write"

    # the object is free again: a new intent, with the revision a caller would
    # read now, is admitted and writes once more
    shape = _tool(run, operation_id="op-after-reconcile", content="after reconcile\n",
                  expected_revision=_current(gh.root))
    assert shape["outcome"] == "confirmed"
    assert H.writes(gh.root) == writes_after_ambiguous + 1


def test_reconcile_refuses_a_pending_operation_unless_the_process_is_declared_dead(
    gh, tmp_path, monkeypatch
):
    store = tmp_path / "coordinator.db"
    coordinator = K.Coordinator(str(store))
    coordinator.admit(
        object_key=H.OBJECT, operation_id="op-pending", fingerprint="fp",
        base_revision=gh.sha, actor="profile:test",
        # The immutable base reference (review P1-3) is recorded on every
        # admission the tool performs. Since CONCORD-007 (review F7-2) it is no
        # longer a release: a dispatched request may land after this read.
        base_identity=H.repo_state(gh.root)["commits"][0]["sha"],
    )
    coordinator.begin_dispatch("op-pending")

    refused = _cli(["concord", "reconcile", "op-pending"], store=store, tmp_path=tmp_path)
    assert refused.returncode == 1
    assert "may still be in flight" in refused.stderr
    assert _state(store, "op-pending")["state"] == "pending"

    allowed = _cli(
        ["concord", "reconcile", "op-pending", "--assume-dead-process"],
        store=store, tmp_path=tmp_path,
    )
    # CONTRACT CHANGE (CONCORD-007, review F7-2): the flag unlocks the READ, not
    # a release. The target has not moved, and that is not a proof it will not.
    assert allowed.returncode == 0, allowed.stderr
    assert "still unknown" in allowed.stdout
    assert _state(store, "op-pending")["state"] == "unknown"


def test_reconcile_says_so_when_the_operation_is_already_settled(run, gh, tmp_path, monkeypatch):
    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    assert _tool(run)["outcome"] == "confirmed"

    result = _cli(["concord", "reconcile", "op-1"], store=store, tmp_path=tmp_path)
    assert result.returncode == 0
    assert "already confirmed" in result.stdout


# ------------------------------------------------------------------ cancel
def test_cancel_only_works_before_dispatch(gh, tmp_path):
    store = tmp_path / "coordinator.db"
    coordinator = K.Coordinator(str(store))
    coordinator.admit(
        object_key=H.OBJECT, operation_id="op-reserved", fingerprint="fp",
        base_revision=gh.sha, actor="profile:test",
    )

    first = _cli(["concord", "cancel", "op-reserved"], store=store, tmp_path=tmp_path)
    assert first.returncode == 0, first.stderr
    assert _state(store, "op-reserved")["state"] == "cancelled"

    second = _cli(["concord", "cancel", "op-reserved"], store=store, tmp_path=tmp_path)
    assert second.returncode == 1
    assert "cannot cancel" in second.stderr


# ------------------------------------------------------------------ export
def test_export_dumps_every_operation_and_attempt(run, gh, tmp_path, monkeypatch):
    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    assert _tool(run, operation_id="op-1")["outcome"] == "confirmed"
    assert _tool(run, operation_id="op-2",
                 expected_revision=_current(gh.root))["outcome"] == "confirmed"

    target = tmp_path / "export.json"
    result = _cli(["concord", "export", "--output", str(target)], store=store, tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["format"] == "concord-export/v1"
    assert payload["counts"]["confirmed"] == 2
    assert {op["operation_id"] for op in payload["operations"]} == {"op-1", "op-2"}
    assert len(payload["attempts"]) == 2
    assert all(attempt["state"] == "confirmed" for attempt in payload["attempts"])
    # No credential-shaped material can be in there: the store never held any.
    assert "token" not in json.dumps(payload).lower()


# --------------------------------------------------------- disable / re-enable
def test_disabling_the_plugin_stops_the_command_but_keeps_the_records(
    run, gh, tmp_path, monkeypatch
):
    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    H.set_config(fixture="write_ambiguous")
    assert _tool(run, operation_id="op-held")["outcome"] == "unknown"

    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(HOME / "plugins" / "concord-for-hermes", home / "plugins" / "concord-for-hermes")
    config = (HOME / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        config.replace("plugins:\n", "plugins:\n  disabled:\n    - concord-for-hermes\n", 1),
        encoding="utf-8",
    )

    disabled = _cli(["concord", "status"], store=store, tmp_path=tmp_path, home=home)
    assert disabled.returncode == 2, disabled.stdout
    assert "not a `hermes` command" in disabled.stderr or "invalid choice" in disabled.stderr
    # The unknown is still there, unreadable by a command that no longer exists.
    assert _state(store, "op-held")["state"] == "unknown"

    # Re-enabling restores the surface and the unresolved operation is still shown.
    (home / "config.yaml").write_text(config, encoding="utf-8")
    enabled = _cli(["concord", "status"], store=store, tmp_path=tmp_path, home=home)
    assert enabled.returncode == 0, enabled.stderr
    assert "holder: op-held" in enabled.stdout
    recovered = _cli(["concord", "reconcile", "op-held"], store=store, tmp_path=tmp_path, home=home)
    assert recovered.returncode == 0, recovered.stderr
    assert _state(store, "op-held")["state"] in ("confirmed", "failed_before_execution")


# -------------------------------------------------------------- contention
def test_a_status_read_never_waits_for_an_active_writer(run, gh, tmp_path, monkeypatch):
    """The read path is ``mode=ro`` + ``query_only``: a writer holding the lock
    must not delay it, and the connection must be unable to write at all."""
    import sqlite3

    import concord_status as ST

    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    H.set_config(fixture="write_ok")
    assert _tool(run, operation_id="op-1")["outcome"] == "confirmed"

    writer = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "store_lock_child.py"), str(store), "3.0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert writer.stdout.readline().strip() == "locked"

        started = time.monotonic()
        snap = ST.read_snapshot(store)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"status read took {elapsed:.2f}s behind a write lock"
        assert snap.initialized and snap.error == ""
        assert [row["operation_id"] for row in snap.rows] == ["op-1"]

        conn = ST.open_readonly(store)
        try:
            with pytest.raises(sqlite3.Error):
                conn.execute("DELETE FROM concord_operations")
        finally:
            conn.close()
    finally:
        writer.communicate(timeout=60)


def test_two_status_processes_run_while_a_writer_is_mid_transaction(
    run, gh, tmp_path, monkeypatch
):
    """End to end through the real CLI: two status processes complete while the
    write lock is still held, and neither reports an unreadable store."""
    store = tmp_path / "coordinator.db"
    monkeypatch.setenv("CONCORD_DB", str(store))
    H.set_config(fixture="write_ok")
    assert _tool(run, operation_id="op-1")["outcome"] == "confirmed"

    _cli(["concord", "status"], store=store, tmp_path=tmp_path)  # warm the CLI's imports

    release = tmp_path / "release.lock"
    writer = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "store_lock_child.py"), str(store), "120",
         str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert writer.stdout.readline().strip() == "locked", writer.stderr.read()

        readers = [
            subprocess.Popen(
                [str(CLI), "concord", "status"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "HERMES_HOME": str(HOME), "CONCORD_DB": str(store)},
                cwd=str(tmp_path),
            )
            for _ in range(2)
        ]
        outputs = [reader.communicate(timeout=120) for reader in readers]

        for reader, (out, err) in zip(readers, outputs):
            assert reader.returncode == 0, err
            assert "op-1" in out
            assert "STORE UNREADABLE" not in out
        # The structural proof: the writer was still inside its transaction.
        assert writer.poll() is None, "the writer had already released the lock"
    finally:
        release.write_text("go\n", encoding="utf-8")
        writer.communicate(timeout=60)


def test_the_coordinator_opens_while_another_process_holds_a_write_transaction(tmp_path):
    """A concurrent call must reach a decision, not a spurious storage_locked refusal."""
    store = tmp_path / "coordinator.db"
    K.Coordinator(str(store))  # create the schema first

    writer = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "store_lock_child.py"), str(store), "3.0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert writer.stdout.readline().strip() == "locked"
        started = time.monotonic()
        coordinator = K.Coordinator(str(store), busy_ms=8000)
        elapsed = time.monotonic() - started
        assert coordinator.stats()["unresolved"] == 0
        assert elapsed < 5.0, f"opening the store took {elapsed:.1f}s"
    finally:
        writer.communicate(timeout=30)
