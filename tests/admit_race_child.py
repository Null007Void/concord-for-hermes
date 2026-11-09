"""One contender for the C9 concurrent-admission regression - a real OS process.

Usage (all positional):
    admit_race_child.py <plugin_dir> <db> <object_key> <operation_id> <start_at> <out_path>

The contender spins until its start instant (the harness the C2 process tests
already use for an aligned race - no sleep that hopes an interleaving happens),
then performs the REAL admission and writes its outcome as JSON.

It admits only: the regression is about admission, so the provider is not
involved and no second external effect can be created by the test.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    plugin_dir, db, object_key, operation_id, start_at, out_path = sys.argv[1:7]
    sys.path.insert(0, plugin_dir)

    import concord_kernel as K  # noqa: E402  (path is set above)

    material = {"object": object_key, "value": "race", "expected_revision": 1}
    started = time.time()
    while time.time() < float(start_at):  # align both contenders on one instant
        time.sleep(0.001)

    record = {"pid": os.getpid(), "object": object_key, "operation": operation_id}
    try:
        coordinator = K.Coordinator(db)
        admission = coordinator.admit(
            object_key=object_key,
            operation_id=operation_id,
            fingerprint=K.fingerprint(material),
            base_revision=1,
            actor="contender",
        )
        record.update(kind=admission.kind, reason=admission.reason,
                      holder=admission.holder, state=admission.state,
                      admit_ms=round((time.time() - started) * 1000, 1))
    except BaseException as exc:  # report, never mask
        record.update(kind="error", error=f"{type(exc).__name__}: {exc}")

    payload = json.dumps(record)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
