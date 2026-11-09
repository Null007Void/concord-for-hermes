"""One contender in the two-process C1 test — a real, separate OS process.

Usage (all positional):
    contend.py <plugin_dir> <db> <store> <object_key> <operation_id>
               <actor> <value> <expected_revision> <start_at_epoch> <out_path>

The contender spins until its start instant so both processes hit the
coordinator together, then does the full admit -> dispatch -> confirm sequence
itself. It imports the same concord_core module the plugin uses, so the
concurrency proof is about SQLite's cross-process behaviour, not a mock.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    (plugin_dir, db, store, object_key, operation_id, actor, value,
     expected_revision, start_at, out_path) = sys.argv[1:11]
    sys.path.insert(0, plugin_dir)

    from concord_core import (  # noqa: E402  (path is set above)
        ADMITTED,
        Coordinator,
        LocalProvider,
        StaleRevision,
        fingerprint,
    )

    expected_revision = int(expected_revision)
    start_at = float(start_at)
    fp = fingerprint(
        object_key=object_key,
        operation_id=operation_id,
        value=value,
        expected_revision=expected_revision,
    )

    record = {
        "pid": os.getpid(),
        "object": object_key,
        "operation": operation_id,
        "actor": actor,
    }
    started = time.time()
    while time.time() < start_at:      # align both processes on one instant
        time.sleep(0.001)

    try:
        coord = Coordinator(db)
        admission = coord.admit(
            object_key=object_key, operation_id=operation_id, fingerprint=fp, actor=actor
        )
        record.update(kind=admission.kind, reason=admission.reason, holder=admission.holder,
                      admit_ms=round((time.time() - started) * 1000, 1))
        if admission.kind == ADMITTED:
            coord.begin_dispatch(operation_id)
            try:
                receipt = LocalProvider(store).update(
                    object_key, expected_revision=expected_revision, value=value
                )
            except StaleRevision as exc:
                coord.fail_before_execution(operation_id, "stale_revision")
                record.update(status="conflict", detail=str(exc))
            else:
                coord.confirm(operation_id, receipt)
                record.update(status="confirmed", receipt=receipt)
        else:
            record.update(status=admission.kind)
    except Exception as exc:  # report, never mask
        record.update(status="error", error=f"{type(exc).__name__}: {exc}")

    payload = json.dumps(record)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
