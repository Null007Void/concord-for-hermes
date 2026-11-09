"""Concord C3 - the GitHub Contents adapter, over the ``gh`` CLI.

Scope: transport plus the two target-specific predicates. This module owns
nothing about admission, identity or retention - those live in
``concord_kernel`` and are called, not re-implemented.

Design constraints, each one enforced below rather than described:

  * **One canonical object key**: ``github:{owner}/{repo}@{branch}:{path}``.
    The key is the address; the adapter parses it and refuses anything it
    cannot address exactly.
  * **The disposable-repository allowlist is adapter configuration**, not a
    model argument. There is no field in any payload or key that can widen it.
  * **The request body goes through ``--input``, a temp file.** File content
    and commit message never appear in argv, so they never appear in a process
    listing. The adapter does not read, copy, log or persist a credential: the
    environment is inherited unread and ``gh`` resolves its own token.
  * **A minimum ``gh`` version, checked and failing closed.** An unparseable or
    old version is a refusal, never a "probably fine".
  * **Receipt fields** ``content.sha`` and ``commit.sha`` follow the GitHub
    Contents API response. They were exercised by offline fixtures and observed
    in two private owner-operated write/replay demonstrations. This is narrow
    live evidence; not every provider failure or race was exercised live.

Error discipline. The kernel distinguishes two classes: a provider refusal that
proves nothing was applied, and everything else. This adapter only raises
``Rejected`` when it can prove nothing was applied (spawn failure, version gate,
allowlist refusal, 401/403/404/409/422 before the write) and raises
``AdapterAmbiguous`` whenever a request may have reached the provider (timeout,
5xx, output that cannot be parsed). That distinction is the whole reason the
unknown state is trustworthy.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # imported as part of the plugin package
    from . import concord_kernel as K
except ImportError:  # imported flat from the installed plugin directory
    import concord_kernel as K


# --------------------------------------------------------------- constants
#: The floor this adapter will run under. ``gh api --method/--input`` are the
#: flags used; the number is a configured pin, not a measured fact about which
#: release introduced them). It is a single constant so raising it is one edit.
MIN_GH_VERSION = (2, 0, 0)

#: The commit-message grammar that carries attribution. Parsed by exact match
#: on the captured group, so ``op-1`` cannot be satisfied by ``op-10``.
_MESSAGE_RE = re.compile(r"concord\((?P<op>[^()]+)\):")

_OBJECT_KEY_RE = re.compile(
    r"^github:(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)"
    r"/(?P<repo>[A-Za-z0-9._-]+)"
    r"@(?P<branch>[A-Za-z0-9._/-]+)"
    r":(?P<path>[^:]+)$"
)

_VERSION_RE = re.compile(r"gh version (\d+)\.(\d+)\.(\d+)")
_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)\s*\Z")

#: Paths this adapter refuses to address at all: the GitHub Actions workflow
#: directory. The tool refuses them first (review P2-3); this is the second
#: gate, so a hand-built key cannot reach one either.
_WORKFLOW_PARTS = (".github", "workflows")


def is_workflow_path(path: str) -> bool:
    """Is *path* inside ``.github/workflows`` (case-insensitive, one rule)?"""
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    return len(parts) >= 2 and tuple(part.lower() for part in parts[:2]) == _WORKFLOW_PARTS

#: Statuses that mean "the provider refused and nothing was applied".
DEFINITIVE_REFUSALS = {
    401: "unauthorized",
    403: "forbidden_or_rate_limited",
    404: "repository_or_file_not_found",
    409: "stale_revision",
    422: "unprocessable",
}


# ----------------------------------------------------------------- errors
class AdapterError(RuntimeError):
    """Base class. ``reason`` is carried into the tool's refusal shape."""

    reason = "adapter_error"

    def __init__(self, detail: str, *, reason: Optional[str] = None) -> None:
        super().__init__(detail)
        if reason is not None:
            self.reason = reason


class AdapterMisconfigured(AdapterError):
    """The target is not addressable under this adapter's configuration."""

    reason = "target_not_allowed"


class AdapterUnavailable(AdapterError):
    """``gh`` is missing, too old, or unparseable. Nothing was sent."""

    reason = "adapter_unavailable"


