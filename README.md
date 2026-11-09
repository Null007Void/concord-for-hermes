# Concord for Hermes

Coordinates file updates on GitHub so independent Hermes sessions cannot silently
overwrite or repeat one another's work. When two agents (or one agent twice)
touch the same file, Concord admits exactly one intent, leaves an honest record
of what happened, and refuses to guess when an outcome is unknown.

Independent plugin, compatible with Hermes Agent. Not an official Nous Research
product.

**Version 0.1.0.** The plugin has passed 353 offline regression tests, clean
installation and native plugin checks on Windows. Two owner-operated private
GitHub write-and-exact-replay demonstrations were recorded on November 8, 2026.
They demonstrate existing-file replacement, verified receipts and no second
write on an exact replay. They do not establish every GitHub error, rate-limit,
concurrency or recovery case. Private repository identities, credentials, raw
recordings and receipts are not distributed.

The offline agent and guardrail objects are scripted/permissive test doubles.
See [verification scope](VERIFICATION.md) for exact runtime pins and limits.
Released under the [MIT License](LICENSE).

## What it does

One tool, one action family: **replace the content of a file that already
exists**, on one branch of one repository in an allowlist.

```
concord_github_write(repository, path, branch, operation_id, expected_revision, content)
```

For each call it:

1. validates the target against the configured allowlist (a model argument
   cannot widen it) and the minimum `gh` version (fails closed);
2. takes the base from `expected_revision` - **the blob SHA the caller read
   before composing the content**, so the content is bound to the revision it
   was written against. The tool does *not* refresh the base at dispatch: a
   draft prepared from a superseded revision conflicts instead of silently
   rebasing onto newer content (review P1-1). A replay reuses the recorded base;
3. **admits** the intent: an atomic, per-object reservation in SQLite. A second
   intent on the same object is refused and recorded, not queued;
4. writes with the provider's own conditional-write check (`sha` in the body),
   so a concurrent change is refused by GitHub, not by Concord;
5. **verifies by reading back**: the blob must be the one the write reported,
   its content must be ours, and the latest commit on the path must carry
   `concord(<operation_id>):`;
6. settles in exactly one of five outcomes.

| Outcome | Meaning | What you may do |
|---|---|---|
| `confirmed` | written and verified; `receipt` with `commit_sha`, `blob_sha`, `object_key`, `operation_id` | nothing - it is done |
| `replayed` | this `operation_id` already succeeded; original receipt, `second_write: false` | nothing - no second write happened |
| `conflict` | another operation holds the file, the file moved, or the provider refused; a refusal row is recorded | reconcile the holder, or start a new intent after a fresh read |
| `unknown` | the write may have applied; the object stays **held**, `auto_retry: false` | `hermes concord reconcile <id>` - a read, never a retry |
| `refused` | nothing was sent and nothing was recorded | fix the argument or configuration |

`operation_id` is the idempotence key. Same id + same content + same
`expected_revision` = replay, never a second write. Same id + different content
(or the same content with a differently observed base) = a recorded conflict. A
new intent needs a new id.

**Releasing a hold needs proof, not an impression.** `reconcile` settles an
`unknown` operation only on attributable application - the current content hashes
to this operation's fingerprint *and* the latest commit on the path carries
`concord(<operation_id>):`. A file that has not moved is **not** proof: a request
that timed out can still be applied by the provider after the read, so a
dispatched operation stays held until the change shows up as ours, or until the
provider makes an authoritative non-execution statement that names the operation
(recorded as `non_execution` evidence with `operation_id`, `authoritative`,
`source` and `covers`). A synchronous refusal from the provider at dispatch time
(a 409/401/403/422) is also authoritative: nothing was applied, and the operation
settles `failed_before_execution` immediately. See
`concord_kernel.authoritative_non_execution`.

## Coverage - what is protected and what is not

**Protected:** calls that arrive through `concord_github_write`, for a file that
already exists, on a branch of a repository in `allowed_repositories`.

**NOT protected:**

- any other tool (including `write_file`, `terminal`, MCP tools, the GitHub
  connector if you have one);
- shell commands and direct `gh` use;
- browser actions and raw HTTP;
- a profile that has not enabled the plugin, or has disabled it;
- another machine, another coordinator store, another user;
- file creation, deletion, binary content, and every other action family.

