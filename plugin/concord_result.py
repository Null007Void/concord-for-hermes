"""The five result shapes a Concord tool call can return.

One outcome vocabulary, five shapes, no overlap. The kernel already returns a
status dict; this module is the *presentation* contract on top of it, and it is
deliberately a pure function so the shape rules can be tested without a store,
a provider or a plugin runtime.

Why a separate module: the kernel answers "what happened to the operation"; a
model (and the CLI) needs "what is true now, what may I do next, and what must I
not do". Those are different questions, and a caller that has to infer the
second from a status string is exactly the ambiguity Concord exists to remove.

The shapes, and the field that makes each one impossible to confuse with another:

  confirmed  second_write=true, receipt present, recorded=true
  replayed   second_write=false, receipt present-or-pruned, recorded=true
  conflict   recorded=true, held=false, holder and next_action present
  unknown    held=true, second_write=null, reconcile command present
  refused    recorded=false, held=false, no receipt, no holder

Every shape carries ``outcome`` as an explicit discriminator, so no consumer has
to guess from a nullable field.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

CONFIRMED = "confirmed"
REPLAYED = "replayed"
CONFLICT = "conflict"
UNKNOWN = "unknown"
REFUSED = "refused"

OUTCOMES = (CONFIRMED, REPLAYED, CONFLICT, UNKNOWN, REFUSED)

#: What a caller may do next, as a closed set. The wording of each action lives
#: in :func:`next_action_for`, so the tool and the CLI cannot drift apart.
NEW_OPERATION = "create-new-reviewed-operation"
RECONCILE = "reconcile"
CANCEL = "cancel-if-reserved"
FIX_AND_RETRY = "fix-and-retry"
RECOVER_STORE = "inspect-and-recover-store"
NONE = "none"


def reconcile_command(operation_id: str) -> str:
    return f"hermes concord reconcile {operation_id}"


def status_command() -> str:
    return "hermes concord status"


def next_action_for(kind: str, *, operation_id: str, detail: str = "") -> dict[str, Any]:
    """One safe next action, named and runnable. Never an automatic retry."""
    if kind == RECONCILE:
        return {
            "kind": RECONCILE,
            "why": detail or "settle the operation from a provider read; never re-sends the write",
            "how": reconcile_command(operation_id),
        }
    if kind == RECOVER_STORE:
        return {
            "kind": RECOVER_STORE,
            "why": detail or "stop writes; inspect and recover the store before"
                   " reconciling or cancelling this operation. Do not retry it.",
            "how": status_command(),
        }
    if kind == CANCEL:
        return {
            "kind": CANCEL,
            "why": detail or "the intent was reserved but never dispatched, so it can be withdrawn",
            "how": f"hermes concord cancel {operation_id}",
        }
    if kind == NEW_OPERATION:
        return {
            "kind": NEW_OPERATION,
            "why": detail
            or "this intent is closed; a fresh intent needs a new operation id after a fresh read",
            "how": f"{status_command()}  # then call concord_github_write with a new operation_id",
        }
    if kind == FIX_AND_RETRY:
        return {
            "kind": FIX_AND_RETRY,
            "why": detail or "nothing was sent; correct the arguments or configuration and call again",
            "how": "no record was written for this call",
        }
    return {"kind": NONE, "why": detail or "", "how": ""}


def _receipt_view(
    receipt: Optional[Mapping[str, Any]],
    *,
    object_key: str,
    operation_id: str,
    reconciled: bool,
) -> Optional[dict[str, Any]]:
    """The receipt as a model/CLI should read it: identity first, provenance last."""
    if not receipt:
        return None
    view = {
        "object_key": object_key,
        "operation_id": operation_id,
        "reconciled": bool(reconciled),
    }
    for key, value in receipt.items():
        if key not in view:
            view[key] = value
    # A reconcile receipt carries ``revision``; a write receipt carries
    # ``blob_sha``. Present one name for "what the provider now holds".
    if "blob_sha" not in view and view.get("revision") is not None:
        view["blob_sha"] = view["revision"]
    return view


def present(
    raw: Mapping[str, Any],
    *,
    object_key: str,
    operation_id: str,
) -> dict[str, Any]:
    """Map one kernel outcome to exactly one public shape."""
    status = str(raw.get("status") or "")
    state = raw.get("state")

    if status == CONFIRMED:
        return {
            "outcome": CONFIRMED,
            "state": CONFIRMED,
            "object_key": object_key,
            "operation_id": operation_id,
            "recorded": True,
            "held": False,
            "second_write": True,
            "receipt": _receipt_view(
                raw.get("receipt"), object_key=object_key, operation_id=operation_id,
                reconciled=False,
            ),
            "next_action": next_action_for(NONE, operation_id=operation_id),
        }

    if status in ("replayed", "replayed_pruned"):
        pruned = status == "replayed_pruned" or bool(raw.get("pruned"))
        receipt = raw.get("receipt")
        # A pruned receipt still means the write happened. Recommending a new
        # operation here would be recommending a second external effect for work
        # that is already recorded as applied (review carry-forward), so the
        # safe next action is "nothing" plus the reason.
        action = next_action_for(
            NONE,
            operation_id=operation_id,
            detail=(
                "this operation is recorded as confirmed and applied; its receipt payload"
                " was pruned by the retention policy. Do not repeat the write - the"
                " evidence is gone by policy, not missing"
            ),
        ) if pruned else next_action_for(NONE, operation_id=operation_id)
        return {
            "outcome": REPLAYED,
            "state": CONFIRMED,
            "object_key": object_key,
            "operation_id": operation_id,
            "recorded": True,
            "held": False,
            "second_write": False,
            "pruned": pruned,
            "receipt": None
            if pruned
            else _receipt_view(
                receipt, object_key=object_key, operation_id=operation_id,
                reconciled=bool((receipt or {}).get("reconciled")),
            ),
            "next_action": action,
            "reason": raw.get("reason") or "identical operation already confirmed; no second write",
        }

    if status == CONFLICT or (
        status == REFUSED
        and raw.get("reason") in ("argument_mismatch", "object_mismatch")
    ):
        holder = raw.get("holder")
        # Five different histories land here: another operation holds the object,
        # this operation is unresolved, the provider refused the conditional
        # write, this id is already recorded with different material, or this id
        # is already recorded for a DIFFERENT object (owner F9-1). ``state``
        # names which (``argument_mismatch`` and ``object_mismatch`` are the two
        # values that classify the recorded row rather than naming a kernel state);
        # the next action follows the state, never a guess.
        settled_state = str(state or (
            CONFLICT if status == CONFLICT else str(raw.get("reason") or "argument_mismatch")
        ))
        if settled_state == "failed_before_execution":
            action = next_action_for(
                NEW_OPERATION,
                operation_id=operation_id,
                detail=raw.get("detail")
                or "the provider refused the write, so nothing changed and this intent is closed",
            )
        elif settled_state == "argument_mismatch":
            action = next_action_for(
                NEW_OPERATION,
                operation_id=operation_id,
                detail=raw.get("detail")
                or "this operation id already names different content; a new intent needs a new id",
            )
        elif settled_state == "object_mismatch":
            action = next_action_for(
                NEW_OPERATION,
                operation_id=operation_id,
                detail=raw.get("detail")
                or "this operation id already names a different object; read the new object and"
                " start a new operation id - an identity is never inherited across objects",
            )
        elif settled_state == "cancelled":
            action = next_action_for(
                NEW_OPERATION,
                operation_id=operation_id,
                detail="a cancelled intent is closed; use a new operation id",
            )
        elif holder == operation_id:
            action = next_action_for(
                RECONCILE,
                operation_id=operation_id,
                detail="this operation id is already unresolved; settle it before reusing it",
            )
        else:
            action = next_action_for(
                NEW_OPERATION,
                operation_id=operation_id,
                detail=(
                    f"the object is held by {holder}; wait for that operation to settle, "
                    "or reconcile it, then start a new intent"
                ),
            )
        return {
            "outcome": CONFLICT,
            "state": settled_state,
            "object_key": object_key,
            "operation_id": operation_id,
            "recorded": True,
            "held": bool(holder),
            "holder": holder,
            "reason": raw.get("reason") or "conflict",
            "detail": raw.get("detail"),
            "refusal_row": {
                "operation_id": operation_id,
                "state": settled_state,
                "holder": holder,
            },
            "second_write": False,
            "next_action": action,
        }

    if status == UNKNOWN:
        recovery = bool(raw.get("recovery_required"))
        extra = {key: raw[key] for key in (
            "last_recorded_state", "provider_dispatched", "provider_outcome",
            "settlement_persisted", "storage_reason",
        ) if key in raw}
        return {
            **extra,
            "recovery_required": recovery,
            "outcome": UNKNOWN,
            "state": str(state or UNKNOWN),
            "object_key": object_key,
            "operation_id": operation_id,
            "recorded": True,
            "held": True,
            "auto_retry": False,
            "second_write": raw.get("second_write"),
            "reason": raw.get("reason") or "outcome_not_determinable",
            "detail": raw.get("detail"),
            "observed_receipt": raw.get("observed_receipt"),
            "next_action": next_action_for(
                RECOVER_STORE if recovery else RECONCILE,
                operation_id=operation_id,
                detail=(
                    "the recorded reservation or settlement could not be verified;"
                    " stop writes, inspect and recover the store, then reconcile or"
                    " cancel only if still reserved. Do not retry automatically"
                    if recovery else
                    "the write may or may not have applied; only a provider read may settle it"
                ),
            ),
        }

    # Everything else is a refusal: no admission, no record, nothing sent.
    return {
        "outcome": REFUSED,
        "state": None,
        "object_key": object_key,
        "operation_id": operation_id,
        "recorded": False,
        "held": False,
        "second_write": False,
        "reason": raw.get("reason") or "refused",
        "detail": raw.get("detail"),
        "next_action": next_action_for(
            FIX_AND_RETRY, operation_id=operation_id, detail=raw.get("detail") or ""
        ),
    }


def signature(shape: Mapping[str, Any]) -> tuple:
    """The semantic fingerprint used to prove the shapes are distinguishable."""
    return (
        shape.get("outcome"),
        bool(shape.get("recorded")),
        bool(shape.get("held")),
        shape.get("second_write"),
        shape.get("receipt", "absent") is not None,
    )
