#!/usr/bin/env python3
"""Proves the generated runner invokes on_failure after a failed boot."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess

from python.runfiles import runfiles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("--inner", required=True)
    parser.add_argument("--qemu", required=True)
    args = parser.parse_args()

    locator = runfiles.Create()
    if locator is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    inner = pathlib.Path(locator.Rlocation(args.inner))

    root = pathlib.Path(os.environ["TEST_TMPDIR"]) / "failure-hook-meta"
    temporary = root / "tmp"
    outputs = root / "outputs"
    temporary.mkdir(parents=True)
    outputs.mkdir(parents=True)
    environment = dict(os.environ)
    environment.update(
        {
            "TEST_TARGET": "//tests:qemu_runner_failure_hook_inner_test",
            "TEST_TMPDIR": str(temporary),
            "TEST_UNDECLARED_OUTPUTS_DIR": str(outputs),
        }
    )

    completed = subprocess.run(
        [
            inner,
            "--arch=x86_64",
            f"--qemu={args.qemu}",
            f"--firmware={args.firmware}",
            "--timeout-seconds=5",
            "--success-pattern=OSTEST: PASS",
            "--failure-pattern=OSTEST: FAIL",
            "--memory-mb=64",
            "--cpus=1",
            "--qemu-arg=--fake-mode=failure-hook",
            f"--on-failure={args.hook}",
            "--on-failure-timeout-seconds=5",
        ],
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
    )
    if completed.returncode != 1:
        raise AssertionError(
            f"inner test returned {completed.returncode}, expected 1:\n{completed.stdout}"
        )
    if "rules_ostest: FAIL" not in completed.stdout:
        raise AssertionError(f"inner runner did not report its primary failure:\n{completed.stdout}")

    observed = (outputs / "failure-hook-observed.txt").read_text(encoding="utf-8")
    path, serial = observed.split("\n", 1)
    if pathlib.Path(path).name != "qemu.log":
        raise AssertionError(f"hook received unexpected log path: {path}")
    if "OSTEST: FAIL" not in serial:
        raise AssertionError(f"hook did not receive the completed serial log: {serial!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
