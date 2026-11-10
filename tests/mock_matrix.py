"""The C3 mock matrix: eight provider fixtures × three coordination modes.

Each row is produced by running the real adapter against the real fake ``gh``
process, with the C2 kernel unchanged behind it. The runner returns data, not
assertions, so the same rows can be (a) asserted by ``test_c3_matrix.py`` and
(b) written to ``evidence/`` by ``scripts/mock_matrix.py`` — one execution path,
one set of numbers.

Modes
-----
fresh_admission   the first call for an operation id
replay            the same call again, same id, same arguments
reconcile         a provider read offered to an unresolved operation

Where an operation is already terminal, ``reconcile`` is not a settlement but a
refusal (``IllegalTransition``). That is recorded as its own outcome rather than
skipped: "the kernel refuses to re-settle" is an assertion too.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_DIR = os.pathsep.join([
    str(ROOT / "isolated-home" / "plugins" / "concord_probe"),
    str(ROOT / "isolated-home" / "plugins" / "concord-for-hermes"),
])
CHILD = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "adapter_child.py"

FIXTURES = [
    "read_ok",
    "write_ok",
    "write_stale",
    "write_ambiguous",
    "write_then_readback_mismatch",
    "unauthorized",
    "rate_limited",
    "crash_between_write_and_receipt",
]

MODES = ["fresh_admission", "replay", "reconcile"]

VALUE = "line two\n"


# (expected status, expected state, expected provider writes)
EXPECTED: dict[str, dict[str, tuple[str, str, int]]] = {
    "write_ok": {
        "fresh_admission": ("confirmed", "confirmed", 1),
        "replay": ("replayed", "confirmed", 1),
        "reconcile": ("not_unresolved", "confirmed", 1),
    },
    "read_ok": {
        "fresh_admission": ("confirmed", "confirmed", 1),
        "replay": ("replayed", "confirmed", 1),
        "reconcile": ("not_unresolved", "confirmed", 1),
    },
    "write_stale": {
        "fresh_admission": ("conflict", "failed_before_execution", 0),
        "replay": ("conflict", "failed_before_execution", 0),
        "reconcile": ("not_unresolved", "failed_before_execution", 0),
    },
    "write_ambiguous": {
        "fresh_admission": ("unknown", "unknown", 1),
        "replay": ("conflict", "unknown", 1),
        "reconcile": ("confirmed", "confirmed", 1),
    },
    "write_then_readback_mismatch": {
        "fresh_admission": ("unknown", "unknown", 1),
        "replay": ("conflict", "unknown", 1),
        "reconcile": ("unknown", "unknown", 1),
    },
    "unauthorized": {
        "fresh_admission": ("conflict", "failed_before_execution", 0),
        "replay": ("conflict", "failed_before_execution", 0),
        "reconcile": ("not_unresolved", "failed_before_execution", 0),
    },
    "rate_limited": {
        "fresh_admission": ("conflict", "failed_before_execution", 0),
        "replay": ("conflict", "failed_before_execution", 0),
        "reconcile": ("not_unresolved", "failed_before_execution", 0),
    },
    "crash_between_write_and_receipt": {
        "fresh_admission": ("killed", "pending", 1),
        "replay": ("conflict", "pending", 1),
        "reconcile": ("confirmed", "confirmed", 1),
    },
}


@contextlib.contextmanager
def _fake_env(root: pathlib.Path, fixture: str):
    H.set_config(root, fixture=fixture, version="2.40.1")
    keys = ("GH_CONFIG_DIR", "CONCORD_GH_MOCK")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update(
        {
            "GH_CONFIG_DIR": str(root),
            "CONCORD_GH_MOCK": "1",
        }
    )
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _row(fixture: str, mode: str, *, status: str, state: str, second_write,
         provider_writes: int, note: str = "", checks: list[dict] | None = None) -> dict:
    expected = EXPECTED[fixture][mode]
    checks = checks or []
    matched = (status, state, provider_writes) == expected
    return {
        "fixture": fixture,
        "mode": mode,
        "status": status,
        "state": state,
        "second_write": second_write,
        "provider_writes": provider_writes,
        "expected": {"status": expected[0], "state": expected[1], "provider_writes": expected[2]},
        "matched_expectation": matched,
        "checks": checks,
        "note": note,
        "ok": matched and all(check["ok"] for check in checks),
    }


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _wait_for(path: pathlib.Path, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return True
        time.sleep(0.02)
    return False


def _pid_alive(pid: int | str) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    )
    return str(pid) in (result.stdout or "")


def _crash_rows(workdir: pathlib.Path, root: pathlib.Path, base: str) -> list[dict]:
    db = workdir / "kernel.db"
    applied = root / "applied.json"
    # The fake gh resolves its state files under GH_CONFIG_DIR, so the heartbeat
    # has to live there too — one directory, no path guessing.
    heartbeat = root / "heartbeat"
    result_file = workdir / "child.json"

    proc = subprocess.Popen(
        [
            sys.executable, str(CHILD), str(PLUGIN_DIR), str(db), H.OBJECT, "op-1",
            json.dumps(VALUE), base, str(heartbeat), str(result_file),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    rows: list[dict] = []
    try:
        reached = _wait_for(applied)
        alive_before_kill = proc.poll() is None
        fake_pid = (root / "fake.pid").read_text(encoding="utf-8").strip() if (root / "fake.pid").exists() else ""
        kill = subprocess.run(
            ["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True, text=True
        )
        code = proc.wait(timeout=60)
        died_without_result = not result_file.exists()

        # The fake gh is a grandchild: it must notice the dead parent and leave.
        fake_gone = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if fake_pid and not _pid_alive(fake_pid):
                fake_gone = True
                break
            time.sleep(0.1)

        restarted = K.Coordinator(str(db))
        record = restarted.get("op-1")
        rows.append(
            _row(
                "crash_between_write_and_receipt",
                "fresh_admission",
                status="killed",
                state=record["state"] if record else "missing",
                second_write=None,
                provider_writes=H.writes(root),
                note=(
                    "taskkill /F after the fake gh applied the write and before the"
                    " receipt was recorded"
                ),
                checks=[
                    _check("child reached the post-write boundary", reached),
                    _check("child was alive when the kill was issued", alive_before_kill),
                    _check("taskkill exited 0", kill.returncode == 0, kill.stdout.strip()),
                    _check("process died at the boundary (no result file)", died_without_result),
                    _check("the fake gh child exited after its parent died", fake_gone),
                    _check("durable state is pending", bool(record) and record["state"] == K.PENDING),
                    _check(
                        "the attempt is still open",
                        bool(record) and record["attempts"][0]["state"] == K.PENDING,
                    ),
                ],
            )
        )

        replay = A.guarded_write(
            restarted, H.adapter(root),
            object_key=H.OBJECT, operation_id="op-1", value=VALUE,
            expected_revision=base, actor="restart",
        )
        rows.append(
            _row(
                "crash_between_write_and_receipt",
                "replay",
                status=replay["status"],
                state=replay["state"],
                second_write=replay.get("second_write"),
                provider_writes=H.writes(root),
                note="a pending outcome is held: the restart does not retry it",
            )
        )

        settled = A.reconcile_from_provider(
            restarted, H.adapter(root), operation_id="op-1", object_key=H.OBJECT
        )
        rows.append(
            _row(
                "crash_between_write_and_receipt",
                "reconcile",
                status=settled["state"],
                state=settled["state"],
                second_write=None,
                provider_writes=H.writes(root),
                note="settled from a read of provider evidence; no second write",
                checks=[
                    _check("receipt is marked reconciled", settled["evidence"]["reconciled"] is True),
                    _check("the object is released", restarted.holder_of(H.OBJECT) is None),
                ],
            )
        )
    finally:
        if proc.poll() is None:  # never leave a stray process behind
            proc.kill()
            proc.wait(timeout=30)
    return rows


def _normal_rows(fixture: str, workdir: pathlib.Path, root: pathlib.Path, base: str) -> list[dict]:
    coord = K.Coordinator(str(workdir / "kernel.db"))
    adapter = H.adapter(root)
    rows: list[dict] = []

    checks: list[dict] = []
    if fixture == "read_ok":
        evidence = adapter.read(A.parse_object_key(H.OBJECT))
        checks.append(
            _check("read returns the seeded blob SHA", evidence["revision"] == base, str(evidence["revision"]))
        )
        checks.append(_check("read returns the file content", evidence["value"] == H.VALUE))

    fresh = A.guarded_write(
        coord, adapter, object_key=H.OBJECT, operation_id="op-1", value=VALUE,
        expected_revision=base, actor="c3",
    )
    if fresh["status"] == K.CONFIRMED:
        checks.append(
            _check("receipt is verified by read-after-write",
                   fresh["receipt"].get("verified_by") == "read-after-write")
        )
    rows.append(
        _row(fixture, "fresh_admission", status=fresh["status"], state=fresh["state"],
             second_write=fresh.get("second_write"), provider_writes=H.writes(root),
             note=fresh.get("detail") or fresh.get("reason") or "", checks=checks)
    )

    replay = A.guarded_write(
        coord, adapter, object_key=H.OBJECT, operation_id="op-1", value=VALUE,
        expected_revision=base, actor="c3",
    )
    rows.append(
        _row(fixture, "replay", status=replay["status"], state=replay["state"],
             second_write=replay.get("second_write"), provider_writes=H.writes(root),
             note=replay.get("reason", ""))
    )

    writes_before = H.writes(root)
    try:
        settled = A.reconcile_from_provider(
            coord, adapter, operation_id="op-1", object_key=H.OBJECT
        )
        status, state, note = settled["state"], settled["state"], "settled from a provider read"
        checks = [
            _check("reconcile performed no write", H.writes(root) == writes_before),
        ]
    except K.IllegalTransition as exc:
        status, state, note = "not_unresolved", coord.get("op-1")["state"], str(exc)
        checks = []
    rows.append(
        _row(fixture, "reconcile", status=status, state=state, second_write=None,
             provider_writes=H.writes(root), note=note, checks=checks)
    )
    return rows


def run_matrix(base_dir: pathlib.Path) -> list[dict]:
    """Run every fixture through every mode in isolated directories."""
    base_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for fixture in FIXTURES:
        workdir = base_dir / fixture
        workdir.mkdir(parents=True, exist_ok=True)
        root = workdir / "fake-gh"
        shas = H.seed(root)
        base = shas[H.PATH]
        with _fake_env(root, fixture):
            if fixture == "crash_between_write_and_receipt":
                rows.extend(_crash_rows(workdir, root, base))
            else:
                rows.extend(_normal_rows(fixture, workdir, root, base))
    return rows


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "| Fixture | Mode | Status | Durable state | Provider writes | Second write | Expectation |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| `{fixture}` | {mode} | `{status}` | `{state}` | {provider_writes} | {second} | {verdict} |".format(
                fixture=row["fixture"],
                mode=row["mode"],
                status=row["status"],
                state=row["state"],
                provider_writes=row["provider_writes"],
                second=row["second_write"] if row["second_write"] is not None else "—",
                verdict="match" if row["matched_expectation"] else "MISMATCH",
            )
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - convenience
    import tempfile

    with tempfile.TemporaryDirectory(prefix="concord-matrix-") as tmp:
        result = run_matrix(pathlib.Path(tmp))
    print(render_markdown(result))
    print(json.dumps(result, indent=2))
