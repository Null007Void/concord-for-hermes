"""C1 residue — real process death at the two dispatch boundaries.

The process is killed with ``taskkill /F``, not by raising: an exception is
something the code can catch and settle, which is exactly the case that is not
in question. A terminated process leaves the durable record where it was.

Two windows:

  (a) after the provider write, before the receipt write  -> the record says
      pending. Nothing knows whether the write landed. Only a read of provider
      evidence settles it.
  (b) after the reservation, before dispatch              -> the record says
      reserved. Provider evidence proves nothing was written.

The provider here is the SQLite stand-in from C1. That substitution is explicit:
"provider evidence" in these tests is a local conditional-write store, not the
GitHub Contents API. What transfers to C3 is the procedure (read, compare
fingerprint, settle), not the transport.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import concord_kernel as K
import harness

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parents[1] / "scripts" / "kernel_child.py"
# Both installed plugin dirs: the probe fixture holds the synthetic provider, the
# product plugin holds the kernel. The child takes them os.pathsep-joined.
PLUGIN_DIR = os.pathsep.join([
    str(ROOT / "isolated-home" / "plugins" / "concord_probe"),
    str(ROOT / "isolated-home" / "plugins" / "concord-for-hermes"),
])


def _spawn_crash(*, mode, db, store, object_key, operation_id, value, base_revision, marker):
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(PLUGIN_DIR), mode, str(db), str(store),
         object_key, operation_id, json.dumps(value), str(base_revision), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.02)
    raise AssertionError(f"child never reached the crash boundary ({path})")


def _kill(proc: subprocess.Popen) -> int:
    """Terminate the process with the OS, and prove it is gone."""
    assert proc.poll() is None, "the child exited by itself; nothing was killed at the boundary"
    result = subprocess.run(
        ["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"taskkill failed: {result.stdout}{result.stderr}"
    code = proc.wait(timeout=60)
    assert proc.poll() is not None
    return code


@pytest.fixture
def crash(tmp_path, probe):
    probe.provider.seed("doc", {"body": "before"})
    args = {"object": "doc", "value": {"body": "after"}, "expected_revision": 1}
    return probe, args, tmp_path / "boundary.json"


def _evidence(provider, key="doc"):
    read = provider.read(key)
    return {"revision": read["revision"], "value": read["value"],
            "source": "local SQLite provider stand-in read (not a network call)"}


def test_kill_after_provider_write_before_receipt(crash):
    probe, args, marker = crash
    proc = _spawn_crash(mode="crash_after_write", db=probe.db, store=probe.store,
                        object_key="doc", operation_id="op-1", value=args["value"],
                        base_revision=1, marker=marker)
    _wait_for(marker)
    code = _kill(proc)
    assert code != 0, "the process exited normally; it was not killed at the boundary"
    assert not Path(f"{marker}.done").exists()

    reported = json.loads(marker.read_text(encoding="utf-8"))
    assert reported["state"] == "pending"

    # The write is real and durable; the coordinator has not recorded it.
    assert harness.store_revision(str(probe.store), "doc") == 2

    # A fresh process opening a fresh coordinator sees the ambiguous window.
    restarted = K.Coordinator(str(probe.db))
    record = restarted.get("op-1")
    assert record["state"] == K.PENDING
    assert restarted.holder_of("doc") == "op-1"
    assert record["attempt_count"] == 1
    assert record["attempts"][0]["state"] == K.PENDING
    assert record["attempts"][0]["settled_at"] is None

    # Nothing retries automatically, and no competitor may write past the hold.
    replay = restarted.run(operation_id="op-1", actor="restart", received_args=args,
                           mutate=lambda: pytest.fail("a pending outcome must not be retried"))
    assert replay["status"] == K.CONFLICT
    competitor = restarted.run(operation_id="op-2", actor="other", received_args=args,
                               mutate=lambda: pytest.fail("a held object must not be written"))
    assert competitor["status"] == K.CONFLICT
    assert harness.store_revision(str(probe.store), "doc") == 2, "restart path wrote to the provider"

    # Reconciliation is a read of provider evidence, and only then a settlement.
    settled = restarted.reconcile("op-1", _evidence(probe.provider))
    assert settled["state"] == K.CONFIRMED
    final = restarted.get("op-1")
    assert final["receipt"]["reconciled"] is True
    assert final["receipt"]["revision"] == 2
    assert final["attempts"][0]["state"] == K.CONFIRMED
    assert restarted.holder_of("doc") is None
    assert harness.store_revision(str(probe.store), "doc") == 2, "reconcile must never write"


def test_kill_after_reservation_before_dispatch(crash):
    probe, args, marker = crash
    proc = _spawn_crash(mode="crash_after_reserve", db=probe.db, store=probe.store,
                        object_key="doc", operation_id="op-1", value=args["value"],
                        base_revision=1, marker=marker)
    _wait_for(marker)
    code = _kill(proc)
    assert code != 0
    assert not Path(f"{marker}.done").exists()

    restarted = K.Coordinator(str(probe.db))
    record = restarted.get("op-1")
    assert record["state"] == K.PREPARED
    assert record["attempts"] == [], "no attempt may exist when dispatch never began"
    assert restarted.holder_of("doc") == "op-1"
    assert harness.store_revision(str(probe.store), "doc") == 1

    blocked = restarted.run(operation_id="op-2", actor="other", received_args=args,
                            mutate=lambda: pytest.fail("a held object must not be written"))
    assert blocked["status"] == K.CONFLICT

    settled = restarted.reconcile("op-1", _evidence(probe.provider))
    assert settled["state"] == K.FAILED_BEFORE_EXECUTION
    assert restarted.holder_of("doc") is None

    # Only now may a new intent proceed, and it performs the one write.
    fresh = restarted.run(
        operation_id="op-2", actor="other", received_args=args,
        mutate=lambda: probe.provider.update("doc", expected_revision=1, value=args["value"]),
    )
    assert fresh["status"] == K.CONFIRMED
    assert harness.store_revision(str(probe.store), "doc") == 2
    assert restarted.get("op-1")["state"] == K.FAILED_BEFORE_EXECUTION


def test_unknown_survives_restart_and_stays_held_until_evidence(crash):
    probe, args, marker = crash
    proc = _spawn_crash(mode="crash_after_write", db=probe.db, store=probe.store,
                        object_key="doc", operation_id="op-1", value=args["value"],
                        base_revision=1, marker=marker)
    _wait_for(marker)
    _kill(proc)

    # A coordinator that cannot yet read provider evidence records what it knows.
    first = K.Coordinator(str(probe.db))
    first.mark_unknown("op-1", "restart before provider evidence could be read")
    assert first.get("op-1")["state"] == K.UNKNOWN

    # That judgement is durable: a later process does not "helpfully" forget it.
    second = K.Coordinator(str(probe.db))
    assert second.get("op-1")["state"] == K.UNKNOWN
    assert second.holder_of("doc") == "op-1"

    # Time alone does not release it, and retention does not touch it.
    second.prune_resolved(older_than_seconds=0)
    assert second.get("op-1")["state"] == K.UNKNOWN
    assert second.get("op-1")["note"].startswith("outcome not determinable")

    settled = second.reconcile("op-1", _evidence(probe.provider))
    assert settled["state"] == K.CONFIRMED
    assert second.holder_of("doc") is None
