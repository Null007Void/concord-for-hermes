"""Provider reconciliation is bound to the operation's recorded target."""
import pytest

import concord_gh_adapter as A
import concord_kernel as K

OBJECT = "github:octo/disposable@main:a.txt"


class EvidenceAdapter:
    def __init__(self):
        self.reads = []

    def validate_target(self, key):
        return A.parse_object_key(key)

    def read(self, target):
        self.reads.append(target.key)
        return {"revision": "new", "value": "desired", "commit_sha": "commit",
                "commit_message": "concord(op): desired content"}


@pytest.fixture
def pending(tmp_path):
    coord = K.Coordinator(str(tmp_path / "coord.db"))
    material = {"object": OBJECT, "value": "desired", "expected_revision": "base"}
    coord.admit(object_key=OBJECT, operation_id="op", fingerprint=K.fingerprint(material),
                base_revision="base", actor="test")
    coord.begin_dispatch("op")
    coord.mark_unknown("op", "response lost")
    return coord


@pytest.mark.parametrize("other", [
    "github:octo/disposable@main:b.txt",
    "github:octo/disposable@other:a.txt",
    "github:octo/other@main:a.txt",
])
def test_wrong_object_never_reads_provider_or_releases_hold(pending, other):
    adapter = EvidenceAdapter()
    result = A.reconcile_from_provider(pending, adapter, operation_id="op", object_key=other)
    assert result["status"] == "refused"
    assert result["reason"] == "object_mismatch"
    assert adapter.reads == []
    assert pending.get("op")["state"] == K.UNKNOWN
    assert pending.holder_of(OBJECT) == "op"


def test_canonical_alias_reads_recorded_target_and_reconciles(pending):
    adapter = EvidenceAdapter()
    result = A.reconcile_from_provider(
        pending, adapter, operation_id="op",
        object_key="github:OCTO/Disposable@refs/heads/main:a.txt",
    )
    assert result["state"] == K.CONFIRMED
    assert adapter.reads == [OBJECT]
    assert pending.holder_of(OBJECT) is None


def test_unknown_id_is_refused_without_provider_read(pending):
    adapter = EvidenceAdapter()
    result = A.reconcile_from_provider(pending, adapter, operation_id="missing", object_key=OBJECT)
    assert result["status"] == "refused"
    assert result["reason"] == "operation_not_found"
    assert adapter.reads == []
    assert pending.holder_of(OBJECT) == "op"
