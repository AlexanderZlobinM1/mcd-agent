# MCD Technical Debt

## MCD-TD-001

- Status: open
- Title: Composer dependency write-path admission misses nested parent ownership drift
- Owner scope: MCD upgrade permissions and source-mutation admission; host recovery remains Operations-owned
- Source task: `01a07d46-213b-7e62-b910-08c7a9dacde1`
- Last updated: 2026-09-27

Observed with published MCD 1.2.78 in failed operation
`1d86050c-79ac-4349-b286-9ebd1f0648d6`. Composer ran as the runtime user and
could not unlink a dependency file because its immediate directory was owned
by root with mode 0755. The file itself and higher ancestors had runtime-user
ownership. No immutable attribute or read-only filesystem explained the failure.

`mautic_upgrade._pre_upgrade_permissions_check` delegates to configured
`fs_permissions_guard_paths`. Defaults protect runtime/config/media paths,
not Composer-managed plugin/vendor trees; deep checks cover only selected
runtime paths and stop at depth four. A successful generic permissions check
therefore did not establish actual dependency replacement capability.

Composer replacement is not transactionally rolled back by the patch executor.
Failure preceded the source-install callback: installed dependencies became
partial, while patch rollback reported no applied source patches to restore.
Do not interpret that result, extraction progress, or a target lock as a healthy
completed installation. A complete source backup of the original version was
not available in this incident.

Operations performed the single controlled recovery: narrow parent ownership
repair, completion from the unchanged existing lock under maintenance, the
original immutable patch plan and fresh exclusion proof, all consumer hooks,
normal finish/migration post-check, and asset/postimage/health verification.
The target installation, manifest and 32 referenced assets passed real HTTP
checks; managed consumers were restored only after verified success. The
original failed operation remains historical ERROR. This customer recovery
does not fix or close the generic admission defect.

Operational reports, recovery scripts and source/frontend snapshots remain
solely in the owning Operations project. Evidence locator: the operation ID
above, `continuation-result.json`, `recovery-result-assetgate.json`, and the
original immutable plan/proof in that project's canonical incident evidence.
Do not copy host-local recovery implementation or sensitive snapshots here.

Acceptance for a later MCD-owned change:

- Establish create/unlink/rename capability for the actual Composer runtime identity and controlling directories before live dependency replacement, including nested ownership drift beyond depth four.
- Bind checks and any normal repair to the selected canonical Composer/project/application layout and managed dependency paths; reject symlink escapes, immutable/read-only cases and unknown access evidence without blind recursive permission changes.
- Preserve immutable catalog selection, typed facts, target staging, phase order, idempotency, drift rejection, data boundaries and complete postimage verification.
- Cover nested parent-not-writable/file-writable, differing project/webroot layouts, already-correct paths and genuine pre-mutation failure through the upgrade entrypoint.
- Publish and verify the fix through the normal source/public mirror, package regression, test-machine and approved release route. Report partial Composer failure and rollback limits honestly.

Implementation checkpoint: 1.2.79 prepares Composer-managed directory ownership
and owner write/search permissions during ordinary execution, without a new
read-only admission gate. Nested directory, layout, idempotency, symlink escape
and actual filesystem failure fixtures pass. Source regression is a checkpoint,
not release acceptance; package/test-machine/approved verification remains open.
Parent and subproject indexes both remain open until publication is verified.

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

The corrected Operations input `generic-readiness-dependencies.json` is
published in canonical main `11ad9404e3290507e5c857802c4d1ef3b6e9db18`,
SHA-256 `01f4bbff011423dc52851c34549cd23990eee0f6eaacfa4ad86655cf85cb34bf`.
It supersedes the earlier total-role proposal. The 1.2.64 source candidate adds
typed integer-domain/count/migration facts, independent instance binding,
read-only admission, apply drift guards and complete-plan rollback barriers.
Disposable MySQL 8.0.46 and MariaDB 10.11.19 source checks passed; published
consumer acceptance and owner activation remain separate gates.
These accepted requirements remain tracked until their final acceptance:

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

Next action: release and replay the typed DB capability on the same disposable
databases, then obtain owner/MCC acceptance; complete remaining method/policy
and staged dependency predicates for their applicable version branches.
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
