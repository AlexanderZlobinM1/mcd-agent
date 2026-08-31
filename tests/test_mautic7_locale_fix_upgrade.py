from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.mautic_upgrade import _ensure_mautic7_locale_fix


def test_mautic7_upgrade_forces_only_locale_fix_safeguards() -> None:
    cfg = SimpleNamespace(
        php_bin="/usr/bin/php",
        mautic_run_as_user="www-data",
        command_timeout_sec=300,
    )

    with (
        patch("mcd_agent.mautic_upgrade.run_plugins_interactive") as install_plugin,
        patch(
            "mcd_agent.mautic_upgrade._pick_install_record",
            return_value=SimpleNamespace(mautic_major=7),
        ),
        patch(
            "mcd_agent.mautic_upgrade.execute_mautic_command_template",
            side_effect=[(0, "configured"), (0, "cache cleared")],
        ) as commands,
    ):
        _ensure_mautic7_locale_fix(cfg, "/var/www/example/public_html")

    assert install_plugin.call_args.kwargs["bundles"] == ["MauticLocaleFixBundle"]
    assert install_plugin.call_args.kwargs["action"] == "auto"
    configure = commands.call_args_list[0].kwargs["template"]
    assert "mautic:locale-fix:configure --published=1" in configure
    assert "--gmail-image-proxy-open=1" in configure
    assert "--calendar-enabled" not in configure
    assert commands.call_args_list[1].kwargs["template"] == "cache:clear"
