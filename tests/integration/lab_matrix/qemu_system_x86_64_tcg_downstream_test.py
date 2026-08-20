#!/usr/bin/env python3
"""Executes the TCG wrapper as a downstream executable without a VM."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from python.runfiles import runfiles


class QemuSystemX8664TcgDownstreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--qemu", required=True)
        arguments, _ = parser.parse_known_args()
        cls.qemu = arguments.qemu

    @staticmethod
    def _realistic_arguments(executable: str) -> list[str]:
        return [
            executable,
            "-no-user-config",
            "-nodefaults",
            "-L",
            "/unused-but-shape-realistic-firmware-directory",
            "-machine",
            "q35",
            "-m",
            "192",
            "-accel",
            "kvm",
            "-accel",
            "tcg",
            "--version",
        ]

    @staticmethod
    def _run(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            arguments,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=15,
        )

    def test_downstream_executable_has_the_pinned_launcher_binding(self) -> None:
        executable = runfiles.Create().Rlocation(self.qemu)
        if executable is None:
            self.fail("TCG wrapper is absent from runfiles")
        environment = dict(os.environ)
        environment.pop("RUNFILES_DIR", None)
        environment.pop("RUNFILES_MANIFEST_FILE", None)
        completed = self._run(self._realistic_arguments(executable), environment)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("QEMU emulator version 8.2.2", completed.stdout)

    def test_downstream_executable_propagates_manifest_runfiles(self) -> None:
        executable = runfiles.Create().Rlocation(self.qemu)
        if executable is None:
            self.fail("TCG wrapper is absent from runfiles")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            launcher = temporary_path / "qemu_system_x86_64_tcg"
            shutil.copyfile(executable, launcher)
            launcher.chmod(0o755)
            fake_qemu = temporary_path / "qemu-system-x86_64"
            fake_qemu.write_text(
                "#!/bin/sh\n"
                "test -n \"${RUNFILES_MANIFEST_FILE:-}\"\n"
                "printf '%s\\n' 'QEMU emulator version 8.2.2'\n",
                encoding="utf-8",
            )
            fake_qemu.chmod(0o755)
            manifest = temporary_path / "MANIFEST"
            manifest.write_text(
                "rules_ostest_tcg_wrapped_qemu %s\n" % fake_qemu,
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment.pop("RUNFILES_DIR", None)
            environment["RUNFILES_MANIFEST_FILE"] = str(manifest)
            completed = self._run(self._realistic_arguments(str(launcher)), environment)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("QEMU emulator version 8.2.2", completed.stdout)


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
