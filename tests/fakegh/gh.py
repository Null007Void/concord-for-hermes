"""A fake ``gh`` at the process boundary — the C3 mock provider.

It is a real child process with a real argv, real stdout/stderr and a real exit
code; the only thing it does not do is talk to GitHub. The adapter is not
patched, stubbed or monkeypatched: it spawns this file exactly as it would spawn
``gh``, and every contract assertion in ``tests/test_c3_adapter.py`` is made
against what this process actually received.

State lives in ``FAKEGH_STATE`` (a directory per test): ``repo.json`` holds the
"remote" repository, ``capture.jsonl`` records each invocation. Fixture
selection is ``FAKEGH_FIXTURE``.

Blob and commit SHAs are real git object ids (sha1 over ``blob <len>\\0<bytes>``)
so the revision the adapter sees behaves like the one GitHub returns: opaque,
content-derived, 40 hex characters.

Fixtures
--------
read_ok / any        normal GET
write_ok             PUT applies, 201, documented response shape
write_stale          PUT refused 409 before applying
write_ambiguous      PUT applies, then the process dies with a truncated body
write_then_readback_mismatch
                     PUT applies but stores different content than requested
write_deferred       PUT is recorded in ``pending.json`` and never applied; the
                     process exits 1 with no HTTP status (a transport timeout).
                     ``gh.py --apply-deferred`` applies it later, which is how a
                     test lets the original request land AFTER reconciliation.
unauthorized         PUT refused 401 before applying
rate_limited         PUT refused 403 before applying
crash_between_write_and_receipt
                     PUT applies, then blocks until the parent stops
                     heartbeating (the test kills the parent for real)

``--apply-deferred`` is a harness control channel, not part of the emulated
``gh`` surface: it is checked before the ``api`` dispatch and never stands in for
a real subcommand.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import time
from urllib.parse import parse_qsl, unquote

STATE = os.environ.get("FAKEGH_STATE")
FIXTURE = (os.environ.get("FAKEGH_FIXTURE") or "write_ok").strip()
VERSION = (os.environ.get("FAKEGH_VERSION") or "2.40.1").strip()

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def die(status: int, message: str) -> None:
    sys.stderr.write(f"gh: {message} (HTTP {status})\n" if status >= 400 else f"{message}\n")
    sys.stderr.flush()
    raise SystemExit(1 if status >= 400 else status)


def state_path(name: str) -> str:
    assert STATE, "FAKEGH_STATE is not set"
    return os.path.join(STATE, name)


def load_repo() -> dict:
    with open(state_path("repo.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_repo(repo: dict) -> None:
    tmp = state_path("repo.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(repo, handle)
    os.replace(tmp, state_path("repo.json"))


def blob_sha(data: bytes) -> str:
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def commit_sha(message: str, previous: str | None, when: float) -> str:
    payload = f"tree {previous or 'root'}\nmessage {message}\nwhen {when}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def capture(argv: list[str], extra: dict) -> None:
    record = {
        "argv": argv,
        "fixture": FIXTURE,
        "cwd": os.getcwd(),
        "gh_token_present": bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")),
        "t": time.time(),
        **extra,
    }
    with open(state_path("capture.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_body(path: str) -> tuple[dict, str]:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    return json.loads(text), text


def defer(body: dict, target_path: str) -> None:
    """Park a request the provider never answered, so it can land later."""
    path = state_path("pending.json")
    entries: list = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            entries = json.load(handle)
    entries.append({"path": target_path, "body": body, "deferred_at": time.time()})
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle)
    os.replace(tmp, path)


def apply_deferred() -> int:
    """Land every parked request against the repository, as the provider would.

    The conditional-write check is applied HERE, at application time - which is
    the point: a request that timed out can still be accepted much later, and
    whatever the target looked like when the client gave up is irrelevant to
    whether it applies.
    """
    path = state_path("pending.json")
    if not os.path.exists(path):
        print(json.dumps({"applied": 0, "requests": []}))
        return 0
    with open(path, "r", encoding="utf-8") as handle:
        entries = json.load(handle)
    repo = load_repo()
    results = []
    for entry in entries:
        body = entry.get("body") or {}
        target_path = entry.get("path") or ""
        file_entry = repo.get("files", {}).get(target_path)
        current_sha = file_entry["sha"] if file_entry else None
        if str(body.get("sha")) != str(current_sha):
            results.append({"path": target_path, "applied": False,
                            "reason": "HTTP 409 would have been returned"})
            continue
        raw = base64.b64decode(body.get("content") or "")
        new_sha = blob_sha(raw)
        message = str(body.get("message") or "")
        repo.setdefault("files", {})[target_path] = {
            "sha": new_sha,
            "content": base64.b64encode(raw).decode("ascii"),
        }
        repo.setdefault("commits", []).append(
            {
                "sha": commit_sha(message, current_sha, time.time()),
                "path": target_path,
                "message": message,
                "branch": body.get("branch"),
                "files": copy.deepcopy(repo["files"]),
            }
        )
        repo["writes"] = int(repo.get("writes", 0)) + 1
        results.append({"path": target_path, "applied": True, "blob_sha": new_sha,
                        "applied_at": time.time()})
    save_repo(repo)
    os.remove(path)
    print(json.dumps({"applied": sum(1 for r in results if r["applied"]), "requests": results}))
    return 0


def split_endpoint(endpoint: str) -> tuple[str, dict[str, str]]:
    base, _, query = endpoint.partition("?")
    return unquote(base.strip("/")), dict(parse_qsl(query, keep_blank_values=True))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--apply-deferred":
        return apply_deferred()

    if "--version" in argv:
        print(f"gh version {VERSION} (2024-01-01)")
        print("https://github.com/cli/cli/releases/latest")
        return 0

    if not argv or argv[0] != "api":
        die(2, "fakegh: only `api` and `--version` are emulated")

    method = "GET"
    body_path = None
    endpoint = None
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in ("-X", "--method"):
            method = argv[index + 1].upper()
            index += 2
            continue
        if token == "--input":
            body_path = argv[index + 1]
            index += 2
            continue
        if token == "--hostname":
            if argv[index + 1] != "github.com":
                die(2, "fakegh: unexpected hostname")
            index += 2
            continue
        if token in ("-H", "--header"):
            index += 2
            continue
        if endpoint is None:
            endpoint = token
            index += 1
            continue
        die(2, f"fakegh: unexpected argument {token!r}")
    if endpoint is None:
        die(2, "fakegh: no endpoint")

    path, params = split_endpoint(endpoint)
    parts = path.split("/")
    if parts[0] != "repos" or len(parts) < 3:
        die(2, f"fakegh: unsupported endpoint {endpoint!r}")
    repo_slug = f"{parts[1]}/{parts[2]}"

    if len(parts) == 4 and parts[3] == "commits":
        target_path = ""
    elif len(parts) >= 5 and parts[3] == "contents":
        target_path = "/".join(parts[4:])
    else:
        die(2, f"fakegh: unsupported endpoint {endpoint!r}")

    repo = load_repo()

    # commits list: repos/o/r/commits?path=..&sha=..&per_page=1
    if method == "GET" and parts[3] == "commits":
        path_filter = params.get("path")
        rows = [c for c in repo.get("commits", []) if c.get("path") == path_filter]
        rows.reverse()
        rows = rows[: int(params.get("per_page") or 30)]
        capture(argv, {"op": "commits", "path_filter": path_filter, "rows": len(rows)})
        print(json.dumps([
            {"sha": c["sha"], "commit": {"message": c["message"]}} for c in rows
        ]))
        return 0

    files = repo.get("files", {})
    if method == "GET" and params.get("ref") != "main":
        commits = [c for c in repo.get("commits", []) if c["sha"] == params.get("ref")]
        if not commits:
            die(404, "No commit found for the ref")
        files = commits[-1]["files"]
    file_entry = files.get(target_path)
    current_sha = file_entry["sha"] if file_entry else None

    if method == "GET":
        capture(argv, {"op": "get", "path": target_path, "found": file_entry is not None,
                       "repo": repo_slug})
        if file_entry is None:
            die(404, f"HTTP 404: Not Found (no file {target_path})")
        print(json.dumps({
            "type": "file",
            "encoding": "base64",
            "size": len(base64.b64decode(file_entry["content"])),
            "name": target_path.rsplit("/", 1)[-1],
            "path": target_path,
            "content": file_entry["content"],
            "sha": file_entry["sha"],
        }))
        return 0

    if method != "PUT":
        die(2, f"fakegh: unsupported method {method}")

    body, body_text = read_body(body_path)
    capture(
        argv,
        {
            "op": "put",
            "path": target_path,
            "repo": repo_slug,
            "body_text": body_text,
            "body": body,
            "current_sha": current_sha,
            "body_file_mode": oct(os.stat(body_path).st_mode & 0o777),
        },
    )

    if FIXTURE == "unauthorized":
        die(401, "HTTP 401: Bad credentials (https://api.github.com/graphql)")

    if FIXTURE == "rate_limited":
        die(403, "HTTP 403: API rate limit exceeded for installation (retry-after: 60)")

    if FIXTURE == "write_stale":
        die(409, f'HTTP 409: sha does not match (expected {body.get("sha")}, current {current_sha})')

    if FIXTURE == "write_unprocessable":
        die(422, "HTTP 422: Validation Failed (sha is required to update a file)")

    if FIXTURE == "write_deferred":
        # The request reached the provider and was never answered. Park it so a
        # test can land it later; nothing is applied now, and the exit code
        # carries no HTTP status - the client cannot tell what happened.
        defer(body, target_path)
        sys.stderr.write("fakegh: the request timed out before the answer arrived\n")
        sys.stderr.flush()
        return 1

    # The provider's own compare-and-swap, always on — the same guarantee the
    # real API gives, which Concord credits rather than re-implements.
    if str(body.get("sha")) != str(current_sha):
        die(409, f'HTTP 409: sha does not match (sent {body.get("sha")}, current {current_sha})')

    raw = base64.b64decode(body.get("content") or "")
    stored = raw
    if FIXTURE == "write_then_readback_mismatch":
        stored = raw + b"\n<!-- something else wrote this -->\n"

    new_sha = blob_sha(stored)
    message = str(body.get("message") or "")
    repo.setdefault("files", {})[target_path] = {
        "sha": new_sha,
        "content": base64.b64encode(stored).decode("ascii"),
    }
    c_sha = commit_sha(message, current_sha, time.time())
    repo.setdefault("commits", []).append(
        {"sha": c_sha, "path": target_path, "message": message, "branch": body.get("branch"),
         "files": copy.deepcopy(repo["files"])}
    )
    repo["writes"] = int(repo.get("writes", 0)) + 1
    save_repo(repo)

    if FIXTURE == "crash_between_write_and_receipt":
        with open(state_path("fake.pid"), "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        with open(
            state_path("applied.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({"blob_sha": new_sha, "commit_sha": c_sha, "path": target_path}, handle)
        print(f"child alive pid={os.getpid()} applied blob={new_sha}")
        sys.stdout.flush()
        # Block until the parent stops heartbeating — i.e. until it is killed.
        # A dead parent cannot flush, so a stale heartbeat is proof of death.
        heartbeat = state_path("heartbeat")
        started = time.time()
        while True:
            try:
                age = time.time() - os.path.getmtime(heartbeat)
            except OSError:
                # The parent may not have written its first heartbeat yet.
                age = 0.0 if time.time() - started < 2.0 else 999.0
            if age > 0.6:
                break
            time.sleep(0.05)
        sys.stdout.write('{"content": {"sha": "')
        sys.stdout.flush()
        return 1

    response = {
        "content": {
            "name": target_path.rsplit("/", 1)[-1],
            "path": target_path,
            "sha": new_sha,
            "size": len(stored),
            "type": "file",
        },
        "commit": {"sha": c_sha, "message": message},
    }

    if FIXTURE == "write_ambiguous":
        # The write landed; the answer did not survive the trip.
        text = json.dumps(response)
        sys.stdout.write(text[: len(text) // 2])
        sys.stdout.flush()
        sys.stderr.write("fakegh: connection reset by peer after request body was sent\n")
        sys.stderr.flush()
        return 1

    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
