"""C4 — the registered tool, driven through the real plugin loader and executor.

What is real here: plugin discovery and ``register(ctx)`` by the pinned
``PluginManager``, the tool registry, the agent tool executor's middleware chain,
the coordinator kernel, the GitHub adapter, and a real child process standing in
for ``gh``. What is stubbed: the ``AIAgent`` object only (``tests/harness.py``).

The assertions are about the *public* shape of one call: five outcomes, five
shapes, and the fact that each shape says what may happen next.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

import concord_result as R
import github_harness as H

TOOL = "concord_github_write"

#: The revision the seeded repository starts at. Set by the ``gh`` fixture; the
#: tool requires the caller-observed revision (review P1-1), and in these tests
#: the "caller" is this file.
_BASE_SHA = ""


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake repository, a fresh store, and the settings the plugin will read."""
    global _BASE_SHA
    repo = H.setup(monkeypatch, tmp_path)
    _BASE_SHA = repo.sha
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    return repo


def _current(root) -> str:
    """The blob SHA a caller would read right now."""
    return H.repo_state(root)["files"][H.PATH]["sha"]


def _args(**over):
    args = {
        "repository": "octo/disposable",
        "path": H.PATH,
        "branch": H.BRANCH,
        "operation_id": "op-1",
        "expected_revision": _BASE_SHA,
        "content": "line one\nline two\n",
    }
    args.update(over)
    return args


def _call(run, **over):
    return json.loads(run(TOOL, _args(**over)).result)


def _store(tmp_path) -> pathlib.Path:
    return tmp_path / "coordinator.db"


def _store_path() -> pathlib.Path:
    """The store path this test process is using (CONCORD_DB, set per test)."""
    import os

    return pathlib.Path(os.environ["CONCORD_DB"])


def _row(path, operation_id):
    path = pathlib.Path(str(path))
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT state, holder FROM concord_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    except sqlite3.OperationalError:  # no schema: no operation was ever recorded
        return None
    finally:
        conn.close()


# --------------------------------------------------------------- registration
def test_the_product_plugin_registers_its_tool_cli_and_skill(loaded_plugin_manager):
    from tools.registry import registry

    scope = loaded_plugin_manager.scope_key
    assert registry.get_entry(TOOL, scope=scope) is not None
    commands = loaded_plugin_manager._cli_commands
    assert "concord" in commands
    assert commands["concord"]["plugin_key"] == "concord-for-hermes"
    skills = loaded_plugin_manager._plugin_skills
    assert "concord-for-hermes:write-file" in skills


def test_the_probe_tools_are_no_longer_part_of_the_product_surface(loaded_plugin_manager):
    """The C1 probe still loads (its evidence must reproduce), but as a separate
    fixture plugin - the product plugin registers no probe tool."""
    from tools.registry import registry

    scope = loaded_plugin_manager.scope_key
    product = loaded_plugin_manager._plugins["concord-for-hermes"]
    assert product.manifest.provides_tools == [TOOL]
    assert registry.get_entry("concord_probe_mutate", scope=scope) is not None


# ------------------------------------------------------------------ confirmed
def test_a_write_is_confirmed_only_after_a_read_back(run, gh, tmp_path):
    shape = _call(run)

    assert shape["outcome"] == "confirmed"
    assert shape["state"] == "confirmed"
    assert shape["second_write"] is True
    receipt = shape["receipt"]
    assert receipt["verified_by"] == "read-after-write"
    assert receipt["object_key"] == H.OBJECT
    assert receipt["operation_id"] == "op-1"
    assert receipt["reconciled"] is False
    assert receipt["commit_sha"] and receipt["blob_sha"]
    assert H.writes(gh.root) == 1
    assert H.content_at(gh.root) == "line one\nline two\n"
    # The commit carries the attribution the evaluator later relies on.
    messages = [c["message"] for c in H.repo_state(gh.root)["commits"] if c["path"] == H.PATH]
    assert any("concord(op-1)" in m for m in messages)


