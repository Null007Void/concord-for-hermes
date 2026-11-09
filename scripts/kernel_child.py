"""One real OS process against the C2 kernel — for contention and crash tests.

Usage (all positional):
    kernel_child.py <plugin_dir> <mode> <db> <store> <object> <operation>
                    <value_json> <base_revision> <marker_or_out> [start_at]

Modes:
    contend             admit -> dispatch -> confirm, writes a JSON result
    crash_after_write   admit -> dispatch -> provider write -> marker -> sleep
                        (the parent kills this process with taskkill)
    crash_after_reserve admit -> marker -> sleep (never dispatches)

The crash modes deliberately never write ``<marker>.done``: its absence is the
assertion that the process died at the boundary, not after settling.
"""
from __future__ import annotations

import json
import os
import sys
import time


def _sleep_forever() -> None:
    while True:
        time.sleep(0.2)


def main() -> int:
    plugin_dir, mode, db, store, object_key, operation_id, value_json, base_revision, out = (
        sys.argv[1:10]
    )
    start_at = float(sys.argv[10]) if len(sys.argv) > 10 else 0.0
    # The kernel lives with the product plugin; the synthetic provider lives with
    # the C1 probe fixture. The caller passes both, joined by os.pathsep, and both
    # are the *installed* copies so a stale install cannot pass silently.
    for part in plugin_dir.split(os.pathsep):
        if part and part not in sys.path:
            sys.path.insert(0, part)

    import concord_kernel as K  # noqa: E402  (path is set above)
    from concord_core import LocalProvider, ProviderUnavailable, StaleRevision  # noqa: E402

    base = int(base_revision)
    value = json.loads(value_json)
    provider = LocalProvider(store)
    coordinator = K.Coordinator(db)
    material = {"object": object_key, "value": value, "expected_revision": base}
    record = {"pid": os.getpid(), "mode": mode, "object": object_key, "operation": operation_id}

    while time.time() < start_at:  # align contenders on one instant
        time.sleep(0.001)

    if mode == "contend":
        def mutate():
            try:
                return provider.update(object_key, expected_revision=base, value=value)
            except StaleRevision as exc:
                raise K.Rejected(str(exc)) from exc
            except ProviderUnavailable as exc:
                raise K.Rejected(str(exc)) from exc

        result = coordinator.run(
            operation_id=operation_id, actor="contender", received_args=material, mutate=mutate
        )
        record.update(result)
        payload = json.dumps(record, default=str)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(payload)
        return 0

    admission = coordinator.admit(
        object_key=object_key,
        operation_id=operation_id,
        fingerprint=K.fingerprint(material),
        base_revision=base,
        actor="crasher",
    )
    record["admission"] = admission.kind
    if admission.kind != K.ADMITTED:
        record["detail"] = admission.reason
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record))
        return 3

    if mode == "crash_after_reserve":
        # Reserved, nothing dispatched. The parent kills us here.
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({**record, "state": "reserved"}))
        _sleep_forever()

    if mode == "crash_after_write":
        coordinator.begin_dispatch(operation_id)
        receipt = provider.update(object_key, expected_revision=base, value=value)
        # The write is committed at the provider. The receipt is NOT yet
        # recorded: this is the exact window the crash test targets.
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({**record, "state": "pending", "receipt": receipt}))
        _sleep_forever()

    record["detail"] = f"unknown mode {mode}"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
