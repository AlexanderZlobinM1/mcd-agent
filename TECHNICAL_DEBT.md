# MCD Technical Debt

## MCC-TD-001

- Status: open
- Title: Typed applicability and rollback predicates for catalog upgrade readiness
- Owner scope: MCD agent generic executor; dependency of the existing project catalog debt
- Source task: `01a07d46-213b-7e62-b910-08c7a9dacde1`
- Last updated: 2026-09-26

MCD 1.2.63 implements catalog-only source execution, exact gate field schemas,
application-root binding, Git binary payloads, safe phase rollback, isolated
target staging and signed major-upgrade repair backup attestation. Legacy source
payloads and unverified DB metadata rewrites are retired. The owner-published
4,126,599-byte Composer payload passed isolated consumer acceptance; that record
remains disabled until its owner accepts the published consumer evidence.

The Operations input `generic-readiness-dependencies.json` is published in
canonical ref `8bbbd54980b0f571788690c6bd3140c4056fda02` and retained in the
later canonical ref `3681e699ba6bdd387f6e9d6741c2fc6f1b3bc3ec`.
These requirements are accepted and remain unimplemented typed capabilities:

- Read selected-instance non-admin roles count with the typed filter `is_admin=0`; pending migration and count `>0` require the source patch. One non-admin role triggers; one admin role alone skips. The earlier total-role `>1` proposal is superseded and cannot establish readiness.
- Read configured-prefix Doctrine migration storage with exact namespace/encoding; missing or unknown schema blocks.
- Require the relevant migration to be pending before apply and still pending before source rollback.
- Apply the same executed-migration rollback barrier to dependent DDL/index migrations.
- Validate dependency package/version facts and method-scoped predicates before enabling records requiring them.

Current v3 file gates do not substitute for these facts. Nonempty Git parameters
are rejected rather than ignored. Owner records requiring these capabilities
stay disabled and remain explicit MCC upgrade-readiness blockers. The database
`plugins.metadata` normalization scenario remains disabled: upstream source
does not establish that column as PluginUpdateEvent metadata.

Next action: publish typed field schemas with the evidence owner, implement
read-only facts and rollback barriers, test missing/ambiguous/executed states,
release the supported capability, then obtain owner/MCC readiness acceptance.
Source-only acceptance and an MCD package release do not close this debt or
prove full customer-upgrade readiness. Parent aggregate and this record retain
the same debt identity; MCC implementation details remain with MCC.

## MCC-TD-003

- Status: open
- Owner scope: MCD Composer preparation; MCC admission and disposable orchestration stay with MCC
- Last updated: 2026-09-26

Acceptance requires a disposable M6-to-7 upgrade with runtime-user Composer
execution, verified installer/package identity, exact owning project/lock root,
staged target version and lock proof, original-source drift rejection and
terminal failure evidence. Local layout/staging tests and package regression
are checkpoints, not this end-to-end acceptance. No customer upgrade is implied.

## MCC-TD-004

- Status: open
- Owner scope: MCD schema-repair guard and backup attestation; MCC orchestration stays with MCC
- Last updated: 2026-09-26

Acceptance requires a disposable M6-to-7 database fixture with selected-instance
schema/prefix proof, explicit JSON compatibility diagnostics, signed completed
backup bound to the immutable plan and rollback evidence. Reject missing,
expired, forged, wrong-instance and hash-mismatched attestations before source
mutation. M7-to-7 must retain baseline requested backup even when repair
attestation is not required. Existing unit checks do not close the database
end-to-end gate; serialized Role permissions must never be normalized as JSON.
