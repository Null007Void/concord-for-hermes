"""C1 Q5 — two real OS processes contending for the same object.

Q5: with two independent processes, does exactly one proceed, and do unrelated
objects both proceed? Both halves are required: a global lock that serializes
the fleet would satisfy the first and fail the second.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import harness

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CONTEND = SCRIPTS / "contend.py"


def _spawn(*, plugin_dir, db, store, object_key, operation_id, actor, value,
           expected_revision, start_at, out_path):
    return subprocess.Popen(
        [sys.executable, str(CONTEND), str(plugin_dir), str(db), str(store),
         object_key, operation_id, actor, value, str(expected_revision),
         f"{start_at:.6f}", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_q5_two_processes_one_object_exactly_one_proceeds(probe, tmp_path):
    plugin_dir = Path(__file__).resolve().parents[2] / "isolated-home" / "plugins" / "concord_probe"
    probe.provider.seed("doc-1", {"body": "before"})
    start_at = time.time() + 1.5

    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    procs = [
        _spawn(plugin_dir=plugin_dir, db=probe.db, store=probe.store, object_key="doc-1",
               operation_id=f"op-{i}", actor=f"actor-{i}", value=f"value-{i}",
               expected_revision=1, start_at=start_at, out_path=outs[idx])
        for idx, i in enumerate((1, 2))
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=90)
        assert proc.returncode == 0, f"contender failed: {stderr}\n{stdout}"

    results = [_read(p) for p in outs]
    kinds = sorted(r["kind"] for r in results)
    assert kinds == ["admitted", "conflict"], f"expected exactly one winner, got {results}"
    assert len({r["pid"] for r in results}) == 2, "contenders were not distinct OS processes"
    # Exactly one provider write, despite two processes racing.
    assert harness.store_revision(str(probe.store), "doc-1") == 2


def test_q5_unrelated_objects_both_proceed(probe, tmp_path):
    plugin_dir = Path(__file__).resolve().parents[2] / "isolated-home" / "plugins" / "concord_probe"
    probe.provider.seed("doc-1", {"body": "before"})
    probe.provider.seed("doc-2", {"body": "before"})
    start_at = time.time() + 1.5

    outs = [tmp_path / "c.json", tmp_path / "d.json"]
    procs = [
        _spawn(plugin_dir=plugin_dir, db=probe.db, store=probe.store, object_key=f"doc-{i}",
               operation_id=f"op-{i}", actor=f"actor-{i}", value=f"value-{i}",
               expected_revision=1, start_at=start_at, out_path=outs[idx])
        for idx, i in enumerate((1, 2))
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=90)
        assert proc.returncode == 0, f"contender failed: {stderr}\n{stdout}"

    results = [_read(p) for p in outs]
    assert [r["kind"] for r in results] == ["admitted", "admitted"], f"unrelated objects contended: {results}"
    # Independent progress: each object moved on its own.
    assert harness.store_revision(str(probe.store), "doc-1") == 2
    assert harness.store_revision(str(probe.store), "doc-2") == 2
