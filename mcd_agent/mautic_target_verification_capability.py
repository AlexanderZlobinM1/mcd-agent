"""Runtime advertisement of the file-only read-only collector contract."""
from mcd_agent import __version__


def capability():
    return dict(schema="mcd-mautic-target-patch-verification-capability-v1",
        agent_version=__version__, features={"target_patch_verification_v1": True},
        contract_sha256="42f29b668117316870a698f144a1d8a066b4775d1f9c3449c5967adc830ce1e2",
        command="mcd-cli mautic-target-patch-verify", read_only=True,
        original_plan_database_facts_supported=False, online_admission_required=True,
        purpose="verify_target_excluded_patches", dispatch_authority=False)