`hermes concord status` prints this boundary on every run, because an empty
status is *not* evidence that nobody wrote to the file - only that Concord
recorded nothing.

## Requirements

- Hermes Agent >= 0.21.3 with the plugin API used here (`register_tool`,
  `register_cli_command`, `register_skill`). The manifest declares this baseline.
  Tested at upstream commit
  `c5f347ebe9b840c3dbe5ec99bbe8e36871f24393` and against Hermes 0.21.3.
- Python 3.14.7 is the tested interpreter. The pinned Hermes checkout declares
  Python >= 3.11, < 3.15; those other interpreters have not been verified here.
  Concord itself imports only the standard library.
- The GitHub CLI, `gh` >= 2.0.0 (a configured floor: `MIN_GH_VERSION` in
  `plugin/concord_gh_adapter.py`). An older or unparseable `gh` is a refusal,
  never a "probably fine".
- An isolated `HERMES_HOME` (a profile) you are willing to install a plugin into.
  Nothing here installs globally.

## Install

Copy the `plugin/` directory into your profile's plugin directory, then enable
it by key:

```bash
cp -r plugin "$HERMES_HOME/plugins/concord-for-hermes"     # a copy, not a symlink
```

```yaml
# $HERMES_HOME/config.yaml
plugins:
  enabled:
    - concord-for-hermes
  entries:
    concord-for-hermes:
      settings:
        allowed_repositories:        # there is no default target
          - your-org/your-disposable-repo
```

Check it loaded:

```bash
hermes plugins validate "$HERMES_HOME/plugins/concord-for-hermes"
hermes plugins doctor  "$HERMES_HOME/plugins/concord-for-hermes"
hermes plugins list | grep concord-for-hermes     # enabled, source user
hermes concord status
```

At this Hermes pin, plugin-provided commands are not listed in the top-level
`hermes --help`; the command works when invoked and `hermes plugins list` is the
reliable check that the plugin is enabled.

Install from this repository using the copy step above. A future catalog listing
would require a separately reviewed entry with `subdir: plugin` and an exact
public commit. This repository does not claim catalog admission.

### Quick start (offline, no GitHub account)

The verified end-to-end path uses the process-level fake `gh` shipped in
`tests/fakegh/`. It installs into a throwaway home, checks the plugin and walks
every outcome; nothing is sent anywhere and no credential is read.

```bash
python scripts/fresh_install_check.py --source-tree --out fresh-install.txt
```

With `--source-tree`, the script verifies and copies the files listed in
`RELEASE-MANIFEST.json` from an extracted release. Without that option it exports
a clean `git archive` of the chosen revision. It then copies
`plugin/` into a fresh `HERMES_HOME` (the copy step above, nothing else), runs
`hermes plugins validate` and `hermes plugins doctor`, then drives the four
admitted outcome shapes - `confirmed`, `replayed`, `conflict` (on an argument
mismatch, on a draft prepared from a superseded revision, and on a stale base)
and `unknown` - through the real plugin loader, CLI and tool executor against the
fake `gh`, and recovers the `unknown` with `reconcile`. `refused` needs no
provider and is covered by the suite. It ends with the documented test path and
the suite. Exit 0 means every named check passed; the transcript says which.
`--skip-tests` stops after the CLI walkthrough, `--rev <sha>` pins the exported
revision.

For the first *real* call you need a disposable repository, a `gh` that is
already authenticated for that account, and the repository in
`allowed_repositories`. The agent-facing call is:

```json
{"repository": "your-org/your-disposable-repo",
 "path": "notes/plan.md", "branch": "main",
 "operation_id": "refresh-2026-11-05",
 "expected_revision": "<the blob sha you read>", "content": "..."}
```

Live writes require explicit user authorization for the repository and content.
The private demonstrations observed receipt fields and read-after-write; they
are narrow evidence, not proof of all production behavior.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `allowed_repositories` | `[]` | `owner/repo` entries. Empty refuses every call. |
| `gh_command` | `["gh"]` | Executable prefix for the CLI (e.g. a wrapper on PATH). |
| `timeout_seconds` | `30` | Per-invocation timeout for one `gh` call. |

`CONCORD_DB` overrides the store path. It changes *where* the record lives, never
*what* is protected.

