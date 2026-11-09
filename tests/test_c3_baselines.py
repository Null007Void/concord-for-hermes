"""C3 — the three-baseline comparison, asserted on the numbers that matter.

What this test pins down is not that Concord "wins" but where the deltas are:

  * the provider's CAS, not Concord, is what stops two concurrent stale writes
    from both applying — all three columns end with one applied write;
  * a duplicate replay is the case provider-alone gets *wrong* (a false failure
    for a write that landed);
  * an ambiguous outcome is the case only evidence reconciliation can settle;
  * an unresolved object does not block an unrelated one in any column.
"""
from __future__ import annotations

import baselines


def _rows(tmp_path):
    rows = baselines.run_baselines(tmp_path / "baselines")
    assert len(rows) == len(baselines.SCENARIOS) * len(baselines.BASELINES) == 12
    return {(row["scenario"], row["baseline"]): row for row in rows}


def test_concurrent_stale_writes_are_stopped_by_the_provider_not_by_concord(tmp_path):
    rows = _rows(tmp_path)
    for baseline in baselines.BASELINES:
        row = rows[("concurrent_stale_writes", baseline)]
        assert row["applied_writes"] == 1, f"{baseline}: two writes applied"
    assert rows[("concurrent_stale_writes", "provider_alone")]["provider_dispatch_attempts"] == 2
    assert rows[("concurrent_stale_writes", "provider_plus_advisory")]["provider_dispatch_attempts"] == 2
    assert rows[("concurrent_stale_writes", "concord")]["provider_dispatch_attempts"] == 1, (
        "the only difference Concord makes here is one fewer dispatch"
    )


def test_duplicate_replay_is_where_provider_alone_loses(tmp_path):
    rows = _rows(tmp_path)
    alone = rows[("duplicate_replay", "provider_alone")]
    assert alone["provider_dispatch_attempts"] == 2
    assert alone["applied_writes"] == 1
    assert "false failure" in alone["caller_saw_success"]

    advisory = rows[("duplicate_replay", "provider_plus_advisory")]
    assert advisory["provider_dispatch_attempts"] == 1
    assert "yes" in advisory["caller_saw_success"].lower()

    concord = rows[("duplicate_replay", "concord")]
    assert concord["provider_dispatch_attempts"] == 1
    assert "replayed" in concord["caller_saw_success"]


def test_an_ambiguous_outcome_is_only_settled_by_evidence(tmp_path):
    rows = _rows(tmp_path)
    alone = rows[("ambiguous_outcome", "provider_alone")]
    assert alone["applied_writes"] == 1
    assert alone["false_failures"] == 1
    assert alone["resolved"].startswith("no")

    advisory = rows[("ambiguous_outcome", "provider_plus_advisory")]
    assert advisory["applied_writes"] == 1
    assert advisory["resolved"].startswith("no")
    assert "in_flight" in advisory["resolved"]

    concord = rows[("ambiguous_outcome", "concord")]
    assert concord["applied_writes"] == 1
    assert concord["resolved"].startswith("yes")
    assert "confirmed" in concord["resolved"]


def test_an_unresolved_object_does_not_block_an_unrelated_one(tmp_path):
    rows = _rows(tmp_path)
    for baseline in baselines.BASELINES:
        row = rows[("unrelated_object_progress", baseline)]
        assert row["applied_writes"] == 1
        assert row["held_object_blocked_progress"].startswith("no")
