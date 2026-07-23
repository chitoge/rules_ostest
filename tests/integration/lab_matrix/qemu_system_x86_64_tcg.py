#!/usr/bin/env python3
"""Run the pinned QEMU wrapper with its KVM preference removed."""

from __future__ import annotations

import os
import pathlib
import sys


def _qemu_wrapper() -> pathlib.Path:
    relative = pathlib.Path("_main/tests/integration/qemu_system_x86_64")
    runfiles_dir = os.environ.get("RUNFILES_DIR") or os.environ.get("TEST_SRCDIR")
    if runfiles_dir:
        candidate = pathlib.Path(runfiles_dir) / relative
        if candidate.is_file():
            return candidate
    manifest_path = os.environ.get("RUNFILES_MANIFEST_FILE")
    if manifest_path:
        key = relative.as_posix()
        with pathlib.Path(manifest_path).open(encoding="utf-8") as manifest:
            for line in manifest:
                logical, separator, physical = line.rstrip("\n").partition(" ")
                if logical == key and separator:
                    return pathlib.Path(physical)
    raise RuntimeError("pinned x86_64 QEMU wrapper is absent from runfiles")


def main() -> None:
    arguments = []
    removed_kvm = False
    index = 1
    while index < len(sys.argv):
        if sys.argv[index : index + 2] == ["-accel", "kvm"]:
            removed_kvm = True
            index += 2
            continue
        arguments.append(sys.argv[index])
        index += 1
    if not removed_kvm or arguments[:2] != ["-no-user-config", "-nodefaults"]:
        raise RuntimeError("unexpected rules_ostest QEMU command shape")
    qemu = _qemu_wrapper()
    os.execv(qemu, [str(qemu), *arguments])


if __name__ == "__main__":
    main()
