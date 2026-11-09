"""Short offline demonstration; direct handler, not Hermes loader integration.

Run with Python 3.14 from any directory. Only the bundled fake provider is
spawned. No gh on PATH, network, credentials or existing coordinator store.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def _worker() -> None:
    sys.path.insert(0, str(ROOT / "plugin"))
    import concord_gh_adapter as A
    import concord_kernel as K
    import concord_store as S
    import concord_tool as T

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="concord-offline-demo-") as directory:
        root = Path(directory)
        state = root / "fake"
        state.mkdir()
        data = b"seed\n"
        sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        files = {"notes/plan.md": {"sha": sha, "content": base64.b64encode(data).decode()}}
        (state / "repo.json").write_text(json.dumps({
            "files": files,
            "commits": [{"sha": "a" * 40, "path": "notes/plan.md", "message": "seed",
                         "branch": "main", "files": files}],
            "writes": 0,
        }), encoding="utf-8")
        os.environ.update(FAKEGH_STATE=str(state), FAKEGH_FIXTURE="write_ok",
                          FAKEGH_VERSION="2.40.1", CONCORD_GH_MOCK="1")
        command = (sys.executable, str(ROOT / "tests" / "fakegh" / "gh.py"))
        settings = S.Settings(allowed_repositories=(("octo", "disposable"),),
                              gh_command=command, timeout_s=3, store_path=root / "coordinator.db")
        args = dict(repository="octo/disposable", path="notes/plan.md", branch="main",
                    operation_id="demo-1", expected_revision=sha, content="coordinated edit\n")

        def call(expected, **changes):
            result = T.run_write(dict(args, **changes), settings=settings, actor="offline-demo")
            if result["outcome"] != expected:
                raise RuntimeError(f"Expected {expected}: {result}")
            print(json.dumps({"outcome": result["outcome"], "operation_id": result["operation_id"],
                              "held": result["held"], "second_write": result["second_write"]}), flush=True)
            return result

        print("OFFLINE ONLY: temporary store and bundled fake GitHub child process.", flush=True)
        confirmed = call("confirmed")
        replayed = call("replayed")
        if replayed["second_write"] is not False or replayed["receipt"] != confirmed["receipt"]:
            raise RuntimeError("Replay did not preserve the original receipt")
        call("conflict", operation_id="demo-stale", content="stale draft\n")
        call("refused", repository="other/not-allowed", operation_id="demo-denied")
        current = json.loads((state / "repo.json").read_text(encoding="utf-8"))["files"]["notes/plan.md"]["sha"]
        os.environ["FAKEGH_FIXTURE"] = "write_ambiguous"
        unknown = call("unknown", operation_id="demo-unknown", expected_revision=current, content="ambiguous edit\n")
        if not unknown["held"] or unknown["auto_retry"] is not False:
            raise RuntimeError("Unknown outcome lost its hold or permits automatic retry")
        os.environ["FAKEGH_FIXTURE"] = "write_ok"
        coordinator = K.Coordinator(str(settings.store_path), normalize_object_key=A.canonical_object_key)
        adapter = A.GithubContentsAdapter(allowed_repositories=set(settings.allowed_repositories),
                                           gh_command=command, timeout_s=3)
        before = json.loads((state / "repo.json").read_text(encoding="utf-8"))["writes"]
        reconciled = A.reconcile_from_provider(coordinator, adapter, operation_id="demo-unknown",
                                                object_key="github:octo/disposable@main:notes/plan.md")
        after = json.loads((state / "repo.json").read_text(encoding="utf-8"))["writes"]
        if reconciled.get("state") != "confirmed" or before != after or after != 2:
            raise RuntimeError(f"Read-only reconciliation failed: {reconciled}; writes={before}/{after}")
        print("Reconciled from a provider read; exactly 2 fake writes total, no retry write.", flush=True)
        print(f"PASS: five outcomes and read-only recovery in {time.monotonic() - started:.2f}s. No live verification.", flush=True)


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        _worker()
        return 0
    if sys.argv[1:]:
        print("Usage: python scripts/release_demo.py", file=sys.stderr)
        return 2
    # A clean child environment does not forward provider credential values.
    keep = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PATH", "COMSPEC", "PATHEXT")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                                env=env, timeout=80, capture_output=True, text=True, encoding="utf-8")
    except subprocess.TimeoutExpired:
        print("FAIL: offline demo exceeded its 80-second budget; this is not a pass.", file=sys.stderr)
        return 1
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
