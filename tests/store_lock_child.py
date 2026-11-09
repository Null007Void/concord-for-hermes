"""Hold a SQLite write transaction on a coordinator store.

The C4 contention tests need a *writer* that is demonstrably mid-transaction
while read-only status processes run. Using the real store (not a copy) means the
only thing under test is SQLite's locking behaviour plus Concord's read path.

Usage:
    python store_lock_child.py <store path> <hold seconds> [release file]

Prints ``locked`` once the write lock is held. With a release file it holds the
transaction until that file exists (cap: 120s), which lets a test assert
"finished while the writer was still holding the lock" without a timing bet.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time


def main() -> int:
    path = sys.argv[1]
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    release = sys.argv[3] if len(sys.argv) > 3 else ""
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS concord_lock_probe (t REAL)")
        conn.execute("INSERT INTO concord_lock_probe (t) VALUES (?)", (time.time(),))
        print("locked", flush=True)
        start = time.monotonic()
        deadline = start + 120.0 if release else start + max(hold, 1.0)
        while time.monotonic() < deadline:
            if release and os.path.exists(release):
                break
            time.sleep(0.05)
        conn.execute("COMMIT")
    finally:
        conn.close()
    print("released", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
