"""Read-only view of the coordinator store: what is held, by whom, and what is safe next.

Two properties matter more than the formatting:

  * **It never writes.** The connection is opened ``mode=ro`` and pinned with
    ``PRAGMA query_only``, so it cannot create the schema, cannot take
    ``BEGIN IMMEDIATE``, and cannot become the reason an admission waits. A
    status command that blocked a write would be a coordination failure caused
    by the tool that exists to prevent them.
  * **It never guesses a state.** Rows are shown as the kernel recorded them,
    with the next action tied to the state - not to a heuristic about how long
    an operation has been sitting there.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:  # imported as part of the plugin package
    from . import concord_kernel as K
    from . import concord_result as R
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_kernel as K
    import concord_result as R

#: Every table a correctly initialised store has. Their presence is what makes a
#: read meaningful; one of them being absent is damage, not emptiness (review
#: F7-3). ``concord_migrations`` is not required - a store written before the
#: identity migration existed is a legitimate store, migrated on next open by
#: the kernel - so its absence is not evidence of damage.
_REQUIRED_TABLES = ("concord_operations", "concord_attempts")

#: Damage classes carried by a snapshot, so a machine consumer can tell a store
#: it may not trust from one it may. Empty means "nothing to report".
DAMAGE_NONE = ""
DAMAGE_PARTIAL_SCHEMA = "partial_schema"
DAMAGE_UNREADABLE = "unreadable"

_OPERATION_COLUMNS = (
    "operation_id", "object_key", "state", "holder", "attempt_count", "base_revision",
    "actor", "note", "pruned_at", "updated_seq", "receipt",
)


@dataclass
class Snapshot:
    store_path: str
    exists: bool = False
    initialized: bool = False
    error: str = ""
    attempts_error: str = ""
    damage: str = DAMAGE_NONE
    rows: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        """Could the recorded operations be read at all?"""
        return self.error == ""

    @property
    def healthy(self) -> bool:
        """May a machine interface report on this store without qualification?

        A store that does not exist, or exists with no Concord schema, is
        honestly empty. A store whose operations or attempts cannot be read is
        not: an empty array from it would be indistinguishable from "nothing is
        held", which is exactly the mistake this property exists to prevent
        (review P2-1). And a store that has *some* of the Concord schema while a
        required table is gone is damaged - the rows that survive are positive
        evidence that records existed, so it can never be reported as healthy
        and empty (review F7-3).
        """
        if not self.exists:
            return True
        if self.damage:
            return False
        return self.error == "" and self.attempts_error == ""

    @property
    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in K.ALL_STATES}
        for row in self.rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return counts

    @property
    def unresolved(self) -> int:
        return sum(1 for row in self.rows if row["state"] in K.BLOCKING_STATES)

    def objects(self) -> list[dict[str, Any]]:
        """Rows grouped by canonical object, held objects first, then by recency."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self.rows:
            groups.setdefault(row["object_key"], []).append(row)
        out: list[dict[str, Any]] = []
        for object_key, rows in groups.items():
            rows.sort(key=lambda r: r["updated_seq"], reverse=True)
            holder_row = next((r for r in rows if r["state"] in K.BLOCKING_STATES), None)
            out.append(
                {
                    "object_key": object_key,
                    "holder": holder_row["operation_id"] if holder_row else None,
                    "holder_state": holder_row["state"] if holder_row else None,
                    "operations": rows,
                }
            )
        out.sort(key=lambda g: (g["holder"] is None, -max(r["updated_seq"] for r in g["operations"])))
        return out

    def refusals(self, limit: int = 10) -> list[dict[str, Any]]:
        """Recorded refusals: the conflict view, newest first."""
        rows = [r for r in self.rows if r["state"] in (K.CONFLICT, K.FAILED_BEFORE_EXECUTION)]
        rows.sort(key=lambda r: r["updated_seq"], reverse=True)
        return rows[:limit]


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection that cannot write, even by accident."""
    uri = "file:" + quote(str(path), safe="/:\\") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


class StoreUnreadable(RuntimeError):
    """The store could not be read. Never reported as "nothing is held"."""

    def __init__(self, detail: str, *, store_path: str = "") -> None:
        super().__init__(detail)
        self.store_path = store_path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _row_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    """Rows in a known-constant table, or ``None`` when it cannot be counted."""
    if table not in _REQUIRED_TABLES:
        raise ValueError(f"not a probed table: {table}")
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def _damage_context(conn: sqlite3.Connection, tables: set[str]) -> str:
    """What survived, named in the error - the size of the damage, not a guess."""
    parts = []
    for table in _REQUIRED_TABLES:
        if table in tables:
            count = _row_count(conn, table)
            if count is not None:
                parts.append(f"{table} still holds {count} row(s)")
    return ("; " + "; ".join(parts)) if parts else ""


def _missing_operation_columns(conn: sqlite3.Connection) -> list[str]:
    present = {row[1] for row in conn.execute("PRAGMA table_info(concord_operations)")}
    return [name for name in _OPERATION_COLUMNS if name not in present]


def _operation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = conn.execute(
        f"SELECT {', '.join(_OPERATION_COLUMNS)} FROM concord_operations"
        " ORDER BY object_key, updated_seq DESC"
    )
    out: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        record = {key: row[key] for key in _OPERATION_COLUMNS}
        record["pruned"] = record["pruned_at"] is not None
        record["receipt"] = json.loads(record["receipt"]) if record["receipt"] else None
        out.append(record)
    return out


def read_snapshot(path: Path) -> Snapshot:
    """Read the store, or say precisely why there is nothing to read.

    Operations and attempts are read inside one read transaction, so a status or
    an export can never show two different instants (review P2-1).

    Three states that used to look alike are now distinct (review F7-3):

      * **no Concord schema at all** - a fresh or foreign database. Honestly
        empty; ``initialized`` false, no error, healthy.
      * **partial schema** - some of the schema is present while a required
        table (or a required column) is gone. The surviving rows prove records
        existed, so this is damage: an error, ``damage == "partial_schema"`` and
        a nonzero exit, never empty arrays presented as a fact.
      * **unreadable** - the file cannot be opened or read as SQLite at all.
        Unchanged from P2-1.
    """
    snap = Snapshot(store_path=str(path))
    if not Path(path).exists():
        return snap
    snap.exists = True
    try:
        conn = open_readonly(Path(path))
    except sqlite3.Error as exc:
        snap.error = f"{type(exc).__name__}: {exc}"
        snap.damage = DAMAGE_UNREADABLE
        return snap
    try:
        # Never fall back to separate autocommit reads if a snapshot cannot
        # start: they could report a combination of states that never existed.
        conn.execute("BEGIN")
        tables = _table_names(conn)
        if not any(name.startswith("concord") for name in tables):
            # No Concord table has ever been created here: an empty database.
            return snap

        if "concord_operations" not in tables:
            # The operations cannot be read at all. Attempts are still read
            # (below) so the report shows *what* survived, which is what makes
            # the damage legible instead of an empty answer.
            snap.damage = DAMAGE_PARTIAL_SCHEMA
            snap.error = (
                "partial Concord schema: required table concord_operations is missing"
                + _damage_context(conn, tables)
                + " - the recorded operations are gone, so this is not an empty store"
            )
            if "concord_attempts" in tables:
                try:
                    snap.attempts = _attempt_rows(conn)
                except sqlite3.Error as exc:
                    snap.attempts_error = f"{type(exc).__name__}: {exc}"
            return snap

        missing_columns = _missing_operation_columns(conn)
        if missing_columns:
            snap.initialized = True
            snap.damage = DAMAGE_PARTIAL_SCHEMA
            snap.error = (
                "partial Concord schema: concord_operations is missing column(s) "
                + ", ".join(missing_columns)
                + _damage_context(conn, tables)
                + " - the recorded operations cannot be read completely"
            )
            return snap

        if "concord_attempts" not in tables:
            # The operations are readable, so they are still reported; the
            # attempt history is not, and that is an unqualified failure.
            snap.initialized = True
            snap.damage = DAMAGE_PARTIAL_SCHEMA
            snap.attempts_error = (
                "partial Concord schema: required table concord_attempts is missing"
                + _damage_context(conn, tables)
            )
            snap.rows = _operation_rows(conn)
            return snap

        snap.initialized = True
        snap.rows = _operation_rows(conn)
        try:
            snap.attempts = _attempt_rows(conn)
        except sqlite3.Error as exc:
            # Half a schema is not a healthy store. The operations above are
            # still reported - swallowing this into an empty array is the defect.
            snap.attempts_error = f"{type(exc).__name__}: {exc}"
            snap.damage = DAMAGE_PARTIAL_SCHEMA
        # Keep readable survivor evidence above, but apply the writer's health
        # contract in this SAME read transaction. Missing guards or orphaned
        # attempts must not be reported as a healthy store by status/export.
        verdict = K._validate_snapshot(conn)
        if verdict.damaged:
            snap.damage = verdict.damage_reason
            snap.attempts_error = snap.attempts_error or verdict.detail
    except (sqlite3.Error, ValueError, TypeError) as exc:
        snap.error = f"{type(exc).__name__}: {exc}"
        snap.damage = DAMAGE_UNREADABLE
        snap.initialized = False
        snap.rows = []
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            pass
        conn.close()
    return snap


_ATTEMPT_SQL = (
    "SELECT attempt_id, operation_id, ordinal, state, detail, started_seq, settled_seq,"
    " started_at, settled_at FROM concord_attempts ORDER BY operation_id, ordinal"
)


def _attempt_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(_ATTEMPT_SQL).fetchall()]


def next_actions(row: dict[str, Any]) -> list[dict[str, str]]:
    """Safe next actions for one operation, derived from its recorded state."""
    operation_id = row["operation_id"]
    state = row["state"]
    if state == K.PREPARED:
        return [
            R.next_action_for(R.CANCEL, operation_id=operation_id),
            R.next_action_for(R.RECONCILE, operation_id=operation_id),
        ]
    if state == K.PENDING:
        return [
            R.next_action_for(
                R.RECONCILE,
                operation_id=operation_id,
                detail=(
                    "a dispatch may still be in flight; reconcile only once the process that "
                    "owned it is gone (see `hermes concord reconcile --help`)"
                ),
            )
        ]
    if state == K.UNKNOWN:
        return [
            R.next_action_for(
                R.RECONCILE,
                operation_id=operation_id,
                detail="the write may have applied; do not retry it",
            )
        ]
    return []


def export_model(snap: Snapshot, *, attempts: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """A complete, portable *audit* dump: identities, states, receipts, attempts.

    This is not a backup. It omits the material fingerprint and the rest of the
    persistence metadata, and no import exists - use ``concord backup`` for a
    restorable copy (review P2-2). The ``store`` block says whether the dump is
    complete: a machine consumer must read it before trusting the arrays.
    """
    return {
        "format": "concord-export/v1",
        "store_path": snap.store_path,
        "store": store_status(snap),
        "counts": snap.counts,
        "unresolved": snap.unresolved,
        "operations": snap.rows,
        "attempts": snap.attempts if attempts is None else attempts,
    }


def store_status(snap: Snapshot) -> dict[str, Any]:
    """Where the store is and whether it could be read, in one object."""
    return {
        "path": snap.store_path,
        "exists": snap.exists,
        "initialized": snap.initialized,
        "readable": snap.readable and snap.attempts_error == "",
        "healthy": snap.healthy,
        "damage": snap.damage or None,
        "error": snap.error or snap.attempts_error or None,
        "operations_error": snap.error or None,
        "attempts_error": snap.attempts_error or None,
    }


def read_attempts(path: Path) -> list[dict[str, Any]]:
    """Every attempt row, read-only.

    Raises :class:`StoreUnreadable` when the store cannot be read. It used to
    return an empty list, which made a corrupt store look like one with no
    attempts (review P2-1).
    """
    snap = read_snapshot(snap_path := Path(path))
    if snap.error:
        raise StoreUnreadable(snap.error, store_path=str(snap_path))
    if snap.attempts_error:
        raise StoreUnreadable(snap.attempts_error, store_path=str(snap_path))
    return snap.attempts


COVERAGE_LINES = (
    "Protected: calls that arrive through the concord_github_write tool, for a file that",
    "already exists, on one branch of a repository in the allowlist.",
    "NOT PROTECTED: every other tool, shell commands, browser and raw HTTP actions, direct",
    "`gh` use, a disabled plugin, profiles that have not enabled it, and other machines.",
)


def render_text(snap: Snapshot, *, status: dict[str, Any], limit: int = 20) -> str:
    """The human view. Plain text, stable order, no colour."""
    lines: list[str] = []
    allowed = status.get("allowed_repositories") or []
    lines.append("Concord for Hermes - coordination status")
    lines.append(f"store: {snap.store_path}")
    lines.append(f"allowlist: {', '.join(allowed) if allowed else '(empty - every call is refused)'}")
    lines.append("")

    if not snap.exists:
        lines.append("state: no coordinator store yet - no enrolled operation has ever been recorded.")
        lines.append("")
        lines.extend(COVERAGE_LINES)
        return "\n".join(lines)
    if snap.error:
        if snap.damage == DAMAGE_PARTIAL_SCHEMA:
            lines.append(f"state: STORE DAMAGED - PARTIAL SCHEMA - {snap.error}")
            lines.append(
                "records existed and part of the schema is gone; nothing here is confirmed"
                " or denied. Restore from a `hermes concord backup` file, or repair the file,"
                " then re-run."
            )
        else:
            lines.append(f"state: STORE UNREADABLE - {snap.error}")
            lines.append("nothing here is confirmed or denied; fix access to the file, then re-run.")
        lines.append("")
        lines.extend(COVERAGE_LINES)
        if snap.attempts:
            lines.append("")
            lines.append(
                f"surviving attempt rows: {len(snap.attempts)} - positive evidence that"
                " operations were recorded; the operation table that explains them is gone."
            )
        return "\n".join(lines)
    if snap.attempts_error:
        lines.append(f"store: DAMAGED OR INCOMPLETE - {snap.attempts_error}")
        lines.append("the surviving rows below are evidence, not a complete authority;")
        lines.append("stop writes and recover the store before acting on these records.")
        lines.append("")
    if not snap.initialized:
        lines.append("state: store exists but has no Concord schema - no operation has been recorded.")
        lines.append("")
        lines.extend(COVERAGE_LINES)
        return "\n".join(lines)

    counts = snap.counts
    lines.append(
        f"recorded: {len(snap.rows)} operation(s); unresolved: {snap.unresolved}"
        f"  [reserved {counts.get(K.PREPARED, 0)}, pending {counts.get(K.PENDING, 0)},"
        f" unknown {counts.get(K.UNKNOWN, 0)}, confirmed {counts.get(K.CONFIRMED, 0)},"
        f" failed {counts.get(K.FAILED_BEFORE_EXECUTION, 0)}, conflict {counts.get(K.CONFLICT, 0)},"
        f" cancelled {counts.get(K.CANCELLED, 0)}]"
    )
    lines.append("")

    groups = snap.objects()
    held = [g for g in groups if g["holder"]]
    if not held:
        lines.append(
            "active: no holder visible in surviving rows; this does not prove the object is free."
            if not snap.healthy else
            "active: none - no object is held by an unresolved operation."
        )
    else:
        lines.append("active objects (held - a new intent on the same object is refused):")
        for group in held:
            lines.append(f"  {group['object_key']}")
            for row in group["operations"]:
                if row["state"] not in K.BLOCKING_STATES:
                    continue
                lines.append(
                    f"    holder: {row['operation_id']}  state={row['state']}"
                    f"  attempts={row['attempt_count']}  base={row['base_revision']}"
                    f"  actor={row['actor']}"
                )
                if row["note"]:
                    lines.append(f"    note:   {row['note']}")
                for action in next_actions(row):
                    lines.append(f"    next:   {action['kind']} -> {action['how']}")
                    if action["why"]:
                        lines.append(f"            {action['why']}")
    lines.append("")

    refusals = snap.refusals(limit=limit)
    if refusals:
        lines.append("recorded refusals (these intents are closed; an object may still be held):")
        for row in refusals:
            lines.append(
                f"  {row['operation_id']}  state={row['state']}  object={row['object_key']}"
            )
            if row["note"]:
                lines.append(f"    note: {row['note']}")
            lines.append(
                "    next: create-new-reviewed-operation -> read the file again, then call"
                " concord_github_write with a new operation_id"
            )
        lines.append("")

    settled = [r for r in snap.rows if r["state"] == K.CONFIRMED][:limit]
    if settled:
        lines.append("recent confirmed operations:")
        for row in settled:
            receipt = row["receipt"] or {}
            sha = None if row["pruned"] else receipt.get("commit_sha")
            lines.append(
                f"  {row['operation_id']}  object={row['object_key']}"
                f"  commit={sha or '(none recorded)'}"
            )
        lines.append("")

    lines.extend(COVERAGE_LINES)
    return "\n".join(lines)
