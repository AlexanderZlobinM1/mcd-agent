# Targeted Composer Preflight

## Problem

Plugin installation can require a small runtime package, currently most often
`nikic/php-parser`. Mautic instances also contain private VCS repositories from
the Sales-Snap plugin set. The agent runs Composer as `www-data`; that account
may not have credentials for those private repositories. A normal `composer
require` resolves the whole dependency graph and can therefore fail before the
requested public package is installed, with an HTTP Basic access error from an
unrelated private repository.

## Implemented behavior

The agent now handles each missing runtime package independently:

1. Read and retain the exact bytes of `composer.json` and `composer.lock`.
2. If `repositories` is a list, write a temporary Composer configuration with
   entries whose serialized value contains `git.sales-snap.com` removed.
3. Run `composer require <package> --no-update --no-interaction --no-scripts
   --no-progress`. This records the requirement without triggering a complete
   graph resolution.
4. Run `composer update <package-name> --with-dependencies --no-interaction
   --no-scripts --no-progress`, limiting resolution to the requested package.
5. On success, restore the original private repository list while keeping the
   new root package requirement, so `composer.json` and `composer.lock` remain
   consistent. If either Composer command fails, restore both original files.

The package is installed and locked normally. Only unrelated private VCS
repositories are omitted during the targeted resolution window; they remain in
the instance configuration afterward. Composer scripts stay disabled for this
preflight, so plugin installation does not execute arbitrary project scripts as
a side effect.

## Scope and safety

This is an agent-level preflight used by all plugin operations that request
runtime Composer packages. It is not a Mautic core or plugin source change.
The operation is skipped when the package is already installed, and ordinary
Composer behavior is unchanged for other agent workflows. The Mautic console
health checks before and after the operation remain mandatory.

## Regression coverage

`tests/test_amazon_mailer_dep_safety.py` verifies that the two-command shape is
used and that the original private repository configuration is restored after a
successful targeted update. Failure-path lock restoration is covered by the
same helper's exception handling and should be retained in future changes.
