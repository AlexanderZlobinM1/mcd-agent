"""Legacy source restoration requires a catalog-declared verified descriptor."""


def restore_retired_mcd_core_patches(install):
    return {"status": "skip", "root": install.root, "reason": "legacy_restore_requires_catalog_descriptor"}
