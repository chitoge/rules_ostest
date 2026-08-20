#!/usr/bin/env python3
"""Executes the generated public QEMU launcher without starting a guest."""

from __future__ import annotations

import argparse
import os
import subprocess
import unittest

from python.runfiles import runfiles


class QemuRuntimeLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--qemu", required=True)
        arguments, _ = parser.parse_known_args()
        cls.qemu = arguments.qemu

    def test_x86_64_launcher_uses_declared_runtime(self) -> None:
        launcher = runfiles.Create().Rlocation(self.qemu)
        if launcher is None:
            self.fail("generated QEMU launcher is absent from runfiles")

        environment = dict(os.environ)
        environment["PATH"] = ""
        completed = subprocess.run(
            [launcher, "--version"],
            capture_output = True,
            check = False,
            env = environment,
            text = True,
            timeout = 15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("QEMU emulator version 8.2.2", completed.stdout)


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
