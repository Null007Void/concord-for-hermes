"""The model-facing tool: ``concord_github_write``.

One frame, architecture B: argument validation, allowlist and version gates, the
base-revision read, admission, the provider write, the read-back and the
settlement all happen inside this handler. There is no framework seam between
the admission decision and the write, so a coordinator failure is a refusal in
the same frame that would have performed the write (C1 verdict).

The model surface is six fields on purpose: repository, path, branch,
operation_id, expected_revision, content. ``expected_revision`` is the blob SHA
the caller READ before composing the content; it is the base the write is
conditioned on and part of the operation's identity. The tool does not read a
fresh base at dispatch and use it instead - that is what let two editors
prepared from the same revision both report ``confirmed`` while one edit was
destroyed (review P1-1).

Consequences worth stating:

  * a draft prepared from a superseded revision CONFLICTS. It is never silently
    rebased onto the current blob; the caller must re-read and start a new
    operation;
  * a replay costs no provider read at all when the operation is already
    recorded locally, so a durable receipt survives a missing or unreachable
    provider;
  * if someone else moves the file, the provider refuses the conditional write
    (HTTP 409) and the operation settles as ``failed_before_execution``; the
    safe next step is a new operation after a fresh read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

try:  # imported as part of the plugin package
    from . import concord_gh_adapter as A
    from . import concord_kernel as K
    from . import concord_result as R
    from . import concord_store as S
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_gh_adapter as A
    import concord_kernel as K
    import concord_result as R
    import concord_store as S

TOOL_NAME = "concord_github_write"
TOOLSET = "concord"

#: A single write is a text file; the bound keeps one call from turning into an
#: unbounded transfer. Stated in the README.
MAX_CONTENT_BYTES = 1024 * 1024

_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
#: A revision token is opaque to this tool (a blob SHA in practice). The bound
#: exists so a pathological argument cannot be stored, not to validate the shape.
_MAX_REVISION_CHARS = 200

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Replace the content of an existing file on a GitHub branch, coordinated across "
        "sessions. Concord reserves the file, writes with GitHub's own conditional-write "
        "check, verifies the result by reading it back, and answers with exactly one of: "
        "confirmed (receipt), replayed (same operation_id, no second write), conflict "
        "(someone else holds the file, or the file moved), unknown (the write may have "
        "applied - held, never retried automatically) or refused (nothing was sent). "
        "Treat operation_id as an idempotence key: reuse it only to repeat the same intent. "
        "Creating a new file is not supported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repository": {
                "type": "string",
                "description": "Repository as owner/repo, e.g. 'octo/notes'. Must be in the configured allowlist.",
            },
            "path": {
                "type": "string",
                "description": "Repository-relative path of the file to replace, e.g. 'docs/plan.md'. The file must already exist.",
            },
            "branch": {
                "type": "string",
                "description": "Branch the file is on, e.g. 'main'.",
            },
            "operation_id": {
                "type": "string",
                "description": (
                    "Stable idempotence key for this intent, e.g. 'plan-md-2026-11-05-a'. "
                    "The same id, content and expected_revision returns the original receipt "
                    "without writing again; a different content, a different observed base or a "
                    "different file/branch/repository under the same id is refused; a new intent "
                    "needs a new id."
                ),
            },
            "expected_revision": {
                "type": "string",
                "description": (
                    "The blob SHA of the file as you read it before composing `content`: "
                    "GET /repos/{owner}/{repo}/contents/{path}?ref={branch} and take `.sha`. "
                    "The write is conditioned on this exact revision. If the file has moved "
                    "since you read it, the call conflicts - read it again and start a new "
                    "operation; the tool will not rebase your draft onto newer content."
                ),
            },
            "content": {
                "type": "string",
                "description": "The complete new UTF-8 text of the file (at most 1 MiB).",
            },
        },
        "required": [
            "repository", "path", "branch", "operation_id", "expected_revision", "content"
        ],
    },
}


class InvalidArguments(ValueError):
    """The call cannot be turned into a canonical object at all."""

    reason = "invalid_arguments"


class WorkflowChangeRefused(InvalidArguments):
    """The target is a GitHub Actions workflow, which this tool never writes.

    The agent-facing skill promised this refusal before the code performed it
    (review P2-3). It is a promise kept in the refusal path now, and the
    adapter enforces the same boundary so a hand-built key cannot address a
    workflow either.
    """

    reason = "workflow_change_not_supported"


_WORKFLOW_PARTS = (".github", "workflows")


def is_workflow_path(path: str) -> bool:
    """Is this path inside the repository's GitHub Actions workflow directory?

    Case-insensitive on the directory names on purpose: in git those are
    different files, but the protection should be one rule, and a request aimed
    at a workflow by spelling is still a request to change a workflow.
    """
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    return len(parts) >= 2 and tuple(part.lower() for part in parts[:2]) == _WORKFLOW_PARTS


def parse_arguments(args: Mapping[str, Any]) -> dict[str, str]:
    """Validate the six fields and build the canonical object key.

    Everything here is a refusal *before* any record exists: a malformed call
    never reserves an object.
    """
    if not isinstance(args, Mapping):
        raise InvalidArguments("arguments must be an object")

    def text(key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InvalidArguments(f"'{key}' is required and must be a non-empty string")
        return value

    repository = text("repository").strip()
    if not _REPOSITORY_RE.match(repository):
        raise InvalidArguments("'repository' must be 'owner/repo'")
    owner, _, repo = repository.partition("/")
    path = text("path").strip()
    branch = text("branch").strip()
    operation_id = text("operation_id").strip()
    if not _OPERATION_RE.match(operation_id):
        raise InvalidArguments(
            "'operation_id' must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    expected_revision = text("expected_revision").strip()
    if len(expected_revision) > _MAX_REVISION_CHARS or any(
        ch.isspace() or ord(ch) < 32 for ch in expected_revision
    ):
        raise InvalidArguments(
            "'expected_revision' must be the revision token you read (no whitespace,"
            f" at most {_MAX_REVISION_CHARS} characters)"
        )
    content = args.get("content")
    if not isinstance(content, str):
        raise InvalidArguments("'content' is required and must be a string")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidArguments("'content' must be valid UTF-8 text") from exc
    if len(encoded) > MAX_CONTENT_BYTES:
        raise InvalidArguments(
            f"'content' is {len(encoded)} bytes; this tool writes at most {MAX_CONTENT_BYTES}"
        )
    if not branch or any(ch.isspace() for ch in branch):
        raise InvalidArguments("'branch' must be a ref name without whitespace")
    if not A.canonical_branch(branch):
        raise InvalidArguments("'branch' must be a ref name, not only a 'refs/heads/' prefix")
    if path.startswith("/") or path.endswith("/") or ".." in path.split("/"):
        raise InvalidArguments("'path' must be relative and may not traverse")
    if is_workflow_path(path):
        raise WorkflowChangeRefused(
            f"'{path}' is a GitHub Actions workflow. Workflow changes are outside this"
            " tool's action family and are refused before anything is read, recorded or"
            " sent - use a reviewed change outside an agent session"
        )
    # One provider object, one identity (review P1-2): the store key is built
    # from the canonical form, so `OCTO/Disposable@refs/heads/main` and
    # `octo/disposable@main` are the same reservation.
    object_key = A.canonical_key(owner, repo, branch, path)
    return {
        "object_key": object_key,
        "repository": repository,
        "path": path,
        "branch": branch,
        "operation_id": operation_id,
        "expected_revision": expected_revision,
        "content": content,
    }


def _observed_base(coordinator, adapter, target, operation_id: str) -> tuple[Any, Any, bool]:
    """What the provider holds now, for the existence check and the staleness check.

    Returns ``(blob_revision, commit_reference, exists)``. A recorded operation
    answers from its own row instead: a replay must not depend on the provider
    being readable, and the recorded base is the one the operation is bound to.
    """
    existing = coordinator.get(operation_id)
    if existing is not None:
        return existing["base_revision"], existing.get("base_identity"), True
    evidence = adapter.read(target)
    return evidence.get("revision"), evidence.get("commit_sha"), bool(evidence.get("exists"))


def _no_dispatch() -> dict[str, Any]:
    """A mutate callable for a call that can only replay or conflict.

    Reached only if the kernel were to admit, which the caller has already
    ruled out from the recorded state - so this is an assertion, not a policy.
    """
    raise AssertionError("a recorded operation must not dispatch a write")


def run_write(
    args: Mapping[str, Any],
    *,
    settings: S.Settings,
    actor: str = "unknown",
) -> dict[str, Any]:
    """The whole dispatch frame, returning one public shape. Never raises."""
    try:
        call = parse_arguments(args)
    except InvalidArguments as exc:
        return R.present(
            {
                "status": "refused",
                "reason": getattr(exc, "reason", "invalid_arguments"),
                "detail": str(exc),
            },
            object_key="", operation_id=str(args.get("operation_id") or "") if isinstance(args, Mapping) else "",
        )

    object_key = call["object_key"]
    operation_id = call["operation_id"]
    try:
        coordinator = K.Coordinator(
            str(settings.store_path), normalize_object_key=A.canonical_object_key
        )
    except K.CoordinatorUnavailable as exc:
        return R.present(K.refusal(exc), object_key=object_key, operation_id=operation_id)

    # Durable local identity BEFORE provider availability (review P2-4). A
    # recorded operation is answered from the store: an already-confirmed
    # receipt must not become a refusal because gh is missing, old or offline,
    # and an id reused with different material is refused locally too.
    #
    # Identity is decided before compatibility (owner F9-1): the record must
    # address THIS object - exactly, or canonically equivalent through the same
    # canonicaliser the key was built with - before its digest is consulted. The
    # digest relaxation below exists for an older spelling of one object; it can
    # never make two objects one.
    #
    # "Same material" includes the spelling the record's digest was computed
    # over (owner F8-C): a store written before the identity fix holds a digest
    # of the caller's original key, and the same call today canonicalises it.
    material = {
        "object": object_key,
        "value": call["content"],
        "expected_revision": call["expected_revision"],
    }
    try:
        existing = coordinator.get(operation_id)
    except K.CoordinatorUnavailable as exc:
        return R.present(K.refusal(exc), object_key=object_key, operation_id=operation_id)
    same_object = existing is None or K.object_key_matches(
        existing["object_key"], object_key, A.canonical_object_key
    )
    if existing is not None and (
        not same_object
        or not K.fingerprint_matches(
            material, existing["fingerprint"],
            fingerprint_object=existing.get("fingerprint_object"),
        )
        or existing["state"] not in (K.FAILED_BEFORE_EXECUTION, K.CONFLICT)
    ):
        raw = coordinator.run(
            operation_id=operation_id,
            actor=actor,
            received_args=material,
            revision_model="opaque",
            mutate=_no_dispatch,
        )
        return R.present(raw, object_key=object_key, operation_id=operation_id)

    try:
        adapter = S.build_adapter(settings)
        target = adapter.validate_target(object_key)
        adapter.ensure_ready()
    except Exception as exc:  # AdapterError and anything a bad config raises
        return R.present(_refusal_from(exc), object_key=object_key, operation_id=operation_id)

    try:
        observed, observed_commit, exists = _observed_base(
            coordinator, adapter, target, operation_id
        )
    except Exception as exc:
        return R.present(_refusal_from(exc), object_key=object_key, operation_id=operation_id)
    if not exists or observed is None or str(observed).strip() == "":
        return R.present(
            {
                "status": "refused",
                "reason": "missing_base_revision",
                "detail": (
                    f"no file exists at {call['path']} on {call['branch']}; creating a new "
                    "file is outside this action family and nothing was sent"
                ),
            },
            object_key=object_key,
            operation_id=operation_id,
        )

    raw = A.guarded_write(
        coordinator,
        adapter,
        object_key=object_key,
        operation_id=operation_id,
        value=call["content"],
        expected_revision=call["expected_revision"],
        observed_base=observed,
        base_identity=observed_commit,
        actor=actor,
    )
    return R.present(raw, object_key=object_key, operation_id=operation_id)


def _refusal_from(exc: BaseException) -> dict[str, Any]:
    """Turn a pre-admission dependency failure into a refusal shape."""
    reason = getattr(exc, "reason", None) or "unavailable"
    return {"status": "refused", "reason": reason, "detail": f"{type(exc).__name__}: {exc}"}


def handle_write(args: Mapping[str, Any], ctx: Any = None, **_: Any) -> str:
    """The registered handler. Returns the result JSON as text."""
    settings = S.settings_from_ctx(ctx) if ctx is not None else S.resolve_settings({})
    actor = "unknown"
    profile = getattr(ctx, "profile_name", None) if ctx is not None else None
    if profile:
        actor = f"profile:{profile}"
    shape = run_write(args, settings=settings, actor=actor)
    return json.dumps(shape, ensure_ascii=False, sort_keys=False)
