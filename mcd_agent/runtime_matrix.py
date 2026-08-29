from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


MATRIX_SCHEMA = "mautic-runtime-matrix-v1"
KNOWN_RUNTIMES = frozenset({"host", "docker"})
KNOWN_INSTALL_TYPES = frozenset({"zip", "composer"})

_STYLE_TOKENS = {
    ("host", "zip"): "host-zip",
    ("host", "composer"): "host-composer",
    ("docker", "zip"): "docker-zip",
    ("docker", "composer"): "docker-composer",
}

_HOST_DEFAULT_CAPABILITIES = frozenset(
    {
        "console",
        "database",
        "filesystem",
        "plugin-read",
        "plugin-write",
        "migration-source",
        "migration-target",
        "bulk-operations",
        "host-managed-upgrade",
    }
)


def normalize_runtime(value: object) -> str:
    return str(value or "host").strip().lower() or "host"


def normalize_install_type(value: object) -> str:
    return str(value or "unknown").strip().lower() or "unknown"


def normalize_capabilities(values: Iterable[object] | None) -> frozenset[str]:
    return frozenset(
        value
        for value in (str(item or "").strip().lower() for item in values or ())
        if value
    )


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    runtime: str
    install_type: str
    capabilities: frozenset[str]
    operations: frozenset[str]
    style_token: str
    supported: bool
    blockers: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.runtime}+{self.install_type}"

    def allows(self, operation: str) -> bool:
        return str(operation or "").strip().lower() in self.operations

    def safe_dict(self) -> dict[str, object]:
        return {
            "schema": MATRIX_SCHEMA,
            "key": self.key,
            "runtime": self.runtime,
            "install_type": self.install_type,
            "style_token": self.style_token,
            "supported": self.supported,
            "capabilities": sorted(self.capabilities),
            "operations": sorted(self.operations),
            "blockers": list(self.blockers),
        }


def build_runtime_profile(
    *,
    runtime: object,
    install_type: object,
    capabilities: Iterable[object] | None = None,
) -> RuntimeProfile:
    runtime_name = normalize_runtime(runtime)
    install_name = normalize_install_type(install_type)
    declared = normalize_capabilities(capabilities)
    effective = _HOST_DEFAULT_CAPABILITIES if runtime_name == "host" and not declared else declared
    blockers: list[str] = []
    if runtime_name not in KNOWN_RUNTIMES:
        blockers.append(f"unsupported runtime: {runtime_name}")
    if install_name not in KNOWN_INSTALL_TYPES:
        blockers.append(f"unsupported install type: {install_name}")

    operations: set[str] = set()
    if "console" in effective:
        operations.update({"console-jobs", "cache", "reset-password"})
    if "database" in effective:
        operations.add("database-operations")
    if {"database", "filesystem"}.issubset(effective):
        operations.add("backup")
    if "plugin-read" in effective:
        operations.add("plugin-inventory")
    if {"plugin-write", "console"}.issubset(effective):
        operations.add("plugin-install")
    if {"migration-source", "database"}.issubset(effective) and (
        "filesystem" in effective or "migration-adapter" in effective
    ):
        operations.add("migration-source")
    if {"migration-target", "database"}.issubset(effective) and (
        "filesystem" in effective or "migration-adapter" in effective
    ):
        operations.add("migration-target")
    if {"bulk-operations", "console"}.issubset(effective):
        operations.add("bulk-operations")

    if runtime_name == "host":
        if "filesystem" in effective:
            operations.add("filesystem-operations")
        if install_name in KNOWN_INSTALL_TYPES and "host-managed-upgrade" in effective:
            operations.add("core-upgrade")
        if install_name == "zip" and {"filesystem", "database", "console"}.issubset(effective):
            operations.add("composer-move")
        if install_name in KNOWN_INSTALL_TYPES and "filesystem" in effective:
            operations.add("runtime-package-mutation")
    elif runtime_name == "docker" and "image-managed-upgrade" in effective:
        operations.add("image-sync")

    if runtime_name not in KNOWN_RUNTIMES:
        operations.difference_update(
            {
                "core-upgrade",
                "composer-move",
                "filesystem-operations",
                "image-sync",
                "migration-source",
                "migration-target",
                "plugin-install",
                "runtime-package-mutation",
            }
        )
    elif install_name not in KNOWN_INSTALL_TYPES:
        operations.difference_update(
            {
                "core-upgrade",
                "composer-move",
                "image-sync",
                "migration-source",
                "migration-target",
                "plugin-install",
                "runtime-package-mutation",
            }
        )

    return RuntimeProfile(
        runtime=runtime_name,
        install_type=install_name,
        capabilities=effective,
        operations=frozenset(operations),
        style_token=_STYLE_TOKENS.get((runtime_name, install_name), "unsupported"),
        supported=not blockers,
        blockers=tuple(blockers),
    )
