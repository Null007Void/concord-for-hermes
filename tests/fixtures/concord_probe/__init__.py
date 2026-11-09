"""Concord for Hermes — C1 integration probe plugin.

SYNTHETIC TEST SCAFFOLDING. This is the smallest native plugin that can be
loaded by the real Hermes plugin manager and driven through the real dispatch
paths. It is not the product, holds no provider adapter and makes no network
call. Its only job is to answer the C1 go/no-go question: on the pinned
revision, can a coordination guard placed on a supported seam actually stop a
downstream mutation when the guard fails?

Three seams are registered against one synthetic target:

  * ``pre_tool_call`` hook        — architecture A, seam 1
  * ``tool_execution`` middleware — architecture A, seam 2
  * ``concord_probe_mutate`` tool — architecture B: the check lives inside the
    tool's own dispatch path, so there is no framework seam between the
    admission decision and the write.

Failure behaviour is injected through environment variables so the plugin body
stays deterministic; nothing here is re-exported to the product tree.

Environment contract (all optional except the two paths):
  CONCORD_PROBE_DB              coordinator SQLite path
  CONCORD_PROBE_STORE           synthetic provider SQLite path
  CONCORD_PROBE_TRACE           append-only JSONL of hook/middleware entry
  CONCORD_PROBE_HOOK            off | allow | block | raise | hang | coord_fail
  CONCORD_PROBE_MW              off | allow | block | raise | raise_after_next
  CONCORD_PROBE_HANG_SECONDS    seconds the 'hang' hook sleeps (default 3)
  CONCORD_PROBE_HANG_MARKER     file written AFTER the hang sleep returns
"""

from __future__ import annotations

import json
import os
import time

from .concord_core import (
    ADMITTED,
    REPLAY_CONFIRMED,
    Coordinator,
    CoordinatorUnavailable,
    LocalProvider,
    ProviderUnavailable,
    StaleRevision,
    fingerprint as make_fingerprint,
)

TARGET_TOOL = "concord_probe_write"
GUARDED_TOOL = "concord_probe_mutate"
TOOLSET = "concord_probe"


# ---------------------------------------------------------------- helpers
def _enabled(name: str, default: str = "off") -> str:
    return (os.environ.get(name) or default).strip().lower()


