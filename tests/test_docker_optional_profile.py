from __future__ import annotations

from types import SimpleNamespace

from mcd_agent import service_profiles


def test_docker_profile_is_builtin_and_only_planned_on_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(
        service_profiles,
        "_docker_runtime_state",
        lambda: {
            "installed": False,
            "path": "",
            "version": "",
            "active": False,
            "daemon_reachable": False,
        },
    )

    result = service_profiles.service_profiles_apply_once(
        SimpleNamespace(cluster_id=""), component="docker", dry_run=True
    )

    assert result["status"] == "ok"
    assert result["fetch"] == {"status": "ok", "source": "builtin"}
    assert result["apply"]["status"] == "planned"
    assert ["apt-get", "install", "-y", "docker.io"] in result["apply"]["commands"]


def test_docker_profile_noops_when_daemon_is_ready(monkeypatch) -> None:
    state = {
        "installed": True,
        "path": "/usr/bin/docker",
        "version": "Docker version 27.0.0",
        "active": True,
        "daemon_reachable": True,
    }
    monkeypatch.setattr(service_profiles, "_docker_runtime_state", lambda: state)

    result = service_profiles.service_profiles_apply_once(
        SimpleNamespace(cluster_id=""), component="docker", dry_run=False
    )

    assert result["status"] == "ok"
    assert result["apply"]["status"] == "noop"

