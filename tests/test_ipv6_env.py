from __future__ import annotations

import unittest

from mcd_agent.env import ipv6_disable_intent_enabled, ipv6_runtime_disabled


class IPv6EnvTests(unittest.TestCase):
    def test_runtime_disabled_requires_visible_interfaces(self) -> None:
        status = {
            "net.ipv6.conf.all.disable_ipv6": "1",
            "net.ipv6.conf.default.disable_ipv6": "1",
            "net.ipv6.conf.lo.disable_ipv6": "1",
            "net.ipv6.conf.eth0.disable_ipv6": "0",
            "persistent_exists": "1",
        }

        self.assertFalse(ipv6_runtime_disabled(status))

    def test_disable_intent_uses_persistent_mcd_file_and_base_keys(self) -> None:
        status = {
            "net.ipv6.conf.all.disable_ipv6": "1",
            "net.ipv6.conf.default.disable_ipv6": "1",
            "net.ipv6.conf.lo.disable_ipv6": "1",
            "net.ipv6.conf.eth0.disable_ipv6": "0",
            "persistent_exists": "1",
        }

        self.assertTrue(ipv6_disable_intent_enabled(status))

    def test_disable_intent_ignores_runtime_only_state_without_file(self) -> None:
        status = {
            "net.ipv6.conf.all.disable_ipv6": "1",
            "net.ipv6.conf.default.disable_ipv6": "1",
            "net.ipv6.conf.lo.disable_ipv6": "1",
            "persistent_exists": "0",
        }

        self.assertFalse(ipv6_disable_intent_enabled(status))


if __name__ == "__main__":
    unittest.main()