class AdapterAmbiguous(AdapterError):
    """A request may have reached the provider. Never retried in place."""

    reason = "outcome_ambiguous"


class ReadFailed(AdapterError):
    """A provider read did not produce usable evidence."""

    reason = "read_failed"


# ----------------------------------------------------------------- target
def canonical_branch(branch: str) -> str:
    """Reduce a branch to the one name GitHub resolves it to.

    ``refs/heads/main`` and ``main`` are the same ref, so they must be the same
    object. Branch *case* is preserved: git refs are case-sensitive.
    """
    text = branch.strip()
    prefix = "refs/heads/"
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text


@dataclass(frozen=True)
class GithubTarget:
    owner: str
    repo: str
    branch: str
    path: str

    def __post_init__(self) -> None:
        # Canonical identity, applied at every construction site. GitHub treats
        # owner and repository as case-insensitive, so `OCTO/Disposable` and
        # `octo/disposable` are one object and must share one store key -
        # otherwise an alias bypasses an unresolved hold (review P1-2). Path and
        # branch case are NOT folded: both are case-sensitive in git.
        object.__setattr__(self, "owner", self.owner.lower())
        object.__setattr__(self, "repo", self.repo.lower())
        object.__setattr__(self, "branch", canonical_branch(self.branch))

    @property
    def key(self) -> str:
        return f"github:{self.owner}/{self.repo}@{self.branch}:{self.path}"

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def api_path(self) -> str:
        # Each path segment is percent-encoded; the separator is preserved so
        # the endpoint addresses the same repository path the key names.
        quoted = "/".join(_quote(part) for part in self.path.split("/"))
        return quoted

    def commit_message(self, operation_id: str, summary: str = "") -> str:
        body = summary.strip() or f"update {self.path}"
        return f"concord({operation_id}): {body}"


def _quote(part: str) -> str:
    from urllib.parse import quote

    return quote(part, safe="")


def parse_object_key(object_key: str) -> GithubTarget:
    """Parse the canonical key. Refuses anything that is not exactly it."""
    if not isinstance(object_key, str):
        raise AdapterMisconfigured("object key must be a string")
    match = _OBJECT_KEY_RE.match(object_key.strip())
    if not match:
        raise AdapterMisconfigured(
            "object key must be 'github:{owner}/{repo}@{branch}:{path}'",
            reason="invalid_object_key",
        )
    path = match.group("path")
    branch = match.group("branch")
    if path.startswith("/") or path.endswith("/") or ".." in path.split("/"):
        raise AdapterMisconfigured("path must be relative and may not traverse", reason="invalid_object_key")
    if branch.startswith("/") or branch.endswith("/") or ".." in branch.split("/"):
        raise AdapterMisconfigured("branch is not a usable ref", reason="invalid_object_key")
    if any(ord(ch) < 32 for ch in object_key):
        raise AdapterMisconfigured("object key contains control characters", reason="invalid_object_key")
    if not canonical_branch(branch):
        raise AdapterMisconfigured("branch is not a usable ref", reason="invalid_object_key")
    return GithubTarget(
        owner=match.group("owner"),
        repo=match.group("repo"),
        branch=branch,
        path=path,
    )


def canonical_key(owner: str, repo: str, branch: str, path: str) -> str:
    """Build the key from parts - the only constructor callers need."""
    return GithubTarget(owner=owner, repo=repo, branch=branch, path=path).key


def canonical_object_key(object_key: str) -> str:
    """Canonicalise an existing key: the migration hook for older records.

    Raises ``AdapterMisconfigured`` for anything this adapter cannot address, so
    a migration can leave a foreign record alone instead of rewriting it.
    """
    return parse_object_key(object_key).key


def commit_attributes_to(commit_message: str, operation_id: str) -> bool:
    """Is this commit message attributable to *this* operation, exactly?"""
    if not commit_message or not operation_id:
        return False
    match = _MESSAGE_RE.search(commit_message)
    return bool(match) and match.group("op") == operation_id


