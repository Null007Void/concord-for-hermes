"""C7 — a timed-out request may still land, so reconciliation keeps the hold (F7-2).

The defect (review F7-2): ``opaque_evidence_verdict`` released a dispatched-but-
uncertain operation as ``failed_before_execution`` when the latest commit on the
path was still the base commit recorded at admission. "The ref has not moved" is
an observation of the past; it says nothing about a request that is still on its
way. The sequence the owner named as the acceptance test is:

    dispatch -> the request times out (uncertain)
             -> reconciliation runs while nothing has landed
             -> the original request lands at the provider
             -> an identical retry is issued

Under the defect, reconciliation answered "nothing was applied, the object is
released" at step 2 and the retry at step 4 was dispatched a second time against
a write that had already applied.

The contract now:

* **completion** - CONFIRMED, on attributable application: the content hashes to
  this operation's fingerprint *and* the latest commit on the path carries
  ``concord(<operation_id>)``.
* **non-execution** - FAILED_BEFORE_EXECUTION, only from an authoritative
  provider statement that names this operation and covers the window
  (``evidence["non_execution"]``, see
  :func:`concord_kernel.authoritative_non_execution`), or from a synchronous
  definitive refusal at dispatch time. A read of the current ref is neither.
* **anything else** - UNKNOWN, held.

Every provider interaction below is the real adapter spawning the fake ``gh`` as
a child process. ``write_deferred`` parks the request without applying it, and
``gh.py --apply-deferred`` lands it later - outside the process under test, the
way a delayed provider-side application would.
"""
from __future__ import annotations

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

OBJECT = H.OBJECT
VALUE = "the edit that timed out\n"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return H.setup(monkeypatch, tmp_path)


def _coord(tmp_path, name="k.db"):
    return K.Coordinator(str(tmp_path / name))


def _seed_commit(root) -> str:
    return H.repo_state(root)["commits"][0]["sha"]


def _dispatch_then_time_out(coord, gh, *, operation_id="op-1", value=VALUE):
    """One dispatch whose transport dies: the request may or may not have landed."""
    return A.guarded_write(
        coord, gh.adapter,
        object_key=OBJECT, operation_id=operation_id, value=value,
        expected_revision=gh.sha, actor="c7", base_identity=_seed_commit(gh.root),
    )


# ------------------------------------------- the owner's acceptance sequence
def test_a_request_that_lands_after_reconciliation_is_not_treated_as_safe(gh, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_deferred")
    coord = _coord(tmp_path)

    # 1. dispatch, then the request times out. Nothing has been applied yet.
    dispatched = _dispatch_then_time_out(coord, gh)
    assert dispatched["status"] == K.UNKNOWN, dispatched
    assert H.writes(gh.root) == 0
    assert H.puts(gh.root) == 1, "the request did reach the provider"

    # 2. reconcile while the target is untouched: an unchanged commit and an
    #    unchanged blob. This must NOT be read as "nothing was applied".
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ok")
    reconciled = A.reconcile_from_provider(
        coord, gh.adapter, operation_id="op-1", object_key=OBJECT
    )
    assert reconciled["state"] == K.UNKNOWN, reconciled
    assert coord.holder_of(OBJECT) == "op-1", "the object stays held"
    note = coord.get("op-1")["note"]
    assert "does not prove" in note or "not proof" in note

    # The hold survives closing and reopening the store.
    coord = _coord(tmp_path)
    assert coord.get("op-1")["state"] == K.UNKNOWN
    assert coord.holder_of(OBJECT) == "op-1"

    # 3. the original request lands at the provider, after reconciliation.
    landed = H.apply_deferred(gh.root)
    assert landed["applied"] == 1, landed
    assert H.writes(gh.root) == 1
    assert H.content_at(gh.root) == VALUE

    # 4. the identical retry: same id, same content, same base revision.
    retried = _dispatch_then_time_out(coord, gh)
    assert retried["status"] == K.CONFLICT, retried
    assert retried["holder"] == "op-1"
    assert retried.get("second_write") is False
    assert H.puts(gh.root) == 1, "no second request may be dispatched"
    assert H.writes(gh.root) == 1, "no double write"
    assert coord.get("op-1")["attempt_count"] == 1

    # 5. and the hold is resolvable by evidence once the application IS
    #    attributable: the deferred commit carries concord(op-1).
    settled = A.reconcile_from_provider(
        coord, gh.adapter, operation_id="op-1", object_key=OBJECT
    )
    assert settled["state"] == K.CONFIRMED, settled
    assert coord.holder_of(OBJECT) is None


def test_a_deferred_request_that_never_lands_still_keeps_the_hold(gh, tmp_path, monkeypatch):
    """The other half: an unchanged read is not a proof even when nothing lands."""
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_deferred")
    coord = _coord(tmp_path)
    assert _dispatch_then_time_out(coord, gh)["status"] == K.UNKNOWN

    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ok")
    first = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)
    second = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)

    assert first["state"] == second["state"] == K.UNKNOWN
    assert coord.holder_of(OBJECT) == "op-1", (
        "nothing observed here can distinguish 'not yet' from 'never'"
    )


