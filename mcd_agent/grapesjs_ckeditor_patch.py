"""Compatibility entry point for catalog-managed asset remediation."""
from mcd_agent.mautic_patch_runtime import run


def ensure_grapesjs_ckeditor_gpl_patch(install, config=None):
    return run(config, install, phase="before_asset_generation")
