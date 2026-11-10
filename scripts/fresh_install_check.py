"""Reproduce a first-time install of Concord from a clean source archive.

This is the C5 acceptance check, written so a stranger can run it: it never
reads the developer's working checkout for anything it installs. The only
inputs are

  * a git revision of this repository (default ``HEAD``), exported with
    ``git archive`` into a temporary directory;
  * a Python interpreter and a ``hermes`` executable (the pinned runtime).

It then follows the README exactly:

  1. copy ``plugin/`` to ``$HERMES_HOME/plugins/concord-for-hermes`` (a copy,
     not a symlink) and write the documented ``config.yaml``;
  2. ``hermes plugins validate`` / ``doctor`` / ``list``;
  3. ``hermes concord status`` on a home with no store;
  4. the offline quick start: real plugin loader, real CLI, real tool executor,
     process-level fake ``gh`` - status, confirmed, replay, conflict, unknown,
     reconcile, export;
  5. the documented test path (``scripts/install_plugin.py`` + ``pytest``).

Nothing here touches the network, a credential, or a real Hermes home. The
fake ``gh`` is the one in ``tests/fakegh/gh.py``; every provider response in
the transcript comes from that child process.

Usage
-----
    python scripts/fresh_install_check.py                  # archive HEAD
    python scripts/fresh_install_check.py --rev 40072c1    # a specific commit
    python scripts/fresh_install_check.py --out log.txt    # keep transcript
    python scripts/fresh_install_check.py --skip-tests     # install proof only

Exit code is 0 only if every check passed. The transcript is printed and, with
``--out``, also written to that path.
"""
from __future__ import annotations

import argparse
import io
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[1]                      # the repository this script ships in


def _find_hermes() -> pathlib.Path:
    override = (os.environ.get("HERMES_BIN") or "").strip()
    if override:
        candidate = pathlib.Path(override)
        if candidate.exists():
            return candidate
    for guess in (
        REPO.parent / ".venv" / "Scripts" / "hermes.exe",
        REPO.parent / ".venv" / "bin" / "hermes",
    ):
        if guess.exists():
            return guess
    found = shutil.which("hermes")
    if found:
        return pathlib.Path(found)
    raise SystemExit(
        "no hermes executable found. Activate the Hermes environment or set "
        "HERMES_BIN to the hermes executable you want to test."
    )