# --------------------------------------------- the CLI surface takes the same view
def test_the_cli_reconcile_of_a_pending_operation_does_not_release_it(gh, tmp_path):
    """``--assume-dead-process`` unlocks the read, not the release."""
    import json
    import os
    import pathlib
    import shutil
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    override = (os.environ.get("HERMES_BIN") or "").strip()
    candidate = pathlib.Path(override) if override else None
    if candidate is None or not candidate.exists():
        candidate = root / ".venv" / "Scripts" / "hermes.exe"
        if not candidate.exists():
            candidate = root / ".venv" / "bin" / "hermes"
    if not candidate.exists():
        found = shutil.which("hermes")
        candidate = pathlib.Path(found) if found else None
    if candidate is None:
        pytest.skip("no hermes CLI found (set HERMES_BIN)")
    cli = candidate

    store = tmp_path / "coordinator.db"
    coord = K.Coordinator(str(store))
    coord.admit(
        object_key=OBJECT, operation_id="op-pending", fingerprint="fp",
        base_revision=gh.sha, actor="profile:test", base_identity=_seed_commit(gh.root),
    )
    coord.begin_dispatch("op-pending")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(pathlib.Path(__file__).resolve().parents[2] / "isolated-home")
    env["CONCORD_DB"] = str(store)
    completed = subprocess.run(
        [str(cli), "concord", "reconcile", "op-pending", "--assume-dead-process", "--json"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["state"] == K.UNKNOWN, payload
    assert payload["held"] is True
    assert coord.get("op-pending")["state"] == K.UNKNOWN
    assert H.puts(gh.root) == 0, "reconcile never sends a write"


# ------------------------------------------------------- what DOES release a hold
def test_the_kernel_guard_refuses_a_bare_assertion_of_non_execution(gh, tmp_path):
    coord = _coord(tmp_path)
    coord.admit(
        object_key=OBJECT, operation_id="op-1", fingerprint="fp",
        base_revision=gh.sha, actor="c7", base_identity=_seed_commit(gh.root),
    )
    coord.begin_dispatch("op-1")

    def says_not_applied(**_kwargs):
        return K.EvidenceVerdict(K.FAILED_BEFORE_EXECUTION, "evaluator says nothing applied")

    # No proof object at all.
    first = coord.reconcile("op-1", {"revision": gh.sha}, evaluate=says_not_applied,
                            require_release_proof=True)
    assert first["state"] == K.UNKNOWN

    # A proof that does not name this operation.
    other = coord.reconcile(
        "op-1",
        {"revision": gh.sha,
         "non_execution": {"operation_id": "op-2", "authoritative": True,
                           "source": "provider audit", "covers": "attempt 1"}},
        evaluate=says_not_applied, require_release_proof=True,
    )
    assert other["state"] == K.UNKNOWN

    # A proof with no window.
    bare = coord.reconcile(
        "op-1",
        {"revision": gh.sha,
         "non_execution": {"operation_id": "op-1", "authoritative": True,
                           "source": "provider audit"}},
        evaluate=says_not_applied, require_release_proof=True,
    )
    assert bare["state"] == K.UNKNOWN

    # The documented shape: named, authoritative, sourced, covering the window.
    released = coord.reconcile(
        "op-1",
        {"revision": gh.sha,
         "non_execution": {"operation_id": "op-1", "authoritative": True,
                           "source": "provider audit log", "covers": "attempt 1"}},
        evaluate=says_not_applied, require_release_proof=True,
    )
    assert released["state"] == K.FAILED_BEFORE_EXECUTION
    assert coord.holder_of(OBJECT) is None


def test_an_operation_that_was_never_dispatched_needs_no_proof(gh, tmp_path):
    """The guard is about a request that may have left the process, not about
    every settlement: a reserved operation never sent anything."""
    coord = _coord(tmp_path)
    coord.admit(
        object_key=OBJECT, operation_id="op-reserved", fingerprint="fp",
        base_revision=gh.sha, actor="c7", base_identity=_seed_commit(gh.root),
    )

    def says_not_applied(**_kwargs):
        return K.EvidenceVerdict(K.FAILED_BEFORE_EXECUTION, "never dispatched")

    settled = coord.reconcile("op-reserved", {"revision": gh.sha},
                              evaluate=says_not_applied, require_release_proof=True)
    assert settled["state"] == K.FAILED_BEFORE_EXECUTION
