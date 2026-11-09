"""Concord C2 — the deterministic coordination kernel.

Scope of this module: coordination only. No provider HTTP client, no adapter,
no CLI, no Desktop surface, no plugin registration. It is a library that a
tool body (C3) will call *inside its own dispatch frame*, following the C1
verdict: correctness may not depend on a framework seam we cannot enforce.

Three identities, deliberately distinct:

  * **object** — the canonical thing being changed (``object_key``). Admission
    is scoped to it; two unresolved intents on one object cannot coexist.
  * **operation** — one intent, durable across processes, restarts and clock
    changes (``operation_id``). Replaying it returns evidence, never a second
    external action.
  * **attempt** — one dispatch of one operation (``attempt_id``). Attempts are
    what actually touch the provider; a retry after a provably-not-executed
    outcome is a new attempt of the same operation.

Storage is SQLite in WAL mode. ``BEGIN IMMEDIATE`` serialises writers and a
partial UNIQUE index is the real admission guard, so there is no
check-then-insert gap between two OS processes.

The kernel's material schema is one action family — a revision-conditional
write of a value to an object — because that is the action family the C1
evidence supports and C3 will adapt. Generalising to arbitrary action families
is not claimed here.

Nothing in this module reads the wall clock for a correctness decision.
Timestamps are diagnostics; ordering is a durable counter (``created_seq`` /
``updated_seq``), so a backwards or jumping clock changes no behaviour.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

# ---------------------------------------------------------------- states
# One state, one spelling: PREPARED is the constant and "reserved" is its
# durable value, the name the README's outcome table and the CLI use.
PREPARED = "reserved"
PENDING = "pending"
CONFIRMED = "confirmed"
FAILED_BEFORE_EXECUTION = "failed_before_execution"
CONFLICT = "conflict"
UNKNOWN = "unknown"
CANCELLED = "cancelled"

ALL_STATES = (
    PREPARED,
    PENDING,
    CONFIRMED,
    FAILED_BEFORE_EXECUTION,
    CONFLICT,
    UNKNOWN,
    CANCELLED,
)

# States that keep the object held. UNKNOWN belongs here on purpose: an
# ambiguous remote outcome is not safe to repeat, so neither time nor a restart
# may release it. Only reconciliation against provider evidence settles it.
BLOCKING_STATES = (PREPARED, PENDING, UNKNOWN)

TERMINAL_STATES = (CONFIRMED, FAILED_BEFORE_EXECUTION, CONFLICT, CANCELLED)

# Terminal states that carry a proof nothing was sent: safe to re-attempt under
# the same operation id. CONFIRMED replays; CANCELLED is closed (the intent was
# withdrawn, so reusing the id would be ambiguous).
RETRYABLE_STATES = (FAILED_BEFORE_EXECUTION, CONFLICT)

# ------------------------------------------------------------ admission kinds
ADMITTED = "admitted"
REPLAY_CONFIRMED = "replay_confirmed"
REPLAY_PRUNED = "replay_pruned"
REJECTED_ARGUMENT_MISMATCH = "rejected_argument_mismatch"
#: The operation id is recorded for a DIFFERENT provider object (path, branch or
#: repository changed). Kept separate from ``REJECTED_ARGUMENT_MISMATCH`` because
#: the cause is the object-identity gate, not a digest comparison (owner F9-1).
REJECTED_OBJECT_MISMATCH = "rejected_object_mismatch"


# ------------------------------------------------------------------ errors
class CoordinatorUnavailable(RuntimeError):
    """The coordinator could not serve a decision.

    Callers MUST refuse on this. It is never an invitation to proceed. ``reason``
    classifies the storage fault so a refusal can name it instead of saying
    "something went wrong".
    """

    def __init__(self, detail: str, *, reason: str = "unavailable") -> None:
        super().__init__(detail)
        self.reason = reason


class CapacityExceeded(CoordinatorUnavailable):
    """A configured storage bound would be exceeded.

    Refused, never silently dropped: bounded storage must not turn into
    discarded evidence.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail, reason="capacity")


class IllegalTransition(ValueError):
    """An operation cannot move to the requested state from where it is."""


class Rejected(RuntimeError):
    """Provider refused the write and nothing changed (e.g. a stale revision).

    Raised by the ``mutate`` callable. The kernel maps it to
    FAILED_BEFORE_EXECUTION — a proof, not a guess: the provider rejected the
    conditional write, so no mutation was applied.
    """


def refusal(exc: BaseException) -> dict[str, Any]:
    """The refusal shape a tool body returns. Never a bypass."""
    reason = getattr(exc, "reason", "unavailable")
    return {"status": "refused", "reason": reason, "detail": str(exc)}


