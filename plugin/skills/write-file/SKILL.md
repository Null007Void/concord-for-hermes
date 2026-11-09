---
name: write-file
description: Use concord_github_write for GitHub file updates instead of a plain write, and read its five outcomes correctly.
---

# Coordinated GitHub file writes

Use `concord_github_write` whenever you are about to **replace the content of a
file that already exists** in a GitHub repository. It is not a general GitHub
tool: it updates one file's text on one branch, and it exists so two sessions
cannot silently overwrite or repeat each other's work.

## When to use it

- You are changing an existing file's content in a repository.
- Another session, agent or person might be touching the same file.

Do **not** use it to create a file, to write binary content, to change a
workflow, or for anything outside the repository allowlist. Those calls are
refused, and the refusal is the honest answer, not an obstacle to route around.

The workflow boundary is enforced, not advisory: any path under
`.github/workflows/` is refused with `workflow_change_not_supported`, before
anything is read or recorded. Workflow changes go through a reviewed change
outside an agent session. Every other file under `.github/` (CODEOWNERS, issue
templates, `dependabot.yml`) is an ordinary text edit and is allowed.

## Read first - the revision is required

The tool needs the file's current blob SHA, because that is what binds your
content to the version you actually read:

```bash
gh api "repos/OWNER/REPO/contents/PATH?ref=BRANCH" --jq .sha
```

Call the tool with that value in `expected_revision`. If the file moves after
you read it, the call returns `conflict` and **nothing of yours was written** -
read again, re-apply your edit to the current content, and use a new
`operation_id`. The tool will never rebase your draft onto newer content for
you: that is how one agent's edit silently deletes another's.

## Arguments

| Field | Meaning |
|---|---|
| `repository` | `owner/repo`. Must be in the configured allowlist. Owner and repository case are folded, so `Octo/Repo` and `octo/repo` address one object and share one hold. |
| `path` | Repository-relative path of the existing file, e.g. `docs/plan.md`. |
| `branch` | Branch the file is on, e.g. `main`. |
| `operation_id` | **Idempotence key.** One id per intent, and one intent means **one object**: the id is bound to the file, branch and repository it was first admitted for. Letters, digits, `.`, `_`, `:`, `-`. |
| `expected_revision` | The blob SHA you read before composing `content`. |
| `content` | The complete new UTF-8 text (at most 1 MiB). |

Pick `operation_id` from the intent, not from the moment: `plan-md-refresh`,
`readme-typo-42`. If the call must be repeated for any reason, repeat it with the
**same** id, the **same** content and the **same** `expected_revision` - that is
what makes the repeat safe. A new intent needs a new id; reusing an id for
different content, or for a differently observed base, is refused.

## The five outcomes

Read the `outcome` field; each one demands a different response.

- **`confirmed`** - written and verified by read-back. `receipt` carries
  `commit_sha`, `blob_sha`, `object_key` and `operation_id`. Report it as done.
- **`replayed`** - this id already succeeded; the original receipt is returned
  and `second_write` is `false`. **Nothing was written now.** Do not claim a new
  change, and do not "retry" it.
- **`conflict`** - someone else holds the file, or the file moved under you, or
  the provider refused the conditional write, or the `operation_id` already names
  a **different** object (`state: "object_mismatch"`). A refusal row is recorded.
  Read `next_action`: either reconcile the holding operation, or start a new
  intent after a fresh read. Do not loop the same call. **If the file moved since
  you read it, your draft was not applied and must not be re-sent unchanged:**
  re-read the file, re-apply your edit to the new content, use a new
  `operation_id`. **If the state is `object_mismatch`, your id belongs to another
  file/branch/repository:** nothing was replayed, nothing was dispatched, and the
  other object's hold was not touched. Read the new object and use a new id - an
  id never transfers to a different object.
- **`unknown`** - the write may or may not have applied; the file stays held and
  `auto_retry` is `false`. **Never retry an unknown.** Tell the user, then settle
  it with `hermes concord reconcile <operation_id>` (a read, not a write), or
  leave it held deliberately. Reconciling does not release it just because the
  file looks unchanged: a request that timed out can still land afterwards, so
  the operation only settles as `confirmed` when the commit on the file carries
  this operation's marker. If `recovery_required` is true, the reservation or
  settlement could not be durably verified. Follow `next_action` to inspect and
  recover the store first, before cancelling or reconciling. `last_recorded_state`
  is historical evidence, not a fresh certification of damaged storage.
  `provider_dispatched: false` or `provider_outcome: "not_applied"` can establish
  that this call did not write while its coordination record still needs recovery.
  Neither permits another write around the unresolved record.
- **`refused`** - nothing was sent and nothing was recorded. Fix the argument or
  the configuration and try again. One refusal is not about your call at all:
  `reason: "partial_schema"` or `"damaged_history"` means the local coordination
  store has lost required history. Nothing was rebuilt, sent or recorded, the
  stored evidence is untouched, and the fix is to restore the store from a
  `hermes concord backup` - retrying, or inventing a new `operation_id`, cannot
  help and would hide the damage. This refusal can arrive *at admission time*,
  after the call has already read the file: the store is checked inside the same
  transaction that decides the write, so damage that lands while the call is in
  flight (an outside process touching the database) is refused rather than
  written past. Same reason, same next step: stop and tell the operator.

## Never

- Never treat `replayed` as a new write, or `unknown` as success or failure.
- Never invent a new `operation_id` to get past a conflict - that is exactly the
  silent second write Concord prevents. Read the file, decide, then use a new id
  deliberately.
- Never reuse an `operation_id` for a different file, branch or repository. The
  id is bound to the object it was admitted for; a reuse is refused as
  `object_mismatch` and moves nothing.
- Never work around a refusal with a shell command or a direct `gh` call while
  claiming the change is coordinated: those routes are **not protected**.
- Never treat a `partial_schema` / `damaged_history` refusal as a reason to fall
  back to a plain write. The coordination store is damaged; only an operator can
  restore it, and a write that bypasses Concord is the one nobody can see.
