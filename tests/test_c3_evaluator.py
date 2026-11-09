"""C3 — the opaque-revision evidence predicate, and the seam it lives in.

These tests are about one function plus the kernel contract around it. No
process is spawned here: the adapter's transport is exercised in
``test_c3_adapter.py`` against a real child process.

The claim under test is narrow on purpose. The kernel's three settlements are
unchanged; what changes for a blob-SHA provider is *how* evidence is read.
"""
from __future__ import annotations

import pytest

import concord_gh_adapter as A
import concord_kernel as K

OBJECT = "github:octo/disposable@main:notes/plan.md"
BASE = "a" * 40          # the blob SHA the caller saw
OURS = "b" * 40          # the blob SHA after our write
FOREIGN = "c" * 40


def _fingerprint(value, base=BASE, key=OBJECT):
    return K.fingerprint({"object": key, "value": value, "expected_revision": base})


def _verdict(evidence, *, stored=None, base=BASE, operation_id="op-1", key=OBJECT,
             base_identity=None):
    return A.opaque_evidence_verdict(
        evidence=evidence,
        object_key=key,
        stored_fingerprint=stored if stored is not None else _fingerprint("after"),
        base_revision=base,
        operation_id=operation_id,
        base_identity=base_identity,
    )


# ------------------------------------------------------------ the predicate
def test_unchanged_blob_sha_proves_nothing_was_written():
    """CONTRACT CHANGE (CONCORD-006, review P1-3).

    This test used to assert ``FAILED_BEFORE_EXECUTION`` for an unchanged blob
    SHA, i.e. it encoded the premise "equal blob SHA == no write happened". That
    premise is false: a Git blob identifies content, not an event, and a
    same-content commit or an A-to-B-to-A revert leaves the SHA unchanged while
    the file was genuinely changed. The kernel released the object on that
    inference and a second caller could then write on top of an applied write.

    The contract now: equal content alone settles nothing. "Nothing applied" may
    only be concluded from an immutable reference (the latest commit on the path
    is still the base commit recorded at admission).
    """
    verdict = _verdict({"revision": BASE, "value": "before", "commit_message": "seed"})
    assert verdict.state == K.UNKNOWN
    assert "not history" in verdict.reason
    assert verdict.revision == BASE


def test_an_unchanged_blob_with_the_base_commit_still_proves_nothing():
    """CONTRACT CHANGE (CONCORD-007, review F7-2).

    This test used to assert ``FAILED_BEFORE_EXECUTION`` when the blob SHA was
    unchanged *and* the latest commit on the path was the base commit. That is
    the same invalid inference in a new costume: a ref that has not moved is an
    observation of the past, and a request that timed out can still be applied by
    the provider after this read. A dispatched operation therefore stays held
    until its application is attributable - or until an authoritative provider
    negative names it, which this predicate is never handed.
    """
    verdict = A.opaque_evidence_verdict(
        evidence={
            "revision": BASE,
            "value": "before",
            "commit_message": "seed",
            "commit_sha": "f" * 40,
        },
        object_key=OBJECT,
        stored_fingerprint=_fingerprint("after"),
        base_revision=BASE,
        operation_id="op-1",
        base_identity="f" * 40,
    )
    assert verdict.state == K.UNKNOWN
    assert "still the base commit" in verdict.reason
    assert "does not prove" in verdict.reason


def test_a_same_content_commit_with_our_marker_is_ours_not_unchanged():
    """A no-op write that applied: equal SHA, our commit, our content."""
    verdict = _verdict(
        {
            "revision": BASE,
            "value": "after",
            "commit_message": "concord(op-1): update notes/plan.md",
            "commit_sha": "e" * 40,
        },
        base_identity="f" * 40,
    )
    assert verdict.state == K.CONFIRMED
    assert verdict.revision == BASE


def test_our_content_with_our_commit_marker_is_ours():
    verdict = _verdict(
        {
            "revision": OURS,
            "value": "after",
            "commit_message": "concord(op-1): update notes/plan.md",
        }
    )
    assert verdict.state == K.CONFIRMED
    assert verdict.revision == OURS


def test_content_that_matches_without_attribution_is_never_claimed():
    """Same bytes, someone else's commit: we cannot say the write was ours."""
    verdict = _verdict(
        {"revision": OURS, "value": "after", "commit_message": "edited by hand"}
    )
    assert verdict.state == K.UNKNOWN
    assert "not attributable" in verdict.reason


def test_attribution_without_content_match_is_never_claimed():
    verdict = _verdict(
        {
            "revision": FOREIGN,
            "value": "someone else's text",
            "commit_message": "concord(op-1): update notes/plan.md",
        }
    )
    assert verdict.state == K.UNKNOWN
    assert "not attributable" in verdict.reason


def test_a_moved_blob_with_foreign_content_is_unknown():
    verdict = _verdict({"revision": FOREIGN, "value": "theirs", "commit_message": "theirs"})
    assert verdict.state == K.UNKNOWN


def test_attribution_is_exact_not_a_prefix_match():
    """`op-1` must not be satisfied by a commit belonging to `op-10`."""
    verdict = _verdict(
        {
            "revision": OURS,
            "value": "after",
            "commit_message": "concord(op-10): unrelated work",
        },
        operation_id="op-1",
    )
    assert verdict.state == K.UNKNOWN


def test_a_missing_file_is_unknown_not_unchanged():
    verdict = _verdict({"revision": None, "value": None, "commit_message": None})
    assert verdict.state == K.UNKNOWN
    assert "creation" in verdict.reason


def test_commit_attribution_parser_rejects_a_bare_mention():
    assert not A.commit_attributes_to("see concord(op-1) for context", "op-1")
    assert A.commit_attributes_to("concord(op-1): x", "op-1")
    assert not A.commit_attributes_to("concord(op-1): x", "op-")