# -------------------------------------------------------------- identity
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _identity_text(value: Any) -> Optional[str]:
    """Store an opaque provider identity as text (``None`` stays ``None``)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_revision(value: Any, *, model: str = "integer") -> Any:
    """Read a base revision under a named revision model.

    ``integer`` (the default, and C2's contract): the value must be a
    non-negative integer. ``int()`` semantics are kept exactly, so a decimal
    string is the same revision as the number.

    ``opaque``: a provider whose revision is an opaque token (GitHub's blob
    SHA). A non-empty string is accepted as-is; an integer is accepted and kept
    as an integer. The kernel does not interpret the token — it stores it and
    hands it to an evidence evaluator — so this is the only rule the kernel can
    honestly impose.

    The model is named rather than guessed because "the kernel never compared
    revisions arithmetically" would be false: C2's default still does, and a
    caller that wants the opaque model has to say so.
    """
    if model not in ("integer", "opaque"):
        raise ValueError(f"unknown revision model {model!r}")
    if isinstance(value, bool):
        raise ValueError("a boolean is not a revision")
    if model == "integer":
        base = int(value)
        if base < 0:
            raise ValueError("'expected_revision' must be >= 0")
        return base
    if isinstance(value, int):
        if value < 0:
            raise ValueError("'expected_revision' must be >= 0")
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("'expected_revision' must be a non-empty token")
        return text
    raise ValueError(f"unsupported revision type {type(value).__name__}")


def material_from_arguments(args: Mapping[str, Any], *, revision_model: str = "integer") -> dict[str, Any]:
    """The material fields of a call, taken from the arguments the tool received.

    Q2 of the C1 verdict: ``tool_request`` middleware can rewrite arguments
    before the tool body runs, so the fingerprint must be derived here, from
    what the body actually got — never from the model's original call.

    ``revision_model`` selects how ``expected_revision`` is read; see
    :func:`normalize_revision`. The default is C2's integer contract.
    """
    if not isinstance(args, Mapping):
        raise TypeError("arguments must be a mapping")
    object_key = args.get("object")
    if not isinstance(object_key, str) or not object_key.strip():
        raise ValueError("'object' is required and must be a non-empty string")
    if "value" not in args:
        raise ValueError("'value' is required")
    try:
        base_revision = normalize_revision(args.get("expected_revision", 0), model=revision_model)
    except (TypeError, ValueError) as exc:
        raise ValueError("'expected_revision' must be an integer or an opaque revision token") from exc
    return {"object": object_key.strip(), "value": args["value"], "expected_revision": base_revision}




def fingerprint(material: Mapping[str, Any]) -> str:
    """Immutable digest of the material arguments.

    Reusing an operation id with different material must fail rather than
    silently become a second, different external action.
    """
    return hashlib.sha256(canonical_json(dict(material)).encode("utf-8")).hexdigest()


def fingerprint_matches(
    material: Mapping[str, Any],
    stored: str,
    *,
    fingerprint_object: Any = None,
) -> bool:
    """Is *material* the same intent as a record whose digest is *stored*?

    Two spellings of one provider object hash differently, because the object
    key is part of the digest. A record written before the identity fix stored
    its digest over the spelling the caller used then; the same call today
    canonicalises the key first. Comparing only the new digest would turn an
    honest replay into ``rejected_argument_mismatch`` and make reconciliation
    unable to attribute the operation's own value (owner F8-C).

    ``fingerprint_object`` is the spelling the record's digest was derived from
    (see ``Coordinator._record_fingerprint_identity``). When it differs from the
    material's object, the digest is recomputed under it: same value, same base
    revision, same operation intent, older spelling. Nothing is fuzzed - a
    different value or base still mismatches.

    PRECONDITION (owner F9-1): the caller has already established that the
    requested object IS this record's object - exact, or canonically equivalent
    through :func:`object_key_matches`. The relaxation below deliberately
    ignores the difference between the two spellings, so it can say nothing about
    two different objects; a caller that checks the digest first has already
    accepted another object's identity.
    """
    if fingerprint(material) == stored:
        return True
    original = _identity_text(fingerprint_object)
    current = _identity_text(material.get("object"))
    if original is None or original == current:
        return False
    relaxed = dict(material)
    relaxed["object"] = original
    return fingerprint(relaxed) == stored


def object_key_matches(
    stored: Any,
    requested: Any,
    normalize: Optional[Callable[[str], str]] = None,
) -> bool:
    """Is *requested* the SAME provider object as *stored*? (owner F9-1)

    The object-identity gate. It is consulted before any fingerprint comparison,
    because a digest says what a call *meant*, never what it *addressed*: the
    F8-C relaxation re-checks a stored digest under the spelling it was derived
    from, so two different objects carrying the same value and base revision can
    hash to the same stored digest. Fingerprint compatibility must therefore only
    ever decide between two calls that already address one object.

    Two ways to be the same object, in order:

      * the strings are equal; or
      * both canonicalise, through the provider canonicaliser the coordinator was
        configured with, to the same key - the CONCORD-006 contract: owner and
        repository case folded, ``refs/heads/`` stripped from the branch, path and
        branch case preserved (they are case-sensitive in git).

    Without a canonicaliser only exact equality passes: the kernel stays
    provider-agnostic, and "the caller said it is equivalent" is not verification.
    """
    if not isinstance(stored, str) or not isinstance(requested, str):
        return False
    if stored == requested:
        return True
    if normalize is None:
        return False
    try:
        left, right = normalize(stored), normalize(requested)
    except Exception:  # noqa: BLE001 - an odd/foreign key is only itself
        return False
    return bool(left) and bool(right) and isinstance(left, str) and left == right


def fingerprint_from_arguments(args: Mapping[str, Any]) -> str:
    return fingerprint(material_from_arguments(args))


# ------------------------------------------------- evidence evaluation
@dataclass(frozen=True)
class EvidenceVerdict:
    """What a provider read proves about an unresolved operation.

    ``state`` is one of CONFIRMED, FAILED_BEFORE_EXECUTION, UNKNOWN — the three
    settlements reconcile is allowed to reach. ``revision`` is what goes into
    the receipt (opaque tokens allowed); ``None`` means "use the evidence's own
    revision field".
    """

    state: str
    reason: str
    revision: Any = None


def integer_revision_evaluator(
    *,
    evidence: Mapping[str, Any],
    object_key: str,
    stored_fingerprint: str,
    base_revision: int,
    operation_id: str,
    base_identity: Any = None,
    fingerprint_object_key: Any = None,
) -> EvidenceVerdict:
    """C2's predicate: a monotonic integer revision.

    Unchanged here on purpose — this is the behaviour the C2 tests were written
    against, kept as the default so an adapter that does not supply an evaluator
    behaves exactly as it did before C3.

    ``base_identity`` is accepted and ignored: an integer revision *is*
    monotonic, so "still at the base" is a proof here. A content-derived token
    is not (see ``concord_gh_adapter.opaque_evidence_verdict``), which is why the
    two models do not share this inference.

    ``fingerprint_object_key`` is the spelling the stored digest was computed
    over, when it differs from the canonical lock key: the content fingerprint is
    checked under it so a legacy record stays checkable (owner F8-C).
    """
    material_object = _identity_text(fingerprint_object_key) or object_key
    revision = int(evidence["revision"])
    if revision == base_revision + 1 and fingerprint(
        {"object": material_object, "value": evidence.get("value"), "expected_revision": base_revision}
    ) == stored_fingerprint:
        return EvidenceVerdict(CONFIRMED, "write is ours", revision)
    if revision == base_revision:
        return EvidenceVerdict(FAILED_BEFORE_EXECUTION, "provider still at the base revision", revision)
    return EvidenceVerdict(
        UNKNOWN,
        f"provider at revision {revision}; change is not attributable to this operation",
        revision,
    )



@dataclass
class Admission:
    kind: str
    object_key: str = ""
    operation_id: str = ""
    reason: str = ""
    receipt: Optional[dict[str, Any]] = None
    holder: Optional[str] = None
    state: str = ""
    seq: int = 0
    pruned: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_key": self.object_key,
            "operation_id": self.operation_id,
            "reason": self.reason,
            "receipt": self.receipt,
            "holder": self.holder,
            "state": self.state,
            "seq": self.seq,
            "pruned": self.pruned,
        }


# ---------------------------------------------------------------- storage
def _connect(path: str, busy_ms: int, max_pages: Optional[int] = None) -> sqlite3.Connection:
    # isolation_level=None -> BEGIN/COMMIT are explicit, which is what makes
    # BEGIN IMMEDIATE an actual write-lock acquisition point.
    conn = sqlite3.connect(path, timeout=busy_ms / 1000.0, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    if max_pages is not None:
        # A hard size cap in pages. Exceeding it raises SQLITE_FULL, the same
        # engine error a full volume raises, so the refusal path is the real one
        # — but the trigger is the cap, not a full filesystem.
        conn.execute(f"PRAGMA max_page_count = {int(max_pages)}")
    return conn


def _classify(exc: BaseException) -> str:
    """Name the storage fault. Classification is diagnostic; the behaviour is
    the same refusal for every class."""
    message = str(exc).lower()
    if "not a database" in message or "malformed" in message or "encrypted" in message:
        return "storage_corrupt"
    if "disk is full" in message or "database or disk is full" in message:
        return "storage_full"
    if "readonly" in message or "read-only" in message:
        return "storage_permissions"
    if "locked" in message or "busy" in message:
        return "storage_locked"
    if "unable to open" in message or "no such file" in message:
        return "storage_unavailable"
    return "storage_error"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS concord_counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS concord_operations (
    operation_id  TEXT PRIMARY KEY,
    object_key    TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    fingerprint_object TEXT,
    base_revision INTEGER NOT NULL,
    base_identity TEXT,
    actor         TEXT NOT NULL,
    state         TEXT NOT NULL,
    holder        TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    receipt       TEXT,
    note          TEXT,
    note_chars    INTEGER,
    pruned_at     REAL,
    created_seq   INTEGER NOT NULL,
    updated_seq   INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concord_active_object
    ON concord_operations(object_key)
    WHERE state IN ('reserved', 'pending', 'unknown');
CREATE INDEX IF NOT EXISTS ix_concord_object_seq
    ON concord_operations(object_key, created_seq);
CREATE INDEX IF NOT EXISTS ix_concord_state
    ON concord_operations(state);
CREATE TABLE IF NOT EXISTS concord_attempts (
    attempt_id   TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    state        TEXT NOT NULL,
    detail       TEXT,
    started_seq  INTEGER NOT NULL,
    settled_seq  INTEGER,
    started_at   REAL NOT NULL,
    settled_at   REAL,
    UNIQUE(operation_id, ordinal)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concord_open_attempt
    ON concord_attempts(operation_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ix_concord_attempt_op
    ON concord_attempts(operation_id, ordinal);
"""

_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in _SCHEMA.split(";") if statement.strip()
)


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the whole store schema inside the CALLER's transaction.

    Per-statement autocommit would let a second process open the file between
    ``concord_operations`` and ``concord_attempts``, see a partial schema and
    refuse itself as damaged - a creation race, not damage. The caller
    (``Coordinator._init``) therefore opens ONE ``BEGIN IMMEDIATE`` around the
    damage verdict, this creation and the additive migrations, so a concurrent
    opener sees either nothing - and then runs these ``IF NOT EXISTS`` statements
    itself, waiting on the write lock - or the complete schema (owner F10: the
    verdict and the mutation share one transaction).
    """
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


_TERMINAL_SQL = "('confirmed','failed_before_execution','conflict','cancelled')"
_BLOCKING_SQL = "('reserved','pending','unknown')"
_MAX_DETAIL_CHARS = 500

# ----------------------------------------------------- validate before write
#: Tables an existing store must have before ANY write path may touch it. A
#: missing one is damage, not emptiness (owner F8-A, residual R1): a write that
#: ran ``CREATE TABLE IF NOT EXISTS`` over such a file would recreate it empty
#: and destroy the only evidence that records existed.
REQUIRED_STORE_TABLES = ("concord_operations", "concord_attempts", "concord_counters")

#: Columns a write must be able to read and write on ``concord_operations``.
#: ``base_identity`` and ``fingerprint_object`` are deliberately absent: both are
#: additive columns this module adds to an older store on open, so a store that
#: lacks only them is a legitimate older store, not a damaged one.
REQUIRED_OPERATION_COLUMNS = (
    "operation_id", "object_key", "fingerprint", "base_revision", "actor", "state",
    "holder", "attempt_count", "receipt", "note", "note_chars", "pruned_at",
    "created_seq", "updated_seq", "created_at", "updated_at",
)

#: The partial unique indexes are the admission guard (one unresolved row per
#: object, one open attempt per operation). Their absence changes what a write
#: may assume about concurrent callers, so it is refused like a missing table
#: rather than silently recreated over whatever the rows happen to be.
REQUIRED_UNIQUE_INDEXES = ("ux_concord_active_object", "ux_concord_open_attempt")

#: Damage classes a write-path refusal names. ``partial_schema`` is the spelling
#: the read path already uses (review F7-3); ``damaged_history`` covers a store
#: whose tables are all present while the rows they explain are not.
DAMAGE_PARTIAL_SCHEMA = "partial_schema"
DAMAGE_HISTORY = "damaged_history"

_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS concord_migrations (
    name        TEXT PRIMARY KEY,
    detail      TEXT,
    applied_seq INTEGER,
    applied_at  REAL
);
"""

#: Executed one statement at a time, never through ``executescript``: that helper
#: commits any open transaction first, which would break the single
#: ``BEGIN IMMEDIATE`` span that holds the damage verdict, schema creation and
#: migration together (owner F10).
_MIGRATIONS_STATEMENTS = tuple(
    statement.strip() for statement in _MIGRATIONS_DDL.split(";") if statement.strip()
)


def _open_readonly(path: str) -> sqlite3.Connection:
    """A connection that cannot write, for validating a file before writing it."""
    from urllib.parse import quote

    uri = "file:" + quote(str(path), safe="/:\\") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _survivor_context(conn: sqlite3.Connection, tables: set[str]) -> str:
    """What survived, named in the refusal - the size of the damage, not a guess."""
    parts = []
    for table in ("concord_operations", "concord_attempts"):
        if table in tables:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                parts.append(f"{table} still holds {int(count)} row(s)")
            except sqlite3.Error:  # noqa: PERF203 - a table that cannot be counted
                continue
    return ("; " + "; ".join(parts)) if parts else ""