def _trace(record: dict) -> None:
    path = os.environ.get("CONCORD_PROBE_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), **record}, default=str) + "\n")
    except OSError:
        pass


def _busy_ms() -> int:
    """Bounded wait on a locked coordinator/provider (default 5000ms)."""
    try:
        return int(os.environ.get("CONCORD_PROBE_BUSY_MS") or 5000)
    except ValueError:
        return 5000


def _coordinator() -> Coordinator:
    path = os.environ.get("CONCORD_PROBE_DB")
    if not path:
        raise CoordinatorUnavailable("CONCORD_PROBE_DB is not set")
    return Coordinator(path, busy_ms=_busy_ms())


def _provider() -> LocalProvider:
    path = os.environ.get("CONCORD_PROBE_STORE")
    if not path:
        raise ProviderUnavailable("CONCORD_PROBE_STORE is not set")
    return LocalProvider(path, busy_ms=_busy_ms())


def _admission_view(args: dict) -> dict:
    """Read-only admission observation. Deliberately performs NO reservation:
    a pre-hook that mutates is the exact anti-pattern C1 is measuring."""
    object_key = str(args.get("object") or "")
    actor = str(args.get("actor") or "probe")
    holder = _coordinator().holder_of(object_key) if object_key else None
    return {"object": object_key, "actor": actor, "holder": holder}


# ------------------------------------------------------- architecture A: hook
def _pre_tool_guard(tool_name: str = "", args: dict | None = None, **kwargs):
    """pre_tool_call guard. Returns a block directive, or None to proceed."""
    if tool_name != TARGET_TOOL:
        return None
    mode = _enabled("CONCORD_PROBE_HOOK")
    _trace({"seam": "pre_tool_call", "mode": mode, "tool": tool_name})
    if mode in ("off", "allow"):
        return None
    if mode == "block":
        return {"action": "block", "message": "concord: object held by an unresolved operation"}
    if mode == "raise":
        raise RuntimeError("concord probe: pre_tool_call guard failed")
    if mode == "hang":
        seconds = float(os.environ.get("CONCORD_PROBE_HANG_SECONDS") or 3)
        time.sleep(seconds)
        marker = os.environ.get("CONCORD_PROBE_HANG_MARKER")
        if marker:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(f"hook worker finished after {seconds}s at {time.time()}\n")
        _trace({"seam": "pre_tool_call", "mode": "hang", "event": "worker_completed"})
        return {"action": "block", "message": "concord: late block (worker outlived the timeout)"}
    if mode == "coord_fail":
        view = _admission_view(args or {})  # raises CoordinatorUnavailable uncaught
        return {"action": "block", "message": f"concord: {view}"} if view["holder"] else None
    return None


# ------------------------------------------- architecture A: execution middleware
def _execution_guard(tool_name: str = "", args: dict | None = None, next_call=None, **kwargs):
    """tool_execution middleware guard.

    The callback returns a refusal by NOT calling ``next_call`` — that part is
    fail-closed by construction. What C1 measures is what the framework does
    when this callback raises instead.
    """
    if tool_name != TARGET_TOOL:
        return next_call()
    mode = _enabled("CONCORD_PROBE_MW")
    _trace({"seam": "tool_execution", "mode": mode, "tool": tool_name})
    if mode in ("off", "allow"):
        return next_call()
    if mode == "block":
        return json.dumps({"error": "concord: blocked before dispatch", "status": "conflict"})
    if mode == "raise":
        raise RuntimeError("concord probe: tool_execution middleware failed")
    if mode == "raise_after_next":
        result = next_call()
        raise RuntimeError("concord probe: middleware failed after dispatch")
    return next_call()


# ------------------------------------------------- unguarded synthetic target
def _plain_write(args: dict, **kwargs) -> str:
    """The 'existing tool' analogue: mutates with no coordination at all."""
    object_key = str(args["object"])
    value = args.get("value")
    expected = int(args.get("expected_revision", 0))
    _trace({"seam": "tool", "tool": TARGET_TOOL, "object": object_key})
    try:
        receipt = _provider().update(object_key, expected_revision=expected, value=value)
    except StaleRevision as exc:
        return json.dumps({"status": "conflict", "reason": "stale_revision", "detail": str(exc)})
    except ProviderUnavailable as exc:
        return json.dumps({"status": "error", "reason": "provider_unavailable", "detail": str(exc)})
    return json.dumps({"status": "written", "receipt": receipt})


# ------------------------------------ architecture B: self-coordinating tool
def _guarded_mutate(args: dict, **kwargs) -> str:
    """Coordination held inside the tool's own dispatch path.

    The admission decision and the write are in one function body. There is no
    framework seam between them, so no framework-level failure mode can let the
    write happen on an unadmitted operation. Every dependency failure is
    converted to an explicit refusal.
    """
    object_key = str(args["object"])
    operation_id = str(args["operation_id"])
    value = args.get("value")
    expected = int(args.get("expected_revision", 0))
    actor = str(args.get("actor") or "probe")
    fp = make_fingerprint(
        object_key=object_key, operation_id=operation_id, value=value, expected_revision=expected
    )
    _trace({"seam": "tool", "tool": GUARDED_TOOL, "object": object_key, "operation": operation_id})

    try:
        admission = _coordinator().admit(
            object_key=object_key, operation_id=operation_id, fingerprint=fp, actor=actor
        )
    except CoordinatorUnavailable as exc:
        # Fail closed: no admission decision means no mutation.
        return json.dumps({"status": "refused", "reason": "coordinator_unavailable", "detail": str(exc)})

    if admission.kind == REPLAY_CONFIRMED:
        return json.dumps(
            {
                "status": "replayed",
                "reason": admission.reason,
                "receipt": admission.receipt,
                "second_write": False,
            }
        )
    if admission.kind != ADMITTED:
        return json.dumps(
            {"status": admission.kind, "reason": admission.reason, "holder": admission.holder}
        )

    try:
        _coordinator().begin_dispatch(operation_id)
    except CoordinatorUnavailable as exc:
        return json.dumps({"status": "refused", "reason": "coordinator_unavailable", "detail": str(exc)})

    try:
        receipt = _provider().update(object_key, expected_revision=expected, value=value)
    except StaleRevision as exc:
        _safe(lambda: _coordinator().fail_before_execution(operation_id, "stale_revision"))
        return json.dumps({"status": "conflict", "reason": "stale_revision", "detail": str(exc)})
    except ProviderUnavailable as exc:
        # The request may or may not have reached the store: unknown, not failed.
        _safe(lambda: _coordinator().mark_unknown(operation_id, f"provider_unavailable: {exc}"))
        return json.dumps({"status": "unknown", "reason": "provider_unavailable", "detail": str(exc)})

    _safe(lambda: _coordinator().confirm(operation_id, receipt))
    return json.dumps({"status": "confirmed", "receipt": receipt, "second_write": True})


def _safe(fn) -> None:
    """Receipt bookkeeping must never turn a settled provider outcome into a crash."""
    try:
        fn()
    except CoordinatorUnavailable:
        pass


# ------------------------------------------------------------- tool schemas
_WRITE_SCHEMA = {
    "name": TARGET_TOOL,
    "description": "Synthetic unguarded mutation target for the Concord C1 probe.",
    "parameters": {
        "type": "object",
        "properties": {
            "object": {"type": "string", "description": "Canonical object key."},
            "value": {"type": "string", "description": "Replacement value."},
            "expected_revision": {"type": "integer", "description": "Revision the caller saw."},
            "actor": {"type": "string", "description": "Enrolled actor scope."},
        },
        "required": ["object", "value"],
    },
}

_MUTATE_SCHEMA = {
    "name": GUARDED_TOOL,
    "description": "Synthetic self-coordinating mutation tool for the Concord C1 probe.",
    "parameters": {
        "type": "object",
        "properties": {
            "object": {"type": "string"},
            "operation_id": {"type": "string"},
            "value": {"type": "string"},
            "expected_revision": {"type": "integer"},
            "actor": {"type": "string"},
        },
        "required": ["object", "operation_id", "value"],
    },
}


def register(ctx) -> None:
    """Called once by the real PluginManager."""
    ctx.register_tool(
        name=TARGET_TOOL,
        toolset=TOOLSET,
        schema=_WRITE_SCHEMA,
        handler=_plain_write,
        description="Synthetic unguarded mutation target (C1 probe).",
    )
    ctx.register_tool(
        name=GUARDED_TOOL,
        toolset=TOOLSET,
        schema=_MUTATE_SCHEMA,
        handler=_guarded_mutate,
        description="Synthetic self-coordinating mutation tool (C1 probe).",
    )
    ctx.register_hook("pre_tool_call", _pre_tool_guard)
    ctx.register_middleware("tool_execution", _execution_guard)