# ------------------------------------------------------- the kernel seam
@pytest.fixture
def coord(tmp_path):
    return K.Coordinator(str(tmp_path / "c3.db"))


def _pending(coord, value="after", base=BASE, op="op-1"):
    coord.admit(
        object_key=OBJECT,
        operation_id=op,
        fingerprint=_fingerprint(value, base),
        base_revision=base,
        actor="c3",
    )
    coord.begin_dispatch(op)


def test_reconcile_settles_confirmed_from_opaque_evidence(coord):
    _pending(coord)
    result = coord.reconcile(
        "op-1",
        {
            "revision": OURS,
            "value": "after",
            "commit_message": "concord(op-1): update",
            "commit_sha": "d" * 40,
            "source": "fake gh read",
        },
        evaluate=A.opaque_evidence_verdict,
    )
    assert result["state"] == K.CONFIRMED
    record = coord.get("op-1")
    assert record["receipt"]["reconciled"] is True
    assert record["receipt"]["revision"] == OURS, "the receipt keeps the opaque token, not an int()"
    assert coord.holder_of(OBJECT) is None


def test_reconcile_keeps_an_unchanged_blob_and_base_commit_held(coord):
    """CONTRACT CHANGE (CONCORD-007, review F7-2).

    The old shape settled ``failed_before_execution`` here, from an unchanged
    blob plus the immutable base commit. An unchanged ref is history, not a
    forecast: the operation was dispatched, so a request may still land. The
    kernel guard (``require_release_proof``) is what the CLI and the adapter use;
    this evaluator already answers ``unknown`` on its own merits.
    """
    coord.admit(
        object_key=OBJECT,
        operation_id="op-1",
        fingerprint=_fingerprint("after"),
        base_revision=BASE,
        actor="c3",
        base_identity="f" * 40,
    )
    coord.begin_dispatch("op-1")
    result = coord.reconcile(
        "op-1",
        {"revision": BASE, "value": "before", "commit_message": "seed",
         "commit_sha": "f" * 40},
        evaluate=A.opaque_evidence_verdict,
        require_release_proof=True,
    )
    assert result["state"] == K.UNKNOWN
    assert coord.holder_of(OBJECT) == "op-1"

    # The narrow door that DOES release it: an authoritative provider negative
    # that names this operation and covers the window.
    released = coord.reconcile(
        "op-1",
        {"revision": BASE, "value": "before", "commit_message": "seed",
         "commit_sha": "f" * 40,
         "non_execution": {"operation_id": "op-1", "authoritative": True,
                           "source": "provider audit log", "covers": "attempt 1"}},
        evaluate=A.opaque_evidence_verdict,
        require_release_proof=True,
    )
    assert released["state"] == K.FAILED_BEFORE_EXECUTION
    assert coord.holder_of(OBJECT) is None


def test_reconcile_keeps_an_unchanged_blob_held_without_a_base_commit(coord):
    _pending(coord)
    result = coord.reconcile(
        "op-1",
        {"revision": BASE, "value": "before", "commit_message": "seed"},
        evaluate=A.opaque_evidence_verdict,
    )
    assert result["state"] == K.UNKNOWN
    assert coord.holder_of(OBJECT) == "op-1", (
        "equal content is not proof; the hold may only be released on immutable evidence"
    )


def test_reconcile_stays_unknown_and_held_on_unattributable_evidence(coord):
    _pending(coord)
    result = coord.reconcile(
        "op-1",
        {"revision": FOREIGN, "value": "theirs", "commit_message": "theirs"},
        evaluate=A.opaque_evidence_verdict,
    )
    assert result["state"] == K.UNKNOWN
    assert coord.holder_of(OBJECT) == "op-1", "an unattributable change must not release the object"
    assert "not attributable" in coord.get("op-1")["note"]


def test_an_evaluator_may_not_invent_a_fourth_settlement(coord):
    _pending(coord)

    def rogue(**_kwargs):
        return K.EvidenceVerdict("probably_fine", "optimism")

    with pytest.raises(ValueError):
        coord.reconcile("op-1", {"revision": OURS, "value": "after"}, evaluate=rogue)
    assert coord.get("op-1")["state"] == K.PENDING, "a rejected verdict must not settle anything"


def test_the_default_evaluator_is_still_the_integer_one(coord):
    """Opt-in, not silent: without an evaluator, a SHA is not arithmetic."""
    coord.admit(
        object_key="doc", operation_id="op-int",
        fingerprint=K.fingerprint({"object": "doc", "value": "x", "expected_revision": 1}),
        base_revision=1, actor="c2",
    )
    coord.begin_dispatch("op-int")
    with pytest.raises(ValueError):
        coord.reconcile("op-int", {"revision": "b" * 40, "value": "x"})
    assert coord.get("op-int")["state"] == K.PENDING


# --------------------------------------------------- admission under SHA
def test_admission_accepts_an_opaque_revision_without_touching_the_c2_path(coord):
    result = coord.run(
        operation_id="op-sha",
        actor="c3",
        received_args={"object": OBJECT, "value": "after", "expected_revision": BASE},
        revision_model="opaque",
        mutate=lambda: {"blob_sha": OURS, "commit_sha": "e" * 40},
    )
    assert result["status"] == K.CONFIRMED
    assert coord.get("op-sha")["base_revision"] == BASE


def test_opaque_model_still_refuses_an_empty_revision(coord):
    with pytest.raises(ValueError):
        K.material_from_arguments(
            {"object": OBJECT, "value": "x", "expected_revision": "  "}, revision_model="opaque"
        )
