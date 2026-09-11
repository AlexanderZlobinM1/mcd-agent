# Plugin Cache Reset v1

MCD agent 1.2.8 exposes capability and evidence schema
`mcd-plugin-cache-reset-v1` for plugin filesystem operations.

After plugin files are removed, installed or replaced and before the first
Symfony console boot, MCD atomically detaches the active Composer/docroot
`var/cache/prod` or legacy `app/cache/prod` directory and immediately recreates
it with the instance runtime owner/group and mode `0775`. The detached cache is
disposable and is deleted best-effort; concurrent Symfony writes during that
deletion cannot fail the plugin job. The normal cache clear and native plugin
reload then generate a clean cache from the current plugin filesystem.

The cache is disposable runtime output and is never snapshotted or restored.
If cache clear or plugin reload fails, the stale compiled generation remains
deleted so a later plugin job or web request cannot inherit references to code
that is no longer present. Ordinary `remove` retains plugin integration settings;
`purge` remains the separately selected destructive settings operation.

Every reset emits one line:

```text
MCD_PLUGIN_CACHE_RESET_EVIDENCE=<JSON>
```

The object contains `schema`, `operation`, `root`, `status`, `reset_paths`,
`cleanup_pending_paths`, and `mode`. A pending path is disposable cache garbage,
not a snapshot or recovery source.
