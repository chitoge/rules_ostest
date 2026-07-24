#!/usr/bin/env python3
"""Executes the pinned QEMU binary with only its declared runtime closure."""

from __future__ import annotations

import os
import pathlib
import sys

from python.runfiles import runfiles


_REPOSITORY = "qemu_noble_x86_64"


def _runfile(locator: runfiles.Runfiles, path: str) -> pathlib.Path:
    resolved = locator.Rlocation(f"{_REPOSITORY}/{path}")
    if resolved is None:
        raise RuntimeError(f"QEMU runtime runfile is missing: {path}")
    result = pathlib.Path(resolved)
    if not result.is_file() and not result.is_dir():
        raise RuntimeError(f"QEMU runtime runfile is absent: {path}")
    return result


def main() -> None:
    locator = runfiles.Create()
    loader = _runfile(
        locator,
        "root/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
    )
    runtime_root = loader.parents[3]
    qemu = _runfile(locator, "root/usr/bin/qemu-system-x86_64")

    library_path = ":".join(
        (
            str(runtime_root / "lib" / "x86_64-linux-gnu"),
            str(runtime_root / "usr" / "lib" / "x86_64-linux-gnu"),
        )
    )
    os.environ["QEMU_MODULE_DIR"] = str(
        runtime_root / "usr" / "lib" / "x86_64-linux-gnu" / "qemu"
    )
    os.execv(
        loader,
        [
            str(loader),
            "--library-path",
            library_path,
            str(qemu),
            *sys.argv[1:],
        ],
    )


if __name__ == "__main__":
    main()
