# Contact field metadata report v1

MCD 1.2.29 publishes the read-only `mcd-contact-field-metadata-v1`
capability for MCC report jobs.

```bash
mcd-cli report:contact-field-metadata --root <root-or-instance-uid> --json
```

The command selects one locally discovered Mautic instance and emits one JSON
object. A successful result contains `schema`, `schema_version`, `capability`,
`mcd_version`, `mautic_version`, `status`, `generated_at`, instance identity
fields, `field_count`, `fields`, and an empty `errors` array. Each field
contains only:

- `alias`, `label`, `type`, `custom`, and the equivalent explicit
  `classification` (`native` or `custom`) and `field_type` fields;
- nullable `group` and `object` values exposed by Mautic's field definition;
- nullable `storage_type`, `max_length`, `numeric_precision` and
  `numeric_scale` values exposed by the database system catalog.

The collector reads `lead_fields` and `information_schema.columns`. It does not
read contact rows. It does not select field defaults, serialized properties,
select options or any stored field values. `classification` maps Mautic's
`fixed=1` definitions to `native`; all other definitions are `custom`.

An instance failure returns exit status 1 and the same top-level envelope with
`status=error`, empty `fields`, `field_count=0`, and one or more structured
`errors` entries containing `code`, `message` and `retryable`. MCC should check
the exact `mcd-contact-field-metadata-v1` value in both `runtime_capabilities`
and `runtime_profile.operations`, and require MCD 1.2.29 or newer before
dispatching this report.

The contract targets the shared Mautic 6 and 7 `lead_fields` schema. A field
whose alias has no physical `leads` column remains present, with nullable
storage metadata. The report is instance-local and does not aggregate or infer
fields across hosts.