## The human surface

```bash
hermes concord status                 # held objects, holders, safe next actions
hermes concord status --json          # the same, machine-readable (check .store.readable)
hermes concord reconcile <id>         # settle one unresolved op from a provider read
hermes concord cancel   <id>          # withdraw an intent that was never dispatched
hermes concord backup   --output f.db  # restorable, verified copy of the store
hermes concord restore  --input  f.db  # replace the store from that copy
hermes concord export   --output f.json # audit dump only - NOT a backup
```

`status` is read-only by construction: the store is opened `mode=ro` with
`PRAGMA query_only`, so it cannot create the schema, cannot take a write lock,
and cannot be the reason an admission waits. `reconcile` reads the file and
settles the record; it never re-sends the write. `cancel` only works from
`reserved` - once a dispatch may have started there is nothing to cancel.

**One provider object is one set of holds.** `owner/repo` is case-insensitive on
GitHub and `refs/heads/main` is `main`, so those spellings are folded into one
canonical object key before anything is admitted. An admission compares
canonical identities, not literal strings, which means a record written before
that rule existed - still stored under its old spelling - blocks every alias of
its object. That check is recomputed from the stored rows on every admission, so
closing and reopening the store cannot dissolve it, and it does not matter which
of two colliding holds settles first: the object stays held until all of them
settle. A colliding legacy row keeps its own key and a note explaining the
collision; merging the evidence away would be a second way to lose a hold.

**An operation id is bound to its object.** A recorded id answers only for the
object it was admitted for: a replay or a retry must address that same object -
exactly, or canonically equivalent under the rules above (`owner/repo` case
folded, `refs/heads/` stripped, path and branch case preserved). The stored
digest can be re-checked under the spelling it was derived from (see below),
which is what keeps an older alias replayable - and that relaxation is exactly
why object identity is decided *first*: a different path, branch or repository
carrying the same content and the same base revision would otherwise hash to the
same stored digest. Such a call is answered with `state: "object_mismatch"`
(outcome `conflict`, `recorded: true`, `held: false`) and nothing is sent: no
receipt is replayed, no retry is dispatched, and the other object's hold is not
inherited. A different object needs a new operation id after a fresh read.

**Validated before it is written, not repaired.** Every write path - admission,
dispatch, settlement, and the CLI commands that open the store for write -
validates an existing store *before* any schema statement or migration runs. If
the file exists and a required table, required column or admission-guard index is
missing, the call refuses with `reason: "partial_schema"` and no byte changes. If
the tables are all present while attempt rows name operations that have no
record, or the durable sequence counter does not cover the rows, it refuses with
`reason: "damaged_history"`. Nothing is rebuilt on the way in, so the write that
would otherwise destroy the evidence that records existed cannot happen. Two
cases stay legitimate: a path with no file yet - and a zero-length file, which
holds no rows - is fresh creation, and an older-but-complete store (missing only
the additive `base_identity`/`fingerprint_object` columns and the migration
ledger) is brought up to date additively on open. An older schema is not damage;
a damaged one is not an empty store.

That verdict is computed from **one consistent read transaction**. The schema,
columns, admission-guard indexes, orphan scan, sequence counter and recorded
maxima are all read inside a single snapshot, so a concurrent writer that commits
meanwhile cannot appear in half of the checks. This was a real defect in the
previous shape, where every statement was its own transaction: a legitimate
concurrent admission landing between the counter read and the row read made a
healthy store look damaged (`damaged_history`), and a verdict could describe a
store that never existed. Lock contention is the one case that needs a decision,
and it is decided the safe way: the snapshot is retried a bounded number of
times and then refused (`reason: "storage_locked"`), never torn.

