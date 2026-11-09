"""Concord C1 probe core — object-scoped coordinator plus a synthetic local provider.

Everything here is synthetic test scaffolding for the C1 integration proof:
no network, no real provider, no credentials, no customer data. The shapes
mirror Concord's design intent (object identity vs operation identity vs
attempt identity) closely enough to answer the C1 go/no-go question, and are
explicitly NOT the C2 kernel.

Two stores, two SQLite files:

  * ``operations`` — the coordinator. Decides admission per canonical object.
  * ``objects``    — the stand-in provider. Holds a value and a revision and
    only accepts a write whose ``expected_revision`` matches (the provider-side
    conditional write Concord must credit, not reinvent).

Cross-process safety is delegated to SQLite: ``BEGIN IMMEDIATE`` serializes
writers and a partial UNIQUE index forbids two active operations on one
object, so there is no check-then-insert gap between two OS processes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ---- admission outcomes -------------------------------------------------
ADMITTED = "admitted"
CONFLICT = "conflict"
REPLAY_CONFIRMED = "replay_confirmed"
REJECTED_ARGUMENT_MISMATCH = "rejected_argument_mismatch"

# ---- operation states (the C1 subset of Concord's state set) ------------
RESERVED = "reserved"          # recorded, no claim yet that a request was sent
PENDING = "pending"            # dispatch started; it may have reached the provider
CONFIRMED = "confirmed"
FAILED_BEFORE = "failed_before_execution"
UNKNOWN = "unknown"

ACTIVE_STATES = (RESERVED, PENDING)
# States that continue to hold the object. UNKNOWN belongs here on purpose:
# an ambiguous remote outcome is not safe to repeat, so time alone must never
# release the object.
BLOCKING_STATES = (RESERVED, PENDING, UNKNOWN)


class CoordinatorUnavailable(RuntimeError):
    """The coordinator could not serve a decision.

    Callers MUST refuse on this. It is never an invitation to proceed.
    """


class ProviderUnavailable(RuntimeError):
    """The synthetic provider store could not be read or written."""


class StaleRevision(RuntimeError):
    """The provider rejected the write because the revision moved."""


@dataclass
class Admission:
    kind: str
    object_key: str = ""
    operation_id: str = ""
    reason: str = ""
    receipt: Optional[dict[str, Any]] = None
    holder: Optional[str] = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_key": self.object_key,
            "operation_id": self.operation_id,
            "reason": self.reason,
            "receipt": self.receipt,
            "holder": self.holder,
            "at": self.at,
        }


def fingerprint(*, object_key: str, operation_id: str, value: Any, expected_revision: int) -> str:
    """Immutable material-argument fingerprint.

    Reusing an operation id with different material content must fail rather
    than silently becoming a second, different external action.
    """
    material = json.dumps(
        {
            "object_key": object_key,
            "operation_id": operation_id,
            "value": value,
            "expected_revision": expected_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _connect(path: str, busy_ms: int) -> sqlite3.Connection:
    # isolation_level=None -> we own BEGIN/COMMIT explicitly, which is what
    # makes BEGIN IMMEDIATE an actual write-lock acquisition point.
    conn = sqlite3.connect(path, timeout=busy_ms / 1000.0, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    object_key   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    actor        TEXT NOT NULL,
    state        TEXT NOT NULL,
    receipt      TEXT,
    note         TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concord_active_object
    ON operations(object_key) WHERE state IN ('reserved', 'pending', 'unknown');
"""


