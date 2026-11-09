"""Run the C3 three-baseline comparison and write its artifacts.

    python scripts/baselines.py [out_dir]

Writes ``c3-baselines.json`` and ``c3-baselines.md`` (default ``evidence/``).
Exits non-zero if the numbers contradict the deltas the report claims.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[1]
INSTALLED = ROOT.parent / "isolated-home" / "plugins"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(INSTALLED / "concord_probe"))
sys.path.insert(0, str(INSTALLED / "concord-for-hermes"))

import baselines  # noqa: E402


def main() -> int:
    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="concord-baselines-") as tmp:
        rows = baselines.run_baselines(pathlib.Path(tmp))

    by_key = {(row["scenario"], row["baseline"]): row for row in rows}
    checks = {
        "concurrent: provider CAS is the guard in every column": all(
            by_key[("concurrent_stale_writes", b)]["applied_writes"] == 1 for b in baselines.BASELINES
        ),
        "duplicate replay: provider-alone dispatches twice": (
            by_key[("duplicate_replay", "provider_alone")]["provider_dispatch_attempts"] == 2
        ),
        "duplicate replay: concord dispatches once": (
            by_key[("duplicate_replay", "concord")]["provider_dispatch_attempts"] == 1
        ),
        "ambiguous: concord is the only column that settles it": (
            by_key[("ambiguous_outcome", "concord")]["resolved"].startswith("yes")
            and by_key[("ambiguous_outcome", "provider_alone")]["resolved"].startswith("no")
            and by_key[("ambiguous_outcome", "provider_plus_advisory")]["resolved"].startswith("no")
        ),
        "unrelated object: no column blocks": all(
            by_key[("unrelated_object_progress", b)]["applied_writes"] == 1
            for b in baselines.BASELINES
        ),
    }

    (out_dir / "c3-baselines-rows.json").write_text(
        json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "c3-baselines-table.md").write_text(baselines.render_markdown(rows), encoding="utf-8")
    print(baselines.render_markdown(rows))
    print(json.dumps({"checks": checks, "out": str(out_dir)}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
