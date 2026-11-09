"""C6 — reconciliation from immutable evidence, not from content identity (P1-3).

The defect: ``opaque_evidence_verdict`` returned ``failed_before_execution`` as
soon as the current blob SHA equalled the base, and the kernel released the
object on that. A Git blob identifies content, not an event, so a same-content
commit (a no-op write that did apply) and an A-to-B-to-A revert both leave the
SHA unchanged while the file was genuinely changed. Releasing the hold there
told the next caller "nothing was written" when something was.

The 006 fix said: a settlement of "nothing applied" may only be reached from an
**immutable** reference - the latest commit on the path is still the commit that
was recorded at admission - plus attributable application evidence for the
confirmed direction.

CONCORD-007 (review F7-2) closed the remaining hole in that fix: an unchanged
reference is not a proof that a *dispatched* request cannot still land. A write
that timed out can be applied by the provider after the read, so a dispatched
operation now stays ``unknown`` and held unless its application is attributable
(receipted as confirmed) or the provider makes an authoritative non-execution
statement naming it. The tests that used to assert the old inference are marked
CONTRACT CHANGE below and in ``test_c7_late_landing``.

Every provider interaction below is a real child process (``tests/fakegh``); the
coordinator is the real kernel.
"""
from __future__ import annotations

import json

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H

OBJECT = "github:octo/disposable@main:notes/plan.md"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return H.setup(monkeypatch, tmp_path)


def _seed_commit(root) -> str:
    return H.repo_state(root)["commits"][0]["sha"]


def _coord(tmp_path, name="k.db"):
    return K.Coordinator(str(tmp_path / name))


def _current_sha(root) -> str:
    return H.repo_state(root)["files"][H.PATH]["sha"]


def _admit(coord, *, base, base_identity, value="our edit\n", operation_id="op-1"):
    coord.admit(
        object_key=OBJECT,
        operation_id=operation_id,
        fingerprint=K.fingerprint({"object": OBJECT, "value": value, "expected_revision": base}),
        base_revision=base,
        actor="c6",
        base_identity=base_identity,
    )
    coord.begin_dispatch(operation_id)
    coord.mark_unknown(operation_id, "the response was lost")


# ---------------------------------------------------------------- no-op writes
def test_a_same_content_write_that_applied_is_not_reconciled_as_not_applied(gh, tmp_path, monkeypatch):
    """The review's no-op reproduction: the write landed, the blob did not move."""
    monkeypatch.setenv("FAKEGH_FIXTURE", "write_ambiguous")
    coord = _coord(tmp_path)
    seed_commit = _seed_commit(gh.root)

    result = A.guarded_write(
        coord, gh.adapter, object_key=OBJECT, operation_id="op-noop",
        value=H.VALUE,                       # identical to what is already there
        expected_revision=gh.sha, base_identity=seed_commit, actor="c6",
    )
    # The fixture applies the write and then loses the answer.
    assert result["status"] == K.UNKNOWN, result
    assert H.writes(gh.root) == 1, "the provider did apply a commit"
    assert _current_sha(gh.root) == gh.sha, "same content: the blob SHA is unchanged"

    settled = A.reconcile_from_provider(
        coord, gh.adapter, operation_id="op-noop", object_key=OBJECT
    )

    assert settled["state"] == K.CONFIRMED, settled
    assert "attribution" in coord.get("op-noop")["note"]
    assert H.writes(gh.root) == 1, "reconcile never writes"


def test_a_same_content_write_needs_the_fixture_to_apply(gh, tmp_path):
    """Guard for the test above: the no-op really is a no-op to the provider."""
    coord = _coord(tmp_path, "guard.db")
    A.guarded_write(
        coord, gh.adapter, object_key=OBJECT, operation_id="op-guard",
        value=H.VALUE, expected_revision=gh.sha, base_identity=_seed_commit(gh.root),
        actor="c6",
    )
    assert H.writes(gh.root) == 1
    assert _current_sha(gh.root) == gh.sha


# ------------------------------------------------------------------ ABA/revert
def test_an_a_to_b_to_a_revert_never_releases_the_hold(gh, tmp_path):
    """Content back at the base SHA, but two commits landed. Unknown, held."""
    coord = _coord(tmp_path)
    seed_commit = _seed_commit(gh.root)
    target = A.parse_object_key(OBJECT)

    # Someone else moves the file and moves it back.
    gh.adapter.write(target, expected_revision=gh.sha, value="B\n", operation_id="other-1")
    after_b = _current_sha(gh.root)
    gh.adapter.write(target, expected_revision=after_b, value=H.VALUE, operation_id="other-2")
    assert _current_sha(gh.root) == gh.sha, "the revert restored the base blob SHA"
    assert len(H.repo_state(gh.root)["commits"]) >= 3, "two commits landed in between"

    _admit(coord, base=gh.sha, base_identity=seed_commit)

    settled = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)

    assert settled["state"] == K.UNKNOWN, settled
    assert coord.holder_of(OBJECT) == "op-1", "an unattributable history must keep the hold"
    note = coord.get("op-1")["note"]
    assert "moved and moved back" in note or "different content" in note