**The verdict is taken again at the mutation, in the mutation's own transaction.**
A snapshot describes an instant, and the write it guards happens later: an
*outside* process that damages the file in between - removing the operations row
behind a surviving `unknown` attempt, say - was invisible to the write that
followed, and a new same-object write could be admitted while the evidence of an
unknown outcome survived. Initialization and admission now take the same verdict
inside the same `BEGIN IMMEDIATE` span as the work they guard: validation, schema
creation/migration and the admission decision commit or roll back together. Damage
committed before that transaction begins is part of the lock-holding snapshot and
refuses it before a row changes; damage cannot land inside the window because the
write lock is held; and nothing is discovered after a commit. The read-only
verdict above is kept as a cheap pre-check - it refuses a damaged file without
opening it for write, so a refused open still changes no bytes - and as the
standalone question a reviewer asks about a file. Admission is refused the same
way if the file no longer holds the Concord schema that was validated when the
coordinator opened: re-creating an empty store over whatever replaced it is never
the answer. Initialization now carries its earlier verdict into this transaction:
if the pre-check saw a Concord schema, losing *all* tables before initialization
is damage too, not permission to start fresh. Every later mutation shares the
same gate, including dispatch, settlement, cancellation, reconciliation and
retention. Journal mode changes happen only after locked validation.

This protects observed history, not history erased before the first observation.
If an outside process removes or replaces every trace before a new coordinator
opens, that cannot be distinguished from a genuine fresh store using this file
alone. Protect the store's location, preserve verified backups, stop writers for
restore, and never reset it to clear a hold.

**Failed settlement is not a released hold.** If admission committed but dispatch
cannot start and its reservation cannot be released, the result is `unknown`
with `recovery_required: true`, `provider_dispatched: false`, and
`settlement_persisted: false`. The `last_recorded_state` is historical evidence,
not a claim that damaged storage still contains it. Similarly a provider refusal
whose settlement cannot be saved says `provider_outcome: "not_applied"` while
keeping coordination unresolved. Inspect and recover the store before cancelling
or reconciling; never turn either result into an automatic retry. `held: true`
in a recovery-required result is the conservative instruction to keep the object
blocked, not a fresh certification of damaged storage.

**The fingerprint keeps its own identity.** The stored SHA-256 is a digest over
the object key *as spelled when the operation was admitted*, so the legacy-alias
migration can rewrite the lock key without rewriting the digest. The original
spelling is kept beside it in `fingerprint_object` and is never overwritten. That
is what lets a replay - whose target is canonicalised today - still be recognised
as the same intent (`replayed`, not `rejected_argument_mismatch`), and what lets
reconciliation attribute the operation's own content after its lock key became
canonical. A digest is only ever re-checked against the spelling it was derived
from; a different value or a different base still mismatches. A row that an
earlier migration already rewrote has lost its original spelling - it is recorded
as its current key, and that limitation is named in the round's result record.

**Empty is not damaged.** `status --json` and `export` distinguish three states
in `store.damage`: `null` for a store that is fine (including one that is simply
empty - no operation has ever been recorded), `"partial_schema"` when some of the
Concord schema is present while a required table or column is gone (nonzero exit,
`export` refused, surviving attempt rows still shown), and `"unreadable"` when
the file cannot be read as SQLite at all. The same read snapshot also detects
missing guard indexes, orphaned attempts and broken sequence history, retaining
readable surviving rows while marking the store unhealthy. A store that is missing its
`concord_operations` table is never reported as healthy with empty arrays.

## Data, retention, and removal

**Where the data is:** `<hermes home>/plugin-data/concord-for-hermes/coordinator.db`
(SQLite, WAL). It survives `hermes plugins update` and is *not* deleted by
`hermes plugins remove`, by design: an unresolved operation is evidence.

**What is stored:** object key, the object spelling its fingerprint was derived
from, operation id, a SHA-256 fingerprint of the
material, state, holder, actor (profile name), attempt counters, the receipt the
provider returned (`commit_sha`, `blob_sha`, commit-attribution check), and the
refusal note. No file content is stored beyond the fingerprint, and no
credential is stored at all.

**Retention:** resolved records keep their identity forever but their payload is
pruned - at most `max_resolved_per_object` (200) receipts per object, with an
explicit `prune_resolved()` policy available. A pruned record still refuses a
replay (`replayed_pruned`), so pruning can never cause a second write.
**Unresolved records are never discarded** - not by retention, not by restart,
not by clock change, not to make room. When the store reaches
`max_unresolved_records` (10 000) new admissions are refused.

**Before removing or disabling:** back up first, because the command that reads
the store goes away with the plugin.

```bash
hermes concord backup --output /safe/place/concord-backup.db
```

