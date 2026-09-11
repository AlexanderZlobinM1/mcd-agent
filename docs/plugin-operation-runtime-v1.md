# Plugin Operation Runtime v1

MCD agent 1.2.11 stores `mcd-plugin-operation-runtime-v1` state for every
scheduled plugin operation and publishes it in
`mcd-state-v1.instances[].plugin_operations_runtime`.

Each record contains `operation_key`, `task_id`, `root`, `state`,
`last_started_at`, `last_finished_at`, `rc`, `duration_sec`, concise `stdout`
and `stderr`, `skipped_reason`, `next_run_at`, `resource_key`, `owner_pid`,
`heartbeat_at`, and `stale`.

Recurring operations use a dedicated scheduler lane. Campaign, import and other
unrelated long-running tasks do not consume this lane. A run may be skipped only
when its exact resource key is already owned; the state then reports
`resource_busy:<resource_key>`.

Generic wall-clock watchdog limits do not terminate recurring plugin commands.
While the owner process is alive MCD refreshes its heartbeat. Missing or changed
owner processes are marked lost/stale from process ownership evidence, not from
elapsed duration, so legitimate multi-day migrations and restores are retained.