def test_an_unchanged_blob_without_an_immutable_reference_stays_unknown(gh, tmp_path):
    """The rewritten premise: equal content alone proves nothing."""
    coord = _coord(tmp_path)
    _admit(coord, base=gh.sha, base_identity=None)

    settled = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)

    assert settled["state"] == K.UNKNOWN, settled
    assert coord.holder_of(OBJECT) == "op-1"
    assert "not history" in coord.get("op-1")["note"]


# ------------------------------------------------- the definitive inference
def test_an_unchanged_base_commit_is_not_proof_that_nothing_landed(gh, tmp_path):
    """CONTRACT CHANGE (CONCORD-007, review F7-2).

    This test used to assert ``failed_before_execution`` here, i.e. it encoded
    "the latest commit on the path is still the base commit, so nothing landed".
    A request that timed out can be applied by the provider *after* that read, so
    the object stays held and the retry stays refused. The sequence is exercised
    end to end in ``test_c7_late_landing``.
    """
    coord = _coord(tmp_path)
    seed_commit = _seed_commit(gh.root)
    _admit(coord, base=gh.sha, base_identity=seed_commit)

    settled = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)

    assert settled["state"] == K.UNKNOWN, settled
    assert coord.holder_of(OBJECT) == "op-1"
    note = coord.get("op-1")["note"]
    assert "still the base commit" in note
    assert "does not prove" in note or "not proof" in note


def test_the_reconciled_receipt_keeps_commit_evidence(gh, tmp_path):
    """Carry-forward: a reconciled receipt must not lose the commit SHA."""
    coord = _coord(tmp_path)
    target = A.parse_object_key(OBJECT)
    _admit(coord, base=gh.sha, base_identity=_seed_commit(gh.root), value="ours\n")
    gh.adapter.write(target, expected_revision=gh.sha, value="ours\n", operation_id="op-1")

    settled = coord.reconcile(
        "op-1",
        gh.adapter.read(target),
        evaluate=A.opaque_evidence_verdict,
    )

    assert settled["state"] == K.CONFIRMED, settled
    receipt = coord.get("op-1")["receipt"]
    assert receipt["reconciled"] is True
    assert receipt["commit_sha"], "the immutable reference is part of the receipt"
    assert receipt["commit_sha"] == _seed_commit(gh.root) or len(receipt["commit_sha"]) == 40


# ------------------------------------------------------- delayed application
def test_an_application_that_lands_late_is_confirmed_when_the_read_sees_it(gh, tmp_path):
    """A delayed request is judged by the evidence, not by the base revision."""
    coord = _coord(tmp_path)
    seed_commit = _seed_commit(gh.root)
    _admit(coord, base=gh.sha, base_identity=seed_commit, value="landed late\n")

    # The request that "went missing" reaches the provider now.
    gh.adapter.write(
        A.parse_object_key(OBJECT), expected_revision=gh.sha,
        value="landed late\n", operation_id="op-1",
    )

    settled = A.reconcile_from_provider(coord, gh.adapter, operation_id="op-1", object_key=OBJECT)

    assert settled["state"] == K.CONFIRMED, settled
    assert coord.holder_of(OBJECT) is None


def test_a_pending_dispatch_is_never_settled_by_an_unchanged_read(gh, tmp_path):
    """CONTRACT CHANGE (CONCORD-007, review F7-2).

    The earlier shape settled this ``failed_before_execution`` - the CLI gated it
    behind ``--assume-dead-process``, but the kernel still called it safe while a
    request might be on its way. A dead process says nothing about a request the
    provider already received: it may be applied afterwards. The read is honest
    about what it saw; the settlement is ``unknown``, and the object stays held.
    """
    coord = _coord(tmp_path)
    seed_commit = _seed_commit(gh.root)
    coord.admit(
        object_key=OBJECT, operation_id="op-pending",
        fingerprint=K.fingerprint({"object": OBJECT, "value": "x\n", "expected_revision": gh.sha}),
        base_revision=gh.sha, actor="c6", base_identity=seed_commit,
    )
    coord.begin_dispatch("op-pending")

    settled = A.reconcile_from_provider(
        coord, gh.adapter, operation_id="op-pending", object_key=OBJECT
    )
    assert settled["state"] == K.UNKNOWN, settled
    assert coord.holder_of(OBJECT) == "op-pending"
    evidence = json.dumps(settled["evidence"])
    assert seed_commit in evidence
    assert H.writes(gh.root) == 0
