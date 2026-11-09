"""C9 — the requested object must gate fingerprint compatibility (F9-1).

``fingerprint`` is a digest over ``{"object": <key>, "value": ..., "expected_revision":
...}``. CONCORD-008 (F8-C) taught admission to re-check a stored digest under the
spelling *that digest was derived from* - which is right for an older spelling of
the same object and wrong for a different object: the relaxation replaces the
material's object with the stored one, so a call for another path, branch or
repository with the same content and base hashes to the stored digest.

The contract this module pins, in this order (owner F9-1):

  1. the requested object must EQUAL the stored object, or
  2. canonical equivalence must be VERIFIED by the configured canonicaliser
     (owner/repo case folded, ``refs/heads/`` stripped, path and branch case
     preserved), before fingerprint compatibility is consulted at all.

A digest match alone never satisfies the object gate. A changed object is
refused with ``object_mismatch`` and gets no dispatch, no receipt and no hold
transferred: a retry must never run under another object's identity. Same-object
replay and same-object retry keep working exactly as the CONCORD-007 contract
says, including across a close/reopen.
"""
from __future__ import annotations

import sys

import concord_gh_adapter as A
import concord_kernel as K
import concord_store as S
import concord_tool as T
import github_harness as H

CANONICAL = "github:octo/disposable@main:notes/plan.md"
OTHER_PATH = "github:octo/disposable@main:notes/other.md"
OTHER_BRANCH = "github:octo/disposable@release:notes/plan.md"
OTHER_REPOSITORY = "github:other/notes@main:notes/plan.md"
#: The same object, spelled the way a pre-canonical client spelled it.
ALIAS = "github:OCTO/Disposable@main:notes/plan.md"
#: The same object, spelled with an explicit ref prefix.
REF_SPELLING = "github:octo/disposable@refs/heads/main:notes/plan.md"

VALUE = "line one\n"
BASE = "a" * 40
RECEIPT = {"blob_sha": "b" * 40, "commit_sha": "c" * 40, "operation_id": "op-9"}

CHANGED = {"path": OTHER_PATH, "branch": OTHER_BRANCH, "repository": OTHER_REPOSITORY}


# ------------------------------------------------------------------ helpers
def open_store(path) -> K.Coordinator:
    """The way the tool and the CLI open it: canonical identity configured."""
    return K.Coordinator(str(path), normalize_object_key=A.canonical_object_key)


def material(object_key: str, *, value: str = VALUE, base: str = BASE) -> dict:
    return {"object": object_key, "value": value, "expected_revision": base}


def never_dispatch():
    raise AssertionError("a request for another object must not dispatch a write")


def confirmed_store(path, *, object_key: str = CANONICAL, base: str = BASE) -> K.Coordinator:
    coord = open_store(path)
    shape = coord.run(
        operation_id="op-9", actor="profile:c9",
        received_args=material(object_key, base=base),
        revision_model="opaque", mutate=lambda: dict(RECEIPT),
    )
    assert shape["status"] == "confirmed", shape
    return coord


def retryable_store(path, *, object_key: str = CANONICAL, base: str = BASE) -> K.Coordinator:
    """A ``failed_before_execution`` operation: the retryable state."""
    coord = open_store(path)
    coord.admit(object_key=object_key, operation_id="op-9",
                fingerprint=K.fingerprint(material(object_key, base=base)),
                base_revision=base, actor="profile:c9",
                material=material(object_key, base=base))
    coord.fail_before_execution("op-9", "c9: provider refused the conditional write")
    assert coord.get("op-9")["state"] == K.FAILED_BEFORE_EXECUTION
    return coord


def _settings(store) -> S.Settings:
    return S.Settings(
        allowed_repositories=(("octo", "disposable"), ("other", "notes")),
        gh_command=(sys.executable, str(H.FAKEGH)),
        timeout_s=30.0,
        store_path=store,
    )


def _tool_args(change: dict, *, base: str) -> dict:
    args = {
        "repository": "octo/disposable", "path": "notes/plan.md", "branch": "main",
        "operation_id": "op-9", "expected_revision": base, "content": VALUE,
    }
    args.update(change)
    return args


