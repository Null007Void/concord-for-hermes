"""C9 — the validation verdict is one consistent read transaction (F9-2).

``validate_existing_store`` reads several tables (schema, columns, indexes, the
orphan scan, the counter and the recorded sequence maxima). With autocommit each
of those is its own transaction, so a concurrent writer that commits between two
of them is visible to the later reads and invisible to the earlier ones. The
verdict is then a mixture of two states: it can refuse a HEALTHY store (an old
counter compared against new rows) and it can describe a store that never existed.

The contract: every read behind one verdict happens inside ONE read transaction
(one snapshot). A concurrent commit during the read is either invisible (the
snapshot is the verdict's instant) or the snapshot is retried/refused - never a
torn view.

The interleaving here is deterministic, not a timing bet: a wrapper around the
read-only connection runs a REAL concurrent commit (a second SQLite connection,
a real transaction) immediately before a named validation statement. That is the
same control-point technique the fake-provider fixtures use, applied to the
store.

The second half is the concurrent-admission regression the owner asked for: two
real OS processes admit on one object, and exactly one may win - no partial
admission, no double hold. The outcome is asserted, not the ordering.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys
import time

import concord_kernel as K

ROOT = pathlib.Path(__file__).resolve().parents[2]
PRODUCT_DIR = ROOT / "isolated-home" / "plugins" / "concord-for-hermes"
CHILD = pathlib.Path(__file__).resolve().parent / "admit_race_child.py"

CANONICAL = "github:octo/disposable@main:notes/plan.md"
OBJECT = "doc-race"


# ------------------------------------------------------------------ helpers
def healthy_store(path) -> pathlib.Path:
    """One confirmed operation with its attempt, plus a sentinel counter row."""
    coordinator = K.Coordinator(str(path))
    coordinator.admit(object_key=CANONICAL, operation_id="op-existing",
                      fingerprint="fp-existing", base_revision="a" * 40,
                      actor="profile:c9")
    coordinator.begin_dispatch("op-existing")
    coordinator.confirm("op-existing", {"blob_sha": "b" * 40, "commit_sha": "c" * 40})
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("INSERT INTO concord_counters (name, value) VALUES ('sentinel', 1)")
    finally:
        conn.close()
    return pathlib.Path(path)


def concurrent_commit(path, work) -> None:
    """A second connection, one real transaction - what another process does."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        work(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class ControlConnection:
    """The real read-only connection with a control point before one statement.

    ``execute`` records, through THIS connection, the sentinel generation visible
    at that moment. One read transaction therefore observes one generation for
    every read; several transactions observe whatever the latest commit left.
    """

    def __init__(self, real, *, at_sql: str, control_point, trace: list, state: dict):
        self._real = real
        self._at_sql = at_sql
        self._control_point = control_point
        self._trace = trace
        self._state = state

    def _generation(self):
        try:
            row = self._real.execute(
                "SELECT value FROM concord_counters WHERE name = 'sentinel'").fetchone()
            return int(row[0]) if row else None
        except sqlite3.Error:
            return None

    def execute(self, sql, params=()):
        if not self._state["fired"] and self._at_sql in sql:
            self._state["fired"] = True
            self._control_point()
        self._trace.append(self._generation())
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def instrument(monkeypatch, *, at_sql: str, control_point):
    trace: list = []
    state = {"fired": False}
    real_factory = K._open_readonly

    def factory(path):
        return ControlConnection(real_factory(path), at_sql=at_sql,
                                 control_point=control_point, trace=trace, state=state)

    monkeypatch.setattr(K, "_open_readonly", factory)
    return trace, state


# ------------------------------------------------------- one-snapshot reads
def test_every_validation_read_sees_one_snapshot(tmp_path, monkeypatch):
    path = healthy_store(tmp_path / "snapshot.db")
    trace, state = instrument(
        monkeypatch,
        at_sql="FROM sqlite_master WHERE type = 'index'",
        control_point=lambda: concurrent_commit(
            path, lambda conn: conn.execute(
                "UPDATE concord_counters SET value = 2 WHERE name = 'sentinel'")),
    )

    verdict = K.validate_existing_store(str(path))

    assert state["fired"], "the control point did not run"
    generations = {value for value in trace if value is not None}
    assert len(trace) >= 3, trace
    assert generations == {1}, (
        f"the verdict mixed instants: generations {sorted(generations)}"
        f" (raw trace {trace})"
    )
    assert not verdict.damaged


