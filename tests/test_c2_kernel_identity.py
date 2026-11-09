"""C2 — identities and the fingerprint contract.

Q2 of the C1 verdict is the load-bearing rule here: `tool_request` middleware
can rewrite arguments before a tool body runs, so the immutable fingerprint has
to be derived from the arguments the body actually received. A fingerprint of
the model's original call would let a rewrite turn one operation id into two
different external actions.
"""
from __future__ import annotations

import concord_kernel as K
import pytest
from concord_core import LocalProvider, StaleRevision


def make_provider(tmp_path, key="doc", value=None):
    provider = LocalProvider(str(tmp_path / "store.db"))
    provider.seed(key, {"body": "before"} if value is None else value)
    return provider


def mutator(provider, key, value, base):
    def mutate():
        try:
            return provider.update(key, expected_revision=base, value=value)
        except StaleRevision as exc:
            raise K.Rejected(str(exc)) from exc

    return mutate


@pytest.fixture
def coord(tmp_path):
    return K.Coordinator(str(tmp_path / "kernel.db"))


# ------------------------------------------------------------- material
def test_fingerprint_ignores_key_order_and_separates_types():
    a = K.fingerprint({"object": "doc", "value": {"a": 1, "b": [2, 3]}, "expected_revision": 7})
    b = K.fingerprint({"expected_revision": 7, "value": {"b": [2, 3], "a": 1}, "object": "doc"})
    assert a == b
    assert a != K.fingerprint({"object": "doc", "value": "1", "expected_revision": 7})
    assert a != K.fingerprint({"object": "doc", "value": {"a": 1, "b": [2, 3]}, "expected_revision": 8})


def test_material_requires_an_object_and_a_value():
    with pytest.raises(ValueError):
        K.material_from_arguments({"value": "x", "expected_revision": 0})
    with pytest.raises(ValueError):
        K.material_from_arguments({"object": "doc", "expected_revision": 0})
    with pytest.raises(ValueError):
        K.material_from_arguments({"object": "doc", "value": "x", "expected_revision": "nope"})


def test_fingerprint_comes_from_the_arguments_the_tool_received(coord, tmp_path):
    """The model's call and the tool's received args differ; the stored digest
    must describe what the tool actually received."""
    provider = make_provider(tmp_path)
    model_call = {"object": "doc", "value": {"body": "model-wrote"}, "expected_revision": 1}
    rewritten = {"object": "doc", "value": {"body": "middleware-wrote"}, "expected_revision": 1}

    result = coord.run(
        operation_id="op-1",
        actor="probe",
        received_args=rewritten,
        mutate=mutator(provider, "doc", rewritten["value"], 1),
    )
    assert result["status"] == K.CONFIRMED

    stored = coord.get("op-1")
    assert stored["fingerprint"] == K.fingerprint_from_arguments(rewritten)
    assert stored["fingerprint"] != K.fingerprint_from_arguments(model_call)
    assert provider.read("doc")["value"] == rewritten["value"]


# ---------------------------------------------------------------- replay
def test_replay_returns_the_original_receipt_and_never_writes_again(coord, tmp_path):
    provider = make_provider(tmp_path)
    args = {"object": "doc", "value": {"body": "after"}, "expected_revision": 1}
    calls = []

    def mutate():
        calls.append(1)
        return provider.update("doc", expected_revision=1, value=args["value"])

    first = coord.run(operation_id="op-1", actor="probe", received_args=args, mutate=mutate)
    second = coord.run(operation_id="op-1", actor="probe", received_args=args, mutate=mutate)

    assert first["status"] == K.CONFIRMED and first["second_write"] is True
    assert second["status"] == "replayed" and second["second_write"] is False
    assert second["receipt"] == first["receipt"]
    assert len(calls) == 1, "replay reached the provider"
    assert provider.read("doc")["revision"] == 2
    assert coord.get("op-1")["attempt_count"] == 1


def test_replay_survives_a_fresh_coordinator_instance(coord, tmp_path):
    provider = make_provider(tmp_path)
    args = {"object": "doc", "value": "after", "expected_revision": 1}
    coord.run(operation_id="op-1", actor="probe", received_args=args,
              mutate=mutator(provider, "doc", "after", 1))

    fresh = K.Coordinator(coord.path)
    replay = fresh.run(operation_id="op-1", actor="probe", received_args=args,
                       mutate=lambda: pytest.fail("replay must not dispatch"))
    assert replay["status"] == "replayed"
    assert replay["receipt"]["revision"] == 2