@dataclass(frozen=True)
class StoreVerdict:
    """What a write path may do with an existing store file.

    ``creatable`` means the store may be created here: the file does not exist,
    is empty, or holds no Concord schema at all (a foreign or brand-new database
    file), so no history of ours can be destroyed. ``damage_reason``/``detail``
    say why a store that DOES hold Concord history may not be touched.
    """

    creatable: bool
    damage_reason: str = ""
    detail: str = ""

    @property
    def damaged(self) -> bool:
        return bool(self.damage_reason)


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Roll back when a transaction is open; a connection with none is fine."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def validate_existing_store(path: str, *, attempts: int = 3) -> StoreVerdict:
    """Validate a store that ALREADY EXISTS, before any schema work or migration.

    Returns a :class:`StoreVerdict`. Raises :class:`CoordinatorUnavailable` when
    the file cannot be read at all.

    The cases this separates, in the order they are decided:

      * **no file, or an empty file** - no history exists, so creating the schema
        destroys nothing. ``creatable``: legitimate fresh creation.
      * **no Concord table at all** - a foreign or brand-new database file (which
        is also what a second process sees while the first one is still creating
        the schema). ``creatable``.
      * **anything else** - every required table, column and admission-guard
        index must be present, every attempt row must belong to a recorded
        operation, and the sequence counter must cover the recorded rows.
        Otherwise the file is damaged, and the caller must refuse both mutation
        and admission instead of rebuilding it.

    An empty *file* is treated as fresh on purpose: it holds no rows, so there is
    no evidence to destroy, and the read path already reports it as healthy
    (review F7-3).

    **One transaction, one snapshot (owner F9-2).** Every read behind one verdict
    - schema, columns, indexes, the orphan scan, the counter and the sequence
    maxima - runs inside ONE read transaction (``BEGIN`` .. ``COMMIT``). With
    autocommit each statement was its own transaction, so a writer that committed
    between two of them was visible to the later checks and invisible to the
    earlier ones: the verdict was a mixture of two instants, and could refuse a
    healthy store (an old counter against new rows) or describe a store that
    never existed. Now a concurrent commit during the reading is simply not in
    the snapshot the verdict describes. Lock contention is the only case that
    needs a decision, and it is decided the safe way: the snapshot is retried a
    bounded number of times and then REFUSED - never a torn view.

    This is the WRITE-path twin of :func:`concord_status.read_snapshot`, which
    answers the same question for readers. The two keep separate lists on
    purpose: a reader that cannot see ``concord_attempts`` can still report the
    operations, while a writer that cannot see it must not touch the file.

    **This is not the authoritative gate for a mutation (owner F10).** A verdict
    describes an instant, and the write it guards happens later; an outside
    process that damages the file between the two would be invisible.
    Initialization and every mutation therefore re-take this verdict INSIDE
    their write transaction (:func:`_damage_gate`): ``_init`` before it creates
    or migrates, and ``_txn`` before it calls any mutation body. This
    function stays as the cheap read-only pre-check - it refuses a damaged file
    without opening it for write, so a refused open changes no bytes - and as the
    standalone question a reviewer or a diagnostic asks about a file.
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return StoreVerdict(creatable=True)
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error) as exc:
        raise CoordinatorUnavailable(
            f"{type(exc).__name__}: {exc}", reason=_classify(exc)
        ) from exc
    budget = max(1, int(attempts))
    try:
        for attempt in range(budget):
            try:
                # The snapshot for the whole verdict. A deferred BEGIN takes it
                # at the first read below and holds it until COMMIT (in WAL a
                # writer may still commit; this reader keeps its own view).
                conn.execute("BEGIN")
            except sqlite3.OperationalError:
                _rollback_quietly(conn)
                if attempt + 1 >= budget:
                    raise
                continue
            try:
                verdict = _validate_snapshot(conn)
            except BaseException:
                _rollback_quietly(conn)
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                _rollback_quietly(conn)
                if attempt + 1 >= budget:
                    raise
                continue
            return verdict
        raise CoordinatorUnavailable(
            f"could not read one consistent snapshot of {path} in {budget} attempt(s);"
            " refusing to validate a torn view",
            reason="storage_locked",
        )
    except sqlite3.Error as exc:
        raise CoordinatorUnavailable(
            f"{type(exc).__name__}: {exc}", reason=_classify(exc)
        ) from exc
    finally:
        conn.close()


def _validate_snapshot(conn: sqlite3.Connection) -> StoreVerdict:
    """Every check behind one verdict, read through ONE open read transaction.

    Split out of :func:`validate_existing_store` so the caller owns the
    transaction boundary: the whole function is one snapshot, never a sequence of
    autocommit reads (owner F9-2).
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not any(name.startswith("concord") for name in tables):
        return StoreVerdict(creatable=True)
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    missing_tables = [name for name in REQUIRED_STORE_TABLES if name not in tables]
    if missing_tables:
        return StoreVerdict(
            creatable=False,
            damage_reason=DAMAGE_PARTIAL_SCHEMA,
            detail=(
                "partial Concord schema: "
                + ", ".join(f"required table {name} is missing" for name in missing_tables)
                + _survivor_context(conn, tables)
                + " - this is damage, not an empty store; nothing will be rebuilt"
                " or migrated. Restore from `hermes concord backup`, or repair the file."
            ),
        )
    present = {row[1] for row in conn.execute("PRAGMA table_info(concord_operations)")}
    missing_columns = [name for name in REQUIRED_OPERATION_COLUMNS if name not in present]
    if missing_columns:
        return StoreVerdict(
            creatable=False,
            damage_reason=DAMAGE_PARTIAL_SCHEMA,
            detail=(
                "partial Concord schema: concord_operations is missing column(s) "
                + ", ".join(missing_columns)
                + _survivor_context(conn, tables)
                + " - the recorded operations cannot be read completely; nothing will"
                " be rebuilt or migrated"
            ),
        )
    missing_indexes = [name for name in REQUIRED_UNIQUE_INDEXES if name not in indexes]
    if missing_indexes:
        return StoreVerdict(
            creatable=False,
            damage_reason=DAMAGE_PARTIAL_SCHEMA,
            detail=(
                "partial Concord schema: admission-guard index(es) "
                + ", ".join(missing_indexes)
                + " are missing - the uniqueness guard is part of the store, not an"
                " optimisation, and it is not recreated over unknown rows"
            ),
        )
    orphan_sql = (
        "SELECT a.operation_id FROM concord_attempts a"
        " WHERE NOT EXISTS (SELECT 1 FROM concord_operations o"
        "                   WHERE o.operation_id = a.operation_id)"
    )
    orphans = conn.execute(f"{orphan_sql} ORDER BY a.operation_id LIMIT 5").fetchall()
    if orphans:
        count = int(conn.execute(f"SELECT COUNT(*) FROM ({orphan_sql})").fetchone()[0])
        return StoreVerdict(
            creatable=False,
            damage_reason=DAMAGE_HISTORY,
            detail=(
                f"damaged Concord history: {count} attempt row(s) name an operation that"
                " has no record ("
                + ", ".join(str(row[0]) for row in orphans)
                + "; a dispatched outcome may survive while the operation that explains"
                " it is gone) - this is not an empty store, and nothing will be rebuilt"
            ),
        )
    # Persisted state controls both the partial lock index and retry eligibility.
    # Unknown spellings are damage, never a terminal outcome or a free object.
    operation_states = ",".join("'" + state + "'" for state in ALL_STATES)
    attempt_states = ",".join("'" + state + "'" for state in
                              (PENDING, CONFIRMED, FAILED_BEFORE_EXECUTION, UNKNOWN))
    invalid_state = conn.execute(
        f"SELECT 1 FROM concord_operations WHERE state NOT IN ({operation_states})"
        " OR state IS NULL UNION ALL "
        f"SELECT 1 FROM concord_attempts WHERE state NOT IN ({attempt_states})"
        " OR state IS NULL LIMIT 1"
    ).fetchone()
    invalid_count = conn.execute(
        "SELECT 1 FROM concord_operations o WHERE typeof(attempt_count) != 'integer'"
        " OR attempt_count < 0 OR attempt_count !="
        " (SELECT COUNT(*) FROM concord_attempts a WHERE a.operation_id = o.operation_id)"
        " OR attempt_count != COALESCE((SELECT MAX(ordinal) FROM concord_attempts a"
        " WHERE a.operation_id = o.operation_id), 0) LIMIT 1"
    ).fetchone()
    invalid_ordinal = conn.execute(
        "SELECT 1 FROM concord_attempts WHERE typeof(ordinal) != 'integer'"
        " OR ordinal < 1 LIMIT 1"
    ).fetchone()
    invalid_pending = conn.execute(
        "SELECT 1 FROM concord_operations o WHERE"
        " (o.state = 'pending') != EXISTS(SELECT 1 FROM concord_attempts a"
        " WHERE a.operation_id = o.operation_id AND a.state = 'pending') LIMIT 1"
    ).fetchone()
    if invalid_state or invalid_count or invalid_ordinal or invalid_pending:
        return StoreVerdict(
            creatable=False, damage_reason=DAMAGE_HISTORY,
            detail="damaged Concord history: invalid operation/attempt state or"
                   " inconsistent dispatch count, ordinal or pending evidence;"
                   " nothing will be rebuilt or mutated",
        )
    # A malformed receipt must not pass backup verification and later crash a
    # replay or recovery command. Missing/pruned receipts remain compatible.
    for (receipt,) in conn.execute(
        "SELECT receipt FROM concord_operations WHERE receipt IS NOT NULL"
    ):
        try:
            if not isinstance(json.loads(receipt), dict):
                raise ValueError("receipt must be an object")
        except (ValueError, TypeError, UnicodeError):
            return StoreVerdict(
                creatable=False, damage_reason=DAMAGE_HISTORY,
                detail="damaged Concord history: a persisted receipt is not a JSON"
                       " object; nothing will be rebuilt or mutated",
            )
    # SQLite permits text in INTEGER columns. Malformed ordering evidence is
    # damage, not a Python conversion error that may escape after dispatch.
    counter = conn.execute(
        "SELECT value FROM concord_counters WHERE name = 'seq'").fetchone()
    invalid_operations = conn.execute(
        "SELECT 1 FROM concord_operations WHERE typeof(created_seq) != 'integer'"
        " OR typeof(updated_seq) != 'integer' OR created_seq < 0 OR updated_seq < 0 LIMIT 1"
    ).fetchone()
    invalid_attempts = conn.execute(
        "SELECT 1 FROM concord_attempts WHERE typeof(started_seq) != 'integer'"
        " OR started_seq < 0 OR (settled_seq IS NOT NULL AND"
        " (typeof(settled_seq) != 'integer' OR settled_seq < 0)) LIMIT 1"
    ).fetchone()
    if (counter is not None and (not isinstance(counter[0], int) or counter[0] < 0)
            or invalid_operations or invalid_attempts):
        return StoreVerdict(
            creatable=False, damage_reason=DAMAGE_HISTORY,
            detail="damaged Concord history: sequence values must be non-negative"
                   " integers; nothing will be rebuilt or mutated",
        )
    highest_row = conn.execute(
        "SELECT COALESCE(MAX(created_seq), 0), COALESCE(MAX(updated_seq), 0)"
        " FROM concord_operations"
    ).fetchone()
    highest_attempt = conn.execute(
        "SELECT COALESCE(MAX(started_seq), 0), COALESCE(MAX(settled_seq), 0)"
        " FROM concord_attempts"
    ).fetchone()
    highest = max(*highest_row, *highest_attempt)
    if highest and (counter is None or int(counter[0]) < highest):
        where = "missing" if counter is None else f"at {int(counter[0])}"
        return StoreVerdict(
            creatable=False,
            damage_reason=DAMAGE_HISTORY,
            detail=(
                f"damaged Concord history: the durable sequence counter is {where} while"
                f" recorded rows reach {highest} - reusing those ordinals would rewrite"
                " ordering evidence, so nothing will be rebuilt"
            ),
        )
    return StoreVerdict(creatable=False)


