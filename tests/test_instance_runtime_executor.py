from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcd_agent.executor import build_mautic_exec_args


class InstanceRuntimeExecutorTest(unittest.TestCase):
    def test_uses_instance_php_wrapper_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
            wrapper = root / ".mcd" / "php"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)

            cmd = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="cache:clear",
                instance_id=None,
                run_as_user=None,
            )

        self.assertEqual(cmd[0], str(wrapper))


if __name__ == "__main__":
    unittest.main()

