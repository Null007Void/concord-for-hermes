"""C3 — the eight-fixture mock matrix, asserted as one table.

The rows come from ``mock_matrix.run_matrix``, the same code that writes
``evidence/c3-matrix.json``. Nothing here re-implements an expectation: if the
artifact and this test disagreed, one of them would be wrong and the test would
say so.
"""
from __future__ import annotations

import json

import mock_matrix


def test_the_matrix_is_complete_and_every_row_matches(tmp_path):
    rows = mock_matrix.run_matrix(tmp_path / "matrix")
    assert len(rows) == len(mock_matrix.FIXTURES) * len(mock_matrix.MODES) == 24
    failures = [row for row in rows if not row["ok"]]
    assert not failures, json.dumps(failures, indent=2, default=str)

    for fixture in mock_matrix.FIXTURES:
        modes = [row["mode"] for row in rows if row["fixture"] == fixture]
        assert modes == mock_matrix.MODES, f"{fixture}: {modes}"


def test_the_matrix_records_the_provider_write_counts_that_matter(tmp_path):
    rows = {(row["fixture"], row["mode"]): row for row in mock_matrix.run_matrix(tmp_path / "m")}

    # A refusal never reached the provider.
    assert rows[("write_stale", "fresh_admission")]["provider_writes"] == 0
    assert rows[("unauthorized", "fresh_admission")]["provider_writes"] == 0
    assert rows[("rate_limited", "fresh_admission")]["provider_writes"] == 0

    # An ambiguous outcome did apply exactly once, and the replay did not add one.
    assert rows[("write_ambiguous", "fresh_admission")]["provider_writes"] == 1
    assert rows[("write_ambiguous", "replay")]["provider_writes"] == 1
    assert rows[("write_ambiguous", "replay")]["second_write"] is False

    # Replay after success returns evidence, never a second write.
    assert rows[("write_ok", "replay")]["status"] == "replayed"
    assert rows[("write_ok", "replay")]["second_write"] is False
    assert rows[("write_ok", "replay")]["provider_writes"] == 1

    # The crash window: pending on restart, held against replay, settled only by
    # a provider read — and the read still did not write.
    crash = rows[("crash_between_write_and_receipt", "fresh_admission")]
    assert crash["state"] == "pending"
    assert crash["provider_writes"] == 1
    checks = {check["name"]: check["ok"] for check in crash["checks"]}
    assert checks["child was alive when the kill was issued"] is True
    assert checks["the fake gh child exited after its parent died"] is True
    assert rows[("crash_between_write_and_receipt", "reconcile")]["provider_writes"] == 1
    assert rows[("crash_between_write_and_receipt", "reconcile")]["state"] == "confirmed"
