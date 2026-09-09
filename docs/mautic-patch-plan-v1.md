# Mautic patch-plan boundary (MCD 1.2.5)

The input remains `mcd-mautic-patch-plan-v1`, pinned to Mautic-Operations
registry revision `8829d322409c66f8ec9e9abf57c9ac42a19022cc`. The exact sanitized
MCC input is retained in `tests/fixtures/mautic_patch_plan/mcc-e74cdc2b-plan.json`.
It contains no paths, commands or patch payloads. The two supported IDs are
`M7-ROLE-PERMISSIONS-HYDRATED-ROW` and `M7-ASSET-MAPPER-WEBROOT`.

## Commands

```sh
mcd-cli mautic-patch-plan contract --json
mcd-cli mautic-patch-plan verify --root INSTANCE_ROOT --plan-json PLAN_JSON --phase post_source_install --run-id RUN_ID --json
mcd-cli mautic-upgrade apply --root INSTANCE_ROOT --mode composer --target 7.2.0 --allow-minor --yes --patch-plan-json PLAN_JSON --patch-run-id RUN_ID
```

`zip` is also a supported install type. The monolithic command invokes role
handling after source replacement, checks it again before migration finish,
and invokes AssetMapper handling before deferred application scripts. The
existing MCC release authorization remains mandatory.

Standalone `verify` emits JSON with `status`, `reason` on error, `run_id`,
`phase`, `registry_revision`, `plan_sha256`, `resolved_source_root`, and
`patches`. Each gate includes the stable ID, state, gate rule, target relative
path, SHA and vulnerable/fixed occurrence counts. Exit code is 0 for success
and 2 for a rejected gate. `apply` additionally returns backup before/after
SHA and per-patch gate evidence. Upgrade output includes one
`MCD_PATCH_PLAN_EVIDENCE=<JSON>` line per phase, including a failing phase
before the command raises an error; it is not a single JSON document.

## Role gate correction

The disposable Composer source matched the official 7.2.0 migration byte for
byte. Its SHA is
`f970321517fa32eed01a031f5110f397e441bb049965efdbbeece7750df4d33c`.
This is a vulnerable file, not an upstream-fixed signature. MCD 1.2.4 matched
four spaces after the loop newline, whereas the real statement has twelve.
Consequently it reported vulnerable/fixed counts 0/0.

MCD 1.2.5 requires both that vulnerable SHA and exactly one complete executable
block with the actual indentation. After canonical hydrated-row handling,
the file must have SHA
`b690b3cdd927a9b8257572cbb7bc42aba79f6c8b90d1ce39bac3154a928f2328`
and one complete fixed block. Only that verified fixed state is `already`.
Unknown, duplicate, mixed, partial, old-version and otherwise changed files
are errors. Version discovery reads Mautic package or release metadata and
does not accept an arbitrary `7.2.0` string elsewhere in a file. ZIP files
may include composer.json without being classified as Composer installs.

## Evidence and recovery

`ROOT/.mcd/patch-runs/RUN_ID/result.json` preserves phase history and original
backup records through repeated successful checks. Pending-write intent is
saved before source replacement. PHP lint runs on staged files; unchanged
source hashes are rechecked before replacement. A pending or failed run
requires explicit recovery, rather than silently treating it as completed.

```sh
mcd-cli mautic-patch-plan rollback --root INSTANCE_ROOT --plan-json PLAN_JSON --run-id RUN_ID --json
```

Rollback verifies all known backup paths/checksums and current-file checksums
before restoring files. It rejects files changed since the run. It restores
patch source files only, not a database migration or a complete upgrade.
After rollback, use a new run ID for a new attempt.

## Acceptance scope

The regression fixtures are complete official source files with independently
captured SHA values, plus the exact MCC payload. PHP execution tests demonstrate
the original hydrated-array failure and corrected handling of array/direct
Role results. Tests also cover both layouts, repeat phases, failure evidence,
interruption recovery and checksum-protected rollback.

These fixtures validate patch behavior and the MCD command contract. A full
disposable database upgrade remains the cross-component acceptance gate.
Publication of 1.2.5 is an off-catalog, one-host candidate for
`host-46-224-209-196`; global channel pointers must not move for this acceptance.
