"""C3 baselines: the same four scenarios, coordinated three ways.

The point is not "Concord wins". It is to record, per scenario, what each
baseline loses or duplicates — including where the provider's own CAS already
carries the guarantee and Concord adds nothing but fewer wasted calls.

  A. provider alone            call the adapter's conditional write directly
  B. provider + advisory log   a local intent log, checked before dispatch,
                               with no admission and no locking
  C. Concord                   admission, durability and evidence-based settle

The advisory log in B is deliberately the *reasonable* version someone would
write first: read the log, if the operation is settled return its receipt,
otherwise record "dispatched" and call the provider. Its gap (check-then-act
across processes, and no evidence path for an ambiguous outcome) is forced by
scheduling both reads before either write, so the interleave is explicit rather
than sampled. Sampling would measure the gap's frequency; forcing it measures
its existence, which is the claim.

Every call goes to the same fake ``gh`` process, so the provider behaviour is
identical across the three columns.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
from typing import Any

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

VALUE_A = "written by intent A\n"
VALUE_B = "written by intent B\n"
VALUE_Y = "an unrelated object\n"

SCENARIOS = [
    "concurrent_stale_writes",
    "duplicate_replay",
    "ambiguous_outcome",
    "unrelated_object_progress",
]

BASELINES = ["provider_alone", "provider_plus_advisory", "concord"]


# ------------------------------------------------------------ baseline B
class AdvisoryHistory:
    """A local intent log: durable-ish, advisory, and not an admission control.

    Labelled scaffolding, not product: it exists to be the honest middle
    baseline, not to be shipped.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def lookup(self, operation_id: str) -> dict | None:
        return self._read().get(operation_id)

    def mark_dispatched(self, operation_id: str) -> None:
        data = self._read()
        data.setdefault(operation_id, {})["dispatched_at"] = time.time()
        self._write(data)

    def mark_settled(self, operation_id: str, receipt: dict) -> None:
        data = self._read()
        data.setdefault(operation_id, {})["receipt"] = receipt
        self._write(data)

    def state(self, operation_id: str) -> str:
        record = self.lookup(operation_id) or {}
        if "receipt" in record:
            return "settled"
        if "dispatched_at" in record:
            return "in_flight"
        return "unknown"


