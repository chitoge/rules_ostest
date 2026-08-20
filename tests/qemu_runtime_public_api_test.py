#!/usr/bin/env python3
"""Checks the public, opt-in QEMU runtime repository contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import unittest

from python.runfiles import runfiles


_ALIASES = {
    ":qemu_system_x86_64",
    ":qemu_firmware_dir",
    ":ovmf_code",
    ":ovmf_vars",
    ":runtime",
    ":licenses",
    ":PACKAGES.txt",
}


class QemuRuntimePublicApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--module", required=True)
        parser.add_argument("--repository-rule", required=True)
        parser.add_argument("--lock", required=True)
        parser.add_argument("--lock-digest", required=True)
        parser.add_argument("--integration-build", required=True)
        parser.add_argument("--consumer-module", required=True)
        parser.add_argument("--consumer-build", required=True)
        arguments, _ = parser.parse_known_args()
        locator = runfiles.Create()

        def read(logical_path: str) -> str:
            path = locator.Rlocation(logical_path)
            if path is None:
                raise RuntimeError(f"missing runfile: {logical_path}")
            return pathlib.Path(path).read_text(encoding="utf-8")

        cls.module = read(arguments.module)
        cls.repository_rule = read(arguments.repository_rule)
        cls.lock_bytes = read(arguments.lock).encode("utf-8")
        cls.lock_digest = read(arguments.lock_digest).strip()
        cls.integration_build = read(arguments.integration_build)
        cls.consumer_module = read(arguments.consumer_module)
        cls.consumer_build = read(arguments.consumer_build)

    def test_public_rule_is_explicit_opt_in(self) -> None:
        self.assertIn('"//ostest:qemu_runtime_repository.bzl"', self.module)
        self.assertIn('"qemu_runtime_repository"', self.module)
        self.assertIn('lock = "//ostest:qemu_noble.lock.json"', self.module)
        self.assertNotIn("//tests/integration:qemu_runtime_repository.bzl", self.module)
        self.assertNotIn("//tests/integration:qemu_noble.lock.json", self.module)

    def test_stable_aliases_and_raw_paths_are_generated(self) -> None:
        for alias in _ALIASES:
            target = alias.removeprefix(":")
            with self.subTest(alias=alias):
                if target == "PACKAGES.txt":
                    self.assertIn('"PACKAGES.txt"', self.repository_rule)
                else:
                    self.assertIn(f'name = "{target}"', self.repository_rule)
        self.assertIn('"root/usr/share/OVMF/OVMF_CODE_4M.fd"', self.repository_rule)
        self.assertIn('"root/usr/share/OVMF/OVMF_VARS_4M.fd"', self.repository_rule)
        self.assertIn('"root/usr/bin/qemu-system-x86_64"', self.repository_rule)
        self.assertIn('repository_ctx.file(\n        "qemu_system_x86_64.sh"', self.repository_rule)
        self.assertIn('qemu_launcher(', self.repository_rule)
        self.assertIn('_LAUNCHER_RULE', self.repository_rule)

    def test_lock_provenance_and_closure_rules_are_explicit(self) -> None:
        self.assertEqual(2, len(self.lock_digest.split()))
        expected_digest, filename = self.lock_digest.split()
        self.assertEqual("qemu_noble.lock.json", filename)
        self.assertEqual(expected_digest, hashlib.sha256(self.lock_bytes).hexdigest())
        lock = json.loads(self.lock_bytes)
        self.assertEqual(90, len(lock["packages"]))
        self.assertIn("_EXPECTED_PACKAGE_COUNT = 90", self.repository_rule)
        self.assertIn("duplicate package identity", self.repository_rule)
        self.assertIn("unlocked dependency", self.repository_rule)
        self.assertIn("_LOWER_HEX", self.repository_rule)

    def test_integration_uses_the_public_generated_launchers(self) -> None:
        self.assertIn('actual = "@qemu_noble_x86_64//:qemu_system_x86_64"', self.integration_build)
        self.assertIn('actual = "@qemu_noble_x86_64//:qemu_system_aarch64"', self.integration_build)
        self.assertNotIn('srcs = ["qemu_system_x86_64.py"]', self.integration_build)

    def test_separate_consumer_root_uses_the_public_rule_and_aliases(self) -> None:
        self.assertIn('name = "rules_ostest_qemu_runtime_consumer"', self.consumer_module)
        self.assertIn('"@rules_ostest//ostest:qemu_runtime_repository.bzl"', self.consumer_module)
        self.assertIn('lock = "@rules_ostest//ostest:qemu_noble.lock.json"', self.consumer_module)
        for alias in _ALIASES:
            self.assertIn('"@qemu_noble_x86_64//%s"' % alias, self.consumer_build)


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
