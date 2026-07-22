#!/usr/bin/env python3
"""Executes the staged QEMU binary with only its staged runtime closure."""

from __future__ import annotations

import os
import pathlib
import sys


def main() -> None:
    source = pathlib.Path(__file__)
    if not source.is_absolute():
        source = pathlib.Path.cwd() / source
    runtime_root = source.parent / "runtime" / "root"
    loader = runtime_root / "lib64" / "ld-linux-x86-64.so.2"
    qemu = runtime_root / "usr" / "bin" / "qemu-system-x86_64"

    if not loader.is_file() or not qemu.is_file():
        raise SystemExit(
            "real-QEMU runtime is not staged; run tests/integration/stage_runtime.sh"
        )

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
            "-L",
            str(runtime_root / "usr" / "share" / "qemu"),
            *sys.argv[1:],
        ],
    )


if __name__ == "__main__":
    main()