That is a real backup: a consistent copy of the SQLite store (SQLite's online
backup API), verified for the Concord schema and integrity before it is moved
into place. It preserves everything a recovery needs - unresolved holds,
operation identities, material fingerprints, receipts and attempts - because it
*is* the store. `restore` re-verifies the file and refuses to replace a store
that still holds operations, or whose emptiness cannot be verified, unless you
pass `--replace`; stop other Concord processes first. Missing guards, orphaned
attempts and incomplete schemas fail backup/restore verification. A staged copy
is checked before the destination is replaced; an invalid copy does not overwrite
a good destination. Source and destination must be different files. `export` is a different thing and is **not** restorable: it is
an audit dump of identities, states and receipts, and it deliberately omits the
fingerprint (no file content is stored, so it could not be rebuilt).

```bash
hermes concord export --output concord-audit.json   # for reading, not restoring
```

Then either disable it (keeps the code, stops the tool and the command) or remove
the plugin directory (deletes the code, keeps `plugin-data/`). Disabling is a
config edit, and an explicit disable wins over the allow-list:

```yaml
# $HERMES_HOME/config.yaml
plugins:
  disabled:
    - concord-for-hermes
```

To delete the records too, delete `plugin-data/concord-for-hermes/` yourself -
nothing does it for you, and the plugin will not do it silently.

**Disabling stops protection; it does not resolve anything retrospectively.** An
`unknown` operation stays `unknown` and its object stays held in the store. That
is the point: a disabled plugin must not turn an ambiguous outcome into an
implicit "fine". Re-enable the plugin and run `hermes concord reconcile <id>` to
settle it from the provider's current state - the recovery path is documented
above and tested (`tests/test_c4_status.py`, disable/re-enable case).

## Network, subprocess and filesystem disclosure

- Network: operational calls run the configured GitHub CLI (`gh api`) against
  `github.com`, using GitHub's API endpoints to read and replace an existing
  allowlisted file and inspect commit attribution. Submitted data includes the
  owner/repository, branch, path, base blob SHA, replacement content and a commit
  message containing the operation ID. No telemetry or separate Concord server.
- Subprocesses: the plugin starts `gh --version` and `gh api` as argument lists,
  not shell strings. `gh_command` is trusted operator configuration and can name
  another executable or wrapper; do not accept it from untrusted input.
- Authentication: `gh` resolves the user's existing login or inherited credential
  environment. Concord does not fetch tokens through Hermes secret_scope and does
  not itself read, log or persist their values. This delegated credential behavior
  is disclosed for catalog review; approval under the catalog credential rule is
  not yet confirmed. No credential is bundled with this package.
- Local data: a SQLite coordinator stores operation identities, digests, receipts
  and history under the profile. `CONCORD_DB` can point outside that directory.
  CLI backup, restore and export use explicit operator paths. Request body files
  containing replacement content are created in the OS temporary directory for
  `gh --input` and removed after the call. See retention and removal above.
- No daemon, background service, self-update, remote code loading or core-tool
  override is installed. Synchronous provider calls have configured timeouts.
  Loading the plugin only registers a tool, CLI command and skill.

## Secrets

