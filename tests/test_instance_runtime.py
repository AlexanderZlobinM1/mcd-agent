from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import instance_runtime
from mcd_agent.models import MauticInstall


class InstanceRuntimeTest(unittest.TestCase):
    def _install(self, root: Path) -> MauticInstall:
        docroot = root / "docroot"
        docroot.mkdir(parents=True)
        return MauticInstall(
            instance_uid="merkurosiguranje.sales-snap.com",
            name="merkurosiguranje",
            root=str(root),
            console_path=str(root / "bin" / "console"),
            primary_domain="merkurosiguranje.sales-snap.com",
            mautic_timezone="Europe/Belgrade",
            domains=["merkurosiguranje.sales-snap.com"],
        )

    def _fake_run(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    def test_materializes_pool_include_symlink_and_nginx_socket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            include_file = php_etc / "8.3" / "fpm" / "pool.d" / "99-mcd.conf"
            pool_link = php_etc / "8.3" / "fpm" / "pool.d" / "mcd"
            pool_file = generated / "php" / "8.3" / "fpm" / "pools" / "mcd-merkurosiguranje.conf"
            wrapper = generated / "instances" / "merkurosiguranje" / "php"
            instance_wrapper = inst_root / ".mcd" / "php"
            self.assertIn("include=/etc/php/8.3/fpm/pool.d/mcd/*.conf", include_file.read_text(encoding="utf-8"))
            self.assertTrue(pool_link.is_symlink())
            self.assertEqual(pool_link.resolve(), (generated / "php" / "8.3" / "fpm" / "pools").resolve())
            pool_text = pool_file.read_text(encoding="utf-8")
            self.assertIn("[mcd-merkurosiguranje]", pool_text)
            self.assertIn("php_admin_value[date.timezone] = Europe/Belgrade", pool_text)
            self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;", vhost.read_text(encoding="utf-8"))
            self.assertIn("-d date.timezone='Europe/Belgrade'", wrapper.read_text(encoding="utf-8"))
            self.assertTrue(instance_wrapper.is_symlink())
            self.assertEqual(instance_wrapper.resolve(), wrapper.resolve())

    def test_ignores_inactive_backup_nginx_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            backup_vhost = sites_available / "merkurosiguranje.sales-snap.com.conf.emmy-backup-20260628T120000Z.bak"
            original = f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }}
}}
"""
            backup_vhost.write_text(original, encoding="utf-8")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertFalse(payload["changed"])
            self.assertEqual(payload["instances"][0]["status"], "skipped")
            self.assertEqual(payload["instances"][0]["nginx_files"], [])
            self.assertEqual(backup_vhost.read_text(encoding="utf-8"), original)

    def test_fpm_include_repair_triggers_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            slug = "merkurosiguranje"
            pool_dir = generated / "php" / "8.3" / "fpm" / "pools"
            pool_dir.mkdir(parents=True)
            (pool_dir / f"mcd-{slug}.conf").write_text(
                instance_runtime._pool_conf("8.3", inst, slug),
                encoding="utf-8",
            )
            wrapper = generated / "instances" / slug / "php"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text(instance_runtime._wrapper_script("8.3", inst, slug), encoding="utf-8")
            wrapper.chmod(0o755)
            instance_wrapper = inst_root / ".mcd" / "php"
            instance_wrapper.parent.mkdir(parents=True)
            instance_wrapper.symlink_to(wrapper)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                if cmd[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(cmd, 0, "active\n", "")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=True)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertIn("fpm_include:8.3", payload["actions"])
            self.assertIn("fpm_link:8.3", payload["actions"])
            self.assertIn(["systemctl", "reload", "php8.3-fpm"], calls)
            self.assertIn(["systemctl", "reload", "nginx"], calls)

    def test_rolls_back_nginx_when_config_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            original = f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }}
}}
"""
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(original, encoding="utf-8")
            (sites_enabled / vhost.name).symlink_to(vhost)

            def fail_fpm(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if cmd and cmd[0] == "php-fpm8.3":
                    return subprocess.CompletedProcess(cmd, 1, "", "bad fpm")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", fail_fpm),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "error")
            self.assertEqual(vhost.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
