from __future__ import annotations

from mcd_agent.runtime_matrix import build_runtime_profile


def test_four_runtime_layout_combinations_have_stable_distinct_styles() -> None:
    profiles = [
        build_runtime_profile(runtime=runtime, install_type=install_type)
        for runtime in ("host", "docker")
        for install_type in ("zip", "composer")
    ]
    assert {profile.style_token for profile in profiles} == {
        "host-zip",
        "host-composer",
        "docker-zip",
        "docker-composer",
    }


def test_host_zip_supports_composer_move_but_host_composer_does_not() -> None:
    zipped = build_runtime_profile(runtime="host", install_type="zip")
    composer = build_runtime_profile(runtime="host", install_type="composer")
    assert zipped.allows("composer-move")
    assert not composer.allows("composer-move")
    assert zipped.allows("core-upgrade")
    assert composer.allows("core-upgrade")
    assert zipped.allows("backup")
    assert composer.allows("backup")


def test_docker_composer_uses_image_upgrade_and_explicit_plugin_capability() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=[
            "console",
            "database",
            "plugin-read",
            "plugin-write",
            "bulk-operations",
            "image-managed-upgrade",
        ],
    )
    assert profile.allows("console-jobs")
    assert profile.allows("bulk-operations")
    assert profile.allows("plugin-install")
    assert profile.allows("image-sync")
    assert not profile.allows("core-upgrade")
    assert not profile.allows("composer-move")
    assert not profile.allows("runtime-package-mutation")


def test_unknown_layout_fails_closed_for_mutating_operations() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="unknown",
        capabilities=["console", "plugin-write", "migration-source"],
    )
    assert not profile.supported
    assert profile.style_token == "unsupported"
    assert not profile.allows("plugin-install")
    assert not profile.allows("migration-source")


def test_docker_migration_requires_explicit_adapter_capability() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=["database", "migration-adapter", "migration-source"],
    )
    assert profile.allows("migration-source")
    without_adapter = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=["database", "migration-source"],
    )
    assert not without_adapter.allows("migration-source")
