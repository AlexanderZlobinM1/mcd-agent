# Import tag workaround

Reviewed on 2026-09-05 against the immutable Mautic release tags 7.0.2,
7.1.3 and 7.2.0. All retain the existing-tag assignment in
`app/bundles/LeadBundle/Model/LeadModel.php::modifyTags`:

```php
$tagToBeAdded = $foundTags[$tag];
```

The workaround reacquires a managed Doctrine reference by tag ID before adding
it to the contact. It does not create a new tag or change import criteria.

- Upstream fix: https://github.com/mautic/mautic/pull/17209
- Released source: https://github.com/mautic/mautic/blob/7.2.0/app/bundles/LeadBundle/Model/LeadModel.php
- Release notes: https://github.com/mautic/mautic/releases/tag/7.2.0

PR 17209 was open and unmerged at review time. Its regression test supplies a
detached existing Tag and verifies that the contact receives the managed
reference instead. The proposed fix is absent from 7.2.0.

MCD reconciles this workaround by default during inventory planning, including
passive hosts. It skips a scheduler maintenance pause and Docker instances.
Only 7.0, 7.1 and 7.2 stable version signatures and one exact assignment with
its enclosing branch are accepted. Shipped release metadata takes precedence
over stale local configuration. Unknown or upstream-rewritten code is skipped.

The legacy CLI name and backup metadata identifier remain compatible with
existing 7.1.3 installations. A file lock serializes manual and automatic patch
operations without blocking the daemon. Core updates refresh the rollback
generation; rollback checks both original and patched hashes. An unmarked
upstream fix is never reverted. Tests cover the complete upgrade sequence:
restore, replace core, reapply, then resume scheduled work.
