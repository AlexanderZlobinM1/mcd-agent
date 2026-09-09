# Preflighted manual single-instance upgrade

Minimum MCD version: **1.2.6**. The existing patch adapter contract and its
minimum version 1.2.5 remain unchanged.

MCC owns selection of the exact host and instance, plugin compatibility
preflight, supported/applicable patch selection and explicit operator risk
acknowledgement for an unsupported release. After those checks, it dispatches
one authenticated host job with an explicit root and target:

```sh
mcd-cli mautic-upgrade apply \
  --root /absolute/canonical/project/root \
  --mode composer --target 7.2.0 --allow-minor --yes \
  --patch-plan-json '<exact revision-pinned patch plan>' \
  --patch-run-id '<exact MCC job ID>' \
  --mcc-preflighted-single-instance
```

Use `--mode zip` only for a ZIP layout and a matching ZIP patch plan.
The flag is a trusted root operator's assertion of the completed MCC checks,
not cryptographic proof of origin or an independent MCD plugin/risk preflight.
Root already owns manual host work. Do not offer this flag in an ordinary,
unmanaged or bulk update workflow. Do not infer it from a release version,
active job, update channel or the existence of a patch plan.

MCD rejects non-root execution, non-apply use, absent/noncanonical roots, a
selected instance that differs from the explicit root, absent/unsafe run IDs,
nonexplicit modes and versions other than exactly 7.1.3 -> 7.2.0. Source
metadata must exist and agree with the installed version; metadata ambiguity
fails closed. The strict pinned plan must match the actual layout and exact
source/target. Duplicate keys, noninteger numeric values, unknown fields and
wrong registry revisions are rejected. The run ID uses the existing 1-96
character safe patch-run format. `--yes` and `--allow-minor` are required;
major and system-upgrade flags are forbidden.

Checks run before entering maintenance and again before the first source
mutation, including permission alignment and reverting a local core patch.
Failure restores only maintenance state owned by this invocation. A valid
manual invocation skips both independent global release-authorization
callbacks. No new callback, token or secret transport is introduced.

Without this explicit flag, MCD retains its existing global release checks.
A blocked 7.2.0 release must remain blocked for ordinary and bulk paths even
while a manual job is running. Neither invocation changes MCC release policy
or any agent channel pointer. Patch phases, fail-closed source gates and
`MCD_PATCH_PLAN_EVIDENCE=<JSON>` output remain unchanged.

Source rollback evidence covers patch files only. It does not promise a
database rollback or successful end-to-end disposable acceptance.
