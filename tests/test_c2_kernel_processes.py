"""C2 — per-object concurrency with real OS processes.

Two requirements at once: one object admits exactly one operation, and unrelated
objects make independent progress. A global lock would satisfy the first and
fail the second, so both halves are asserted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import harness

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parents[1] / "scripts" / "kernel_child.py"
# Both installed plugin dirs (probe fixture: synthetic provider; product: kernel).
PLUGIN_DIR = os.pathsep.join([
    str(ROOT / "isolated-home" / "plugins" / "concord_probe"),
    str(ROOT / "isolated-home" / "plugins" / "concord-for-hermes"),
])


def _spawn(*, db, store, object_key, operation_id, value, base_revision, start_at, out_path):
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(PLUGIN_DIR), "contend", str(db), str(store),
         object_key, operation_id, json.dumps(value), str(base_revision),
         str(out_path), f"{start_at:.6f}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_all(procs):
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"contender failed: {stderr}\n{stdout}"
        results.append(proc)
    return results


def test_two_processes_one_object_exactly_one_proceeds(probe, tmp_path):
    probe.provider.seed("doc-1", {"body": "before"})
    start_at = time.time() + 1.5
    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    procs = [
        _spawn(db=probe.db, store=probe.store, object_key="doc-1", operation_id=f"op-{i}",
               value=f"value-{i}", base_revision=1, start_at=start_at, out_path=outs[idx])
        for idx, i in enumerate((1, 2))
    ]
    _run_all(procs)

    results = [_read(p) for p in outs]
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["confirmed", "conflict"], results
    assert len({r["pid"] for r in results}) == 2, "contenders were not distinct OS processes"
    assert harness.store_revision(str(probe.store), "doc-1") == 2, "expected exactly one provider write"

    # The conflict is a durable record naming the holder, not just a reply.
    loser = next(r for r in results if r["status"] == "conflict")
    assert loser["holder"] == "op-1" or loser["holder"] == "op-2"


def test_unrelated_objects_make_independent_progress(probe, tmp_path):
    probe.provider.seed("doc-1", {"body": "before"})
    probe.provider.seed("doc-2", {"body": "before"})
    start_at = time.time() + 1.5
    outs = [tmp_path / "c.json", tmp_path / "d.json"]
    procs = [
        _spawn(db=probe.db, store=probe.store, object_key=f"doc-{i}", operation_id=f"op-{i}",
               value=f"value-{i}", base_revision=1, start_at=start_at, out_path=outs[idx])
        for idx, i in enumerate((1, 2))
    ]
    _run_all(procs)

    results = [_read(p) for p in outs]
    assert [r["status"] for r in results] == ["confirmed", "confirmed"], results
    assert harness.store_revision(str(probe.store), "doc-1") == 2
    assert harness.store_revision(str(probe.store), "doc-2") == 2


def test_four_processes_two_objects_exactly_one_winner_each(probe, tmp_path):
    probe.provider.seed("doc-1", {"body": "before"})
    probe.provider.seed("doc-2", {"body": "before"})
    start_at = time.time() + 1.5
    outs = [tmp_path / f"{i}.json" for i in range(4)]
    procs = [
        _spawn(db=probe.db, store=probe.store, object_key=f"doc-{1 + i % 2}",
               operation_id=f"op-{i}", value=f"value-{i}", base_revision=1,
               start_at=start_at, out_path=outs[i])
        for i in range(4)
    ]
    _run_all(procs)

    results = [_read(p) for p in outs]
    confirmed = [r for r in results if r["status"] == "confirmed"]
    conflicts = [r for r in results if r["status"] == "conflict"]
    assert len(confirmed) == 2, results
    assert len(conflicts) == 2, results
    assert {r["object"] for r in confirmed} == {"doc-1", "doc-2"}
    assert harness.store_revision(str(probe.store), "doc-1") == 2
    assert harness.store_revision(str(probe.store), "doc-2") == 2