def _damage_gate(
    conn: sqlite3.Connection,
    *,
    phase: str,
    require_schema: bool = False,
) -> StoreVerdict:
    """The damage verdict at the MUTATION instant, inside the write transaction (F10).

    CONCORD-009 made the verdict one consistent read snapshot, but that snapshot
    was still a *separate* transaction from the schema work or admission it
    guarded. An outside process could damage the file after the verdict and
    before the mutation - for example delete the ``concord_operations`` row that
    backs a surviving ``unknown`` attempt - and the mutation would proceed as if
    the store were healthy. This function is called through the SAME connection
    that already holds ``BEGIN IMMEDIATE`` and is about to mutate, so:

      * damage committed before this transaction began is part of the lock-holding
        snapshot and REFUSES here, before a single row is written;
      * damage cannot land inside this window, because the write lock is held;
      * nothing is discovered after a commit - the refusal and the mutation are
        the same transaction's decision.

    ``phase`` names the transaction for the refusal text. ``require_schema`` is
    for every mutation after initialization, and for initialization when the
    preflight already observed a Concord schema. An empty schema then means the
    store was erased or replaced, never permission to recreate its history.
    Fresh initialization is permitted only when both observations allow it.
    """
    verdict = _validate_snapshot(conn)
    if verdict.damaged:
        raise CoordinatorUnavailable(verdict.detail, reason=verdict.damage_reason)
    if require_schema and verdict.creatable:
        raise CoordinatorUnavailable(
            f"the coordination store no longer holds the Concord schema that was"
            f" validated when this coordinator opened it (seen at {phase}); the file"
            " was emptied or replaced outside Concord. Mutation is refused rather"
            " than recreating an empty store over whatever is there now - restore"
            " from `hermes concord backup`, or repair the file",
            reason=DAMAGE_PARTIAL_SCHEMA,
        )
    return verdict


def _ensure_journal_mode(conn: sqlite3.Connection, want: str) -> None:
    """Put the database in *want* without fighting a concurrent writer.

    Reading the mode is lock-free; switching it is not, and a second caller that
    races the first one's switch gets SQLITE_BUSY immediately rather than after
    a busy_timeout. The bounded retry exists for that window only - a fresh store
    being created by two processes at once - and re-reads the mode first, because
    the other process may already have done the work.
    """
    current = conn.execute("PRAGMA journal_mode").fetchone()
    if current and str(current[0]).lower() == want.lower():
        return
    deadline = time.monotonic() + 1.0
    while True:
        try:
            conn.execute(f"PRAGMA journal_mode = {want}")
            return
        except sqlite3.OperationalError:
            current = conn.execute("PRAGMA journal_mode").fetchone()
            if current and str(current[0]).lower() == want.lower():
                return
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _evaluate_evidence(evaluator: Callable[..., "EvidenceVerdict"], **kwargs: Any) -> "EvidenceVerdict":
    """Call an evidence evaluator, passing the stored fingerprint identity when it can take it.

    ``fingerprint_object_key`` is the object spelling the record's digest was
    computed over (owner F8-C). An evaluator written before this round does not
    name that parameter and keeps working exactly as it did: the extra keyword is
    dropped for it rather than raising ``TypeError`` from inside a settlement.
    """
    try:
        params = inspect.signature(evaluator).parameters
    except (TypeError, ValueError):  # a builtin or C callable: pass what we have
        params = None
    if params is not None and "fingerprint_object_key" not in params and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    ):
        kwargs.pop("fingerprint_object_key", None)
    return evaluator(**kwargs)


def authoritative_non_execution(evidence: Mapping[str, Any], operation_id: str) -> bool:
    """Is *evidence* the provider's own statement that this operation was never applied?

    A released hold must rest on something that covers the whole window in which
    the request could have been applied, and that names *this* operation. A
    read of the current ref does not qualify - the ref is an observation of the
    past, and a request that timed out can still be applied afterwards (review
    F7-2). The shape this accepts is therefore explicit and narrow:

    ``evidence["non_execution"] = {"operation_id": <this op>, "authoritative": true,
    "source": <who says so>, "covers": <the window/attempt it covers>}``

    An adapter that cannot obtain such a statement simply never supplies it, and
    the operation stays held - which is the safe direction.
    """
    proof = evidence.get("non_execution")
    if not isinstance(proof, Mapping):
        return False
    if str(proof.get("operation_id") or "").strip() != str(operation_id):
        return False
    if proof.get("authoritative") is not True:
        return False
    return bool(str(proof.get("source") or "").strip()) and bool(str(proof.get("covers") or "").strip())


