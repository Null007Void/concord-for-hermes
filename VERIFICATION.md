# Verification scope

This file describes evidence without exposing private repository identities or
live receipts.

## Initial public release (historical evidence)

The initial source baseline is
`acf62042e7c9f5ee9a61ddc49a1790f6eb334a4a`. Release edits are sanitation,
documentation, the confirmed MIT license and an extracted-tree acceptance mode.
That initial publication did not change runtime behavior; its adapter edit was
documentation only. The catalog-review fix below changes subprocess behavior.

Historical baseline: 353 tests, 28 acceptance checks and 5 release smoke checks
passed on Windows/Python 3.14.7 with Hermes
`c5f347ebe9b840c3dbe5ec99bbe8e36871f24393`. A separate current-runtime check at
`0b82ddc96c39449d30763d0fdd50ac22fed0fb31` passed 15 native validation checks,
doctor and the installed offline rehearsal. These prior results do not replace
fresh candidate testing. The history-free source archive
`dd1cfd4cab390ac9806048b851105f4bbca9caa44d315fe8b26f304ac482a2c2`
then passed 353 tests (one non-failing warning), 28 clean-install checks and five
smoke checks on the baseline runtime. It passed 25 clean-install checks on the
additional runtime and 15 native checks with dependency preparation, with zero
native warnings and a safe security scan. Its official payload scan passed.
Initial publication changes from that archive were only documentation, finalized license
notices and the corresponding manifest. Executable Python and test bytes were
unchanged in that publication. The additional runtime did not run the full pytest suite.

Two owner-operated private GitHub existing-file write/replay demonstrations on
November 8, 2026 observed verified receipts and an exact replay without another
write. They did not demonstrate all error paths or a live stale-change conflict.
The final narrated video was owner-approved. No new model requests, live GitHub
calls or external writes are part of release packaging.

Offline tests use a fake provider subprocess with the actual Hermes loader,
CLI and tool executor. The offline AIAgent object is a scripted stub and its
guardrail manager is a permissive double; these tests do not prove live model
behavior or the production approval system. An offline pass is not a live-service guarantee. Catalog
acceptance, public-repository installation, challenge eligibility and final legal
terms have not been verified by those tests.

The official catalog validator/payload scanner and policy snapshot were checked
at `c59b667c76dc75e07bb96e98630de78c14d7a008`. The native validator and plugin
guard files in the additional runtime match that snapshot byte-for-byte. This
is not a claim that every file of the latest Hermes runtime was tested.

## Catalog-review environment fix

The review baseline is public commit
`7f43f84e423b9db143f4ae68d88b517483a5513e`. Both `gh --version` and `gh api`
now use the same explicit environment allowlist. The fake CLI reads fixture
controls from its own test `GH_CONFIG_DIR` instead of passing arbitrary test
variables through the production subprocess environment.

Six new regression tests cover exact POSIX/Windows allowlists, unset and empty
values, fresh environment copies, and actual child processes on both invocation
paths. The child-process regression uses only synthetic credentials and checks
that `OPENAI_API_KEY` and other unrelated variables never arrive while allowed
values remain intact. Windows environment-name normalization is accounted for.

Fresh offline verification on November 10, 2026:

- Windows / Python 3.14.7 / Hermes
  `c5f347ebe9b840c3dbe5ec99bbe8e36871f24393`: the documented install plus full
  pytest suite passed **359 tests and 4 subtests** in 347.24 seconds. One upstream
  `plugins_manifest.py` deprecation warning concerned positional `re.split`
  arguments; no test failed.
- A separate `python scripts/fresh_install_check.py --source-tree` run passed
  **28 checks, 0 failures**, including native Hermes validate/doctor, installed
  execution and another full suite: **359 tests and 4 subtests** in 315.86 seconds,
  with the same one warning.
- `python scripts/release_checks.py`: **5 checks passed**, both directly and from
  the fresh exported tree.
- Linux / Python 3.12.14: the **6 new environment tests** and **5 release smoke
  checks** passed. All **72 Python sources** parsed. This was not a full Hermes
  integration-suite run on Linux.
- The actual-child regression fails on both invocation paths against the
  unmodified public baseline and passes with this fix. Native Windows and Linux
  child processes were both exercised.
- The official payload scanner from Hermes
  `022a14437b2dcd40fb71024b754b59c94afed70e` passed on the plugin payload.
  `git -c core.whitespace=cr-at-eol diff --check` passed, preserving the baseline's
  CRLF files. The release manifest was refreshed for the changed/new source.

Final verification reporting and README edits were made after the full runs;
executable and test bytes are identical to those tested. No live-model request,
GitHub API write or new production credential flow was tested. These checks do
not establish catalog admission or a remote CI pass.