`gh` owns the credential. Concord never reads, copies, logs or persists a token;
it spawns `gh` with the environment it inherits and lets `gh` resolve its own
login. File content and commit messages travel through a `--input` body file,
never through argv, so they do not appear in a process listing. API calls explicitly pin `--hostname github.com`; an ambient `GH_HOST` cannot
redirect an allowlisted repository to another host. GitHub Enterprise is outside
this adapter's supported identity. Provider content and commit attribution are
read at one immutable commit, and a successful write must match the commit in
its receipt. Unavailable or malformed inline content fails closed. HTTP refusal
classification uses gh's trailing status, never a code mentioned inside an error
message. No provider data is sent to any other host by the adapter.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `concord: ... target_not_allowed` | The repository is not in `allowed_repositories`. Add it to `config.yaml`; no argument can do it for you. |
| `refused ... adapter_unavailable` (gh not found / too old) | Install a supported `gh` (>= 2.0.0) on PATH, or point `gh_command` at it. Nothing was sent. |
| `refused ... missing_base_revision` | The file does not exist on that branch. Creation is outside this action family. |
| `conflict`, holder is another operation | Two intents on one file. Inspect with `hermes concord status`; reconcile or wait, then start a new intent. |
| `unknown` | The write may have applied. Do not retry. Run `hermes concord reconcile <id>`. If the process that owned it is gone, add `--assume-dead-process` - that unlocks the read, not a release: an unchanged file is not proof the request will not land, so the operation stays held until the change is attributable to it. |
| `concord: ... coordinator unavailable (storage_locked)` | Another process held the store longer than the busy timeout. Retry; nothing was written. |
| `STORE UNREADABLE` in status | The database file is corrupt or not readable. Nothing is confirmed or denied. `status --json` and `export` now exit nonzero instead of reporting zero operations; fix access, or restore from a `hermes concord backup` file. |
| `STORE DAMAGED - PARTIAL SCHEMA` in status | Part of the Concord schema has gone missing (for example the operations table, or a required column) while other tables still hold rows. Nothing is confirmed or denied, `status --json` exits nonzero with `store.damage: "partial_schema"`, and `export` refuses to write. Restore from `hermes concord backup`, or repair the file. |
| `refused ... partial_schema` / `damaged_history` from a write | The same validation, on the write path: a required table/column/guard index is gone, or attempt rows name operations that have no record (or the sequence counter is behind the rows). Nothing was rebuilt, migrated or sent - the evidence is intact and the file is unchanged. Restore from `hermes concord backup`, or repair the file. |
| `hermes: 'concord' is not a hermes command` | The plugin is disabled or not installed in this profile. `hermes plugins list`, then enable it. |

## Tests

The suite drives the real plugin loader, the real CLI and the real tool
executor against a process-level fake `gh` (`tests/fakegh/gh.py`). No network, no
credential, no model. The source baseline has 353 tests; see `VERIFICATION.md`
for the exact candidate results and runtime pins. Test counts are properties of
the tested tree, not promises about later versions.

```bash
# Run from this repository with the tested Hermes environment activated.
python scripts/install_plugin.py                  # into ../isolated-home
python -m pytest tests/ -q
python scripts/release_checks.py                  # standalone release smoke checks
```

The isolated home installs two plugins: the product (`concord-for-hermes`) and
the C1 measurement fixture (`concord_probe`), which is kept so the earlier
evidence stays reproducible rather than merely reported.

## Offline demonstration and release handoff

`python scripts/release_demo.py` runs a short, credential-free walkthrough against
the bundled fake provider in a disposable store. It shows all five outcomes and
read-only recovery, with an 80-second execution budget. It is not a recorded demo
video or live GitHub proof. The full fresh-install check above separately tests
the real Hermes loader, CLI and tool executor.

Release scope is in `RELEASE-STATUS.md`. Test evidence is summarized in
`VERIFICATION.md`; private development reports are not included.

## Layout

```
plugin/                     the product plugin
  plugin.yaml               manifest: one tool, no privileged capability
  __init__.py               register(): tool + CLI command + skill
  concord_tool.py           the model-facing handler (one frame, architecture B)
  concord_result.py         the five result shapes, as a pure function
  concord_status.py         read-only store view (mode=ro, query_only)
  concord_cli.py            hermes concord status|reconcile|cancel|backup|restore|export
  concord_backup.py         restorable backup + verified restore (SQLite backup API)
  concord_store.py          store path + settings resolution
  concord_kernel.py         coordination kernel (C2)
  concord_gh_adapter.py     GitHub Contents adapter over gh api (C3)
  skills/write-file/        the agent-facing skill
tests/                      loader/CLI/executor integration tests
  fixtures/concord_probe/   the C1 measurement fixture (not the product)
  fakegh/gh.py              the process-level fake gh
scripts/                    install, child processes, evidence runners
  fresh_install_check.py    clean-archive install + quick-start acceptance
```

## Scope this does not cover

No universal exactly-once external effect. No cross-host or distributed
guarantee: two machines with separate stores do not coordinate. No lease expiry
or restart ever releases uncertain work. No protection for anything that does not
call this tool. Live GitHub evidence is limited to the two private write/replay
demonstrations; error strings, rate limits and every recovery path have not all
been tested against the live service.

## License

MIT licensed; see `LICENSE`.
Copyright (c) 2026 Null007Void | Null Foundry.
Third-party notices and dependency boundaries are in `THIRD-PARTY.md`.
No license to separately held private projects or additions is included.