class Coordinator:
    """Durable object-scoped admission control and state machine."""

    def __init__(
        self,
        path: str,
        *,
        busy_ms: int = 5000,
        clock: Optional[Callable[[], float]] = None,
        journal_mode: str = "WAL",
        max_pages: Optional[int] = None,
        max_receipt_bytes: int = 64 * 1024,
        max_note_chars: int = 2000,
        max_attempts_per_operation: int = 16,
        max_resolved_per_object: int = 200,
        max_unresolved_records: int = 10_000,
        normalize_object_key: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.path = path
        self.busy_ms = busy_ms
        self._now = clock or time.time
        if journal_mode not in ("WAL", "DELETE", "TRUNCATE", "PERSIST"):
            raise ValueError(f"unsupported journal_mode {journal_mode!r}")
        self.journal_mode = journal_mode
        self.max_pages = max_pages
        self.max_receipt_bytes = max_receipt_bytes
        self.max_note_chars = max_note_chars
        self.max_attempts_per_operation = max_attempts_per_operation
        self.max_resolved_per_object = max_resolved_per_object
        self.max_unresolved_records = max_unresolved_records
        #: Optional provider-identity canonicaliser. The kernel stays provider-
        #: agnostic: it is handed a function, not a key grammar. When supplied,
        #: records written before the canonical form existed are migrated on
        #: open so an alias cannot leave a hold under a second spelling.
        self.normalize_object_key = normalize_object_key
        self._init()

    # -- storage plumbing ------------------------------------------------
    def _init(self) -> None:
        # A cheap read-only pre-check first, kept for one reason: it refuses a
        # damaged file WITHOUT opening it for write, so a refused open changes no
        # bytes (no journal-mode switch, no migration ledger). It is not the
        # authoritative gate - see _initialize_in_transaction (owner F10).
        verdict = validate_existing_store(self.path)
        if verdict.damaged:
            raise CoordinatorUnavailable(verdict.detail, reason=verdict.damage_reason)
        try:
            conn = _connect(self.path, self.busy_ms, self.max_pages)
            try:
                # Carry the preflight's provenance into the locked decision:
                # a store we already saw must not become "fresh" if an outside
                # process drops ALL its tables before BEGIN IMMEDIATE.
                self._initialize_in_transaction(
                    conn, require_schema=not verdict.creatable,
                )
                # A journal-mode change can write the header. Do it only after
                # locked validation, so a rejected open cannot change a damaged
                # store merely while configuring its connection.
                _ensure_journal_mode(conn, self.journal_mode)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc

    def _initialize_in_transaction(
        self, conn: sqlite3.Connection, *, require_schema: bool,
    ) -> None:
        """Validate, create and migrate inside ONE ``BEGIN IMMEDIATE`` span (owner F10).

        The verdict that decides whether this file may be written and the writes
        it decides about are now the same transaction. The previous shape asked a
        read-only connection whether the store was healthy, then opened a second
        connection and created/migrated the schema in later transactions: an
        outside process that damaged the file in that gap was invisible, and a
        migration could run on history whose operations had just disappeared.

        ``_damage_gate`` runs first, under the write lock. A damaged store raises
        before any statement changes a row; the transaction is rolled back and
        the caller refuses. Fresh creation (no file, an empty file, or a database
        file with no Concord schema) is the ``creatable`` case and stays the
        CONCORD-008 classification, but only when the earlier read-only verdict
        also allowed creation. Losing a previously observed complete schema is
        damage, not a fresh-start opportunity.

        Statements are executed one at a time rather than through
        ``executescript``, which would commit the open transaction first.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            verdict = _damage_gate(
                conn, phase="initialization", require_schema=require_schema,
            )
            if verdict.creatable:
                # No file, an empty file, or a database file with no Concord
                # schema of ours in it: creating the schema destroys nothing.
                _create_schema(conn)
            # A store that already held Concord history has been validated in
            # this same transaction, so only additive bookkeeping is applied to
            # it: the migration ledger and the added columns. No CREATE for a
            # required table ever runs on it.
            for statement in _MIGRATIONS_STATEMENTS:
                conn.execute(statement)
            self._ensure_columns(conn)
            self._migrate(conn)
            conn.execute("COMMIT")
        except BaseException:
            _rollback_quietly(conn)
            raise

    # -- schema additions ------------------------------------------------
    #: Columns added after the first release. Each is additive and idempotent:
    #: an existing store is brought up to date on open, never rebuilt.
    #: ``fingerprint_object`` records the object spelling a row's fingerprint was
    #: derived from, so the canonical lock identity can be rewritten without
    #: invalidating the fingerprint (owner F8-C).
    ADDED_COLUMNS = (("base_identity", "TEXT"), ("fingerprint_object", "TEXT"))

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        present = {row[1] for row in conn.execute("PRAGMA table_info(concord_operations)")}
        for name, sql_type in self.ADDED_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE concord_operations ADD COLUMN {name} {sql_type}")

    # -- migrations ------------------------------------------------------
    MIGRATION_CANONICAL_KEYS = "canonical-object-keys"
    MIGRATION_FINGERPRINT_IDENTITY = "fingerprint-identity"

    def _record_fingerprint_identity(self, conn: sqlite3.Connection) -> int:
        """Persist the object spelling each stored fingerprint was derived from.

        ``fingerprint`` is a digest over ``{"object": <key as spelled>, "value":
        ..., "expected_revision": ...}``, so rewriting ``object_key`` to the
        canonical lock identity would silently invalidate every fingerprint that
        was computed over the older spelling: a replay would be refused as an
        argument mismatch and reconciliation could no longer attribute the
        operation's own value (owner F8-C).

        ``fingerprint_object`` keeps that original spelling *beside* the
        canonical lock identity and is never overwritten. This runs before the
        alias rewrite, so a legacy row records the alias it was written with.
        Rows written by the current code already hold a canonical key, so for
        them the recorded identity is the key itself. A row a previous migration
        already rewrote has lost its original spelling - it can only be recorded
        as its current key, and that limitation is stated in the result record.
        """
        row = conn.execute(
            "SELECT 1 FROM concord_migrations WHERE name = ?",
            (self.MIGRATION_FINGERPRINT_IDENTITY,),
        ).fetchone()
        if row is not None:
            return 0
        cursor = conn.execute(
            "UPDATE concord_operations SET fingerprint_object = object_key"
            " WHERE fingerprint_object IS NULL"
        )
        backfilled = max(int(cursor.rowcount or 0), 0)
        if backfilled:
            conn.execute(
                "INSERT INTO concord_migrations (name, detail, applied_seq, applied_at)"
                " VALUES (?, ?, NULL, ?)",
                (self.MIGRATION_FINGERPRINT_IDENTITY, f"backfilled={backfilled}", self._now()),
            )
        return backfilled

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring an existing store to the current identity rules, once.

        Provider identity is canonicalised at every write path, so this exists
        for records written before that rule did: an unresolved hold stored
        under a non-canonical spelling would otherwise be invisible to a call
        that uses the canonical one, and a second write would land.

        Two rows may canonicalise onto one object key. The partial unique index
        allows one unresolved row per object, so the colliding row keeps its own
        key and a note says why - evidence is never merged away or discarded.
        Leaving it under the alias does not weaken the hold: admission compares
        canonical identities, so a row in either spelling blocks every alias of
        that object (review F7-1). The rewrite is a convenience, not the guard.

        The rewrite never erases the *fingerprint* identity: each row's original
        spelling is recorded in ``fingerprint_object`` first (owner F8-C), so the
        stored digest stays checkable after the lock key becomes canonical.
        """
        self._record_fingerprint_identity(conn)
        normalize = self.normalize_object_key
        if normalize is None:
            return
        row = conn.execute(
            "SELECT 1 FROM concord_migrations WHERE name = ?",
            (self.MIGRATION_CANONICAL_KEYS,),
        ).fetchone()
        if row is not None:
            return
        rewritten = 0
        collided = 0
        for operation_id, object_key in conn.execute(
            "SELECT operation_id, object_key FROM concord_operations ORDER BY created_seq"
        ).fetchall():
            try:
                canonical = normalize(object_key)
            except Exception:  # noqa: BLE001 - a foreign/odd key is left exactly as it is
                continue
            if not isinstance(canonical, str) or not canonical or canonical == object_key:
                continue
            try:
                conn.execute(
                    "UPDATE concord_operations SET object_key = ?,"
                    " fingerprint_object = COALESCE(fingerprint_object, ?)"
                    " WHERE operation_id = ?",
                    (canonical, object_key, operation_id),
                )
                rewritten += 1
            except sqlite3.IntegrityError:
                collided += 1
                note = (
                    f"{object_key} is the same provider object as {canonical};"
                    " canonicalisation was skipped because an unresolved record already"
                    " holds that object. Both spellings are held as one provider object:"
                    " admission compares canonical identities, so no alias of it can be"
                    " written until every hold on it settles - inspect both with"
                    " `concord status`"
                )
                conn.execute(
                    "UPDATE concord_operations SET note = ?, note_chars = ?"
                    " WHERE operation_id = ?",
                    (
                        note,
                        len(note),
                        operation_id,
                    ),
                )
        detail = f"rewritten={rewritten} collisions={collided}"
        conn.execute(
            "INSERT INTO concord_migrations (name, detail, applied_seq, applied_at)"
            " VALUES (?, ?, NULL, ?)",
            (self.MIGRATION_CANONICAL_KEYS, detail, self._now()),
        )

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _txn(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        try:
            conn = _connect(self.path, self.busy_ms, self.max_pages)
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            conn.close()
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc
        try:
            # Every mutation shares this boundary, not just admission. Damage
            # after open must also stop dispatch, settlement, reconciliation,
            # cancellation and retention before they change durable evidence.
            _damage_gate(conn, phase="mutation", require_schema=True)
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except sqlite3.IntegrityError:
            self._rollback(conn)
            raise
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def _read(self, sql: str, params: tuple = ()) -> Any:
        try:
            conn = _connect(self.path, self.busy_ms, self.max_pages)
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise CoordinatorUnavailable(f"{type(exc).__name__}: {exc}", reason=_classify(exc)) from exc
        finally:
            conn.close()

    @staticmethod
    def _next_seq(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM concord_counters WHERE name = 'seq'").fetchone()
        value = (row[0] if row else 0) + 1
        conn.execute(
            "INSERT INTO concord_counters (name, value) VALUES ('seq', ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (value,),
        )
        return value

    def _bounded_note(self, note: str) -> tuple[str, int]:
        note = note or ""
        original = len(note)
        if original <= self.max_note_chars:
            return note, original
        keep = max(0, self.max_note_chars - 40)
        return f"{note[:keep]}…[truncated {original - keep} chars]", original

    def _bounded_receipt(self, receipt: Optional[dict]) -> tuple[Optional[str], bool]:
        if receipt is None:
            return None, False
        raw = canonical_json(receipt)
        size = len(raw.encode("utf-8"))
        if size <= self.max_receipt_bytes:
            return raw, False
        # Bounded, not discarded: the digest preserves the ability to check
        # later that a copy of the receipt is genuine.
        marker = {
            "_truncated": True,
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "bytes": size,
            "keys": sorted(str(k) for k in receipt)[:32],
        }
        return canonical_json(marker), True

    def _unresolved_count(self, conn: sqlite3.Connection) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM concord_operations WHERE state IN {_BLOCKING_SQL}"
        ).fetchone()[0]

    def _enforce_capacity(self, conn: sqlite3.Connection) -> None:
        count = self._unresolved_count(conn)
        if count >= self.max_unresolved_records:
            raise CapacityExceeded(
                f"{count} unresolved operations at the configured limit "
                f"({self.max_unresolved_records}); unresolved records are never discarded"
            )

    def _enforce_object_bound(self, conn: sqlite3.Connection, object_key: str) -> None:
        """Prune payloads of the oldest resolved records for one object.

        A payload-pruned record keeps its identity, state and fingerprint, so a
        later replay is refused as pruned rather than re-executed. This is the
        difference between bounded storage and lost evidence.
        """
        rows = conn.execute(
            f"SELECT operation_id FROM concord_operations WHERE object_key = ?"
            f" AND state IN {_TERMINAL_SQL} AND pruned_at IS NULL"
            f" ORDER BY updated_seq DESC",
            (object_key,),
        ).fetchall()
        excess = [r[0] for r in rows[self.max_resolved_per_object:]]
        if not excess:
            return
        now = self._now()
        conn.executemany(
            "UPDATE concord_operations SET receipt = NULL, note = NULL, pruned_at = ?,"
            " updated_at = ? WHERE operation_id = ?",
            [(now, now, op) for op in excess],
        )

    # -- admission -------------------------------------------------------
    def admit(
        self,
        *,
        object_key: str,
        operation_id: str,
        fingerprint: str,
        base_revision: int,
        actor: str,
        base_identity: Any = None,
        material: Optional[Mapping[str, Any]] = None,
    ) -> Admission:
        """Atomically reserve *object_key* for *operation_id*.

        ``base_identity`` is the provider's immutable reference for the base the
        caller observed (a commit SHA, for GitHub): the thing reconciliation may
        use to prove that nothing has landed on the object since. It is opaque
        here - the kernel stores and returns it, and the adapter's evaluator
        interprets it.

        ``material`` is the same call's material fields, when the caller has them
        (``run`` does). It is used for exactly one decision: an operation id that
        is already recorded with a digest computed over an older spelling of the
        same object is the same intent, not an argument mismatch (owner F8-C).
        Without it, only an exact digest match counts.

        One transaction: damage verdict, replay lookup, retry eligibility,
        active-holder check, insert. The partial unique index is the real guard — a
        second active operation on the same object fails at INSERT, so two
        processes cannot both pass a check. The damage verdict is taken *inside*
        that same ``BEGIN IMMEDIATE`` span (owner F10), so an outside process that
        damaged the store after the coordinator was opened cannot leave this
        admission deciding on a healthy-looking file: the damage is refused before
        a row is read for a decision, and no mutation is committed.

        The holder check is by provider *identity*, not by the literal key: when
        the kernel was given a canonicaliser, an unresolved row stored under an
        alias spelling still holds this object (review F7-1). That check runs
        inside ``BEGIN IMMEDIATE`` and is recomputed from the stored rows, so it
        survives closing and reopening the store.

        A recorded operation id is bound to the object it was admitted for. A
        request for a different object is refused with
        ``rejected_object_mismatch`` BEFORE fingerprint compatibility is
        consulted, so a digest that can be re-checked under an older spelling can
        never move an operation onto another object (owner F9-1). Equality, or
        canonical equivalence verified by the configured canonicaliser, is the
        only route to a replay or a retry.
        """
        if not object_key or not operation_id:
            raise ValueError("object_key and operation_id are required")

        def _do(conn: sqlite3.Connection) -> Admission:
            # _txn validates the store before calling this admission body.
            now = self._now()
            row = conn.execute(
                "SELECT fingerprint, state, receipt, holder, pruned_at, fingerprint_object,"
                " object_key FROM concord_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                (stored_fp, state, receipt, holder, pruned_at, fp_object,
                 stored_object) = row
                # The object-identity gate comes FIRST (owner F9-1). A stored
                # digest can be re-checked under the spelling it was derived
                # from, so a changed path/branch/repository carrying the same
                # value and base would hash to the stored digest - and would then
                # replay a receipt, or re-dispatch a retry, under an identity that
                # belongs to another object. Identity is decided before
                # compatibility, never by it.
                if not self._same_object(stored_object, object_key):
                    return Admission(
                        REJECTED_OBJECT_MISMATCH,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=state,
                        reason=(
                            f"operation id {operation_id} is already recorded for"
                            f" {stored_object}; {object_key} is a different object and will not"
                            " inherit that operation's identity, receipt or hold. A matching"
                            " fingerprint under the recorded spelling does not move the object:"
                            " use a new operation id for a different object"
                        ),
                    )
                if stored_fp != fingerprint and not (
                    material is not None
                    and fingerprint_matches(material, stored_fp, fingerprint_object=fp_object)
                ):
                    return Admission(
                        REJECTED_ARGUMENT_MISMATCH,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=state,
                        reason=(
                            f"operation id {operation_id} is already recorded with different material"
                            " arguments; refusing to become a second external action"
                        ),
                    )
                if state == CONFIRMED:
                    if pruned_at is not None:
                        return Admission(
                            REPLAY_PRUNED,
                            object_key=object_key,
                            operation_id=operation_id,
                            state=state,
                            pruned=True,
                            reason=(
                                "operation already confirmed; its receipt was pruned by the"
                                " retention policy, so replay returns no receipt and performs no write"
                            ),
                        )
                    return Admission(
                        REPLAY_CONFIRMED,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=state,
                        receipt=json.loads(receipt) if receipt else None,
                        reason="identical operation already confirmed; returning original receipt",
                    )
                if state in (PENDING, UNKNOWN, PREPARED):
                    return Admission(
                        CONFLICT,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=state,
                        holder=operation_id,
                        reason=(
                            f"operation {operation_id} is already {state}; outcome is not confirmed"
                            " and it will not be retried automatically"
                        ),
                    )
                if state == CANCELLED:
                    return Admission(
                        CONFLICT,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=state,
                        reason="operation was cancelled; a cancelled intent is closed — use a new operation id",
                    )
                if state not in RETRYABLE_STATES:
                    raise CoordinatorUnavailable(
                        f"unrecognized operation state {state!r}; retry refused",
                        reason=DAMAGE_HISTORY,
                    )
                # FAILED_BEFORE_EXECUTION / CONFLICT: provably nothing was sent,
                # so re-attempting the same operation is safe.
                other = self._active_holder(conn, object_key, exclude=operation_id)
                if other is not None:
                    conn.execute(
                        "UPDATE concord_operations SET state = ?, holder = ?, note = ?, updated_at = ?,"
                        " updated_seq = ? WHERE operation_id = ?",
                        (CONFLICT, other, f"re-attempt blocked: object held by {other}", now,
                         self._next_seq(conn), operation_id),
                    )
                    return Admission(
                        CONFLICT,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=CONFLICT,
                        holder=other,
                        reason=f"object {object_key} is held by an unresolved operation {other}",
                    )
                seq = self._next_seq(conn)
                conn.execute(
                    "UPDATE concord_operations SET state = ?, holder = NULL, note = ?, updated_at = ?,"
                    " updated_seq = ? WHERE operation_id = ?",
                    (PREPARED, f"re-attempted from {state}", now, seq, operation_id),
                )
                self._enforce_object_bound(conn, object_key)
                return Admission(
                    ADMITTED,
                    object_key=object_key,
                    operation_id=operation_id,
                    state=PREPARED,
                    seq=seq,
                    reason=f"re-admitted from {state} (nothing was executed in that state)",
                )
            other = self._active_holder(conn, object_key)
            if other is not None:
                seq = self._next_seq(conn)
                try:
                    conn.execute(
                        "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
                        " fingerprint_object, base_revision, base_identity, actor, state, holder,"
                        " attempt_count, receipt, note, note_chars, created_seq, updated_seq,"
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?)",
                        (
                            operation_id, object_key, fingerprint, object_key, base_revision,
                            _identity_text(base_identity), actor, CONFLICT,
                            other, f"object held by unresolved operation {other}", 0, seq, seq,
                            now, now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    return Admission(
                        CONFLICT,
                        object_key=object_key,
                        operation_id=operation_id,
                        state=CONFLICT,
                        reason="conflict already recorded for this operation id",
                    )
                self._enforce_object_bound(conn, object_key)
                return Admission(
                    CONFLICT,
                    object_key=object_key,
                    operation_id=operation_id,
                    state=CONFLICT,
                    holder=other,
                    seq=seq,
                    reason=f"object {object_key} is held by an unresolved operation {other}",
                )

            self._enforce_capacity(conn)
            seq = self._next_seq(conn)
            try:
                conn.execute(
                    "INSERT INTO concord_operations (operation_id, object_key, fingerprint,"
                    " fingerprint_object, base_revision, base_identity, actor, state, holder,"
                    " attempt_count, receipt, note, note_chars, created_seq, updated_seq,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (operation_id, object_key, fingerprint, object_key, base_revision,
                     _identity_text(base_identity), actor, PREPARED, seq, seq, now, now),
                )
            except sqlite3.IntegrityError:
                # A competing process reserved the same object between our read
                # and our insert. The index decided it; report the conflict.
                return Admission(
                    CONFLICT,
                    object_key=object_key,
                    operation_id=operation_id,
                    state=CONFLICT,
                    reason=f"object {object_key} was reserved concurrently by another operation",
                )
            self._enforce_object_bound(conn, object_key)
            return Admission(
                ADMITTED,
                object_key=object_key,
                operation_id=operation_id,
                state=PREPARED,
                seq=seq,
                reason="reserved",
            )

        return self._txn(_do)

    def _canonical_form(self, object_key: str) -> Optional[str]:
        """The provider-canonical form of a key, when a canonicaliser is configured."""
        normalize = self.normalize_object_key
        if normalize is None or not isinstance(object_key, str) or not object_key:
            return None
        try:
            canonical = normalize(object_key)
        except Exception:  # noqa: BLE001 - a foreign/odd key is only itself
            return None
        return canonical if isinstance(canonical, str) and canonical else None

    def _same_object(self, stored: Any, requested: Any) -> bool:
        """The object-identity gate, with this coordinator's canonicaliser (F9-1)."""
        return object_key_matches(stored, requested, self.normalize_object_key)

    def _blocking_rows(self, conn: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = conn.execute(
            f"SELECT operation_id, object_key FROM concord_operations"
            f" WHERE state IN {_BLOCKING_SQL} ORDER BY created_seq"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _active_holder(
        self, conn: sqlite3.Connection, object_key: str, *, exclude: Optional[str] = None
    ) -> Optional[str]:
        """The unresolved operation holding *object_key*, under any alias spelling.

        The exact key is checked first. When that finds nothing and the kernel has
        a canonicaliser, every unresolved row whose *stored* key canonicalises
        onto the same provider identity also counts as a holder (review F7-1).

        The second pass is why the equivalence holds at admission time and not
        only at migration time: a legacy row the migration could not rewrite -
        because the partial unique index allows one unresolved row per
        ``object_key`` - still blocks every alias spelling of that object, and it
        still blocks after the store is reopened, in either settlement order. The
        scan is bounded by the same record set the capacity limit already caps,
        and only runs when the indexed exact lookup missed.
        """
        row = conn.execute(
            f"SELECT operation_id FROM concord_operations WHERE object_key = ?"
            f" AND state IN {_BLOCKING_SQL} AND operation_id IS NOT ?",
            (object_key, exclude),
        ).fetchone()
        if row is not None:
            return row[0]
        canonical = self._canonical_form(object_key)
        if canonical is None:
            return None
        for operation_id, stored in self._blocking_rows(conn):
            if operation_id == exclude:
                continue
            if self._canonical_form(stored) == canonical:
                return operation_id
        return None

    # -- attempts and transitions ---------------------------------------
    def begin_dispatch(self, operation_id: str) -> str:
        """Prepared -> pending. Opens an attempt and returns its id."""
        now = self._now()

        def _do(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT state, attempt_count FROM concord_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise IllegalTransition(f"unknown operation {operation_id}")
            state, attempts = row
            if state != PREPARED:
                raise IllegalTransition(f"{operation_id} is {state}; dispatch can only start from reserved")
            if attempts >= self.max_attempts_per_operation:
                raise CapacityExceeded(
                    f"operation {operation_id} reached the attempt limit "
                    f"({self.max_attempts_per_operation})"
                )
            ordinal = attempts + 1
            attempt_id = f"{operation_id}#{ordinal}:{uuid.uuid4().hex[:12]}"
            seq = self._next_seq(conn)
            conn.execute(
                "UPDATE concord_operations SET state = ?, attempt_count = ?, note = ?, updated_at = ?,"
                " updated_seq = ? WHERE operation_id = ?",
                (PENDING, ordinal, "dispatch started", now, seq, operation_id),
            )
            conn.execute(
                "INSERT INTO concord_attempts (attempt_id, operation_id, ordinal, state, detail,"
                " started_seq, started_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (attempt_id, operation_id, ordinal, PENDING, seq, now),
            )
            return attempt_id

        return self._txn(_do)

    def _settle_txn(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        state: str,
        *,
        allowed_from: tuple[str, ...],
        receipt: Optional[dict] = None,
        note: str = "",
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT state, attempt_count, object_key FROM concord_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"unknown operation {operation_id}")
        current, attempts, object_key = row
        if current not in allowed_from:
            raise IllegalTransition(
                f"{operation_id}: {current} -> {state} is not a legal transition"
                f" (allowed from {', '.join(allowed_from)})"
            )
        now = self._now()
        seq = self._next_seq(conn)
        note_text, note_chars = self._bounded_note(note)
        stored_receipt, truncated = self._bounded_receipt(receipt)
        if truncated:
            note_text = (note_text + " | receipt exceeded the storage limit; digest recorded").strip()
        conn.execute(
            "UPDATE concord_operations SET state = ?, receipt = COALESCE(?, receipt), note = ?,"
            " note_chars = ?, pruned_at = NULL, updated_at = ?, updated_seq = ? WHERE operation_id = ?",
            (state, stored_receipt, note_text, note_chars, now, seq, operation_id),
        )
        conn.execute(
            "UPDATE concord_attempts SET state = ?, detail = ?, settled_seq = ?, settled_at = ?"
            " WHERE operation_id = ? AND state = 'pending'",
            (state, note_text[:_MAX_DETAIL_CHARS], seq, now, operation_id),
        )
        self._enforce_object_bound(conn, object_key)
        return {"operation_id": operation_id, "state": state, "seq": seq, "attempts": attempts}

    def _settle(self, operation_id: str, state: str, *, allowed_from: tuple[str, ...],
                receipt: Optional[dict] = None, note: str = "") -> dict[str, Any]:
        return self._txn(
            lambda conn: self._settle_txn(
                conn, operation_id, state, allowed_from=allowed_from, receipt=receipt, note=note
            )
        )

    def confirm(self, operation_id: str, receipt: dict) -> dict[str, Any]:
        return self._settle(
            operation_id, CONFIRMED, allowed_from=(PENDING,), receipt=receipt,
            note="provider write returned; receipt recorded",
        )

    def fail_before_execution(self, operation_id: str, reason: str) -> dict[str, Any]:
        return self._settle(
            operation_id, FAILED_BEFORE_EXECUTION, allowed_from=(PREPARED, PENDING),
            note=f"proved not executed: {reason}",
        )

    def mark_unknown(self, operation_id: str, reason: str) -> dict[str, Any]:
        # Deliberately does NOT release the object: an ambiguous outcome is not
        # safe to repeat, and restart or wall-clock expiry must not clear it.
        return self._settle(
            operation_id, UNKNOWN, allowed_from=(PREPARED, PENDING),
            note=f"outcome not determinable: {reason}",
        )

    def cancel(self, operation_id: str, reason: str = "cancelled by caller") -> dict[str, Any]:
        """Cancel is only honest before dispatch.

        Once a request may have left the process there is nothing to cancel —
        the outcome is unknown or confirmed, and only provider evidence settles
        it. Refusing here is the bound, not a limitation to work around.
        """
        return self._settle(
            operation_id, CANCELLED, allowed_from=(PREPARED,),
            note=f"cancelled before dispatch: {reason}",
        )

    def reconcile(
        self,
        operation_id: str,
        evidence: Mapping[str, Any],
        *,
        evaluate: Optional[Callable[..., "EvidenceVerdict"]] = None,
        require_release_proof: bool = False,
    ) -> dict[str, Any]:
        """Settle an ambiguous operation by reading provider evidence.

        Never dispatches, never retries. Three honest outcomes only:

          * the provider is at base+1 and its content hashes to our fingerprint
            -> CONFIRMED (the write was ours);
          * the provider is still at base -> FAILED_BEFORE_EXECUTION (nothing
            has been written to that object since the base revision), subject to
            the ``require_release_proof`` rule below;
          * anything else -> stays UNKNOWN (someone else moved it; we cannot
            claim the value as ours).

        ``evaluate`` is the single target-dependent block. It defaults to the
        integer-revision predicate above; an adapter for a provider whose
        revision is an opaque token (a blob SHA) supplies its own. Everything
        else in this method — the transaction, the receipt shape, the three
        permitted settlements — is independent of the revision model.

        ``require_release_proof`` closes the last inference of that kind for a
        provider whose "still at base" is only an observation: when it is set, a
        *dispatched* operation is not released as FAILED_BEFORE_EXECUTION on the
        evaluator's word alone. It needs either attributable application
        (CONFIRMED) or an authoritative non-execution statement naming the
        operation (:func:`authoritative_non_execution`); otherwise it stays
        UNKNOWN and held (review F7-2). C2's monotonic integer model keeps the
        default, where "still at the base revision" *is* a proof because the
        revision is a per-object counter that cannot move without a write.
        """
        if not isinstance(evidence, Mapping) or "revision" not in evidence:
            raise ValueError("evidence must be a mapping containing 'revision'")
        evaluator = evaluate or integer_revision_evaluator

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT object_key, fingerprint, base_revision, base_identity, state,"
                " attempt_count, fingerprint_object FROM concord_operations"
                " WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise IllegalTransition(f"unknown operation {operation_id}")
            (object_key, stored_fp, base_revision, base_identity, state, attempt_count,
             fp_object) = row
            if state not in BLOCKING_STATES:
                raise IllegalTransition(f"{operation_id} is {state}; only unresolved operations can be reconciled")

            verdict = _evaluate_evidence(
                evaluator,
                evidence=evidence,
                object_key=object_key,
                stored_fingerprint=stored_fp,
                base_revision=base_revision,
                operation_id=operation_id,
                base_identity=base_identity,
                # The spelling this record's digest was computed over: the lock key
                # may since have been canonicalised (owner F8-C).
                fingerprint_object_key=fp_object or object_key,
            )
            if verdict.state not in (CONFIRMED, FAILED_BEFORE_EXECUTION, UNKNOWN):
                raise ValueError(f"evidence evaluator returned an illegal settlement {verdict.state!r}")

            dispatched = int(attempt_count or 0) > 0
            proof = (
                authoritative_non_execution(evidence, operation_id)
                if require_release_proof
                else False
            )
            if require_release_proof and proof and verdict.state == UNKNOWN:
                # An authoritative negative that names this operation and covers
                # the window settles it, even though the provider read alone does
                # not. Attribution still wins: a CONFIRMED verdict is untouched.
                verdict = EvidenceVerdict(
                    FAILED_BEFORE_EXECUTION,
                    "the provider states, authoritatively, that this operation was not"
                    " applied within the window the statement covers",
                    verdict.revision,
                )
            if (
                require_release_proof
                and dispatched
                and verdict.state == FAILED_BEFORE_EXECUTION
                and not proof
            ):
                verdict = EvidenceVerdict(
                    UNKNOWN,
                    f"{verdict.reason} - but operation {operation_id} was dispatched"
                    f" ({attempt_count} attempt(s)) and a target that has not moved is not"
                    " proof that the request cannot still land; held until the application"
                    " is attributable or the provider states, authoritatively, that nothing"
                    " was applied",
                    verdict.revision,
                )


            revision = evidence["revision"] if verdict.revision is None else verdict.revision
            digest = hashlib.sha256(
                canonical_json({"revision": revision, "value": evidence.get("value")}).encode("utf-8")
            ).hexdigest()
            receipt = {
                "reconciled": True,
                "source": str(evidence.get("source") or "provider_read"),
                "revision": revision,
                "evidence_sha256": digest,
            }
            # Immutable commit evidence is carried into the receipt when the
            # provider read supplied it: a reconciled receipt that drops the
            # commit SHA cannot be checked later (review carry-forward).
            commit_sha = _identity_text(evidence.get("commit_sha"))
            if commit_sha is not None:
                receipt["commit_sha"] = commit_sha
            if verdict.state == CONFIRMED:
                result = self._settle_txn(
                    conn, operation_id, CONFIRMED, allowed_from=BLOCKING_STATES, receipt=receipt,
                    note=f"reconciled from provider evidence: {verdict.reason}",
                )
            elif verdict.state == FAILED_BEFORE_EXECUTION:
                result = self._settle_txn(
                    conn, operation_id, FAILED_BEFORE_EXECUTION, allowed_from=BLOCKING_STATES,
                    note=f"reconciled from provider evidence: {verdict.reason}",
                )
            else:
                result = self._settle_txn(
                    conn, operation_id, UNKNOWN, allowed_from=BLOCKING_STATES,
                    note=f"reconciled: {verdict.reason}",
                )
            result["evidence"] = receipt
            return result

        return self._txn(_do)


    # -- reader ----------------------------------------------------------
    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        rows = self._read(
            "SELECT operation_id, object_key, fingerprint, base_revision, base_identity, actor,"
            " state, holder, attempt_count, receipt, note, note_chars, pruned_at, created_seq,"
            " updated_seq, created_at, updated_at, fingerprint_object FROM concord_operations"
            " WHERE operation_id = ?",
            (operation_id,),
        )
        if not rows:
            return None
        keys = (
            "operation_id", "object_key", "fingerprint", "base_revision", "base_identity", "actor",
            "state", "holder", "attempt_count", "receipt", "note", "note_chars", "pruned_at",
            "created_seq", "updated_seq", "created_at", "updated_at", "fingerprint_object",
        )
        out = dict(zip(keys, rows[0]))
        out["receipt"] = json.loads(out["receipt"]) if out["receipt"] else None
        out["pruned"] = out["pruned_at"] is not None
        out["attempts"] = self.attempts(operation_id)
        return out

    def attempts(self, operation_id: str) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT attempt_id, ordinal, state, detail, started_seq, settled_seq, started_at,"
            " settled_at FROM concord_attempts WHERE operation_id = ? ORDER BY ordinal",
            (operation_id,),
        )
        keys = ("attempt_id", "ordinal", "state", "detail", "started_seq", "settled_seq",
                "started_at", "settled_at")
        return [dict(zip(keys, row)) for row in rows]

    def holder_of(self, object_key: str) -> Optional[str]:
        """The unresolved holder of *object_key*, under any alias spelling."""
        rows = self._read(
            f"SELECT operation_id FROM concord_operations WHERE object_key = ?"
            f" AND state IN {_BLOCKING_SQL}",
            (object_key,),
        )
        if rows:
            return rows[0][0]
        canonical = self._canonical_form(object_key)
        if canonical is None:
            return None
        blocking = self._read(
            f"SELECT operation_id, object_key FROM concord_operations"
            f" WHERE state IN {_BLOCKING_SQL} ORDER BY created_seq"
        )
        for operation_id, stored in blocking:
            if self._canonical_form(stored) == canonical:
                return operation_id
        return None

    def stats(self) -> dict[str, Any]:
        counts = dict(
            self._read("SELECT state, COUNT(*) FROM concord_operations GROUP BY state") or []
        )
        return {
            "by_state": {state: int(counts.get(state, 0)) for state in ALL_STATES},
            "unresolved": int(sum(counts.get(state, 0) for state in BLOCKING_STATES)),
            "resolved": int(sum(counts.get(state, 0) for state in TERMINAL_STATES)),
        }

    # -- retention -------------------------------------------------------
    def prune_resolved(
        self,
        *,
        older_than_seconds: Optional[float] = None,
        keep_per_object: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Drop the payload of resolved records. Unresolved records are never touched.

        Pruning is explicit and reported. A pruned record keeps its identity, so
        a replay of it is refused as ``replay_pruned`` instead of executing a
        second external action — the reason pruning is safe to automate.
        """
        keep = self.max_resolved_per_object if keep_per_object is None else int(keep_per_object)

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self._now()
            cutoff = None if older_than_seconds is None else now - float(older_than_seconds)
            candidates = conn.execute(
                f"SELECT operation_id, object_key, updated_seq, updated_at FROM concord_operations"
                f" WHERE state IN {_TERMINAL_SQL} AND pruned_at IS NULL"
                f" ORDER BY updated_seq DESC"
            ).fetchall()
            newest_per_object: dict[str, int] = {}
            for _op, obj, _seq, _at in candidates:
                newest_per_object[obj] = newest_per_object.get(obj, 0) + 1
            seen: dict[str, int] = {}
            targets: list[str] = []
            for op, obj, _seq, at in candidates:
                seen[obj] = seen.get(obj, 0) + 1
                if seen[obj] <= keep:
                    continue
                if cutoff is not None and at > cutoff:
                    continue
                targets.append(op)
            if not dry_run and targets:
                conn.executemany(
                    "UPDATE concord_operations SET receipt = NULL, note = NULL, pruned_at = ?,"
                    " updated_at = ? WHERE operation_id = ?",
                    [(now, now, op) for op in targets],
                )
            unresolved = conn.execute(
                f"SELECT COUNT(*) FROM concord_operations WHERE state IN {_BLOCKING_SQL}"
            ).fetchone()[0]
            return {
                "pruned": len(targets),
                "dry_run": dry_run,
                "scanned": len(candidates),
                "unresolved_preserved": int(unresolved),
            }

        return self._txn(_do)

    # -- the tool-facing path -------------------------------------------
    def run(
        self,
        *,
        operation_id: str,
        actor: str,
        received_args: Mapping[str, Any],
        mutate: Callable[[], dict[str, Any]],
        revision_model: str = "integer",
        base_identity: Any = None,
    ) -> dict[str, Any]:
        """Admit, dispatch and settle in one frame — architecture B.

        ``received_args`` must be the arguments this tool body actually received
        (after any middleware rewrite); the fingerprint is derived from them.
        ``mutate`` performs the provider write and raises ``Rejected`` when the
        provider refuses without changing anything.

        Every dependency failure returns a refusal: there is no path from a
        coordinator fault to the mutation.
        """
        try:
            material = material_from_arguments(received_args, revision_model=revision_model)
            admission = self.admit(
                object_key=material["object"],
                operation_id=operation_id,
                fingerprint=fingerprint(material),
                base_revision=material["expected_revision"],
                actor=actor or "unknown",
                base_identity=base_identity,
                # The same material the digest came from: admission needs it to
                # recognise a replay recorded under an older key spelling (F8-C).
                material=material,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return {"status": "refused", "reason": "invalid_arguments", "detail": str(exc)}
        except CoordinatorUnavailable as exc:
            return refusal(exc)

        if admission.kind == REPLAY_CONFIRMED:
            return {
                "status": "replayed",
                "state": CONFIRMED,
                "reason": admission.reason,
                "receipt": admission.receipt,
                "second_write": False,
            }
        if admission.kind == REPLAY_PRUNED:
            return {
                "status": "replayed_pruned",
                "state": CONFIRMED,
                "reason": admission.reason,
                "receipt": None,
                "second_write": False,
                "pruned": True,
            }
        if admission.kind == REJECTED_OBJECT_MISMATCH:
            return {
                "status": "refused",
                "reason": "object_mismatch",
                "detail": admission.reason,
                "second_write": False,
            }
        if admission.kind == REJECTED_ARGUMENT_MISMATCH:
            return {"status": "refused", "reason": "argument_mismatch", "detail": admission.reason}
        if admission.kind != ADMITTED:
            return {
                "status": CONFLICT,
                "state": admission.state or CONFLICT,
                "reason": admission.reason,
                "holder": admission.holder,
                "second_write": False,
            }

        try:
            attempt_id = self.begin_dispatch(operation_id)
        except CoordinatorUnavailable as exc:
            # We never dispatched. Release the reservation if the store lets us;
            # if it does not, the object stays held, which is the safe direction.
            try:
                self.fail_before_execution(operation_id, f"dispatch could not start: {exc}")
            except (CoordinatorUnavailable, IllegalTransition) as settle_exc:
                return {
                    "status": UNKNOWN, "state": PREPARED,
                    "reason": "reservation_not_released",
                    "detail": f"dispatch did not start: {exc}; release failed: {settle_exc}",
                    "storage_reason": getattr(settle_exc, "reason", "illegal_transition"),
                    "last_recorded_state": PREPARED,
                    "provider_dispatched": False, "provider_outcome": "not_applied",
                    "settlement_persisted": False, "recovery_required": True,
                    "second_write": False,
                }
            # Admission existed and its release committed. Do not describe this
            # as a no-record refusal: a caller can inspect the closed intent.
            return {"status": CONFLICT, "state": FAILED_BEFORE_EXECUTION,
                    "reason": "dispatch_not_started", "detail": str(exc),
                    "second_write": False}
        except IllegalTransition as exc:
            return {
                "status": UNKNOWN, "state": PREPARED,
                "reason": "dispatch_state_changed", "detail": str(exc),
                "last_recorded_state": PREPARED, "provider_dispatched": False,
                "settlement_persisted": False, "recovery_required": True,
                "second_write": False,
            }

        try:
            receipt = mutate()
        except Rejected as exc:
            try:
                self.fail_before_execution(operation_id, f"provider rejected the write: {exc}")
            except (CoordinatorUnavailable, IllegalTransition) as settle_exc:
                return {
                    "status": UNKNOWN, "state": PENDING,
                    "reason": "rejection_not_persisted",
                    "detail": f"provider rejected: {exc}; settlement failed: {settle_exc}",
                    "last_recorded_state": PENDING,
                    "provider_dispatched": True, "provider_outcome": "not_applied",
                    "settlement_persisted": False, "recovery_required": True,
                    "second_write": False,
                }
            return {"status": CONFLICT, "state": FAILED_BEFORE_EXECUTION,
                    "reason": "provider_rejected", "detail": str(exc), "second_write": False}
        except BaseException as exc:  # noqa: BLE001 - any dispatch failure is ambiguous
            try:
                self.mark_unknown(operation_id, f"dispatch raised {type(exc).__name__}: {exc}")
            except (CoordinatorUnavailable, IllegalTransition) as settle_exc:
                return {
                    "status": UNKNOWN, "state": PENDING,
                    "reason": "unknown_not_persisted",
                    "detail": f"dispatch raised {type(exc).__name__}: {exc};"
                              f" settlement failed: {settle_exc}",
                    "last_recorded_state": PENDING, "provider_dispatched": True,
                    "settlement_persisted": False, "recovery_required": True,
                    "second_write": None,
                }
            return {"status": UNKNOWN, "state": UNKNOWN, "reason": "dispatch_error",
                    "detail": f"{type(exc).__name__}: {exc}", "second_write": None}

        try:
            self.confirm(operation_id, receipt)
        except (CoordinatorUnavailable, IllegalTransition) as exc:
            # The write landed but the receipt is not durable. Reporting
            # "confirmed" here would be a claim the next reader cannot verify.
            return {
                "status": UNKNOWN,
                "state": PENDING,
                "reason": "receipt_not_persisted",
                "detail": str(exc),
                "observed_receipt": receipt,
                "last_recorded_state": PENDING, "provider_dispatched": True,
                "settlement_persisted": False, "recovery_required": True,
                "second_write": None,
            }
        return {
            "status": CONFIRMED,
            "state": CONFIRMED,
            "receipt": receipt,
            "attempt_id": attempt_id,
            "second_write": True,
        }
