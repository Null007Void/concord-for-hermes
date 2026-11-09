"""One ``concord_github_write`` call in its own OS process.

Used by the C4 cross-process test: two of these start at the same time on the
same object with different operation ids, against one coordinator store and one
fake provider. The point is that the *tool* - with its read-then-admit order -
serialises them, not just the kernel primitive.

Both children are handed the same ``expected_revision`` (the revision both
"agents" read before either wrote) - that is the overlapping read/edit interval
the review asked for, and the loser must conflict rather than overwrite.

Usage:  python write_child.py '<json payload>'
Payload keys: operation_id, content, expected_revision, store, repository, path,
branch, gh (argv list). The fake provider's state comes from FAKEGH_* in the
environment.
"""
from __future__ import annotations

import json
import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "plugin"
sys.path.insert(0, str(PLUGIN_DIR))

import concord_store as S  # noqa: E402
import concord_tool as T  # noqa: E402


def main() -> int:
    payload = json.loads(sys.argv[1])
    settings = S.resolve_settings(
        {
            "allowed_repositories": [payload["repository"]],
            "gh_command": list(payload["gh"]),
        },
        store=pathlib.Path(payload["store"]),
    )
    shape = T.run_write(
        {
            "repository": payload["repository"],
            "path": payload["path"],
            "branch": payload["branch"],
            "operation_id": payload["operation_id"],
            "expected_revision": payload["expected_revision"],
            "content": payload["content"],
        },
        settings=settings,
        actor="profile:child",
    )
    print(json.dumps(shape))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
