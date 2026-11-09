"""Live-provider boundary regressions, with no network or credentials."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

import concord_gh_adapter as A
import concord_kernel as K
import github_harness as H


def test_every_api_call_pins_github_com_despite_ambient_host(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_HOST", "enterprise.example.invalid")
    result = A.guarded_write(
        K.Coordinator(str(tmp_path / "k.db")), gh.adapter,
        object_key=gh.object, operation_id="host-pin", value="new text",
        expected_revision=gh.sha, actor="test",
    )
    assert result["status"] == K.CONFIRMED
    captures = H.captures(gh.root)
    assert {c["op"] for c in captures} == {"get", "commits", "put"}
    for capture in captures:
        assert capture["argv"][0:3] == ["api", "--hostname", "github.com"]


@pytest.mark.parametrize("stderr,stdout,expected", [
    ("gh: upstream HTTP 409 failure (HTTP 500)", "", 500),
    ("gh: upstream (HTTP 422) failure (HTTP 502)", "", 502),
    ("gh: stale revision (HTTP 409)\n", "", 409),
    ("connection reset after HTTP 403 appeared in a message", "", None),
    ("connection reset", '{"message": "HTTP 404"}', None),
    ("gh: response body mentions HTTP 422", "", None),
])
def test_status_comes_only_from_gh_error_suffix(stderr, stdout, expected):
    completed = subprocess.CompletedProcess([], 1, stdout, stderr)
    assert A.GithubContentsAdapter._status_of(completed) == expected


def test_embedded_refusal_in_500_cannot_release_the_hold(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    original = gh.adapter._invoke
    def invoke(args):
        if "PUT" in args:
            return subprocess.CompletedProcess(args, 1, "", "gh: upstream HTTP 409 failure (HTTP 500)")
        return original(args)
    monkeypatch.setattr(gh.adapter, "_invoke", invoke)
    coord = K.Coordinator(str(tmp_path / "k.db"))
    result = A.guarded_write(
        coord, gh.adapter, object_key=gh.object, operation_id="ambiguous-500",
        value="new text", expected_revision=gh.sha, actor="test",
    )
    assert result["status"] == K.UNKNOWN
    assert coord.holder_of(gh.object) == "ambiguous-500"


def _reader(monkeypatch, content_body, message="concord(op-1): changed"):
    adapter = A.GithubContentsAdapter(allowed_repositories=[("octo", "disposable")])
    adapter._version = (2, 40, 1)
    calls = []
    commit_sha = "c" * 40
    def invoke(args):
        calls.append(args)
        if "/commits?" in args[1]:
            body = [{"sha": commit_sha, "commit": {"message": message}}]
        else:
            assert args[1].endswith("?ref=" + commit_sha), "content must be bound to the attributed commit"
            body = content_body
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
    monkeypatch.setattr(adapter, "_invoke", invoke)
    return adapter, calls


def test_content_and_attribution_are_one_immutable_snapshot(monkeypatch):
    # The moving branch used to return old desired content, then a new commit's
    # operation marker. At that actual commit the content is different.
    adapter, calls = _reader(monkeypatch, {
        "sha": "different-blob", "encoding": "base64",
        "content": base64.b64encode(b"other content").decode(),
    })
    evidence = adapter.read(A.parse_object_key(H.OBJECT))
    verdict = A.opaque_evidence_verdict(
        evidence=evidence, object_key=H.OBJECT,
        stored_fingerprint=K.fingerprint({"object": H.OBJECT, "value": "our intent", "expected_revision": "base"}),
        base_revision="base", operation_id="op-1",
    )
    assert "/commits?" in calls[0][1]
    assert verdict.state == K.UNKNOWN


@pytest.mark.parametrize("encoding,content", [("none", ""), (None, ""), ("base64", "%%%"), ("base64", "Y Q==")])
def test_missing_or_invalid_inline_content_fails_closed(monkeypatch, encoding, content):
    adapter, _ = _reader(monkeypatch, {"sha": "blob", "encoding": encoding, "content": content})
    with pytest.raises(A.ReadFailed):
        adapter.read(A.parse_object_key(H.OBJECT))


def test_documented_base64_line_wrapping_is_accepted(monkeypatch):
    adapter, _ = _reader(monkeypatch, {"sha": "blob", "encoding": "base64", "content": "bGlu\nZSBv\r\nbmU=\n"})
    assert adapter.read(A.parse_object_key(H.OBJECT))["value"] == "line one"


def test_a_different_readback_commit_does_not_claim_the_put_receipt(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    original = gh.adapter.read
    def read(target):
        evidence = original(target)
        evidence["commit_sha"] = "d" * 40
        return evidence
    monkeypatch.setattr(gh.adapter, "read", read)
    with pytest.raises(A.AdapterAmbiguous, match="commit returned by the write"):
        gh.adapter.write(A.parse_object_key(gh.object), expected_revision=gh.sha,
                         value="changed", operation_id="op-1")


def test_fake_provider_really_preserves_immutable_commit_content(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    seed_commit = H.repo_state(gh.root)["commits"][0]["sha"]
    gh.adapter.write(A.parse_object_key(gh.object), expected_revision=gh.sha,
                     value="changed", operation_id="new-commit")
    completed = gh.adapter._invoke(["api", f"repos/octo/disposable/contents/{H.PATH}?ref={seed_commit}"])
    body = json.loads(completed.stdout)
    assert body["sha"] == gh.sha
    assert base64.b64decode(body["content"]).decode() == gh.value


def test_reserved_path_characters_round_trip_without_query_injection(tmp_path, monkeypatch):
    path = "notes/a&b#c?d%.md"
    gh = H.setup(monkeypatch, tmp_path, files={H.PATH: H.VALUE, path: "special"})
    target = A.GithubTarget("octo", "disposable", "main", path)
    assert gh.adapter.read(target)["value"] == "special"
    receipt = gh.adapter.write(target, expected_revision=H.blob_sha(b"special"),
                               value="edited", operation_id="encoded-path")
    assert receipt["blob_sha"] == H.blob_sha(b"edited")


def test_a_branch_name_instead_of_commit_sha_cannot_attribute(monkeypatch):
    adapter = A.GithubContentsAdapter(allowed_repositories=[("octo", "disposable")])
    adapter._version = (2, 40, 1)
    def invoke(args):
        if "/commits?" in args[1]:
            body = [{"sha": "main", "commit": {"message": "concord(op-1): changed"}}]
        else:
            body = {"sha": "blob", "encoding": "base64", "content": ""}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
    monkeypatch.setattr(adapter, "_invoke", invoke)
    evidence = adapter.read(A.parse_object_key(H.OBJECT))
    assert evidence["commit_sha"] is None
    assert evidence["commit_message"] is None