def _git(*args: str, cwd: pathlib.Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(cwd or REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    if completed.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


class Transcript:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.checks = 0
        self.tmp: pathlib.Path | None = None

    def note(self, text: str = "") -> None:
        self.lines.append(text)

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"

    def section(self, title: str) -> None:
        self.note()
        self.note("=" * 72)
        self.note(title)
        self.note("=" * 72)

    def command(self, argv: list[str], *, env: dict | None = None,
                env_note: str = "", timeout: int = 900,
                grep: str = "") -> subprocess.CompletedProcess:
        shown = " ".join(str(a) for a in argv)
        self.note(f"$ {shown}" + (f"    # {env_note}" if env_note else ""))
        completed = subprocess.run(
            [str(a) for a in argv], capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (completed.stdout or "").rstrip("\n")
        err = (completed.stderr or "").rstrip("\n")
        if grep:
            kept = [line for line in out.splitlines() if grep in line]
            out = "\n".join(kept) if kept else "<no matching line>"
        if out:
            self.note(out)
        if err:
            self.note("[stderr] " + err.replace("\n", "\n[stderr] "))
        self.note(f"[exit {completed.returncode}]")
        return completed

    def check(self, condition: bool, description: str, detail: str = "") -> bool:
        self.checks += 1
        mark = "PASS" if condition else "FAIL"
        self.note(f"[{mark}] {description}" + (f" - {detail}" if detail else ""))
        if not condition:
            self.failures.append(description + (f" ({detail})" if detail else ""))
        return condition


DRIVER = r'''
"""One quick-start tool call through the real loader and the real executor.

Run inside the exported archive with HERMES_HOME pointing at the fresh home.
The product plugin is imported from the *installed copy*, not from the
repository that was exported.
"""
import json, os, pathlib, sys

home = pathlib.Path(os.environ["CONCORD_FRESH_HOME"])
archive = pathlib.Path(os.environ["CONCORD_ARCHIVE"])
installed = home / "plugins" / "concord-for-hermes"
sys.path.insert(0, str(installed))
sys.path.insert(0, str(archive / "tests"))

import hermes_cli.plugins as P

manager = P.PluginManager()
manager.discover_and_load()
if "concord-for-hermes" not in manager._plugins:
    raise SystemExit("product plugin did not load: " + repr(sorted(manager._plugins)))
P._plugin_manager = manager

import harness

payload = json.loads(sys.argv[1])
result = harness.run_tool(harness.make_agent("fresh-install"), "concord_github_write", {
    "repository": "octo/disposable",
    "path": "notes/plan.md",
    "branch": "main",
    "operation_id": payload["operation_id"],
    "expected_revision": payload["expected_revision"],
    "content": payload["content"],
})
print(json.dumps({"result": result.result, "success": getattr(result, "success", None)},
                  indent=2))
'''


def main() -> int:
    """Run the walkthrough, then persist the transcript BEFORE rendering it.

    Windows' default standard-output encoding is a legacy codepage; Hermes'
    validate output contains U+2713, and the old order - print, then write -
    meant a UnicodeEncodeError destroyed the transcript after every check had
    already run (review P2-5). The transcript is now written first, the console
    rendering has an explicit error strategy, and an exception is recorded in
    the transcript rather than replacing it.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tree", action="store_true",
                        help="verify and copy a history-free release using RELEASE-MANIFEST.json")
    parser.add_argument("--rev", default="HEAD", help="revision to export (default HEAD)")
    parser.add_argument("--out", default="", help="write the transcript here")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the pytest phase (install + quick start only)")
    parser.add_argument("--workdir", default="",
                        help="where to create the throwaway tree (default: the "
                             "repository's parent directory)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary tree")
    args = parser.parse_args()

    _harden_console()
    t = Transcript()
    status = 0
    try:
        _walkthrough(args, t)
    except BaseException as exc:  # noqa: BLE001 - the transcript is the deliverable
        t.note()
        t.note(f"[fatal] {type(exc).__name__}: {exc}")
        t.note(traceback.format_exc().rstrip())
        status = 1

    t.section("summary")
    t.note(f"checks: {t.checks}   failures: {len(t.failures)}")
    for failure in t.failures:
        t.note(f"  FAILED: {failure}")
    t.note(f"temporary tree: {t.tmp if t.tmp else '(none created)'}")
    text = t.render()

    # Write first: the file is the artifact a reviewer keeps, and it must exist
    # even if this console cannot encode a character in it.
    if args.out:
        path = pathlib.Path(args.out)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            _safe_print(f"[transcript could not be written to {path}: {exc}]\n")
            status = 1
        else:
            _safe_print(f"[transcript written to {path}]\n")
    _safe_print(text)

    if not args.keep and t.tmp is not None:
        shutil.rmtree(t.tmp, ignore_errors=True)
    if t.failures:
        return 1
    return status


def _harden_console() -> None:
    """Give stdout/stderr a defined behaviour for characters they cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def _safe_print(text: str) -> None:
    """Render to the console without ever raising, whatever the encoding is."""
    stream = sys.stdout
    try:
        stream.write(text)
        stream.flush()
        return
    except UnicodeEncodeError:
        pass
    except (ValueError, OSError):
        return
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
        stream.flush()
    except (ValueError, OSError):
        pass


def _walkthrough(args: argparse.Namespace, t: Transcript) -> None:
    hermes = _find_hermes()
    python = pathlib.Path(sys.executable)

    t.section("0. clean source archive")
    manifest = None
    if args.source_tree:
        manifest = json.loads((REPO / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        rev = "release-tree:" + manifest["source_commit"]
        expected = set(manifest["files"]) | {"RELEASE-MANIFEST.json"}
    else:
        rev = _git("rev-parse", args.rev).strip()
        expected = set(_git("ls-tree", "-r", "--name-only", rev).splitlines())
    t.note(f"repository: {REPO}")
    t.note(f"revision:   {rev}")
    t.note(f"hermes:     {hermes} ({_hermes_version(hermes)})")
    t.note(f"python:     {python} ({sys.version.split()[0]})")
    t.note(f"tracked:    {len(expected)} files")
    workdir = pathlib.Path(args.workdir).resolve() if args.workdir else REPO.parent
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".c5-fresh-", dir=str(workdir)))
    t.tmp = tmp
    archive = tmp / "concord-for-hermes-download"
    home = tmp / "isolated-home"
    archive.mkdir()
    if manifest is not None:
        for relative, wanted in manifest["files"].items():
            rel = pathlib.PurePosixPath(relative)
            if rel.is_absolute() or ".." in rel.parts or "\\" in relative or ":" in relative:
                raise ValueError("unsafe release manifest path")
            source = REPO / relative
            if source.is_symlink() or not source.is_file():
                raise ValueError("missing or symlinked release file: " + relative)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != wanted:
                raise ValueError("release hash mismatch: " + relative)
            destination = archive / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        shutil.copy2(REPO / "RELEASE-MANIFEST.json", archive)
    else:
        blob = subprocess.run(["git", "archive", "--format=tar", rev], cwd=str(REPO),
                              capture_output=True, timeout=120)
        if blob.returncode != 0:
            raise SystemExit("git archive failed: " + blob.stderr.decode("utf-8", "replace"))
        with tarfile.open(fileobj=io.BytesIO(blob.stdout)) as tar:
            tar.extractall(archive, filter="data")
    t.check(not (archive / ".git").exists(), "exported tree has no .git directory")
    actual = {p.relative_to(archive).as_posix() for p in archive.rglob("*") if p.is_file()}
    t.check(expected == actual, "exported tree matches verified file inventory",
            f"{len(actual)} files" if expected == actual else
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}")

    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(home).replace("\\", "/"),
        "HERMES_BIN": str(hermes),
        "HERMES_RUNTIME_DIR": str(home / "runtime").replace("\\", "/"),
        "CONCORD_DB": str(tmp / "coordinator.db").replace("\\", "/"),
        "CONCORD_FRESH_HOME": str(home).replace("\\", "/"),
        "CONCORD_ARCHIVE": str(archive).replace("\\", "/"),
        "CONCORD_GH_MOCK": "1",
        "GH_CONFIG_DIR": str(tmp / "fake-gh").replace("\\", "/"),
    })

    def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return t.command(argv, env=env, **kwargs)

    def cli(*rest: str) -> subprocess.CompletedProcess:
        return run([str(hermes), *rest], timeout=300)

    # 1. documented install -------------------------------------------------
    t.section("1. install, exactly as the README says")
    destination = home / "plugins" / "concord-for-hermes"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(archive / "plugin", destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = (
        "# fresh HERMES_HOME written by scripts/fresh_install_check.py\n"
        "plugins:\n"
        "  enabled:\n"
        "    - concord-for-hermes\n"
        "  entries:\n"
        "    concord-for-hermes:\n"
        "      settings:\n"
        "        allowed_repositories:\n"
        "          - octo/disposable\n"
        "        gh_command:\n"
        f"          - {json.dumps(str(python))}\n"
        f"          - {json.dumps(str(archive / 'tests' / 'fakegh' / 'gh.py'))}\n"
    )
    (home / "config.yaml").write_text(config, encoding="utf-8")
    t.note(f"$ cp -r plugin {destination}    # a copy, not a symlink")
    t.note(f"$ cat {home / 'config.yaml'}")
    t.note(config.rstrip())
    t.check(not list(destination.rglob("__pycache__")), "installed copy has no __pycache__")
    t.check(sorted(p.name for p in destination.iterdir()) == sorted(
        p.name for p in (archive / "plugin").iterdir() if p.name != "__pycache__"),
        "installed file set matches plugin/")

    # 2. validate / doctor / list -------------------------------------------
    t.section("2. validate, doctor, discovery")
    validate = cli("plugins", "validate", str(destination).replace("\\", "/"))
    t.check(validate.returncode == 0 and "Validation passed." in validate.stdout,
            "hermes plugins validate passes")

    doctor = cli("plugins", "doctor", str(destination).replace("\\", "/"))
    t.check(doctor.returncode == 0 and "OK:" in doctor.stdout, "hermes plugins doctor passes")

    listing = run([str(hermes), "plugins", "list"], grep="concord-for-hermes", timeout=300)
    rows = [line for line in listing.stdout.splitlines() if "concord-for-hermes" in line]
    t.check(bool(rows), "plugin is listed as installed/enabled")

    # 3. status on a home with no store -------------------------------------
    t.section("3. status before anything has run")
    status = cli("concord", "status")
    t.check(status.returncode == 0 and "no coordinator store" in status.stdout.lower(),
            "fresh home reports no store and creates none")
    t.check(not (tmp / "coordinator.db").exists(), "status did not create the store file")

    help_out = cli("concord", "--help")
    t.check("NOT PROTECTED" in help_out.stdout, "the coverage boundary is in the command help")

    # 4. offline quick start -------------------------------------------------
    t.section("4. quick start against the fake gh")
    sys.path.insert(0, str(archive / "tests"))
    sys.path.insert(0, str(destination))
    import github_harness as H  # noqa: E402

    shas = H.seed(tmp / "fake-gh")
    seed_sha = shas["notes/plan.md"]
    driver = tmp / "drive_tool.py"
    driver.write_text(DRIVER, encoding="utf-8")

    def call(operation_id: str, content: str, fixture: str, *, expected: str = "") -> dict:
        """One tool call through the real loader. ``expected`` is the revision a
        caller would read now; the tool requires it (review P1-1)."""
        H.set_config(tmp / "fake-gh", fixture=fixture, version="2.40.1")
        revision = expected or H.repo_state(tmp / "fake-gh")["files"]["notes/plan.md"]["sha"]
        completed = run(
            [str(python), str(driver),
             json.dumps({"operation_id": operation_id, "content": content,
                         "expected_revision": revision})],
            env_note=f"fixture={fixture}",
        )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"raw": completed.stdout, "stderr": completed.stderr}

    first = call("plan-refresh-1", "line one\nline two\n", "write_ok")
    confirmed = json.loads(first["result"]).get("outcome") if first.get("result") else None
    t.check(confirmed == "confirmed", "first write is confirmed", str(confirmed))

    # A replay is the SAME intent: the same operation_id, the same content AND
    # the same observed base revision. The base is part of the operation's
    # identity now (review P1-1), so a repeat with a freshly read revision is a
    # different intent and must be a new operation id.
    replay = call("plan-refresh-1", "line one\nline two\n", "write_ok", expected=seed_sha)
    replayed = json.loads(replay["result"]).get("outcome") if replay.get("result") else None
    t.check(replayed == "replayed",
            "same operation_id + content + observed base is a replay", str(replayed))
    if replay.get("result"):
        t.check(json.loads(replay["result"]).get("second_write") is False,
                "replay did not write again")

    rebound = call("plan-refresh-1", "line one\nline two\n", "write_ok")
    rebound_outcome = (
        json.loads(rebound["result"]).get("outcome") if rebound.get("result") else None
    )
    t.check(rebound_outcome == "conflict",
            "the same id + content with a different base is a conflict", str(rebound_outcome))

    mismatch = call("plan-refresh-1", "different content\n", "write_ok")
    conflict = json.loads(mismatch["result"]).get("outcome") if mismatch.get("result") else None
    t.check(conflict == "conflict", "same id + different content is a conflict", str(conflict))

    writes_before = H.writes(tmp / "fake-gh")
    stale_draft = call("plan-stale-draft", "prepared from the first revision\n",
                       "write_ok", expected=seed_sha)
    stale_draft_outcome = (
        json.loads(stale_draft["result"]).get("outcome") if stale_draft.get("result") else None
    )
    t.check(stale_draft_outcome == "conflict",
            "a draft prepared from a superseded revision conflicts", str(stale_draft_outcome))
    t.check(H.writes(tmp / "fake-gh") == writes_before,
            "the stale draft did not overwrite the confirmed content")

    stale = call("plan-refresh-2", "stale base\n", "write_stale")
    stale_outcome = json.loads(stale["result"]).get("outcome") if stale.get("result") else None
    t.check(stale_outcome == "conflict", "provider 409 is a recorded conflict", str(stale_outcome))

    ambiguous = call("plan-ambiguous", "ambiguous content\n", "write_ambiguous")
    unknown = json.loads(ambiguous["result"]).get("outcome") if ambiguous.get("result") else None
    t.check(unknown == "unknown", "a lost response is unknown, never retried", str(unknown))

    status = cli("concord", "status")
    t.check("held" in status.stdout.lower(), "status shows the held object")

    H.set_config(tmp / "fake-gh", fixture="read_ok")
    reconcile = cli("concord", "reconcile", "plan-ambiguous")
    t.check(reconcile.returncode == 0, "reconcile settles from a provider read")

    status_json = cli("concord", "status", "--json")
    try:
        parsed = json.loads(status_json.stdout)
        t.check(isinstance(parsed, dict), "status --json is parseable")
    except json.JSONDecodeError:
        t.check(False, "status --json is parseable", status_json.stdout[:200])

    export_path = tmp / "export.json"
    export = cli("concord", "export", "--output", str(export_path))
    exported = json.loads(export_path.read_text(encoding="utf-8")) if export_path.exists() else {}
    t.check(export.returncode == 0 and len(exported.get("operations", [])) == 4
            and len(exported.get("attempts", [])) == 4,
            "export reads every operation and attempt",
            f"{len(exported.get('operations', []))} operations, "
            f"{len(exported.get('attempts', []))} attempts")

    # 5. backup and machine-readable store health ---------------------------
    t.section("5. backup and the machine-readable store view")
    backup_path = tmp / "backup.db"
    backup = cli("concord", "backup", "--output", str(backup_path))
    t.check(backup.returncode == 0 and backup_path.exists(),
            "backup writes a restorable copy", (backup.stdout or "").strip()[:120])
    exported_status = json.loads(status_json.stdout) if status_json.stdout.strip() else {}
    t.check(exported_status.get("store", {}).get("readable") is True,
            "status --json reports the store as readable")

    # 6. documented test path ------------------------------------------------
    if not args.skip_tests:
        t.section("6. the documented test path")
        install = run([str(python), str(archive / "scripts" / "install_plugin.py")])
        t.check(install.returncode == 0, "scripts/install_plugin.py succeeds")
        pytest_run = subprocess.run(
            [str(python), "-m", "pytest", "tests/", "-q",
             "--junitxml=" + str(tmp / "pytest-results.xml")],
            cwd=str(archive), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=1200,
        )
        tail = (pytest_run.stdout or "").strip().splitlines()
        t.note("$ python -m pytest tests/ -q")
        t.note("\n".join(tail if pytest_run.returncode else tail[-3:]))
        if pytest_run.stderr:
            t.note("[pytest stderr] " + pytest_run.stderr.rstrip())
        t.note(f"JUnit evidence: {tmp / 'pytest-results.xml'}")
        t.check(pytest_run.returncode == 0, "test suite passes",
                tail[-1] if tail else "no output")
        import re
        match = re.search(r"(\d+) passed", pytest_run.stdout or "")
        t.note(f"[note] collected passed count: {match.group(1) if match else 'unknown'}")
        release = run([str(python), str(archive / "scripts" / "release_checks.py")])
        t.check(release.returncode == 0, "offline release smoke checks pass from the archive")


def _hermes_version(hermes: pathlib.Path) -> str:
    try:
        completed = subprocess.run([str(hermes), "--version"], capture_output=True,
                                   text=True, timeout=60)
        return (completed.stdout or "").strip().splitlines()[0]
    except Exception as exc:                                  # pragma: no cover
        return f"unknown ({exc})"


if __name__ == "__main__":
    raise SystemExit(main())