def test_the_base_revision_is_the_one_the_caller_read_and_it_is_recorded(run, gh):
    """Contract change (review P1-1). The model used to send five fields and the
    tool read a fresh base at dispatch; a draft prepared from a stale revision
    was then rebased onto the newer blob and the other editor's change was lost.
    Now the caller sends the revision it read, the write is conditioned on it,
    and the operation records exactly that base."""
    shape = _call(run)
    assert "expected_revision" not in json.dumps(shape)

    conn = sqlite3.connect(str(_store_path()))
    try:
        base = conn.execute(
            "SELECT base_revision FROM concord_operations WHERE operation_id = 'op-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert base == gh.sha, "the recorded base is the revision the caller observed"
    assert H.repo_state(gh.root)["commits"][-1]["message"].startswith("concord(op-1):")


# ------------------------------------------------------------------- replayed
def test_the_same_operation_replays_the_original_receipt_without_a_second_write(run, gh):
    first = _call(run)
    second = _call(run)

    assert second["outcome"] == "replayed"
    assert second["second_write"] is False
    assert second["receipt"]["commit_sha"] == first["receipt"]["commit_sha"]
    assert second["receipt"]["blob_sha"] == first["receipt"]["blob_sha"]
    assert H.writes(gh.root) == 1


def test_the_same_operation_id_with_different_content_is_a_recorded_conflict(run, gh):
    _call(run)
    shape = _call(run, content="something else\n")

    assert shape["outcome"] == "conflict"
    assert shape["state"] == "argument_mismatch"
    assert shape["recorded"] is True
    assert shape["next_action"]["kind"] == R.NEW_OPERATION
    assert H.writes(gh.root) == 1


def test_a_pruned_receipt_still_refuses_a_second_write(run, gh, monkeypatch):
    """Retention drops the payload, never the identity: the replay is refused a
    receipt but is still not executed again.

    Contract change (CONCORD-006 carry-forward): the pruned replay's
    ``next_action`` used to be ``create-new-reviewed-operation``, which invites a
    second external effect for work already recorded as applied. It is now
    ``none`` with the reason.
    """
    import concord_kernel as K

    monkeypatch.setenv("CONCORD_DB", str(_store_path()))
    assert _call(run)["outcome"] == "confirmed"

    coordinator = K.Coordinator(str(_store_path()))
    report = coordinator.prune_resolved(keep_per_object=0)
    assert report["pruned"] == 1

    shape = _call(run)

    assert shape["outcome"] == "replayed"
    assert shape["pruned"] is True
    assert shape["receipt"] is None
    assert shape["second_write"] is False
    assert shape["next_action"]["kind"] == R.NONE
    assert "do not repeat" in shape["next_action"]["why"].lower()
    assert H.writes(gh.root) == 1


# ------------------------------------------------------------------- conflict
def test_a_stale_base_is_refused_by_the_provider_and_settled_as_failed(run, gh, monkeypatch):
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_stale")
    shape = _call(run)

    assert shape["outcome"] == "conflict"
    assert shape["state"] == "failed_before_execution"
    assert shape["holder"] is None
    assert shape["recorded"] is True
    assert shape["refusal_row"]["state"] == "failed_before_execution"
    assert shape["next_action"]["kind"] == R.NEW_OPERATION
    assert H.writes(gh.root) == 0


def test_a_second_intent_on_a_held_object_is_refused_with_the_holder_named(run, gh, monkeypatch):
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    first = _call(run, operation_id="op-held")
    assert first["outcome"] == "unknown"

    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ok")
    shape = _call(run, operation_id="op-next")

    assert shape["outcome"] == "conflict"
    assert shape["holder"] == "op-held"
    assert shape["recorded"] is True
    assert shape["next_action"]["kind"] == R.NEW_OPERATION
    writes_before = H.writes(gh.root)
    assert writes_before == 1  # only the ambiguous write landed
    assert _row(_store_path(), "op-next")[0] == "conflict"


# -------------------------------------------------------------------- unknown
def test_an_ambiguous_outcome_is_unknown_held_and_never_retried(run, gh, monkeypatch):
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    shape = _call(run, operation_id="op-amb")

    assert shape["outcome"] == "unknown"
    assert shape["state"] == "unknown"
    assert shape["held"] is True
    assert shape["auto_retry"] is False
    assert shape["second_write"] is None
    assert "hermes concord reconcile op-amb" in shape["next_action"]["how"]
    assert _row(_store_path(), "op-amb")[0] == "unknown"


def test_retrying_the_unknown_operation_itself_does_not_write_again(run, gh, monkeypatch):
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    _call(run, operation_id="op-amb")
    writes = H.writes(gh.root)

    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ok")
    shape = _call(run, operation_id="op-amb")

    assert shape["outcome"] == "conflict"
    assert shape["state"] == "unknown"
    assert shape["holder"] == "op-amb"
    assert shape["next_action"]["kind"] == R.RECONCILE
    assert H.writes(gh.root) == writes


# -------------------------------------------------------------------- refused
@pytest.mark.parametrize(
    "over,reason",
    [
        ({"repository": "evil/repo"}, "target_not_allowed"),
        ({"path": "notes/missing.md"}, "missing_base_revision"),
        ({"path": "../escape.md"}, "invalid_arguments"),
        ({"operation_id": "bad id with spaces"}, "invalid_arguments"),
        ({"operation_id": "op(x)"}, "invalid_arguments"),
        ({"content": "x" * (1024 * 1024 + 1)}, "invalid_arguments"),
        ({"branch": "main branch"}, "invalid_arguments"),
    ],
)
def test_a_call_that_cannot_be_honest_is_refused_with_no_record(run, gh, over, reason):
    shape = _call(run, **over)

    assert shape["outcome"] == "refused"
    assert shape["reason"] == reason
    assert shape["recorded"] is False
    assert shape["held"] is False
    assert shape["next_action"]["kind"] == R.FIX_AND_RETRY
    # Nothing was sent and nothing was recorded.
    assert not any(c["op"] == "put" for c in H.captures(gh.root))
    op = over.get("operation_id", "op-1")
    if reason != "invalid_arguments":
        assert _row(_store_path(), op) is None


def test_a_missing_field_is_refused_before_any_provider_call(run, gh):
    args = _args()
    del args["content"]
    shape = json.loads(run(TOOL, args).result)

    assert shape["outcome"] == "refused"
    assert shape["reason"] == "invalid_arguments"
    assert H.captures(gh.root) == []


# ------------------------------------------------- the shapes are distinguishable
def _shape(outcome: str):
    base = {"object_key": H.OBJECT, "operation_id": "op-1"}
    if outcome == R.CONFIRMED:
        raw = {"status": "confirmed", "receipt": {"commit_sha": "c", "blob_sha": "b"}}
    elif outcome == R.REPLAYED:
        raw = {"status": "replayed", "receipt": {"commit_sha": "c", "blob_sha": "b"}}
    elif outcome == R.CONFLICT:
        raw = {"status": "conflict", "state": "conflict", "holder": "op-0", "reason": "held"}
    elif outcome == R.UNKNOWN:
        raw = {"status": "unknown", "state": "unknown", "reason": "ambiguous"}
    else:
        raw = {"status": "refused", "reason": "target_not_allowed", "detail": "nope"}
    return R.present(raw, **base)


def test_the_five_outcomes_have_five_distinguishable_shapes():
    shapes = {outcome: _shape(outcome) for outcome in R.OUTCOMES}

    assert set(shapes) == set(R.OUTCOMES)
    signatures = {outcome: R.signature(shape) for outcome, shape in shapes.items()}
    assert len(set(signatures.values())) == len(signatures), signatures
    for outcome, shape in shapes.items():
        assert shape["outcome"] == outcome


def test_no_outcome_can_be_read_as_another_without_losing_something():
    confirmed = _shape(R.CONFIRMED)
    replayed = _shape(R.REPLAYED)
    conflict = _shape(R.CONFLICT)
    unknown = _shape(R.UNKNOWN)
    refused = _shape(R.REFUSED)

    # confirmed vs replayed: did this call write, and is the receipt new?
    assert confirmed["second_write"] is True and replayed["second_write"] is False
    assert confirmed["receipt"]["reconciled"] is False
    # conflict vs unknown: recorded but not held vs held with no auto retry.
    assert conflict["held"] is True and conflict["holder"] == "op-0"
    assert conflict.get("auto_retry") is None
    assert unknown["held"] is True and unknown["auto_retry"] is False
    assert unknown.get("holder") is None
    # refused vs everything else: no record at all.
    assert refused["recorded"] is False and refused["state"] is None
    assert all(s["recorded"] is True for s in (confirmed, replayed, conflict, unknown))


# --------------------------------------------------------- two OS processes
def test_two_processes_cannot_both_write_the_same_object(run, gh, tmp_path):
    """Two tool calls in two real processes, same object, different operation ids.

    Both children are handed the same ``expected_revision``: that is the
    overlapping read/edit interval (two agents read the same blob, then both
    dispatch). The kernel still admits exactly one, the loser is refused with
    the winner's id, and exactly one provider write happens. The loser also
    cannot fall back to the provider's conditional write, because its base is
    the revision it read - not a base refreshed at dispatch (review P1-1).
    """
    from_conftest = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "write_child.py"
    payload_base = {
        "store": str(_store(tmp_path)),
        "repository": "octo/disposable",
        "path": H.PATH,
        "branch": H.BRANCH,
        "expected_revision": _BASE_SHA,
        "gh": [sys.executable, str(H.FAKEGH)],
    }
    procs = []
    for op in ("child-a", "child-b"):
        payload = dict(payload_base, operation_id=op, content=f"written by {op}\n")
        procs.append(
            subprocess.Popen(
                [sys.executable, str(from_conftest), json.dumps(payload)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=None,
            )
        )
    shapes = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        shapes.append(json.loads(out))

    outcomes = sorted(shape["outcome"] for shape in shapes)
    assert outcomes == ["confirmed", "conflict"], shapes
    assert H.writes(gh.root) == 1
    winner = next(s for s in shapes if s["outcome"] == "confirmed")
    loser = next(s for s in shapes if s["outcome"] == "conflict")
    assert loser["holder"] == winner["operation_id"]
