# Verification scope

This file describes evidence without exposing private repository identities or
live receipts. The source baseline is
`acf62042e7c9f5ee9a61ddc49a1790f6eb334a4a`. Release edits are sanitation,
documentation, the confirmed MIT license and an extracted-tree acceptance mode.
Runtime behavior is unchanged; the adapter edit is documentation only.

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
Publication changes from that archive are only documentation, finalized license
notices and the corresponding manifest. Executable Python and test bytes are
unchanged. The additional runtime did not run the full pytest suite.

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
