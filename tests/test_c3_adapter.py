"""C3 — the adapter contract, asserted against what a real child process saw.

Every test here spawns ``tests/fakegh/gh.py``. Nothing about the adapter's argv
construction, base64 encoding, subprocess handling, error mapping or read-back
verification is patched: the fake is the only thing that knows the fixture name,
and it records exactly what it received.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return H.setup(monkeypatch, tmp_path)


def _adapter(gh):
    return gh.adapter


# ------------------------------------------------------------- object keys
def test_the_canonical_key_round_trips():
    key = A.canonical_key("octo", "disposable", "main", "notes/plan.md")
    assert key == "github:octo/disposable@main:notes/plan.md"
    target = A.parse_object_key(key)
    assert (target.owner, target.repo, target.branch, target.path) == (
        "octo", "disposable", "main", "notes/plan.md",
    )
    assert target.key == key


@pytest.mark.parametrize(
    "bad",
    [
        "octo/disposable@main:notes/plan.md",          # no scheme
        "github:octo/disposable:notes/plan.md",        # no branch
        "github:octo/disposable@main",                 # no path
        "github:octo/disposable@main:/notes/plan.md",  # absolute path
        "github:octo/disposable@main:notes/../secret",  # traversal
        "github:octo/disposable@main:notes/\x00x",     # control character
        "github:octo@main:notes/plan.md",              # no repo
    ],
)
def test_a_key_that_is_not_canonical_is_refused(bad):
    with pytest.raises(A.AdapterMisconfigured):
        A.parse_object_key(bad)


def test_the_allowlist_is_configuration_and_cannot_be_widened_by_an_argument(gh):
    with pytest.raises(A.AdapterMisconfigured) as excinfo:
        _adapter(gh).validate_target("github:someone/else@main:notes/plan.md")
    assert excinfo.value.reason == "target_not_allowed"
    assert H.captures(gh.root) == [], "a refused target must not reach the process boundary"


def test_an_adapter_without_an_allowlist_refuses_to_construct(tmp_path):
    with pytest.raises(A.AdapterMisconfigured) as excinfo:
        A.GithubContentsAdapter(allowed_repositories=[])
    assert excinfo.value.reason == "allowlist_missing"


def test_a_hand_built_target_is_checked_too(gh):
    """Defence in depth: bypassing the key parser does not bypass the allowlist."""
    rogue = A.GithubTarget(owner="someone", repo="else", branch="main", path="x.md")
    with pytest.raises(A.AdapterMisconfigured):
        _adapter(gh).read(rogue)


# ----------------------------------------------------------- the happy path
def test_a_guarded_write_confirms_and_writes_once(gh, tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh),
        object_key=gh.object, operation_id="op-1", value="line two\n",
        expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == K.CONFIRMED, result
    assert result["second_write"] is True
    assert result["receipt"]["verified_by"] == "read-after-write"
    assert result["receipt"]["blob_sha"] and result["receipt"]["commit_sha"]
    assert H.writes(gh.root) == 1
    assert H.content_at(gh.root) == "line two\n"
    assert coord.holder_of(gh.object) is None


def test_the_write_is_conditional_on_the_base_blob_sha(gh):
    target = A.parse_object_key(gh.object)
    receipt = _adapter(gh).write(
        target, expected_revision=gh.sha, value="line two\n", operation_id="op-1"
    )
    assert receipt["revision"] == gh.sha
    assert receipt["blob_sha"] != gh.sha


# ----------------------------------------------------- contract assertions
def _put_capture(gh):
    puts = [c for c in H.captures(gh.root) if c["op"] == "put"]
    assert len(puts) == 1, f"expected exactly one PUT, saw {len(puts)}"
    return puts[0]


def test_the_payload_is_exactly_the_documented_shape(gh, tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    value = "unicode ✓ and a newline\n"
    A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value=value, expected_revision=gh.sha, actor="c3",
    )
    payload = _put_capture(gh)["body"]
    assert set(payload) == {"message", "content", "sha", "branch"}
    assert payload["sha"] == gh.sha
    assert payload["branch"] == "main"
    assert base64.b64decode(payload["content"]) == value.encode("utf-8")
    assert "concord(op-1)" in payload["message"]


def test_content_and_commit_message_never_appear_in_argv(gh, tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    value = "a distinctly findable line\n"
    A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value=value, expected_revision=gh.sha, actor="c3",
    )
    capture = _put_capture(gh)
    joined = " ".join(capture["argv"])
    assert value not in joined
    assert base64.b64encode(value.encode()).decode() not in joined
    assert capture["body"]["message"] not in joined
    assert "--input" in capture["argv"], "the body must arrive through --input, not as a field"
    assert not ({"-f", "--raw-field", "-F", "--field"} & set(capture["argv"]))


def test_no_credential_value_reaches_argv_or_the_body(gh, tmp_path, monkeypatch):
    """``gh`` owns the credential; the adapter must neither place nor copy it."""
    sentinel = "ghp_SENTINEL_NOT_A_REAL_TOKEN_0123456789"
    monkeypatch.setenv("GH_TOKEN", sentinel)
    coord = K.Coordinator(str(tmp_path / "k.db"))
    A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    capture = _put_capture(gh)
    assert sentinel not in " ".join(capture["argv"])
    assert sentinel not in capture["body_text"]
    assert capture["gh_token_present"] is True, (
        "the environment must reach gh (that is how gh authenticates), "
        "without logging or persisting the token"
    )


def test_the_body_file_is_deleted_after_the_call(gh, tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    body_file = pathlib.Path(_put_capture(gh)["argv"][-1])
    assert not body_file.exists(), "the temp body must not outlive the call"
    assert not body_file.parent.exists(), "the private temp directory must be removed"


# ------------------------------------------------------------- the gh gate
def test_an_old_gh_fails_closed_before_any_admission(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path, version="1.9.0")
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == "refused"
    assert result["reason"] == "adapter_unavailable"
    assert coord.get("op-1") is None, "the version gate runs before admission"
    assert H.captures(gh.root) == [], "no api call may be made under an unsupported gh"


def test_an_unparseable_gh_version_fails_closed(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path, version="not-a-version")
    with pytest.raises(A.AdapterUnavailable):
        _adapter(gh).ensure_ready()


def test_a_missing_gh_binary_is_a_refusal_not_an_unknown(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path,
                 command=("gh-that-does-not-exist-on-this-host",))
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == "refused"
    assert result["reason"] == "adapter_unavailable"
    assert coord.get("op-1") is None


# --------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    "fixture,status",
    [
        ("write_stale", 409),
        ("write_unprocessable", 422),
        ("unauthorized", 401),
        ("rate_limited", 403),
    ],
)
def test_a_definitive_refusal_settles_failed_before_execution(fixture, status, tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path, fixture=fixture)
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == K.CONFLICT, result
    assert result["state"] == K.FAILED_BEFORE_EXECUTION
    assert result["second_write"] is False
    assert H.writes(gh.root) == 0, f"HTTP {status} must not have applied anything"
    assert coord.holder_of(gh.object) is None, "a proven non-execution releases the object"
    assert coord.get("op-1")["attempt_count"] == 1

    # Retryable, because the proof is that nothing was sent.
    retry = coord.admit(
        object_key=gh.object, operation_id="op-1",
        fingerprint=K.fingerprint(
            {"object": gh.object, "value": "line two\n", "expected_revision": gh.sha}
        ),
        base_revision=gh.sha, actor="c3",
    )
    assert retry.kind == K.ADMITTED


def test_an_ambiguous_outcome_is_unknown_and_held(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path, fixture="write_ambiguous")
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == K.UNKNOWN, result
    assert H.writes(gh.root) == 1, "the write did apply; only the answer was lost"
    assert coord.holder_of(gh.object) == "op-1"

    replay = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert replay["status"] == K.CONFLICT
    assert H.writes(gh.root) == 1, "a held object must not be written twice"

    settled = A.reconcile_from_provider(
        coord, _adapter(gh), operation_id="op-1", object_key=gh.object
    )
    assert settled["state"] == K.CONFIRMED, settled
    assert settled["evidence"]["reconciled"] is True
    assert H.writes(gh.root) == 1, "reconcile must never write"


def test_a_readback_that_is_not_ours_stays_unknown(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path, fixture="write_then_readback_mismatch")
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="line two\n", expected_revision=gh.sha, actor="c3",
    )
    assert result["status"] == K.UNKNOWN, result
    assert coord.holder_of(gh.object) == "op-1"

    settled = A.reconcile_from_provider(
        coord, _adapter(gh), operation_id="op-1", object_key=gh.object
    )
    assert settled["state"] == K.UNKNOWN
    assert "not attributable" in coord.get("op-1")["note"]
    assert coord.holder_of(gh.object) == "op-1", "unattributable evidence never releases the object"


# ------------------------------------------------------------------ reads
def test_read_returns_the_revision_content_and_commit(gh):
    evidence = _adapter(gh).read(A.parse_object_key(gh.object))
    assert evidence["revision"] == gh.sha
    assert evidence["value"] == gh.value
    assert evidence["exists"] is True
    assert evidence["commit_message"] == "seed the repository"


def test_read_of_a_missing_path_is_evidence_not_an_exception(gh):
    target = A.GithubTarget(owner="octo", repo="disposable", branch="main", path="nope.md")
    evidence = _adapter(gh).read(target)
    assert evidence["exists"] is False
    assert evidence["revision"] is None


def test_a_failed_write_never_becomes_a_receipt(tmp_path, monkeypatch):
    """The adapter's Rejected carries the provider's reason, not a stack trace."""
    gh = H.setup(monkeypatch, tmp_path, fixture="write_stale")
    coord = K.Coordinator(str(tmp_path / "k.db"))
    target = A.parse_object_key(gh.object)
    with pytest.raises(K.Rejected) as excinfo:
        _adapter(gh).write(target, expected_revision="0" * 40, value="x", operation_id="op-9")
    assert "409" in str(excinfo.value) or "stale" in str(excinfo.value).lower()
    assert coord.get("op-9") is None, "the adapter alone does not create durable records"
    assert H.writes(gh.root) == 0


# ----------------------------------------------------- a second operation
def test_two_operations_on_one_object_are_serialised(gh, tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    first = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-1",
        value="first\n", expected_revision=gh.sha, actor="a",
    )
    assert first["status"] == K.CONFIRMED
    second = A.guarded_write(
        coord, _adapter(gh), object_key=gh.object, operation_id="op-2",
        value="second\n", expected_revision=gh.sha, actor="b",
    )
    assert second["status"] == K.CONFLICT
    assert second["state"] == K.FAILED_BEFORE_EXECUTION
    assert H.writes(gh.root) == 1, "one applied write; the stale intent was refused by the provider's CAS"
