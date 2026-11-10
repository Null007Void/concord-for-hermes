"""Fixtures for the C3 GitHub mock — one fake repository, one real child process.

Nothing here patches the adapter. ``adapter()`` returns the real adapter
configured to spawn ``tests/fakegh/gh.py`` with the current interpreter; every
argv, payload and exit code the tests assert on was produced by that process.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import concord_gh_adapter as A
from fakegh.config import configure as set_config

FAKEGH = pathlib.Path(__file__).resolve().parent / "fakegh" / "gh.py"
OWNER, REPO = "octo", "disposable"
BRANCH = "main"
PATH = "notes/plan.md"
OBJECT = f"github:{OWNER}/{REPO}@{BRANCH}:{PATH}"
VALUE = "line one\n"


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _write_repo(root: pathlib.Path, repo: dict) -> None:
    tmp = root / "repo.json.tmp"
    tmp.write_text(json.dumps(repo), encoding="utf-8")
    tmp.replace(root / "repo.json")


def seed(root: pathlib.Path, files: dict[str, str] | None = None) -> dict[str, str]:
    """Create a fake repository with ``files`` (path -> text). Returns path -> blob SHA."""
    root.mkdir(parents=True, exist_ok=True)
    files = {"notes/plan.md": VALUE} if files is None else files
    repo: dict = {"files": {}, "commits": [], "writes": 0}
    for path, text in files.items():
        data = text.encode("utf-8")
        sha = blob_sha(data)
        repo["files"][path] = {"sha": sha, "content": base64.b64encode(data).decode("ascii")}
        repo["commits"].append(
            {
                "sha": hashlib.sha1(f"seed:{path}".encode()).hexdigest(),
                "path": path,
                "message": "seed the repository",
                "branch": BRANCH,
            }
        )
    for commit in repo["commits"]:
        commit["files"] = copy.deepcopy(repo["files"])
    _write_repo(root, repo)
    return {path: entry["sha"] for path, entry in repo["files"].items()}


def configure(monkeypatch, root: pathlib.Path, fixture: str = "write_ok",
              version: str = "2.40.1") -> None:
    monkeypatch.setenv("GH_CONFIG_DIR", str(root))
    set_config(root, fixture=fixture, version=version)
    monkeypatch.setenv("CONCORD_GH_MOCK", "1")


def adapter(root: pathlib.Path, *, allow=None, command=None, timeout_s: float = 30.0):
    allow = {(OWNER, REPO)} if allow is None else allow
    command = command or (sys.executable, str(FAKEGH))
    return A.GithubContentsAdapter(
        allowed_repositories=allow,
        gh_command=command,
        timeout_s=timeout_s,
    )


def setup(monkeypatch, tmp_path, *, fixture: str = "write_ok", version: str = "2.40.1",
          files: dict[str, str] | None = None, **adapter_kwargs) -> SimpleNamespace:
    root = tmp_path / "fake-gh"
    shas = seed(root, files)
    configure(monkeypatch, root, fixture=fixture, version=version)
    return SimpleNamespace(
        root=root,
        sha=shas[PATH],
        object=OBJECT,
        value=VALUE,
        adapter=adapter(root, **adapter_kwargs),
        A=A,
    )


def captures(root: pathlib.Path) -> list[dict]:
    path = root / "capture.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repo_state(root: pathlib.Path) -> dict:
    return json.loads((root / "repo.json").read_text(encoding="utf-8"))


def writes(root: pathlib.Path) -> int:
    return int(repo_state(root).get("writes", 0))


def content_at(root: pathlib.Path, path: str = PATH) -> str | None:
    entry = repo_state(root).get("files", {}).get(path)
    if entry is None:
        return None
    return base64.b64decode(entry["content"]).decode("utf-8")


def apply_deferred(root: pathlib.Path) -> dict:
    """Land every deferred request at the fake provider - a provider-side event.

    This runs the harness control channel as a real child process, so the late
    application happens outside the process under test, exactly as a delayed
    server-side application would.
    """
    completed = subprocess.run(
        [sys.executable, str(FAKEGH), "--apply-deferred"],
        capture_output=True, text=True,
        env={**os.environ, "GH_CONFIG_DIR": str(root), "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def puts(root: pathlib.Path) -> int:
    return sum(1 for record in captures(root) if record.get("op") == "put")