# ------------------------------------------- a confirmed operation: replay
def test_a_confirmed_operation_does_not_replay_under_a_changed_path(tmp_path):
    store = tmp_path / "confirmed.db"
    coord = confirmed_store(store)

    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_PATH), revision_model="opaque",
                      mutate=never_dispatch)

    assert shape["status"] == "refused", shape
    assert shape["reason"] == "object_mismatch"
    row = coord.get("op-9")
    assert row["object_key"] == CANONICAL, "the record still names the object it was admitted for"
    assert row["state"] == K.CONFIRMED

    # Durability: the refusal is a property of the store, not of one connection.
    reopened = open_store(store)
    again = reopened.run(operation_id="op-9", actor="profile:c9",
                         received_args=material(OTHER_PATH), revision_model="opaque",
                         mutate=never_dispatch)
    assert again["status"] == "refused", again
    assert again["reason"] == "object_mismatch"


def test_a_confirmed_operation_does_not_replay_under_a_changed_branch(tmp_path):
    coord = confirmed_store(tmp_path / "confirmed-branch.db")
    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_BRANCH), revision_model="opaque",
                      mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape


def test_a_confirmed_operation_does_not_replay_under_a_changed_repository(tmp_path):
    coord = confirmed_store(tmp_path / "confirmed-repo.db")
    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_REPOSITORY), revision_model="opaque",
                      mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape


# ----------------------------------- a retryable operation: never dispatch
def test_a_retry_does_not_dispatch_for_a_changed_path(tmp_path):
    store = tmp_path / "retry.db"
    coord = retryable_store(store)

    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_PATH), revision_model="opaque",
                      mutate=never_dispatch)

    assert shape["status"] == "refused", shape
    assert shape["reason"] == "object_mismatch"
    row = coord.get("op-9")
    assert row["state"] == K.FAILED_BEFORE_EXECUTION, "a refused retry changes nothing"
    assert row["attempt_count"] == 0, "nothing was dispatched"

    reopened = open_store(store)
    again = reopened.run(operation_id="op-9", actor="profile:c9",
                         received_args=material(OTHER_PATH), revision_model="opaque",
                         mutate=never_dispatch)
    assert (again["status"], again["reason"]) == ("refused", "object_mismatch"), again


def test_a_retry_does_not_dispatch_for_a_changed_branch(tmp_path):
    coord = retryable_store(tmp_path / "retry-branch.db")
    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_BRANCH), revision_model="opaque",
                      mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape


def test_a_retry_does_not_dispatch_for_a_changed_repository(tmp_path):
    coord = retryable_store(tmp_path / "retry-repo.db")
    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_REPOSITORY), revision_model="opaque",
                      mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape


def test_a_conflict_retry_does_not_dispatch_for_a_changed_branch(tmp_path):
    """The other retryable state: a conflict row is retried for a different object."""
    store = tmp_path / "conflict-retry.db"
    coord = open_store(store)
    # op-holder holds CANONICAL, so op-9's first intent is recorded as a conflict
    # on the SAME object - the state the tool allows a retry from.
    coord.admit(object_key=CANONICAL, operation_id="op-holder",
                fingerprint=K.fingerprint(material(CANONICAL)), base_revision=BASE,
                actor="profile:c9", material=material(CANONICAL))
    conflict = coord.admit(object_key=CANONICAL, operation_id="op-9",
                           fingerprint=K.fingerprint(material(CANONICAL)), base_revision=BASE,
                           actor="profile:c9", material=material(CANONICAL))
    assert conflict.kind == K.CONFLICT, conflict.as_dict()
    assert coord.get("op-9")["state"] == K.CONFLICT

    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(OTHER_BRANCH), revision_model="opaque",
                      mutate=never_dispatch)

    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape
    assert coord.get("op-9")["state"] == K.CONFLICT, "a refused retry changes nothing"
    assert coord.get("op-9")["attempt_count"] == 0


# --------------------------------------------------------- same object works
def test_a_replay_of_the_same_object_still_returns_the_original_receipt(tmp_path):
    store = tmp_path / "replay.db"
    coord = confirmed_store(store)

    for _ in range(2):  # one connection, then a reopened one
        shape = coord.run(operation_id="op-9", actor="profile:c9",
                          received_args=material(CANONICAL), revision_model="opaque",
                          mutate=never_dispatch)
        assert shape["status"] == "replayed", shape
        assert shape["second_write"] is False
        assert shape["receipt"]["commit_sha"] == RECEIPT["commit_sha"]
        coord = open_store(store)


def test_a_retry_of_the_same_object_still_dispatches(tmp_path):
    """CONCORD-007's contract: an identical retry of a proved-not-executed intent runs."""
    store = tmp_path / "same-object-retry.db"
    coord = retryable_store(store)
    dispatched: list[str] = []

    def mutate():
        dispatched.append("sent")
        return dict(RECEIPT)

    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(CANONICAL), revision_model="opaque",
                      mutate=mutate)

    assert shape["status"] == "confirmed", shape
    assert shape["second_write"] is True
    assert dispatched == ["sent"]
    assert coord.get("op-9")["attempt_count"] == 1


