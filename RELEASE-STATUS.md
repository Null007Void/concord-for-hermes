# Concord for Hermes 0.1.0

MIT licensed. Copyright (c) 2026 Null007Void | Null Foundry.
Independent plugin compatible with Hermes Agent, not an official Nous Research
product. No catalog acceptance, competition eligibility or prize/adoption terms
are claimed by publishing this source.

## Release boundary

This release includes the Hermes plugin, reproducible test fixtures/runners,
source documentation and notices. It excludes private development history,
personal Git identity metadata, credentials, local configuration, coordinator
databases, live receipts and private test-repository identities, recordings,
private plans, and separate commercial implementations.

The initial package was exported from the qualified source and given a new
public Git history. The subsequent catalog-review fix restricts both GitHub CLI
subprocess environments and adapts the offline fixture controls. It leaves the
write protocol, coordinator and plugin API unchanged. See VERIFICATION.md for
the checks on this delta.

## Operational disclosure

Concord coordinates enrolled existing-file replacements through one shared
SQLite store and the GitHub provider's conditional-write check. It does not
protect other tools, shell commands, separate stores or machines. Exact replay
must reuse the original operation ID, content and expected revision.

The README discloses GitHub CLI subprocesses, GitHub network requests, delegated
credential resolution, temporary request-body files and operator-selected local
paths. Acceptance of delegated gh authentication under the catalog's credential
rule remains subject to maintainer review. Passing automated checks does not
resolve that policy interpretation.

## Reproducible checks

See README and VERIFICATION.md for the tested pins, commands and proof limits.
RELEASE-MANIFEST.json lists SHA256 values for every source file except itself.
The new public history contains no earlier private commits. Existing third-party
notices remain in THIRD-PARTY.md. Root LICENSE and plugin/LICENSE are identical,
so the notice also travels with the installable plugin directory.
