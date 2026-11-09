"""Credential-free recording rehearsal using an installed Hermes plugin.

Uses the real Hermes plugin loader, registered CLI callbacks and tool executor.
The agent object is a scripted test double and the provider is bundled fake gh.
No model, credential, live provider, user's profile or durable store is used.
Run with the documented Hermes Python environment; see VERIFICATION.md.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def _worker() -> None:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="concord-recording-") as directory:
        root = Path(directory)
        home = root / "home"
        installed = home / "plugins" / "concord-for-hermes"
        installed.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "plugin", installed,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        command = [sys.executable, str(ROOT / "tests" / "fakegh" / "gh.py")]
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - concord-for-hermes\n  entries:\n"
            "    concord-for-hermes:\n      settings:\n        allowed_repositories:\n"
            "          - octo/disposable\n        gh_command: " + json.dumps(command) + "\n",
            encoding="utf-8",
        )
        os.environ.update(HOME=str(root), USERPROFILE=str(root),
                          HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(home / "runtime"),
                          CONCORD_DB=str(root / "coordinator.db"), CONCORD_GH_MOCK="1",
                          FAKEGH_STATE=str(root / "fake"), FAKEGH_VERSION="2.40.1",
                          FAKEGH_FIXTURE="write_ok")
        sys.path[:0] = [str(installed), str(ROOT / "tests")]
        import hermes_cli.plugins as P
        import harness
        import github_harness as H

        manager = P.PluginManager()
        manager.discover_and_load()
        assert "concord-for-hermes" in manager._plugins, "Installed plugin did not load"
        P._plugin_manager = manager
        import concord_tool
        assert Path(concord_tool.__file__).parent == installed, "Demo imported source, not installed copy"
        cli = argparse.ArgumentParser(prog="hermes")
        from hermes_cli.main import _attach_plugin_cli_command
        subparsers = cli.add_subparsers()
        _attach_plugin_cli_command(subparsers, manager._cli_commands["concord"])

        def run_cli(*arguments: str) -> dict:
            parsed = cli.parse_args(["concord", *arguments])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = parsed.func(parsed)
            assert code == 0, out.getvalue()
            return json.loads(out.getvalue())

        print("OFFLINE REHEARSAL: real Hermes loader + tool executor; scripted agent, simulated GitHub.", flush=True)
        print("Installed copy loaded. No credentials, model or live GitHub calls.", flush=True)
        baseline = H.seed(root / "fake")[H.PATH]
        args = dict(repository="octo/disposable", path=H.PATH, branch="main",
                    operation_id="recording-1", expected_revision=baseline, content="coordinated edit\n")

        def call(label: str, expected: str, session: str = "agent-A", **overrides) -> dict:
            result = harness.run_tool(harness.make_agent(session), "concord_github_write",
                                      dict(args, **overrides), scope=manager.scope_key)
            shape = json.loads(result.result)
            assert shape["outcome"] == expected, shape
            print(label + ": " + json.dumps({key: shape[key] for key in
                  ("outcome", "operation_id", "held", "second_write")}), flush=True)
            return shape

        first = call("1. First intent", "confirmed")
        replay = call("2. Identical replay", "replayed")
        assert replay["receipt"] == first["receipt"] and H.writes(root / "fake") == 1
        call("3. Another session, stale draft", "conflict", session="agent-B",
             operation_id="recording-stale", content="stale edit\n")
        call("4. Outside allowlist", "refused", repository="other/not-allowed",
             operation_id="recording-denied")
        assert H.writes(root / "fake") == 1
        current = H.repo_state(root / "fake")["files"][H.PATH]["sha"]
        os.environ["FAKEGH_FIXTURE"] = "write_ambiguous"
        uncertain = call("5. SIMULATED lost response", "unknown", operation_id="recording-unknown",
                         expected_revision=current, content="ambiguous edit\n")
        assert uncertain["held"] is True and uncertain["auto_retry"] is False
        # CLI is parsed and dispatched through the descriptor registered by the
        # installed plugin, using Hermes' own CLI attachment function.
        status = run_cli("status", "--json")
        assert status["store"]["readable"] is True
        assert status["unresolved"] == 1
        assert any(obj["holder"] == "recording-unknown" and obj["holder_state"] == "unknown"
                   for obj in status["objects"])
        print("Registered Hermes status: unresolved operation retained; auto_retry=false.", flush=True)
        os.environ["FAKEGH_FIXTURE"] = "read_ok"
        before = H.writes(root / "fake")
        settled = run_cli("reconcile", "recording-unknown", "--json")
        assert settled.get("state") == "confirmed", settled
        assert H.writes(root / "fake") == before == 2
        assert run_cli("status", "--json")["unresolved"] == 0
        print("6. Registered Hermes reconcile: confirmed by read; no retry write.", flush=True)
        print(f"PASS: installed Hermes rehearsal in {time.monotonic()-started:.2f}s; exactly 2 simulated writes.", flush=True)
        print("Boundary: enrolled existing text-file replacements, shared local store only. LIVE TEST STILL REQUIRED.", flush=True)


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        _worker()
        return 0
    if sys.argv[1:]:
        print("Usage: python scripts/hermes_recording_demo.py", file=sys.stderr)
        return 2
    keep = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PATH", "COMSPEC", "PATHEXT")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                                env=env, timeout=120, capture_output=True, text=True, encoding="utf-8")
    except subprocess.TimeoutExpired:
        print("FAIL: rehearsal exceeded 120 seconds; not a pass.", file=sys.stderr)
        return 1
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
