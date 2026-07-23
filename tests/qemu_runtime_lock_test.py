#!/usr/bin/env python3
"""Checks that the real-QEMU test runtime remains content pinned."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import unittest

from python.runfiles import runfiles


_REQUIRED_PACKAGES = {
    "efi-shell-aa64",
    "efi-shell-x64",
    "ipxe-qemu",
    "libc6",
    "ovmf",
    "qemu-efi-aarch64",
    "qemu-system-arm",
    "qemu-system-common",
    "qemu-system-data",
    "qemu-system-x86",
    "seabios",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT_PREFIX = "https://snapshot.ubuntu.com/ubuntu/20260720T000000Z/"


class QemuRuntimeLockTest(unittest.TestCase):
    lock: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--lock", required=True)
        arguments, _ = parser.parse_known_args()

        locator = runfiles.Create()
        resolved = locator.Rlocation(arguments.lock)
        if resolved is None:
            raise RuntimeError(f"lock runfile is missing: {arguments.lock}")
        with pathlib.Path(resolved).open(encoding="utf-8") as lock_file:
            cls.lock = json.load(lock_file)

    def test_lock_schema_and_required_packages(self) -> None:
        self.assertEqual(self.lock["version"], 1)
        packages = self.lock["packages"]
        self.assertIsInstance(packages, list)
        self.assertTrue(packages)

        names = [package["name"] for package in packages]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(_REQUIRED_PACKAGES, set(names))

    def test_every_archive_is_immutable(self) -> None:
        for package in self.lock["packages"]:
            with self.subTest(package=package["name"]):
                self.assertEqual(package["arch"], "amd64")
                self.assertRegex(package["sha256"], _SHA256)
                self.assertEqual(len(package["urls"]), 1)
                self.assertTrue(package["urls"][0].startswith(_SNAPSHOT_PREFIX))
                self.assertTrue(package["urls"][0].endswith(".deb"))

    def test_dependency_entries_are_locked(self) -> None:
        keys = {package["key"] for package in self.lock["packages"]}
        for package in self.lock["packages"]:
            for dependency in package["dependencies"]:
                with self.subTest(
                    package=package["name"],
                    dependency=dependency["name"],
                ):
                    self.assertIn(dependency["key"], keys)


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
