"""C6 — a durable receipt must not depend on a working provider (P2-4).

The review: with an already-confirmed operation in the store, pointing
``gh_command`` at a missing executable turned an identical repeat from
``replayed`` into ``refused adapter_unavailable``, because the adapter was built
and version-checked before the operation was looked up. A receipt that a
dependency outage can erase is not durable, and the outage is exactly when
someone re-runs a command.

The fix: resolve the durable local identity first. When the recorded material
matches and the state cannot dispatch (confirmed, pruned, cancelled, reserved,
pending, unknown), the answer comes from the store and the provider is never
touched. Authorisation is unchanged: a new intent still needs the allowlist, the
``gh`` version floor and the base read, and a retry of a provably-unexecuted
operation still needs the provider.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

import concord_kernel as K
import concord_store as S
import concord_tool as T
import github_harness as H

MISSING_GH = "Z:/no/such/gh-binary"
OBJECT = "github:octo/disposable@main:notes/plan.md"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    repo = H.setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CONCORD_DB", str(tmp_path / "coordinator.db"))
    return repo


def _settings(tmp_path, command):
    return S.resolve_settings(
        {"allowed_repositories": ["octo/disposable"], "gh_command": list(command)},
        store=tmp_path / "coordinator.db",
    )


def _working(tmp_path):
    return _settings(tmp_path, [sys.executable, str(H.FAKEGH)])


def _args(**over):
    args = {
        "repository": "octo/disposable", "path": H.PATH, "branch": H.BRANCH,
        "operation_id": "op-1", "expected_revision": "", "content": "line one\nline two\n",
    }
    args.update(over)
    return args


def _confirm(tmp_path, gh, **over):
    return T.run_write(_args(expected_revision=gh.sha, **over), settings=_working(tmp_path))


def test_a_confirmed_operation_replays_with_gh_missing(tmp_path, gh):
    first = _confirm(tmp_path, gh)
    assert first["outcome"] == "confirmed", first
    captures = len(H.captures(gh.root))

    repeat = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )

    assert repeat["outcome"] == "replayed", repeat
    assert repeat["second_write"] is False
    assert repeat["receipt"]["commit_sha"] == first["receipt"]["commit_sha"]
    assert len(H.captures(gh.root)) == captures, "the provider must not be touched at all"


def test_a_confirmed_operation_replays_when_gh_is_too_old(tmp_path, gh, monkeypatch):
    assert _confirm(tmp_path, gh)["outcome"] == "confirmed"
    monkeypatch.setenv("FAKEGH_VERSION", "1.0.0")
    captures = len(H.captures(gh.root))

    repeat = T.run_write(_args(expected_revision=gh.sha), settings=_working(tmp_path))

    assert repeat["outcome"] == "replayed", repeat
    assert len(H.captures(gh.root)) == captures, "an old gh must not be consulted for a replay"


def test_a_confirmed_operation_replays_while_the_provider_is_offline(tmp_path, gh):
    assert _confirm(tmp_path, gh)["outcome"] == "confirmed"
    offline = _settings(tmp_path, [MISSING_GH])

    first_replay = T.run_write(_args(expected_revision=gh.sha), settings=offline)
    second_replay = T.run_write(_args(expected_revision=gh.sha), settings=offline)

    assert first_replay["outcome"] == second_replay["outcome"] == "replayed"
    assert H.writes(gh.root) == 1


def test_a_pruned_receipt_still_replays_offline_and_does_not_urge_a_second_write(
    tmp_path, gh, monkeypatch
):
    import concord_result as R

    assert _confirm(tmp_path, gh)["outcome"] == "confirmed"
    coordinator = K.Coordinator(str(tmp_path / "coordinator.db"))
    assert coordinator.prune_resolved(keep_per_object=0)["pruned"] == 1

    repeat = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )

    assert repeat["outcome"] == "replayed", repeat
    assert repeat["pruned"] is True
    assert repeat["receipt"] is None
    # Carry-forward: the work already happened, so the safe next action is
    # "nothing", not "create a new operation".
    assert repeat["next_action"]["kind"] == R.NONE, repeat["next_action"]
    assert "do not repeat" in repeat["next_action"]["why"].lower()
    assert H.writes(gh.root) == 1


def test_a_reconciled_receipt_replays_offline(tmp_path, gh, monkeypatch):
    import concord_gh_adapter as A

    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    assert _confirm(tmp_path, gh)["outcome"] == "unknown"

    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ok")
    coordinator = K.Coordinator(str(tmp_path / "coordinator.db"))
    adapter = H.adapter(gh.root)
    settled = A.reconcile_from_provider(
        coordinator, adapter, operation_id="op-1", object_key=OBJECT
    )
    assert settled["state"] == K.CONFIRMED, settled

    repeat = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )

    assert repeat["outcome"] == "replayed", repeat
    assert repeat["receipt"]["reconciled"] is True
    assert repeat["receipt"]["commit_sha"], "the reconciled receipt keeps commit evidence"


def test_an_unresolved_operation_answers_from_the_store_when_gh_is_missing(tmp_path, gh, monkeypatch):
    """`unknown` is a local fact: repeating it must not need the provider."""
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    assert _confirm(tmp_path, gh)["outcome"] == "unknown"

    repeat = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )

    assert repeat["outcome"] == "conflict", repeat
    assert repeat["state"] == "unknown"
    assert repeat["holder"] == "op-1"
    assert H.writes(gh.root) == 1


def test_a_new_operation_still_needs_a_working_provider(tmp_path, gh):
    shape = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )
    assert shape["outcome"] == "refused", shape
    assert shape["reason"] == "adapter_unavailable"
    assert shape["recorded"] is False
    assert H.writes(gh.root) == 0


def test_a_retry_of_a_proved_non_execution_still_needs_the_provider(tmp_path, gh, monkeypatch):
    """Nothing was applied, so there is no receipt to replay - the adapter runs."""
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_stale")
    first = _confirm(tmp_path, gh)
    assert first["state"] == "failed_before_execution", first

    retry = T.run_write(
        _args(expected_revision=gh.sha), settings=_settings(tmp_path, [MISSING_GH])
    )
    assert retry["outcome"] == "refused"
    assert retry["reason"] == "adapter_unavailable"


def test_reusing_an_id_with_different_material_is_refused_offline(tmp_path, gh):
    assert _confirm(tmp_path, gh)["outcome"] == "confirmed"

    other = T.run_write(
        _args(expected_revision=gh.sha, content="something else\n"),
        settings=_settings(tmp_path, [MISSING_GH]),
    )
    assert other["outcome"] == "conflict", other
    assert other["state"] == "argument_mismatch"
    assert H.writes(gh.root) == 1