class Coordinator:
    """Durable object-scoped admission control over a local SQLite file."""

    def __init__(self, path: str, *, busy_ms: int = 5000) -> None:
        self.path = path
        self.busy_ms = busy_ms
        try:
            conn = _connect(path, busy_ms)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        except sqlite3.Error as exc:  # corrupt file, locked file, bad perms, full disk
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # -- internals ------------------------------------------------------
    def _txn(self, fn):
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    # -- admission ------------------------------------------------------
    def admit(self, *, object_key: str, operation_id: str, fingerprint: str, actor: str) -> Admission:
        """Atomically reserve *object_key* for *operation_id*.

        One transaction: replay lookup, active-holder check, insert. The partial
        unique index is the real guard — a second active operation on the same
        object fails at INSERT, so two processes cannot both pass a check.
        """
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Admission:
            row = conn.execute(
                "SELECT fingerprint, state, receipt FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                stored_fp, state, receipt = row
                if stored_fp != fingerprint:
                    return Admission(
                        REJECTED_ARGUMENT_MISMATCH,
                        object_key=object_key,
                        operation_id=operation_id,
                        reason=f"operation id {operation_id} already recorded with different material arguments",
                    )
                if state == CONFIRMED:
                    return Admission(
                        REPLAY_CONFIRMED,
                        object_key=object_key,
                        operation_id=operation_id,
                        reason="identical operation already confirmed; returning original receipt",
                        receipt=json.loads(receipt) if receipt else None,
                    )
                return Admission(
                    CONFLICT,
                    object_key=object_key,
                    operation_id=operation_id,
                    reason=f"operation {operation_id} is already {state}; outcome is not confirmed",
                )

            holder = conn.execute(
                "SELECT operation_id, actor, state FROM operations "
                "WHERE object_key = ? AND state IN ('reserved', 'pending', 'unknown')",
                (object_key,),
            ).fetchone()
            if holder is not None:
                return Admission(
                    CONFLICT,
                    object_key=object_key,
                    operation_id=operation_id,
                    reason=f"object {object_key} is held by an unresolved operation {holder[0]} ({holder[2]})",
                    holder=holder[0],
                )

            try:
                conn.execute(
                    "INSERT INTO operations (operation_id, object_key, fingerprint, actor, state,"
                    " receipt, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (operation_id, object_key, fingerprint, actor, RESERVED, now, now),
                )
            except sqlite3.IntegrityError:
                # A competing process inserted between our SELECT and INSERT and
                # committed first. The index decided it; report the conflict.
                return Admission(
                    CONFLICT,
                    object_key=object_key,
                    operation_id=operation_id,
                    reason=f"object {object_key} was reserved concurrently by another operation",
                )
            return Admission(
                ADMITTED, object_key=object_key, operation_id=operation_id, reason="reserved"
            )

        return self._txn(_do)

    # -- transitions ----------------------------------------------------
    def _transition(self, operation_id: str, state: str, *, receipt: Optional[dict] = None,
                    note: str = "") -> None:
        now = time.time()

        def _do(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "UPDATE operations SET state = ?, receipt = COALESCE(?, receipt), note = ?,"
                " updated_at = ? WHERE operation_id = ?",
                (state, json.dumps(receipt) if receipt is not None else None, note, now, operation_id),
            )
            if cur.rowcount == 0:
                raise CoordinatorUnavailable(f"operation {operation_id} not found")

        self._txn(_do)

    def begin_dispatch(self, operation_id: str) -> None:
        self._transition(operation_id, PENDING, note="dispatch started")

    def confirm(self, operation_id: str, receipt: dict) -> None:
        self._transition(operation_id, CONFIRMED, receipt=receipt, note="provider evidence matched")

    def fail_before_execution(self, operation_id: str, reason: str) -> None:
        self._transition(operation_id, FAILED_BEFORE, note=reason)

    def mark_unknown(self, operation_id: str, reason: str) -> None:
        # Deliberately does NOT release the object: an unknown outcome is not
        # safe to repeat, and lease/wall-clock expiry must not clear it.
        self._transition(operation_id, UNKNOWN, note=reason[:500])

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            row = conn.execute(
                "SELECT operation_id, object_key, fingerprint, actor, state, receipt, note,"
                " created_at, updated_at FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        if row is None:
            return None
        keys = ("operation_id", "object_key", "fingerprint", "actor", "state", "receipt",
                "note", "created_at", "updated_at")
        out = dict(zip(keys, row))
        out["receipt"] = json.loads(out["receipt"]) if out["receipt"] else None
        return out

    def holder_of(self, object_key: str) -> Optional[str]:
        """The unresolved operation id holding *object_key*, if any."""
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            row = conn.execute(
                "SELECT operation_id FROM operations WHERE object_key = ?"
                " AND state IN ('reserved', 'pending', 'unknown')",
                (object_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        return row[0] if row else None


_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""


class LocalProvider:
    """Synthetic provider: revision-conditional writes, no network."""

    def __init__(self, path: str, *, busy_ms: int = 5000) -> None:
        self.path = path
        self.busy_ms = busy_ms
        try:
            conn = _connect(path, busy_ms)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_STORE_SCHEMA)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def seed(self, key: str, value: Any) -> None:
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO objects (key, value, revision, updated_at) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, revision = objects.revision + 1",
                (key, json.dumps(value), time.time()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def read(self, key: str) -> dict[str, Any]:
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            row = conn.execute("SELECT value, revision FROM objects WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        if row is None:
            return {"key": key, "value": None, "revision": 0}
        return {"key": key, "value": json.loads(row[0]), "revision": row[1]}

    def update(self, key: str, *, expected_revision: int, value: Any) -> dict[str, Any]:
        """Conditional write. Rejects a stale ``expected_revision`` like the real
        provider does (GitHub Contents API needs the previous blob SHA for the
        same reason); Concord credits this rather than claiming it."""
        try:
            conn = _connect(self.path, self.busy_ms)
        except sqlite3.Error as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM objects WHERE key = ?", (key,)).fetchone()
            current = row[0] if row else 0
            if current != expected_revision:
                conn.execute("ROLLBACK")
                raise StaleRevision(f"expected revision {expected_revision}, provider is at {current}")
            new_rev = current + 1
            conn.execute(
                "INSERT INTO objects (key, value, revision, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, revision = excluded.revision",
                (key, json.dumps(value), new_rev, time.time()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        return {
            "key": key,
            "revision": new_rev,
            "previous_revision": current,
            "receipt_id": hashlib.sha256(f"{key}:{new_rev}:{time.time_ns()}".encode()).hexdigest()[:16],
        }
