"""``hermes concord ...`` - the human surface.

Four subcommands, each doing one thing:

  status     read-only view: what is held, by whom, what is safe next
  reconcile  settle one unresolved operation from a provider READ (never a write)
  cancel     withdraw an intent that was reserved but never dispatched
  export     dump every operation and attempt as JSON, for keeping or moving

The help text states the coverage boundary, because a command whose output can
be mistaken for "everything is protected" would be worse than no command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # imported as part of the plugin package
    from . import concord_backup as B
    from . import concord_gh_adapter as A
    from . import concord_kernel as K
    from . import concord_status as ST
    from . import concord_store as S
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_backup as B
    import concord_gh_adapter as A
    import concord_kernel as K
    import concord_status as ST
    import concord_store as S

DESCRIPTION = """\
Inspect and recover Concord's coordinated GitHub writes.

Protected: calls to the concord_github_write tool, for a file that already
exists, on one branch of an allowlisted repository.
NOT PROTECTED: every other tool, shell commands, browser and raw HTTP actions,
direct `gh` use, a disabled plugin, profiles that have not enabled Concord, and
other machines. Concord records only its own calls - `status` shows those, and
an empty result does not mean nobody wrote to the file.
"""


def build_parser(subparser: argparse.ArgumentParser) -> None:
    """argparse tree for ``hermes concord``."""
    subs = subparser.add_subparsers(dest="concord_command")

    status = subs.add_parser(
        "status",
        help="Show held objects, holders and safe next actions (read-only)",
        description=(
            "List unresolved operations per enrolled object with their holder and safe"
            " next actions, plus recently recorded refusals. Read-only: the connection is"
            " opened mode=ro and never takes a write lock."
        ),
    )
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("--limit", type=int, default=10, help="refusals/confirmed rows to show")

    reconcile = subs.add_parser(
        "reconcile",
        help="Settle an unresolved operation from a provider read (never re-sends the write)",
        description=(
            "Read the target file and settle one unresolved operation: confirmed if the"
            " content and commit belong to it, still unknown otherwise. A blob that has not"
            " moved is not proof that nothing was applied - a timed-out request can land"
            " later - so a dispatched operation is only released on attributable application"
            " or an authoritative provider negative. This command performs no write and never"
            " retries the original action."
        ),
    )
    reconcile.add_argument("operation_id")
    reconcile.add_argument(
        "--assume-dead-process",
        action="store_true",
        help=(
            "required for a pending operation: assert that the process which owned it is"
            " gone. Without it, reconcile refuses a pending operation because a dispatch"
            " may still be in flight. This unlocks the read, not a release: an unchanged"
            " target is still not proof that the request will not land, so a dispatched"
            " operation is only released when its application is attributable or the"
            " provider states authoritatively that nothing was applied."
        ),
    )
    reconcile.add_argument("--json", action="store_true", help="machine-readable output")

    cancel = subs.add_parser(
        "cancel",
        help="Withdraw an intent that was reserved but never dispatched",
        description=(
            "Only a reserved operation can be cancelled - once a dispatch may have started,"
            " there is nothing to cancel and the outcome must be settled from provider"
            " evidence. No provider call is made."
        ),
    )
    cancel.add_argument("operation_id")
    cancel.add_argument("--reason", default="cancelled by operator")
    cancel.add_argument("--json", action="store_true", help="machine-readable output")

    export = subs.add_parser(
        "export",
        help="Write an audit dump (identities, states, receipts, attempts) to JSON",
        description=(
            "Read-only AUDIT dump of the coordinator store. It is not a backup: it omits"
            " the material fingerprint and cannot be imported. Use `concord backup` for"
            " a restorable copy."
        ),
    )
    export.add_argument("--output", default="", help="file to write (default: stdout)")

    backup = subs.add_parser(
        "backup",
        help="Write a restorable, verified copy of the coordinator store",
        description=(
            "Consistent copy of the store via SQLite's backup API, verified before it is"
            " moved into place. Preserves unresolved holds, operation identities,"
            " fingerprints and receipts - everything `export` does not."
        ),
    )
    backup.add_argument("--output", required=True, help="where to write the backup")
    backup.add_argument("--force", action="store_true", help="overwrite an existing backup file")

    restore = subs.add_parser(
        "restore",
        help="Replace the coordinator store with a verified backup",
        description=(
            "Refuses a file that is not a Concord store, and refuses to replace a store"
            " that still holds operations unless --replace is given. Stop other Concord"
            " processes first: restoring over live state is a way to lose a hold."
        ),
    )
    restore.add_argument("--input", required=True, help="the backup to restore")
    restore.add_argument(
        "--replace",
        action="store_true",
        help="replace a non-empty store (the live records are discarded)",
    )

    subparser.set_defaults(func=make_handler(None))


def make_handler(ctx: Any):
    """Bind the plugin context (settings source) into the argparse handler."""

    def handler(args: argparse.Namespace) -> int:
        action = getattr(args, "concord_command", None)
        if not action:
            print("Usage: hermes concord {status|reconcile|cancel|backup|restore|export}")
            return 2
        if action == "status":
            return _status(args, ctx)
        if action == "reconcile":
            return _reconcile(args, ctx)
        if action == "cancel":
            return _cancel(args, ctx)
        if action == "export":
            return _export(args, ctx)
        if action == "backup":
            return _backup(args, ctx)
        if action == "restore":
            return _restore(args, ctx)
        print(f"Unknown concord command: {action}")
        return 2

    return handler


def _settings(ctx: Any, *, create: bool = False) -> S.Settings:
    """Resolve settings. ``create`` is true only on the write paths, so `status`
    and `export` leave a fresh home completely untouched."""
    store = S.store_path(create=create)
    if ctx is not None:
        return S.settings_from_ctx(ctx, store=store)
    return S.resolve_settings({}, store=store)


def _status(args: argparse.Namespace, ctx: Any) -> int:
    settings = _settings(ctx)
    snap = ST.read_snapshot(settings.store_path)
    status = {"allowed_repositories": settings.allowed_slugs}
    if getattr(args, "json", False):
        model = ST.export_model(snap)
        model["format"] = "concord-status/v1"
        model["allowlist"] = settings.allowed_slugs
        model["objects"] = [
            {
                "object_key": group["object_key"],
                "holder": group["holder"],
                "holder_state": group["holder_state"],
                "operations": group["operations"],
            }
            for group in snap.objects()
        ]
        model["refusals"] = snap.refusals(limit=getattr(args, "limit", 10))
        print(json.dumps(model, indent=2, ensure_ascii=False, default=str))
        # A machine consumer must not read an unreadable store as "nothing held"
        # (review P2-1): the payload still names the error, and the exit code
        # says not to trust the empty arrays.
        return 0 if snap.healthy else 1
    print(ST.render_text(snap, status=status, limit=getattr(args, "limit", 10)))
    return 0 if snap.healthy else 1


def _reconcile(args: argparse.Namespace, ctx: Any) -> int:
    operation_id = str(args.operation_id).strip()
    settings = _settings(ctx, create=True)
    try:
        coordinator = K.Coordinator(
            str(settings.store_path), normalize_object_key=A.canonical_object_key
        )
    except K.CoordinatorUnavailable as exc:
        return _fail(f"coordinator unavailable ({exc}); nothing was read or changed")
    row = coordinator.get(operation_id)
    if row is None:
        return _fail(f"no operation {operation_id!r} in {settings.store_path}")
    if row["state"] not in K.BLOCKING_STATES:
        return _already_settled(row, json_mode=getattr(args, "json", False))
    if row["state"] == K.PENDING and not getattr(args, "assume_dead_process", False):
        return _fail(
            f"{operation_id} is pending: a dispatch may still be in flight. A read that shows"
            " the file unchanged would not prove it will not land. If the process that owned"
            " this operation is gone, re-run with --assume-dead-process."
        )

    try:
        adapter = S.build_adapter(settings)
        target = adapter.validate_target(row["object_key"])
        evidence = adapter.read(target)
    except Exception as exc:  # AdapterError and anything a bad config raises
        return _fail(f"could not read provider evidence ({getattr(exc, 'reason', 'error')}): {exc}")

    try:
        result = coordinator.reconcile(
            operation_id, evidence, evaluate=A.opaque_evidence_verdict,
            require_release_proof=True,
        )
    except (K.IllegalTransition, K.CoordinatorUnavailable, ValueError) as exc:
        return _fail(f"could not settle {operation_id}: {exc}")

    state = result["state"]
    payload = {
        "operation_id": operation_id,
        "object_key": row["object_key"],
        "state": state,
        "held": state in K.BLOCKING_STATES,
        "evidence": result.get("evidence"),
        "wrote": False,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0
    if state == K.CONFIRMED:
        print(
            f"{operation_id}: confirmed from provider evidence - the content and commit are"
            " this operation's. The object is released and the original write is not repeated."
        )
    elif state == K.FAILED_BEFORE_EXECUTION:
        print(
            f"{operation_id}: failed before execution - the provider states authoritatively"
            " that nothing was applied, or the provider refused the write outright. The"
            " object is released; a new intent needs a new operation id after a fresh read."
        )
    else:
        print(
            f"{operation_id}: still unknown - the provider cannot attribute the current state to"
            " this operation. An unchanged file is not proof that the request will not land, so"
            " the object stays held and no retry was attempted. If the application appears later,"
            " reconcile again: it settles as confirmed when the commit carries this operation's"
            " marker."
        )
    return 0


def _cancel(args: argparse.Namespace, ctx: Any) -> int:
    operation_id = str(args.operation_id).strip()
    settings = _settings(ctx, create=True)
    try:
        coordinator = K.Coordinator(
            str(settings.store_path), normalize_object_key=A.canonical_object_key
        )
    except K.CoordinatorUnavailable as exc:
        return _fail(f"coordinator unavailable ({exc}); nothing was changed")
    try:
        result = coordinator.cancel(operation_id, str(args.reason))
    except K.IllegalTransition as exc:
        return _fail(f"cannot cancel: {exc}")
    payload = {
        "outcome": "cancelled",
        "operation_id": operation_id,
        "state": result["state"],
        "reason": str(args.reason),
        "note": "no dispatch had started; nothing was sent",
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"cancelled {operation_id}: state={result['state']} (nothing was sent)")
    return 0


def _export(args: argparse.Namespace, ctx: Any) -> int:
    settings = _settings(ctx)
    snap = ST.read_snapshot(settings.store_path)
    if not snap.healthy:
        # Refuse to write a file that would look like a complete dump
        # (review P2-1). The error goes to stderr; nothing is created.
        return _fail(
            f"the coordinator store at {snap.store_path} cannot be read completely"
            f" ({snap.error or snap.attempts_error}); no export was written"
        )
    model = ST.export_model(snap)
    model["allowlist"] = settings.allowed_slugs
    text = json.dumps(model, indent=2, ensure_ascii=False, default=str)
    target = str(getattr(args, "output", "") or "").strip()
    if not target:
        print(text)
        return 0
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write {path}: {exc}")
    print(
        f"exported {len(model['operations'])} operation(s) and {len(model['attempts'])}"
        f" attempt(s) to {path}   # an audit dump, not a restorable backup"
    )
    return 0


def _backup(args: argparse.Namespace, ctx: Any) -> int:
    settings = _settings(ctx)
    try:
        made = B.backup_store(
            settings.store_path, Path(str(args.output)), overwrite=bool(getattr(args, "force", False))
        )
    except B.BackupError as exc:
        return _fail(f"{exc.reason}: {exc}")
    print(
        f"backed up {made['operations']} operation(s) and {made['attempts']} attempt(s) to"
        f" {made['target']} ({made['unresolved']} unresolved, preserved)"
    )
    return 0


def _restore(args: argparse.Namespace, ctx: Any) -> int:
    settings = _settings(ctx)
    try:
        done = B.restore_store(
            Path(str(args.input)), settings.store_path, replace=bool(getattr(args, "replace", False))
        )
    except B.BackupError as exc:
        return _fail(f"{exc.reason}: {exc}")
    print(
        f"restored {done['operations']} operation(s) and {done['attempts']} attempt(s) into"
        f" {done['target']} ({done['unresolved']} unresolved)"
    )
    return 0


def _already_settled(row: dict[str, Any], *, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps({"operation_id": row["operation_id"], "state": row["state"],
                          "note": "operation is already settled; nothing to reconcile"}, indent=2))
    else:
        print(
            f"{row['operation_id']} is already {row['state']}; nothing to reconcile."
        )
    return 0


def _fail(message: str) -> int:
    print(f"concord: {message}", file=sys.stderr)
    return 1
