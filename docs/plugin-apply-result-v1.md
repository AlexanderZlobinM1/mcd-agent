# Plugin Apply Result v1

MCD agent 1.2.9 exposes `mcd-plugin-apply-result-v1` for plugin filesystem
operations. It writes newline-delimited callback/result records as:

```text
MCD_PLUGIN_APPLY_RESULT=<JSON>
```

After selected plugin files are installed, replaced or removed, MCD immediately
flushes a non-terminal record with `event=files_applied`, `files_applied=true`,
`post_step_status=pending`, `cache_inventory_confirmed=false`, and
`overall_status=pending`.

MCD then runs cache clear, native plugin install/reload, cache warmup and local
plugin inventory confirmation. Only completion of every required step emits a
terminal record with `post_step_status=success`,
`cache_inventory_confirmed=true`, and `overall_status=success`.

For install and update actions, inventory confirmation requires exact equality
between each selected `expected_version` and the corresponding
`installed_version`. A catalog `OK` status, bundle presence, or a no-op file
phase is not sufficient. No-op file phases still run post-steps and exact
inventory confirmation before terminal success.

Starting with MCD 1.2.13, cluster operations follow the same callback contract.
A cluster no-change, delegated-success or wait-success path cannot return
`rc=0` before emitting a terminal result containing one exact confirmed
inventory row per selected bundle. The reference no-change path still verifies
file synchronization and runs configured post-steps before confirmation.

Any exception or nonzero result from SQL fixes, cache clear, plugin
install/reload, cache warmup or inventory confirmation emits a terminal record
with `post_step_status=failed`, `cache_inventory_confirmed=false`, and
`overall_status=failed`, then keeps the command exit nonzero. A plugin version
visible in later inventory never overrides this terminal failure.

Every object contains `schema`, `operation`, `operation_id`, `root`, `action`,
`event`, `terminal`, `selected`, `files_applied`, `post_step_status`,
`cache_inventory_confirmed`, `overall_status`, `inventory`, and `error`.
`rc` is `null` for the non-terminal callback, `0` for terminal success and
nonzero for terminal failure.
Consumers correlate records by `operation_id`, treat only `event=completed` as
terminal, and must never derive success from inventory when terminal
`overall_status=failed`.
