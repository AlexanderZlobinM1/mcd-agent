# Guarded Mautic upgrade preflight v1

`mcd-cli mautic-upgrade preflight --root INSTANCE --mode composer --target VERSION --json`
emits one compact `MCD_UPGRADE_PREFLIGHT_EVIDENCE=` line with schema
`mcd-mautic-upgrade-preflight-v1`. The evidence includes `run_id`, `root`,
`source_version`, and `target_version` and is intended for MCC persistence and
orchestration.

Version detection in this contract is strictly read-only: MCD reads its
version cache, `release_metadata.json`, or `composer.lock`. Static on-disk
metadata takes precedence over the cache in both directions; conflicting
static sources fail closed. It does not run
`bin/console`, Symfony bootstrap, cache warmers, migrations, or write version
cache files. If static evidence is unavailable, the marker is
`status=needs_attention` with `version_source=unavailable_read_only`.

`mcd-cli mautic-upgrade check --root INSTANCE` is also bootstrap-free and emits
`MCD_UPGRADE_VERSION_EVIDENCE=` with `current_version` and `version_source`.
Inventory refresh uses the same authoritative metadata and may rewrite the MCD
version cache downward after a rollback.

The `composer.php` object also exposes the existing target-runtime policy as
`required_version`, `compatible`, `status`, `remediation`, and `decision`.
Mautic 7 uses the established PHP 8.4 policy and Mautic 6 uses PHP 8.3. An
incompatible runtime is blocked before Composer/package work; with
`--with-system-upgrade`, preflight returns `decision=allow_with_system_upgrade`
and apply performs the guarded system runtime stage before package work.

The `composer` object uses `mcd-mautic-composer-readiness-v1`. It contains the
effective PHP path/version, Composer path/version, minimum compatible Composer
version, and one of these statuses: `missing`, `incompatible`,
`download_failure`, `signature_failure`, `bootstrap_failure`, `reused`, or
`success`. Preflight never bootstraps Composer. The apply path may bootstrap
only the MCD-pinned Composer artifact after verifying its official SHA-256
checksum; UI/MCC input cannot provide a URL, checksum, or command.

For a Mautic 6 to 7 target, `json_schema_repair` uses
`mcd-mautic-json-schema-repair-v1`. It reports the database server/version,
resolved table prefix, the declared affected JSON columns, read-only JSON
validity counts, and the backup prerequisite. Its diagnosis is
`supported`, `unsupported`, or `needs_attention`.

An optional `--repair-plan-json` is accepted only when it matches
`mcd-mautic-json-schema-repair-plan-v1`, the SQLSTATE 1253 condition, Mautic
6-to-7 version pair, declared table prefix, declared column allowlist, and
`normalize_declared_json_columns` action. The plan is validated but not
executed by this contract. Arbitrary SQL, shell commands, migration-history
deletion, and blind retries are not part of the interface.

If Composer is missing or incompatible, MCC may invoke
`mcd-cli mautic-upgrade composer-prepare --root INSTANCE --mode composer --target VERSION --json`.
That operation performs only the pinned verified Composer bootstrap and emits
the same evidence marker; its download, signature and bootstrap failure states
remain distinct.

The guarded `mautic-upgrade apply` path accepts the plan only together with a
root-owned signed MCC context file (`--repair-auth-context-file`) and its
configured signing key. After the external backup gate and before Composer or
Doctrine work it emits one compact
`MCD_JSON_REPAIR_EVIDENCE=` line with schema
`mcd-mautic-json-schema-repair-execution-v1`. The executor applies only the
declared `normalize_declared_json_columns` action, in canonical order, and
records sanitized before/after schema, JSON-validity counts, applied/no-op
columns, backup identity, rollback capability and an explicit status. It never
accepts arbitrary SQL, shell commands, migration-history deletion or blind
retry.

MCC obtains that context without distributing a secret to the host by calling:

`mcd-cli mautic-upgrade authorize-repair --root INSTANCE --target VERSION --repair-plan-json PLAN --backup-manifest-path PATH --json`

The operation verifies the actual `backup instance-run` `.mcd-backup.json`
marker, instance UID, root, completion age, `status=ok`, manifest SHA-256 and
restorability, then creates a short-lived root-owned context file. The backup
command exposes `backup_id`, `manifest_path`, `sha256`, and `completed_at` for
this handoff. The signing key is generated and retained locally by MCD with
root-only permissions; MCC never receives it.
