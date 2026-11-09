"""A restorable backup of the coordinator store, and a verified restore.

Why this exists (review P2-2): ``export`` is an audit dump. It omits the
material fingerprint and the rest of the persistence metadata, and nothing can
import it - so a store "restored" from an export could not tell a replay from a
new intent, would forget which objects are unresolved, and would invite a second
external write. The README nevertheless told operators to restore from one.

A coordinator's recovery requirement is specific: after a disk loss, the holds
must still be holds and a confirmed operation must still replay. That is exactly
a physical copy of the store, so that is what ``backup`` produces:

  * **consistent** - SQLite's online backup API takes a snapshot that is valid
    even while another process is writing (WAL), so a backup is not a torn file;
  * **verified** - the copy is reopened, probed for the Concord schema and
    ``PRAGMA quick_check``-ed before it is moved into place, and the file is
    written beside the target and atomically renamed;
  * **restorable** - ``restore`` re-verifies the source, refuses to clobber a
    non-empty store unless told to, and writes atomically.

What it does not do: coordinate across machines, or let you pick records. A
backup is all-or-nothing by design - partially restoring a coordination store is
how a hold disappears.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional

try:  # imported as part of the plugin package
    from . import concord_kernel as K
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_kernel as K

FORMAT = "concord-backup/v1"
_REQUIRED = ("concord_operations", "concord_attempts", "concord_counters")


class BackupError(RuntimeError):
    """A backup or restore that cannot be done honestly. Nothing is written."""

    def __init__(self, detail: str, *, reason: str = "backup_failed") -> None:
        super().__init__(detail)
        self.reason = reason


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        from urllib.parse import quote

        uri = "file:" + quote(str(path), safe="/:\\") + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    return sqlite3.connect(str(path), timeout=5.0, isolation_level=None)


def inspect_store(path: Path) -> dict[str, Any]:
    """Is this file a Concord store, and what does it hold?"""
    path = Path(path)
    out: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_concord_store": False,
        "operations": 0,
        "attempts": 0,
        "unresolved": 0,
        "error": None,
    }
    if not path.exists():
        out["error"] = "no such file"
        return out
    try:
        conn = _connect(path, readonly=True)
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        conn.execute("BEGIN")
        verdict = K._validate_snapshot(conn)
        if verdict.damaged or verdict.creatable:
            out["error"] = verdict.detail or "not a Concord store"
            return out
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = [name for name in _REQUIRED if name not in tables]
        if missing:
            out["error"] = f"not a Concord store (missing {', '.join(missing)})"
            return out
        out["is_concord_store"] = True
        out["operations"] = int(conn.execute("SELECT COUNT(*) FROM concord_operations").fetchone()[0])
        out["attempts"] = int(conn.execute("SELECT COUNT(*) FROM concord_attempts").fetchone()[0])
        out["unresolved"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM concord_operations WHERE state IN"
                " ('reserved','pending','unknown')"
            ).fetchone()[0]
        )
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            out["error"] = f"integrity check failed: {check[0] if check else 'no result'}"
            out["is_concord_store"] = False
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["is_concord_store"] = False
    finally:
        conn.close()
    return out


def _copy(source: Path, target: Path) -> dict[str, Any]:
    """Stage and verify a consistent snapshot before replacing any destination."""
    fd, name = tempfile.mkstemp(prefix=f"{target.name}.concord-tmp-", dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        src = _connect(Path(source), readonly=True)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                # A portable backup is one file, without WAL sidecar dependencies.
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        written = inspect_store(tmp)
        if not written["is_concord_store"]:
            raise BackupError(
                f"the staged copy did not verify ({written['error']}); destination unchanged",
                reason="copy_unverified",
            )
        os.replace(tmp, target)
        written["path"] = str(target)
        return written
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"could not copy {source} to {target}: {exc}") from exc
    finally:
        if tmp.exists():
            tmp.unlink()


def _distinct_paths(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and os.path.samefile(source, target)
    ):
        raise BackupError("source and destination must be different files", reason="same_file")


def backup_store(store_path: Path, target: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Write a restorable copy of the store. Refuses anything it cannot verify."""
    store_path, target = Path(store_path), Path(target)
    _distinct_paths(store_path, target)
    if not store_path.exists():
        raise BackupError(f"no coordinator store at {store_path}; nothing to back up",
                          reason="store_missing")
    if target.exists() and not overwrite:
        raise BackupError(
            f"{target} already exists; refusing to overwrite it", reason="target_exists"
        )
    source = inspect_store(store_path)
    if not source["is_concord_store"]:
        raise BackupError(
            f"{store_path} is not a readable Concord store ({source['error']});"
            " no backup was written",
            reason="store_unreadable",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    written = _copy(store_path, target)
    return {
        "format": FORMAT,
        "source": str(store_path),
        "target": str(target),
        "operations": written["operations"],
        "attempts": written["attempts"],
        "unresolved": written["unresolved"],
    }


def restore_store(source: Path, store_path: Path, *, replace: bool = False) -> dict[str, Any]:
    """Replace the store with a verified backup.

    Refuses to overwrite a store that holds records unless ``replace`` is set:
    restoring over live coordination state is itself a way to lose a hold.
    """
    source, store_path = Path(source), Path(store_path)
    _distinct_paths(source, store_path)
    inspected = inspect_store(source)
    if not inspected["is_concord_store"]:
        raise BackupError(
            f"{source} is not a Concord store ({inspected['error']}); nothing was restored",
            reason="source_invalid",
        )
    if store_path.exists():
        current = inspect_store(store_path)
        if not replace and (not current["is_concord_store"] or current["operations"]):
            raise BackupError(
                f"{store_path} is not a verified empty Concord store; refusing to replace it."
                " Re-run with --replace if the live state is meant to be discarded",
                reason="target_not_empty",
            )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    written = _copy(source, store_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{store_path}{suffix}")
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
    written = inspect_store(store_path)
    if not written["is_concord_store"]:
        raise BackupError(
            f"the restored store at {store_path} did not verify ({written['error']})",
            reason="restore_unverified",
        )
    return {
        "format": FORMAT,
        "source": str(source),
        "target": str(store_path),
        "operations": written["operations"],
        "attempts": written["attempts"],
        "unresolved": written["unresolved"],
        "replaced": bool(replace),
    }