# ---------------------------------------------------- the opaque predicate
def _token(value: Any) -> Optional[str]:
    """An opaque provider token as text; ``None``/blank stays ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def opaque_evidence_verdict(
    *,
    evidence: Mapping[str, Any],
    object_key: str,
    stored_fingerprint: str,
    base_revision: Any,
    operation_id: str,
    base_identity: Any = None,
    fingerprint_object_key: Any = None,
) -> K.EvidenceVerdict:
    """Evaluate provider evidence for an opaque (blob SHA) revision.

    The inference this function may and may not make (review P1-3): a Git blob
    SHA identifies *content*, never an event. Equal content is not history, so
    it never proves on its own that nothing was written - a same-content commit
    and an A-to-B-to-A revert both leave the blob SHA unchanged while the file
    was genuinely changed. The earlier version returned
    ``failed_before_execution`` from equal SHA alone and could release a hold on
    a write that had applied.

    What the function may conclude:

    * ``ours`` - the current content hashes to this operation's fingerprint
      **and** the latest commit on the path carries this operation's marker.
      Both halves are required: content alone could be a coincidence or a
      collision, attribution alone could be someone else committing our text.
    * ``unknown`` - everything else, including a target that has not moved. An
      unchanged commit or an unchanged blob SHA is an observation of the past,
      not a proof about the future: a request that timed out may still be
      applied by the provider *after* this read (review F7-2). A dispatched
      operation therefore stays held until its application is attributable, or
      until an authoritative provider negative says nothing was applied - and
      this predicate is not handed one.

    The earlier version settled ``failed_before_execution`` from an unchanged
    base commit ("no commit has landed on it since the operation was admitted").
    That inference is invalid for the same reason the equal-blob-SHA one was
    banned in 006: it treats a snapshot of the reference as a proof of
    non-execution over a window that is still open.

    ``fingerprint_object_key`` is the object spelling this record's digest was
    computed over. It is normally the lock key; after the legacy-alias migration
    the lock key is canonical while the digest is still a digest of the older
    spelling, and checking content against the canonical key would refuse to
    attribute the operation's own value (owner F8-C).
    """
    revision = evidence.get("revision")
    if revision is None:
        return K.EvidenceVerdict(
            K.UNKNOWN,
            "provider reports no file at this path; a creation is outside this"
            " action family and is not attributable",
            None,
        )
    material_object = _token(fingerprint_object_key) or object_key
    material = {
        "object": material_object,
        "value": evidence.get("value"),
        "expected_revision": base_revision,
    }
    content_matches = K.fingerprint(material) == stored_fingerprint
    if content_matches:
        if commit_attributes_to(evidence.get("commit_message") or "", operation_id):
            return K.EvidenceVerdict(
                K.CONFIRMED,
                f"content fingerprint and commit attribution both match at blob {revision}",
                revision,
            )
        if str(revision).strip() != str(base_revision).strip():
            return K.EvidenceVerdict(
                K.UNKNOWN,
                f"content matches but the latest commit on the path is not attributable"
                f" to operation {operation_id} (no concord({operation_id}) marker)",
                revision,
            )

    observed_commit = _token(evidence.get("commit_sha"))
    recorded_commit = _token(base_identity)
    if recorded_commit is not None and observed_commit is not None:
        if str(recorded_commit) == str(observed_commit):
            return K.EvidenceVerdict(
                K.UNKNOWN,
                f"the latest commit on the path is still the base commit {recorded_commit},"
                " but an unchanged ref does not prove the request cannot still land: a"
                " dispatched request that timed out may be applied by the provider after"
                " this read. Held until the application is attributable or the provider"
                " states, authoritatively, that nothing was applied",
                revision,
            )
        if str(revision).strip() == str(base_revision).strip():
            return K.EvidenceVerdict(
                K.UNKNOWN,
                f"blob SHA is unchanged at {revision}, but a commit did land on the path"
                f" since the base ({recorded_commit} -> {observed_commit}); the provider"
                " moved and moved back, so nothing may be inferred from the content",
                revision,
            )

    if not content_matches:
        unchanged = str(revision).strip() == str(base_revision).strip()
        return K.EvidenceVerdict(
            K.UNKNOWN,
            f"provider blob {revision} holds different content than this operation"
            " intended; the change is not attributable to this operation"
            + (
                ". The blob SHA is unchanged, but equal content is not history: a"
                " same-content commit or an A->B->A revert leaves it unchanged too"
                if unchanged
                else ""
            ),
            revision,
        )

    return K.EvidenceVerdict(
        K.UNKNOWN,
        f"cannot prove whether this operation applied: no immutable base commit was"
        f" recorded for it, and blob SHA {revision} is content, not history"
        " (it is unchanged, which a same-content commit or an A->B->A revert would"
        " also leave behind)",
        revision,
    )


# ----------------------------------------------------------------- adapter
class GithubContentsAdapter:
    """A revision-conditional file write to one allowlisted repository.

    ``gh_command`` is the executable prefix (default ``("gh",)``). Tests pass a
    real child process here; the boundary is the same one production uses -
    argv in, stdout/stderr/exit code out.
    """

    def __init__(
        self,
        *,
        allowed_repositories: Iterable[tuple[str, str]],
        gh_command: Sequence[str] = ("gh",),
        min_version: tuple[int, int, int] = MIN_GH_VERSION,
        timeout_s: float = 30.0,
    ) -> None:
        allow = {(owner.lower(), repo.lower()) for owner, repo in allowed_repositories}
        if not allow:
            raise AdapterMisconfigured(
                "no disposable repository is configured; the adapter refuses to run"
                " without an explicit allowlist",
                reason="allowlist_missing",
            )
        self._allow = frozenset(allow)
        self.gh_command = tuple(gh_command)
        self.min_version = tuple(min_version)
        self.timeout_s = timeout_s
        self._version: Optional[tuple[int, int, int]] = None

    # -- configuration ---------------------------------------------------
    def validate_target(self, object_key: str) -> GithubTarget:
        target = parse_object_key(object_key)
        if is_workflow_path(target.path):
            raise AdapterMisconfigured(
                f"{target.path} is a GitHub Actions workflow; this adapter does not"
                " address workflow files at all",
                reason="workflow_change_not_supported",
            )
        if (target.owner.lower(), target.repo.lower()) not in self._allow:
            raise AdapterMisconfigured(
                f"{target.repo_slug} is not in this adapter's allowlist"
                f" ({', '.join(sorted('/'.join(p) for p in self._allow))})",
                reason="target_not_allowed",
            )
        return target

    def _assert_allowed(self, target: GithubTarget) -> None:
        """Defence in depth: a hand-built target is checked too, not just a key."""
        if is_workflow_path(target.path):
            raise AdapterMisconfigured(
                f"{target.path} is a GitHub Actions workflow; this adapter does not"
                " address workflow files at all",
                reason="workflow_change_not_supported",
            )
        if (target.owner.lower(), target.repo.lower()) not in self._allow:
            raise AdapterMisconfigured(
                f"{target.repo_slug} is not in this adapter's allowlist",
                reason="target_not_allowed",
            )

    def ensure_ready(self) -> tuple[int, int, int]:
        """Fail closed unless a usable ``gh`` is present. Cached after success."""
        if self._version is not None:
            return self._version
        argv = [*self.gh_command, "--version"]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailable(
                f"gh executable not found ({self.gh_command[0]!r}); install is an"
                " owner gate, and nothing was sent"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterUnavailable(f"gh --version timed out: {exc}") from exc
        blob = f"{completed.stdout}\n{completed.stderr}"
        match = _VERSION_RE.search(blob)
        if not match:
            raise AdapterUnavailable(
                "could not parse a gh version from --version output; failing closed"
            )
        version = tuple(int(part) for part in match.groups())
        if version < self.min_version:
            raise AdapterUnavailable(
                f"gh {'.'.join(map(str, version))} is below the required"
                f" {'.'.join(map(str, self.min_version))}; failing closed"
            )
        self._version = version  # type: ignore[assignment]
        return self._version

    # -- transport -------------------------------------------------------
    def _invoke(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        """Run one gh invocation. Distinguishes 'not sent' from 'may have sent'."""
        # The canonical github: identity and allowlist refer to GitHub.com.
        # Ambient GH_HOST must never redirect the same operation to another host.
        args = list(args)
        if args and args[0] == "api":
            args[1:1] = ["--hostname", "github.com"]
        argv = [*self.gh_command, *args]
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            # Spawn failed: no request can have left the process.
            raise AdapterUnavailable(
                f"gh executable not found ({self.gh_command[0]!r}); nothing was sent"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterAmbiguous(
                f"gh did not return within {self.timeout_s}s; a request may have been sent"
            ) from exc

    @staticmethod
    def _status_of(completed: subprocess.CompletedProcess) -> Optional[int]:
        # gh renders its authoritative API status as a trailing `(HTTP NNN)`.
        # Provider-controlled message/body text can itself mention HTTP codes;
        # never classify those as proof that a dispatched write was refused.
        stderr = completed.stderr.strip()
        if not stderr.startswith("gh: "):
            return None
        match = _HTTP_STATUS_RE.search(stderr)
        return int(match.group(1)) if match else None

    def _json(self, completed: subprocess.CompletedProcess) -> Any:
        if completed.returncode != 0:
            raise AdapterAmbiguous(
                f"gh exited {completed.returncode} with no definitive status;"
                " a request may have been sent"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterAmbiguous(
                "gh returned output that is not JSON; the provider's answer is not"
                " determinable"
            ) from exc

    # -- write -----------------------------------------------------------
    def write(
        self,
        target: GithubTarget,
        *,
        expected_revision: Any,
        value: str,
        operation_id: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """Conditional write. Returns a receipt, or raises.

        Raises ``K.Rejected`` for a definite refusal (nothing applied) and
        ``AdapterAmbiguous`` when the outcome is not determinable.
        """
        self.ensure_ready()
        self._assert_allowed(target)
        if expected_revision is None or str(expected_revision).strip() == "":
            # Creation is a different action family: without a base blob SHA
            # there is nothing to condition the write on.
            raise AdapterMisconfigured(
                "a base blob SHA is required; creation (no base revision) is out of"
                " scope for this action family",
                reason="missing_base_revision",
            )
        if not isinstance(value, str):
            raise AdapterMisconfigured(
                "value must be text (this adapter base64-encodes UTF-8)", reason="invalid_value"
            )

        payload = {
            "message": target.commit_message(operation_id, summary),
            "content": base64.b64encode(value.encode("utf-8")).decode("ascii"),
            "sha": str(expected_revision),
            "branch": target.branch,
        }

        workdir = tempfile.mkdtemp(prefix="concord-gh-")
        body_path = os.path.join(workdir, "body.json")
        try:
            # The body is the only place content and commit message appear.
            fd = os.open(body_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            endpoint = f"repos/{target.repo_slug}/contents/{target.api_path()}"
            completed = self._invoke(["api", "-X", "PUT", endpoint, "--input", body_path])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if completed.returncode != 0:
            status = self._status_of(completed)
            if status in DEFINITIVE_REFUSALS:
                raise K.Rejected(
                    f"{DEFINITIVE_REFUSALS[status]} (HTTP {status}):"
                    f" {completed.stderr.strip()[:300]}"
                )
            # 5xx, or a failure with no recognised status: the request may have
            # been applied. Prophesying either way would be a guess.
            raise AdapterAmbiguous(
                f"gh exited {completed.returncode} without a definitive status"
                f" (stderr: {completed.stderr.strip()[:300]})"
            )

        body = self._json(completed)
        content = body.get("content") if isinstance(body, Mapping) else None
        commit = body.get("commit") if isinstance(body, Mapping) else None
        blob_sha = content.get("sha") if isinstance(content, Mapping) else None
        commit_sha = commit.get("sha") if isinstance(commit, Mapping) else None
        if not blob_sha or not commit_sha:
            # A 2xx whose body does not carry the field paths this adapter
            # depends on. It may have applied; we cannot say, so it is held.
            raise AdapterAmbiguous(
                "provider returned success without content.sha/commit.sha; the"
                " response shape is not the one this adapter was built against"
            )

        # The 201 body is the provider's claim. ``verified_by`` in the receipt
        # says "read-after-write", so the claim is checked against a read
        # before it is allowed to become a receipt: the blob must be the one
        # the write reported, its content must be ours, and the latest commit on
        # the path must carry this operation's marker.
        try:
            evidence = self.read(target)
        except AdapterError as exc:
            raise AdapterAmbiguous(
                f"write reported success but the read-back failed ({exc});"
                " the write may have applied"
            ) from exc
        if str(evidence.get("revision")) != str(blob_sha):
            raise AdapterAmbiguous(
                f"write reported blob {blob_sha} but the read-back shows"
                f" {evidence.get('revision')}; the provider moved under us"
            )
        if evidence.get("value") != value:
            raise AdapterAmbiguous(
                "write reported success but the read-back content is not the"
                " content that was sent; the outcome is not attributable"
            )
        if not commit_attributes_to(evidence.get("commit_message") or "", operation_id):
            raise AdapterAmbiguous(
                f"write reported success but the latest commit on the path is not"
                f" attributable to operation {operation_id}"
            )
        if evidence.get("commit_sha") != commit_sha:
            raise AdapterAmbiguous(
                "write reported success but the read-back commit is not the"
                " commit returned by the write; the provider moved under us"
            )
        return {
            "commit_sha": commit_sha,
            "blob_sha": str(evidence.get("revision")),
            "revision": str(expected_revision),
            "operation_id": operation_id,
            "verified_by": "read-after-write",
            "source": "gh api PUT contents + read-back (mock fixture)"
            if self._is_mock()
            else "gh api PUT contents + read-back",
        }

    def _is_mock(self) -> bool:
        # Diagnostic only: lets an artifact say which transport produced it
        # without the adapter guessing at production behaviour.
        return os.environ.get("CONCORD_GH_MOCK", "").strip() == "1"

    # -- read ------------------------------------------------------------
    def read(self, target: GithubTarget) -> dict[str, Any]:
        """Evidence: current blob SHA, current content, and commit attribution."""
        self.ensure_ready()
        self._assert_allowed(target)
        # Read attribution first, then bind the content to that immutable commit.
        # Two reads of a moving branch can otherwise join content from one commit
        # with an operation marker from a different commit and falsely confirm.
        commit_shas, messages = self._latest_commit(target)
        content_ref = commit_shas or target.branch
        contents = self._invoke(
            ["api", f"repos/{target.repo_slug}/contents/{target.api_path()}?ref={_quote(content_ref)}"]
        )
        if contents.returncode != 0:
            status = self._status_of(contents)
            if status == 404:
                return {
                    "revision": None,
                    "value": None,
                    "exists": False,
                    "commit_sha": None,
                    "commit_message": None,
                    "source": "gh api GET contents: 404 (no file at path)",
                }
            raise ReadFailed(
                f"content read failed (HTTP {status}): {contents.stderr.strip()[:300]}"
            )

        body = self._json(contents)
        if not isinstance(body, Mapping) or "sha" not in body:
            raise ReadFailed("content read returned no 'sha' field")
        if body.get("encoding") != "base64":
            raise ReadFailed("content read did not return inline base64 encoding")
        raw = body.get("content")
        if not isinstance(raw, str):
            raise ReadFailed("content read returned no inline 'content' (file too large for this surface)")
        try:
            # GitHub wraps base64 using newlines. Do not silently discard other
            # invalid bytes, or an unavailable/malformed body can look empty.
            value = base64.b64decode(raw.replace("\n", "").replace("\r", ""), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReadFailed(f"content is not decodable UTF-8 text: {exc}") from exc

        return {
            "revision": body["sha"],
            "value": value,
            "exists": True,
            "commit_sha": commit_shas,
            "commit_message": messages,
            "source": "gh api GET contents + commit list (mock fixture)" if self._is_mock()
            else "gh api GET contents + commit list",
        }

    def _latest_commit(self, target: GithubTarget) -> tuple[Optional[str], Optional[str]]:
        """Best-effort attribution read. A failure here is 'no attribution'."""
        try:
            completed = self._invoke(
                [
                    "api",
                    f"repos/{target.repo_slug}/commits?path={target.api_path()}"
                    f"&sha={_quote(target.branch)}&per_page=1",
                ]
            )
        except AdapterError:
            return None, None
        if completed.returncode != 0:
            return None, None
        try:
            rows = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(rows, list) or not rows:
            return None, None
        row = rows[0] if isinstance(rows[0], Mapping) else {}
        commit = row.get("commit") if isinstance(row.get("commit"), Mapping) else {}
        sha = row.get("sha")
        message = commit.get("message")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            return None, None  # No immutable identity means no attribution.
        return sha, (message if isinstance(message, str) else None)


# ------------------------------------------------------- the tool-facing path
def guarded_write(
    coordinator: K.Coordinator,
    adapter: GithubContentsAdapter,
    *,
    object_key: str,
    operation_id: str,
    value: str,
    expected_revision: Any,
    actor: str,
    summary: str = "",
    observed_base: Any = None,
    base_identity: Any = None,
) -> dict[str, Any]:
    """Admission, provider write and settlement in one function body.

    This is what a Concord tool handler calls: everything that can refuse
    happens before ``Coordinator.run``, and the mutate callable is closed over
    the parsed target so nothing between admission and the write can re-route
    it.

    ``observed_base`` is what the provider held when the caller's
    ``expected_revision`` was checked. When it differs, the draft was prepared
    from a superseded revision: the operation is admitted (so the refusal has a
    durable record) and then settled as ``failed_before_execution`` WITHOUT
    sending anything. Rebasing the content onto the newer blob instead is the
    lost update this tool exists to prevent.

    ``base_identity`` is the provider's immutable reference for that same read
    (the latest commit on the path). It is what reconciliation may use to prove
    that nothing landed; see :func:`opaque_evidence_verdict`.
    """
    try:
        target = adapter.validate_target(object_key)
        adapter.ensure_ready()
        base = K.normalize_revision(expected_revision, model="opaque")
    except AdapterError as exc:
        return K.refusal(exc)
    except (TypeError, ValueError) as exc:
        return {"status": "refused", "reason": "invalid_arguments", "detail": str(exc)}

    def mutate() -> dict[str, Any]:
        if observed_base is not None and str(observed_base).strip() != str(base):
            raise K.Rejected(
                f"the file moved from {base} to {observed_base} after the caller read it;"
                " nothing was sent - read the file again and start a new operation with"
                " the content you compose from the current revision"
            )
        return adapter.write(
            target,
            expected_revision=base,
            value=value,
            operation_id=operation_id,
            summary=summary,
        )

    return coordinator.run(
        operation_id=operation_id,
        actor=actor,
        received_args={"object": object_key, "value": value, "expected_revision": base},
        revision_model="opaque",
        base_identity=base_identity,
        mutate=mutate,
    )


def reconcile_from_provider(
    coordinator: K.Coordinator,
    adapter: GithubContentsAdapter,
    *,
    operation_id: str,
    object_key: str,
) -> dict[str, Any]:
    """Settle an unresolved operation from a provider read. Never writes.

    ``require_release_proof`` is on: for this provider a dispatched operation is
    only released from ``unknown`` on attributable application (or an
    authoritative provider negative), never on "the ref looks unchanged"
    (review F7-2).
    """
    # The caller's key is an assertion, not permission to source evidence from
    # another object. Matching content and an operation marker on another file
    # cannot settle the recorded operation or release its hold.
    row = coordinator.get(operation_id)
    if row is None:
        return {
            "status": "refused", "reason": "operation_not_found",
            "detail": f"no recorded operation {operation_id!r}; no provider read was made",
        }
    recorded_key = canonical_object_key(row["object_key"])
    if canonical_object_key(object_key) != recorded_key:
        return {
            "status": "refused", "reason": "object_mismatch",
            "detail": "the requested object does not match this operation's recorded target;"
                      " no provider read was made and the operation was not changed",
        }
    target = adapter.validate_target(recorded_key)
    evidence = adapter.read(target)
    return coordinator.reconcile(
        operation_id, evidence, evaluate=opaque_evidence_verdict,
        require_release_proof=True,
    )