def test_an_alias_spelling_of_the_same_object_is_still_the_same_intent(tmp_path):
    """Verified canonical equivalence is consulted BEFORE the digest."""
    store = tmp_path / "alias.db"
    coord = open_store(store)
    # A row written under the alias spelling (what a pre-canonical client stored).
    coord.admit(object_key=ALIAS, operation_id="op-alias",
                fingerprint=K.fingerprint(material(ALIAS)), base_revision=BASE,
                actor="profile:c9", material=material(ALIAS))
    coord.begin_dispatch("op-alias")
    coord.confirm("op-alias", dict(RECEIPT))

    for spelling in (CANONICAL, REF_SPELLING):
        reopened = open_store(store)
        shape = reopened.run(operation_id="op-alias", actor="profile:c9",
                             received_args=material(spelling), revision_model="opaque",
                             mutate=never_dispatch)
        assert shape["status"] == "replayed", (spelling, shape)
        assert shape["second_write"] is False


def test_a_different_object_does_not_ride_a_legacy_fingerprint_match(tmp_path):
    """The exact F8-C x F9-1 collision: the relaxed digest must not move the object."""
    store = tmp_path / "legacy-collision.db"
    coord = open_store(store)
    coord.admit(object_key=ALIAS, operation_id="op-alias",
                fingerprint=K.fingerprint(material(ALIAS)), base_revision=BASE,
                actor="profile:c9", material=material(ALIAS))
    coord.begin_dispatch("op-alias")
    coord.confirm("op-alias", dict(RECEIPT))

    # Same value, same base - only the object differs: the digest CAN be relaxed
    # onto the stored one, which is exactly why the object gate must come first.
    shape = coord.run(operation_id="op-alias", actor="profile:c9",
                      received_args=material(OTHER_PATH), revision_model="opaque",
                      mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "object_mismatch"), shape


def test_a_changed_value_on_the_same_object_is_still_an_argument_mismatch(tmp_path):
    """The new gate did not swallow the older, narrower refusal."""
    coord = confirmed_store(tmp_path / "mismatch.db")
    shape = coord.run(operation_id="op-9", actor="profile:c9",
                      received_args=material(CANONICAL, value="someone else's edit\n"),
                      revision_model="opaque", mutate=never_dispatch)
    assert (shape["status"], shape["reason"]) == ("refused", "argument_mismatch"), shape


# ---------------------------------------------- the real tool, fake provider
def test_the_real_tool_refuses_a_changed_path_instead_of_replaying(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    store = tmp_path / "tool-confirmed.db"
    coord = confirmed_store(store, object_key=CANONICAL, base=gh.sha)
    assert coord.get("op-9")["state"] == K.CONFIRMED

    shape = T.run_write(
        _tool_args({"path": "notes/plan.md"}, base=gh.sha),
        settings=_settings(store), actor="profile:c9",
    )

    assert shape["outcome"] == "replayed", "control: the same call still replays"

    changed = T.run_write(
        _tool_args({"path": "notes/other.md"}, base=gh.sha),
        settings=_settings(store), actor="profile:c9",
    )
    assert changed["outcome"] == "conflict", changed
    assert changed["state"] == "object_mismatch", changed
    assert changed["held"] is False
    assert H.puts(gh.root) == 0, "a changed object must not reach the provider"


def test_the_real_tool_refuses_a_retry_for_a_changed_repository(tmp_path, monkeypatch):
    gh = H.setup(monkeypatch, tmp_path)
    store = tmp_path / "tool-retry.db"
    coord = retryable_store(store, object_key=CANONICAL, base=gh.sha)
    assert coord.get("op-9")["state"] == K.FAILED_BEFORE_EXECUTION

    changed = T.run_write(
        _tool_args({"repository": "other/notes"}, base=gh.sha),
        settings=_settings(store), actor="profile:c9",
    )

    assert changed["outcome"] == "conflict", changed
    assert changed["state"] == "object_mismatch", changed
    assert changed["second_write"] is False
    assert H.puts(gh.root) == 0, "a retry must never dispatch under another object's identity"
    assert H.writes(gh.root) == 0

    # The stored intent is untouched and its own retry still works.
    assert coord.get("op-9")["object_key"] == CANONICAL
    same = T.run_write(_tool_args({}, base=gh.sha), settings=_settings(store), actor="profile:c9")
    assert same["outcome"] == "confirmed", same
    assert H.puts(gh.root) == 1
