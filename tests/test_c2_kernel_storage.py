"""C2 — bounded storage: record limits and a tested retention policy.

The rule under test: bounded storage may drop detail, never evidence of an
unresolved intent. A pruned record keeps its identity, so replay is refused as
pruned instead of silently executing a second external action.
"""
from __future__ import annotations

import hashlib

import pytest

import concord_kernel as K
from concord_core import LocalProvider, StaleRevision


@pytest.fixture
def provider(tmp_path):
    p = LocalProvider(str(tmp_path / "store.db"))
    p.seed("doc", {"body": "before"})
    return p


def _args(value="after", base=1):
    return {"object": "doc", "value": value, "expected_revision": base}


def _confirm(coord, provider, op, value, base):
    def mutate():
        try:
            return provider.update("doc", expected_revision=base, value=value)
        except StaleRevision as exc:
            raise K.Rejected(str(exc)) from exc

    return coord.run(operation_id=op, actor="a", received_args=_args(value, base), mutate=mutate)


def test_an_oversized_receipt_is_bounded_with_a_verifiable_digest(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_receipt_bytes=200)
    receipt = {"blob": "x" * 5000, "revision": 2}
    result = coord.run(operation_id="op-1", actor="a", received_args=_args(),
                       mutate=lambda: receipt)
    assert result["status"] == K.CONFIRMED

    stored = coord.get("op-1")["receipt"]
    assert stored["_truncated"] is True
    assert stored["bytes"] == len(K.canonical_json(receipt).encode("utf-8"))
    assert stored["sha256"] == hashlib.sha256(K.canonical_json(receipt).encode("utf-8")).hexdigest()
    assert "storage limit" in coord.get("op-1")["note"]


def test_an_oversized_note_is_truncated_visibly_not_silently(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_note_chars=80)
    long_reason = "r" * 400
    coord.admit(object_key="doc", operation_id="op-1", fingerprint="f" * 64,
                base_revision=1, actor="a")
    coord.mark_unknown("op-1", long_reason)

    record = coord.get("op-1")
    assert len(record["note"]) <= 80
    assert record["note"].endswith("chars]")
    assert record["note_chars"] > len(record["note"])


def test_resolved_records_are_pruned_beyond_the_per_object_bound(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_resolved_per_object=3)
    revision = 1
    for i in range(1, 7):
        result = _confirm(coord, provider, f"op-{i}", f"v{i}", revision)
        assert result["status"] == K.CONFIRMED, result
        revision += 1
    assert provider.read("doc")["revision"] == 7

    records = [coord.get(f"op-{i}") for i in range(1, 7)]
    pruned = [r for r in records if r["pruned"]]
    kept = [r for r in records if not r["pruned"]]
    assert len(pruned) == 3, [(r["operation_id"], r["pruned"]) for r in records]
    assert [r["operation_id"] for r in kept] == ["op-4", "op-5", "op-6"]
    assert all(r["receipt"] is None for r in pruned)

    # A pruned record still refuses a second write: identity survived, detail did not.
    before = provider.read("doc")["revision"]
    replay = _confirm(coord, provider, "op-1", "v1", 1)
    assert replay["status"] == "replayed_pruned"
    assert replay["second_write"] is False
    assert provider.read("doc")["revision"] == before


def test_unresolved_records_are_never_pruned(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_resolved_per_object=1)
    coord.admit(object_key="held", operation_id="op-held", fingerprint="h" * 64,
                base_revision=0, actor="a")
    coord.begin_dispatch("op-held")
    coord.mark_unknown("op-held", "ambiguous")

    _confirm(coord, provider, "op-1", "v1", 1)
    _confirm(coord, provider, "op-2", "v2", 2)

    report = coord.prune_resolved(older_than_seconds=0, keep_per_object=0)
    assert report["unresolved_preserved"] == 1
    record = coord.get("op-held")
    assert record["state"] == K.UNKNOWN
    assert record["pruned"] is False
    assert record["note"] == "outcome not determinable: ambiguous"
    assert coord.holder_of("held") == "op-held"


def test_prune_respects_age_and_dry_run(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"))
    _confirm(coord, provider, "op-1", "v1", 1)
    _confirm(coord, provider, "op-2", "v2", 2)

    dry = coord.prune_resolved(older_than_seconds=0, keep_per_object=0, dry_run=True)
    assert dry["pruned"] == 2 and dry["dry_run"] is True
    assert coord.get("op-1")["pruned"] is False

    fresh = coord.prune_resolved(older_than_seconds=3600, keep_per_object=0)
    assert fresh["pruned"] == 0, "records newer than the cutoff must not be pruned"

    done = coord.prune_resolved(older_than_seconds=0, keep_per_object=0)
    assert done["pruned"] == 2
    assert coord.get("op-1")["receipt"] is None


def test_unresolved_capacity_refuses_instead_of_discarding(tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_unresolved_records=2)
    for i in (1, 2):
        coord.admit(object_key=f"obj-{i}", operation_id=f"op-{i}", fingerprint="f" * 64,
                    base_revision=0, actor="a")
        coord.begin_dispatch(f"op-{i}")

    with pytest.raises(K.CapacityExceeded):
        coord.admit(object_key="obj-3", operation_id="op-3", fingerprint="f" * 64,
                    base_revision=0, actor="a")

    assert coord.stats()["unresolved"] == 2
    for i in (1, 2):
        assert coord.get(f"op-{i}")["state"] == K.PENDING
    assert coord.get("op-3") is None


def test_capacity_refusal_is_a_refusal_through_run(tmp_path):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_unresolved_records=1)
    coord.admit(object_key="obj-1", operation_id="op-1", fingerprint="f" * 64,
                base_revision=0, actor="a")
    coord.begin_dispatch("op-1")

    result = coord.run(operation_id="op-2", actor="a",
                       received_args={"object": "obj-2", "value": "x", "expected_revision": 0},
                       mutate=lambda: pytest.fail("capacity refusal must not dispatch"))
    assert result["status"] == "refused"
    assert result["reason"] == "capacity"


def test_attempt_limit_is_enforced(tmp_path, provider):
    coord = K.Coordinator(str(tmp_path / "k.db"), max_attempts_per_operation=1)

    def rejected():
        raise K.Rejected("precondition failed")

    first = coord.run(operation_id="op-1", actor="a", received_args=_args(), mutate=rejected)
    assert first["status"] == K.CONFLICT
    again = coord.admit(object_key="doc", operation_id="op-1",
                        fingerprint=K.fingerprint_from_arguments(_args()),
                        base_revision=1, actor="a")
    assert again.kind == K.ADMITTED

    with pytest.raises(K.CapacityExceeded):
        coord.begin_dispatch("op-1")
    assert coord.get("op-1")["state"] == K.PREPARED, "the refused transition must not half-apply"