@contextlib.contextmanager
def _fake_env(root: pathlib.Path, fixture: str):
    keys = ("FAKEGH_STATE", "FAKEGH_FIXTURE", "FAKEGH_VERSION", "CONCORD_GH_MOCK")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update(
        {
            "FAKEGH_STATE": str(root),
            "FAKEGH_FIXTURE": fixture,
            "FAKEGH_VERSION": "2.40.1",
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


def _attempts(root: pathlib.Path) -> int:
    return len([capture for capture in H.captures(root) if capture.get("op") == "put"])


# ---------------------------------------------------------------- scenarios
def _concurrent(workdir: pathlib.Path, baseline: str) -> dict[str, Any]:
    root = workdir / "repo"
    base = H.seed(root, {H.PATH: H.VALUE})[H.PATH]
    adapter = H.adapter(root)
    metrics: dict[str, Any] = {"loser_knows": None}

    if baseline == "provider_alone":
        A_target = A.parse_object_key(H.OBJECT)
        first = adapter.write(A_target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        try:
            adapter.write(A_target, expected_revision=base, value=VALUE_B, operation_id="op-b")
            metrics["loser_knows"] = "wrote anyway (impossible under CAS)"
        except K.Rejected:
            metrics["loser_knows"] = "yes: provider refused with a definitive status"
        metrics["note"] = "both intents were dispatched; the provider chose the winner"
        return metrics

    if baseline == "provider_plus_advisory":
        history = AdvisoryHistory(workdir / "history.json")
        # Forced interleave: both lookups happen before either dispatch.
        seen = {op: history.lookup(op) for op in ("op-a", "op-b")}
        assert seen == {"op-a": None, "op-b": None}
        history.mark_dispatched("op-a")
        history.mark_dispatched("op-b")
        target = A.parse_object_key(H.OBJECT)
        first = adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        history.mark_settled("op-a", first)
        try:
            adapter.write(target, expected_revision=base, value=VALUE_B, operation_id="op-b")
            metrics["loser_knows"] = "wrote anyway (impossible under CAS)"
        except K.Rejected:
            metrics["loser_knows"] = "yes: provider refused with a definitive status"
            history._write({k: v for k, v in history._read().items() if k != "op-b"})
        metrics["note"] = "the advisory log cannot see the other intent; both dispatch"
        return metrics

    # Concord: the concurrency window is forced the same way the advisory
    # baseline's is — op-a is admitted and dispatched, and op-b arrives while
    # op-a is still in flight (pending), which is what "concurrent" means.
    coord = K.Coordinator(str(workdir / "kernel.db"))
    material_a = {"object": H.OBJECT, "value": VALUE_A, "expected_revision": base}
    material_b = {"object": H.OBJECT, "value": VALUE_B, "expected_revision": base}
    assert coord.admit(object_key=H.OBJECT, operation_id="op-a",
                       fingerprint=K.fingerprint(material_a), base_revision=base,
                       actor="a").kind == K.ADMITTED
    coord.begin_dispatch("op-a")
    second = coord.admit(object_key=H.OBJECT, operation_id="op-b",
                         fingerprint=K.fingerprint(material_b), base_revision=base,
                         actor="b")
    receipt = adapter.write(A.parse_object_key(H.OBJECT), expected_revision=base,
                            value=VALUE_A, operation_id="op-a")
    coord.confirm("op-a", receipt)
    assert second.kind == K.CONFLICT
    metrics["loser_knows"] = f"yes: refused before dispatch, holder named ({second.holder})"
    metrics["note"] = "the second intent never reached the provider; its refusal is durable"
    return metrics


def _replay(workdir: pathlib.Path, baseline: str) -> dict[str, Any]:
    root = workdir / "repo"
    base = H.seed(root, {H.PATH: H.VALUE})[H.PATH]
    adapter = H.adapter(root)
    target = A.parse_object_key(H.OBJECT)
    metrics: dict[str, Any] = {"caller_saw_success": None}

    if baseline == "provider_alone":
        adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        try:
            adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
            metrics["caller_saw_success"] = "yes"
        except K.Rejected as exc:
            metrics["caller_saw_success"] = f"NO — false failure ({exc})"
        metrics["note"] = "the retry is a second dispatch and the provider refuses it"
        return metrics

    if baseline == "provider_plus_advisory":
        history = AdvisoryHistory(workdir / "history.json")
        history.mark_dispatched("op-a")
        receipt = adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        history.mark_settled("op-a", receipt)
        cached = history.lookup("op-a")
        metrics["caller_saw_success"] = "yes — receipt served from the local log"
        metrics["note"] = (
            "correct, provided the log write survived; the log is not evidence"
            f" (cached blob {cached['receipt']['blob_sha'][:12]}…)"
        )
        return metrics

    coord = K.Coordinator(str(workdir / "kernel.db"))
    A.guarded_write(coord, adapter, object_key=H.OBJECT, operation_id="op-a",
                    value=VALUE_A, expected_revision=base, actor="a")
    replay = A.guarded_write(coord, adapter, object_key=H.OBJECT, operation_id="op-a",
                             value=VALUE_A, expected_revision=base, actor="a")
    metrics["caller_saw_success"] = f"yes — {replay['status']}, original receipt"
    metrics["note"] = "durable replay: no dispatch, no second write"
    return metrics


def _ambiguous(workdir: pathlib.Path, baseline: str) -> dict[str, Any]:
    root = workdir / "repo"
    base = H.seed(root, {H.PATH: H.VALUE})[H.PATH]
    adapter = H.adapter(root)
    target = A.parse_object_key(H.OBJECT)
    metrics: dict[str, Any] = {"resolved": None, "false_failures": 0}

    if baseline == "provider_alone":
        try:
            adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        except A.AdapterError as exc:
            metrics["resolved"] = f"no — nothing durable remembers the attempt ({type(exc).__name__})"
        try:
            adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
            metrics["resolved"] = "retry applied a second write"
        except K.Rejected:
            metrics["false_failures"] = 1
            metrics["resolved"] = (
                "no — the retry is refused as stale while the first write is in place;"
                " the caller would report failure for a write that landed"
            )
        metrics["note"] = "correctness depends on someone reading the file out of band"
        return metrics

    if baseline == "provider_plus_advisory":
        history = AdvisoryHistory(workdir / "history.json")
        history.mark_dispatched("op-a")
        try:
            adapter.write(target, expected_revision=base, value=VALUE_A, operation_id="op-a")
        except A.AdapterError:
            pass
        metrics["resolved"] = f"no — the log says '{history.state('op-a')}' and has no evidence path"
        metrics["note"] = (
            "a stuck in-flight record either blocks forever or invites a guess;"
            " there is no read that distinguishes the cases"
        )
        return metrics

    coord = K.Coordinator(str(workdir / "kernel.db"))
    first = A.guarded_write(coord, adapter, object_key=H.OBJECT, operation_id="op-a",
                            value=VALUE_A, expected_revision=base, actor="a")
    settled = A.reconcile_from_provider(coord, adapter, operation_id="op-a", object_key=H.OBJECT)
    metrics["resolved"] = (
        f"yes — {first['status']} then reconciled to {settled['state']}"
        " from content + commit attribution"
    )
    metrics["note"] = "the object was held between the two calls; nothing retried it"
    return metrics


def _unrelated(workdir: pathlib.Path, baseline: str) -> dict[str, Any]:
    root = workdir / "repo"
    shas = H.seed(root, {H.PATH: H.VALUE, "notes/other.md": "other\n"})
    adapter = H.adapter(root)
    other = "github:octo/disposable@main:notes/other.md"
    metrics: dict[str, Any] = {"held_object_blocked_progress": None}

    if baseline in ("provider_alone", "provider_plus_advisory"):
        adapter.write(A.parse_object_key(other), expected_revision=shas["notes/other.md"],
                      value=VALUE_Y, operation_id="op-y")
        metrics["held_object_blocked_progress"] = "no (no object is held in this baseline)"
        metrics["note"] = f"{baseline}: nothing was held, so nothing could block"
        return metrics

    coord = K.Coordinator(str(workdir / "kernel.db"))
    # Put the first object into an unresolved state on purpose.
    coord.admit(object_key=H.OBJECT, operation_id="op-x",
                fingerprint=K.fingerprint({"object": H.OBJECT, "value": VALUE_A,
                                           "expected_revision": shas[H.PATH]}),
                base_revision=shas[H.PATH], actor="a")
    coord.begin_dispatch("op-x")
    coord.mark_unknown("op-x", "connection dropped after send")
    result = A.guarded_write(coord, adapter, object_key=other, operation_id="op-y",
                             value=VALUE_Y, expected_revision=shas["notes/other.md"], actor="b")
    metrics["held_object_blocked_progress"] = (
        "no — unrelated object written while op-x stays unknown"
        if result["status"] == K.CONFIRMED else f"YES ({result['status']})"
    )
    metrics["note"] = "admission is per object; an unknown outcome is not a global lock"
    return metrics


_RUNNERS = {
    "concurrent_stale_writes": _concurrent,
    "duplicate_replay": _replay,
    "ambiguous_outcome": _ambiguous,
    "unrelated_object_progress": _unrelated,
}

_FIXTURES = {
    "concurrent_stale_writes": "write_ok",
    "duplicate_replay": "write_ok",
    "ambiguous_outcome": "write_ambiguous",
    "unrelated_object_progress": "write_ok",
}


def run_baselines(base_dir: pathlib.Path) -> list[dict]:
    base_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for scenario in SCENARIOS:
        for baseline in BASELINES:
            workdir = base_dir / scenario / baseline
            workdir.mkdir(parents=True, exist_ok=True)
            root = workdir / "repo"
            with _fake_env(root, _FIXTURES[scenario]):
                metrics = _RUNNERS[scenario](workdir, baseline)
            rows.append(
                {
                    "scenario": scenario,
                    "baseline": baseline,
                    "provider_dispatch_attempts": _attempts(root),
                    "applied_writes": H.writes(root),
                    **metrics,
                }
            )
    return rows


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "| Scenario | Baseline | Provider dispatch attempts | Applied writes | Outcome (what the caller could establish) |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        outcome = (
            row.get("loser_knows")
            or row.get("caller_saw_success")
            or row.get("resolved")
            or row.get("held_object_blocked_progress")
            or "—"
        )
        if len(outcome) > 150:
            outcome = outcome[:147] + "…"
        lines.append(
            f"| {row['scenario'].replace('_', ' ')} | {row['baseline']} |"
            f" {row['provider_dispatch_attempts']} | {row['applied_writes']} | {outcome} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - convenience
    import tempfile

    with tempfile.TemporaryDirectory(prefix="concord-baselines-") as tmp:
        result = run_baselines(pathlib.Path(tmp))
    print(render_markdown(result))
    print(json.dumps(result, indent=2, default=str))
