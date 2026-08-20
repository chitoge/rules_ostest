#!/usr/bin/env python3
"""Checks the materialized public runtime's manifest and license closure."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import unittest

from python.runfiles import runfiles


class QemuRuntimeExtractedProvenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--lock", required=True)
        parser.add_argument("--packages", required=True)
        arguments, _ = parser.parse_known_args()
        locator = runfiles.Create()

        def path(logical_path: str) -> pathlib.Path:
            resolved = locator.Rlocation(logical_path)
            if resolved is None:
                raise RuntimeError(f"missing runfile: {logical_path}")
            return pathlib.Path(resolved)

        cls.lock = json.loads(path(arguments.lock).read_text(encoding="utf-8"))
        cls.packages_path = path(arguments.packages)
        cls.packages_lines = cls.packages_path.read_text(encoding="utf-8").splitlines()
        cls.runtime_root = cls.packages_path.parent

    def test_packages_manifest_is_complete_and_lock_ordered(self) -> None:
        self.assertEqual("# NAME\tVERSION\tARCH\tSHA256\tURL", self.packages_lines[1])
        expected = [
            "\t".join(
                (
                    package["name"],
                    package["version"],
                    package["arch"],
                    package["sha256"],
                    package["urls"][0],
                )
            )
            for package in self.lock["packages"]
        ]
        self.assertEqual(expected, self.packages_lines[2:])
        self.assertEqual(92, len(self.packages_lines))

    def test_each_locked_package_has_an_extracted_ubuntu_notice(self) -> None:
        missing = []
        for package in self.lock["packages"]:
            notice = self.runtime_root / "root/usr/share/doc" / package["name"] / "copyright"
            if not notice.is_file() or not notice.read_text(encoding="utf-8").strip():
                missing.append(package["name"])
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