def test_same_operation_id_with_different_material_is_refused(coord, tmp_path):
    provider = make_provider(tmp_path)
    first = {"object": "doc", "value": "after", "expected_revision": 1}
    second = {"object": "doc", "value": "different", "expected_revision": 1}
    coord.run(operation_id="op-1", actor="probe", received_args=first,
              mutate=mutator(provider, "doc", "after", 1))

    result = coord.run(operation_id="op-1", actor="probe", received_args=second,
                       mutate=lambda: pytest.fail("mismatched replay must not dispatch"))
    assert result["status"] == "refused"
    assert result["reason"] == "argument_mismatch"
    assert provider.read("doc")["value"] == "after"
    assert provider.read("doc")["revision"] == 2


# -------------------------------------------------------------- identities
def test_attempt_identity_is_distinct_from_operation_and_object(coord, tmp_path):
    provider = make_provider(tmp_path)
    result = coord.run(
        operation_id="op-1",
        actor="probe",
        received_args={"object": "doc", "value": "after", "expected_revision": 1},
        mutate=mutator(provider, "doc", "after", 1),
    )
    attempts = coord.get("op-1")["attempts"]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["ordinal"] == 1
    assert attempt["attempt_id"] == result["attempt_id"]
    assert attempt["attempt_id"] != "op-1" and "op-1" in attempt["attempt_id"]
    assert attempt["state"] == K.CONFIRMED
    assert attempt["settled_seq"] > attempt["started_seq"]


def test_retry_after_a_proved_non_execution_opens_a_second_attempt(coord, tmp_path):
    provider = make_provider(tmp_path)
    args = {"object": "doc", "value": "after", "expected_revision": 1}

    def rejected():
        raise K.Rejected("418: precondition failed")

    first = coord.run(operation_id="op-1", actor="probe", received_args=args, mutate=rejected)
    assert first["status"] == K.CONFLICT
    assert coord.get("op-1")["state"] == K.FAILED_BEFORE_EXECUTION

    second = coord.run(operation_id="op-1", actor="probe", received_args=args,
                       mutate=mutator(provider, "doc", "after", 1))
    assert second["status"] == K.CONFIRMED

    record = coord.get("op-1")
    assert record["state"] == K.CONFIRMED
    assert record["attempt_count"] == 2
    assert [a["ordinal"] for a in record["attempts"]] == [1, 2]
    assert record["attempts"][0]["state"] == K.FAILED_BEFORE_EXECUTION
    assert record["attempts"][0]["attempt_id"] != record["attempts"][1]["attempt_id"]


def test_one_object_holds_one_operation_but_objects_are_independent(coord, tmp_path):
    provider = make_provider(tmp_path)
    provider.seed("doc-2", {"body": "before"})
    coord.admit(object_key="doc", operation_id="op-1", fingerprint="a" * 64,
                base_revision=1, actor="one")
    coord.admit(object_key="doc-2", operation_id="op-2", fingerprint="b" * 64,
                base_revision=1, actor="two")

    blocked = coord.admit(object_key="doc", operation_id="op-3", fingerprint="c" * 64,
                          base_revision=1, actor="three")
    assert blocked.kind == K.CONFLICT
    assert blocked.holder == "op-1"
    assert coord.holder_of("doc") == "op-1"
    assert coord.holder_of("doc-2") == "op-2"


def test_a_second_intent_with_identical_material_is_still_a_conflict(coord):
    """Same content, different operation id, while the first is unresolved:
    the provider would accept both writes. Object identity is what prevents it."""
    material = {"object": "doc", "value": "after", "expected_revision": 1}
    fp = K.fingerprint(material)
    coord.admit(object_key="doc", operation_id="op-1", fingerprint=fp, base_revision=1, actor="a")
    second = coord.admit(object_key="doc", operation_id="op-2", fingerprint=fp,
                         base_revision=1, actor="b")
    assert second.kind == K.CONFLICT
    assert second.holder == "op-1"
