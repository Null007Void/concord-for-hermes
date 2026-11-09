"""Run the C3 mock matrix and write the artifacts the report cites.

    python scripts/mock_matrix.py [out_dir]

Writes ``c3-matrix.json`` (every row, with its expectation and checks) and
``c3-matrix.md`` (the table) into ``evidence/`` by default. Exits non-zero if
any row disagrees with its expectation — the artifact is never produced from a
run that did not hold.
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
# The adapter/kernel live with the product plugin; the fake-provider helpers with
# the probe fixture. Both are the installed copies, as in the test suite.
sys.path.insert(0, str(INSTALLED / "concord_probe"))
sys.path.insert(0, str(INSTALLED / "concord-for-hermes"))

import mock_matrix  # noqa: E402


def main() -> int:
    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="concord-matrix-") as tmp:
        rows = mock_matrix.run_matrix(pathlib.Path(tmp))
    (out_dir / "c3-matrix.json").write_text(json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8")
    (out_dir / "c3-matrix.md").write_text(mock_matrix.render_markdown(rows), encoding="utf-8")
    failures = [row for row in rows if not row["ok"]]
    print(mock_matrix.render_markdown(rows))
    print(json.dumps({
        "rows": len(rows),
        "failures": len(failures),
        "out": str(out_dir),
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
