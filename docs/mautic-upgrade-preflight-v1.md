# Guarded Mautic upgrade preflight v1

`mcd-cli mautic-upgrade preflight --root INSTANCE --mode composer --json`
emits one compact JSON object with schema `mcd-mautic-upgrade-preflight-v1`.
The result is read-only and is intended for MCC persistence and orchestration.

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
