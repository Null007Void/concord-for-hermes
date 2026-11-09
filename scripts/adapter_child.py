"""One real OS process running a guarded GitHub write — for the C3 crash window.

Usage (positional):
    adapter_child.py <plugin_dir> <db> <object_key> <operation_id>
                     <value_json> <base_sha> <heartbeat> <result>

The process heartbeats while it is alive so the fake ``gh`` child can tell the
difference between "still waiting" and "the dispatcher was killed" without
depending on Windows process APIs. The parent test kills this process with
``taskkill /F`` while the provider write has applied and the receipt has not
been recorded — the exact window C3 has to survive.

It never writes ``<result>`` in that case: its absence is the proof that the
process died at the boundary rather than settling afterwards.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time


def _heartbeat(path: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))
        except OSError:
            pass
        stop.wait(0.05)


def main() -> int:
    plugin_dir, db, object_key, operation_id, value_json, base_sha, heartbeat, result = sys.argv[1:9]
    for part in plugin_dir.split(os.pathsep):
        if part and part not in sys.path:
            sys.path.insert(0, part)

    stop = threading.Event()
    thread = threading.Thread(target=_heartbeat, args=(heartbeat, stop), daemon=True)
    thread.start()

    import concord_gh_adapter as A  # noqa: E402  (sys.path is set above)
    import concord_kernel as K  # noqa: E402

    adapter = A.GithubContentsAdapter(
        allowed_repositories=[("octo", "disposable")],
        gh_command=(sys.executable, os.path.join(os.path.dirname(__file__), "..", "tests", "fakegh", "gh.py")),
    )
    coordinator = K.Coordinator(db)
    payload = {
        "pid": os.getpid(),
        "object": object_key,
        "operation": operation_id,
        "fixture": os.environ.get("FAKEGH_FIXTURE"),
    }
    try:
        outcome = A.guarded_write(
            coordinator,
            adapter,
            object_key=object_key,
            operation_id=operation_id,
            value=json.loads(value_json),
            expected_revision=base_sha,
            actor="crasher",
        )
        payload.update(outcome)
        with open(result, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
        print(json.dumps(payload, default=str))
        return 0
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