def test_a_concurrent_legitimate_admission_is_not_damage(tmp_path, monkeypatch):
    """An old counter read against a new row set refused a healthy store."""
    path = healthy_store(tmp_path / "refusal.db")

    def admit_elsewhere(conn):
        row = conn.execute("SELECT value FROM concord_counters WHERE name = 'seq'").fetchone()
        seq = int(row[0]) + 1
        conn.execute(
            "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
            " fingerprint_object, base_revision, base_identity, actor, state, holder,"
            " attempt_count, receipt, note, note_chars, created_seq, updated_seq,"
            " created_at, updated_at)"
            " VALUES ('op-concurrent', 'github:octo/disposable@main:notes/concurrent.md',"
            " 'fp-concurrent', 'github:octo/disposable@main:notes/concurrent.md', ?, NULL,"
            " 'profile:other', 'confirmed', NULL, 0, NULL, NULL, NULL, ?, ?, 1.0, 1.0)",
            ("d" * 40, seq, seq),
        )
        conn.execute("UPDATE concord_counters SET value = ? WHERE name = 'seq'", (seq,))

    _trace, state = instrument(monkeypatch, at_sql="COALESCE(MAX(created_seq)",
                               control_point=lambda: concurrent_commit(path, admit_elsewhere))

    verdict = K.validate_existing_store(str(path))
    monkeypatch.undo()

    assert state["fired"], "the control point did not run"
    assert not verdict.damaged, (
        "a healthy store was refused because the verdict straddled two commits: "
        f"{verdict.damage_reason}: {verdict.detail}"
    )
    # And the same file, read without the interleaving, is healthy too - so the
    # store really was never damaged.
    fresh = K.validate_existing_store(str(path))
    assert not fresh.damaged


def test_the_verdict_is_the_same_after_close_and_reopen(tmp_path):
    path = healthy_store(tmp_path / "reopen.db")
    before = K.validate_existing_store(str(path))
    assert not before.damaged

    coordinator = K.Coordinator(str(path))   # close the file, then open it again
    coordinator.stats()
    del coordinator

    after = K.validate_existing_store(str(path))
    assert after.creatable == before.creatable
    assert after.damage_reason == before.damage_reason


def test_a_damaged_store_is_still_refused(tmp_path):
    """Control: the single-transaction change did not weaken the damage checks."""
    path = healthy_store(tmp_path / "damaged.db")
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("DROP TABLE concord_attempts")
    finally:
        conn.close()

    verdict = K.validate_existing_store(str(path))
    assert verdict.damaged
    assert verdict.damage_reason == K.DAMAGE_PARTIAL_SCHEMA


# ------------------------------------------- concurrent admission, two processes
def _spawn(db, *, object_key: str, operation_id: str, start_at: float,
           out_path: pathlib.Path):
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(PRODUCT_DIR), str(db), object_key, operation_id,
         f"{start_at:.6f}", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _collect(procs, outs) -> list[dict]:
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"contender failed: {stderr}\n{stdout}"
    results = [json.loads(path.read_text(encoding="utf-8")) for path in outs]
    assert len({result["pid"] for result in results}) == len(results), (
        "contenders were not distinct OS processes"
    )
    return results


def test_two_processes_racing_on_one_object_exactly_one_is_admitted(tmp_path):
    db = tmp_path / "coordinator.db"
    K.Coordinator(str(db))                       # create the store, then close it
    start_at = time.time() + 1.2
    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    procs = [_spawn(db, object_key=OBJECT, operation_id=f"op-{index}",
                    start_at=start_at, out_path=outs[index])
             for index in (0, 1)]

    results = _collect(procs, outs)
    kinds = sorted(result["kind"] for result in results)
    assert kinds == [K.ADMITTED, K.CONFLICT], results

    winner = next(result for result in results if result["kind"] == K.ADMITTED)
    loser = next(result for result in results if result["kind"] == K.CONFLICT)
    assert loser["holder"] == winner["operation"], results

    # Durable, and no double hold: one blocking row for the object, and the
    # loser's conflict row names the winner.
    reopened = K.Coordinator(str(db))
    assert reopened.holder_of(OBJECT) == winner["operation"]
    conn = sqlite3.connect(str(db))
    try:
        blocking = conn.execute(
            f"SELECT operation_id FROM concord_operations"
            f" WHERE object_key = ? AND state IN {K._BLOCKING_SQL}", (OBJECT,)
        ).fetchall()
    finally:
        conn.close()
    assert len(blocking) == 1, blocking
    assert reopened.get(loser["operation"])["state"] == K.CONFLICT
    assert reopened.get(loser["operation"])["holder"] == winner["operation"]


def test_repeated_races_are_deterministic(tmp_path):
    """Three rounds, three objects: every round has exactly one winner."""
    db = tmp_path / "coordinator.db"
    K.Coordinator(str(db))
    rounds = []
    for round_index in range(3):
        object_key = f"{OBJECT}-{round_index}"
        operation_prefix = f"r{round_index}"
        start_at = time.time() + 1.0
        outs = [tmp_path / f"{operation_prefix}-{index}.json" for index in (0, 1)]
        procs = [_spawn(db, object_key=object_key,
                        operation_id=f"{operation_prefix}-op-{index}",
                        start_at=start_at, out_path=outs[index])
                 for index in (0, 1)]
        results = _collect(procs, outs)
        rounds.append(sorted(result["kind"] for result in results))

    assert rounds == [[K.ADMITTED, K.CONFLICT]] * 3, rounds

    reopened = K.Coordinator(str(db))
    conn = sqlite3.connect(str(db))
    try:
        admitted = conn.execute(
            "SELECT COUNT(*) FROM concord_operations WHERE state = ?", (K.PREPARED,)
        ).fetchone()[0]
        conflicts = conn.execute(
            "SELECT COUNT(*) FROM concord_operations WHERE state = ?", (K.CONFLICT,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert (int(admitted), int(conflicts)) == (3, 3), (admitted, conflicts)
    del reopened
