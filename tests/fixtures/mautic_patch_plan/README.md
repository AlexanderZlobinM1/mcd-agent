# Mautic role gate acceptance fixtures

These are unmodified upstream Mautic files (GPL-3.0), downloaded from official
release tags. They are small regression fixtures, not an upstream checkout.
They match the source files independently collected by MCC from the disposable
Composer acceptance instance on 2026-09-09.

- `https://raw.githubusercontent.com/mautic/mautic/7.2.0/app/migrations/Version20211209022550.php`
  SHA256: `f970321517fa32eed01a031f5110f397e441bb049965efdbbeece7750df4d33c`.
- `https://raw.githubusercontent.com/mautic/mautic/7.1.3/app/migrations/Version20211209022550.php`
  SHA256: `6cc316e8fc611c58c8b6d9120b6d8b0e2c921c151c42bf29e0faaa141262748b`.
- `https://raw.githubusercontent.com/mautic/mautic/7.2.0/app/bundles/CoreBundle/MauticCoreBundle.php`
  SHA256: `8887573631466fec3a99329ba044f8f5fe8e093649685d3fe8961ce8e9cca651`.

`mcc-e74cdc2b-plan.json` is the exact sanitized input from the MCC evidence
handoff, including its dependency and phase lists. It is independent of the
adapter constants. ZIP cases change only `install_type`.

The original role migration is vulnerable: the loop is indented eight spaces
and its first statement twelve. MCD 1.2.4 searched for four spaces after the
newline and reported 0/0. Removing a docblock cannot fix this discrepancy.
After applying the canonical hydrated-row handling, the expected full-file SHA
is `b690b3cdd927a9b8257572cbb7bc42aba79f6c8b90d1ce39bac3154a928f2328`.
