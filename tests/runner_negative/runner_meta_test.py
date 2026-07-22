#!/usr/bin/env python3
"""Wraps intentionally failing generated runners and verifies their evidence."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess

from python.runfiles import runfiles


HOST_FORWARD = '{"guest":1234,"host":"auto","name":"probe","protocol":"tcp"}'


def _case_arguments(
    case: str,
    *,
    companion: str | None,
) -> tuple[list[str], int, str, str]:
    arguments = [f"--qemu-arg=--negative-mode={case}"]
    if case == "custom-success":
        return (
            arguments + [r"--success-pattern=CUSTOM\s+SUCCESS:[0-9]+"],
            0,
            "guest assertions and host companion completed",
            "CUSTOM SUCCESS:42",
        )
    if case == "failure-regex":
        return (
            arguments
            + [
                r"--failure-pattern=FATAL\s+code=[0-9]+",
                "--success-pattern=NEVER MATCH",
            ],
            1,
            "matched failure pattern",
            "FATAL code=42",
        )
    if case == "forbidden-marker":
        return (
            arguments
            + [
                "--failure-pattern=OSTEST: FAIL",
                "--forbidden-marker=NEVER-ALLOWED",
                "--success-marker=GUEST READY",
            ],
            1,
            "matched forbidden marker 'NEVER-ALLOWED'",
            "NEVER-ALLOWED",
        )
    if case == "global-timeout":
        return (
            arguments
            + [
                "--failure-pattern=OSTEST: FAIL",
                "--success-pattern=NEVER MATCH",
                "--timeout-seconds=1",
            ],
            1,
            "timed out after 1 seconds",
            "TIMEOUT STARTED",
        )
    if case == "unexpected-exit":
        return (
            arguments
            + [
                "--failure-pattern=OSTEST: FAIL",
                "--success-pattern=NEVER MATCH",
            ],
            1,
            "QEMU exited with status 23 before a result marker",
            "EARLY EXIT",
        )
    if companion is None:
        raise AssertionError(f"case {case!r} requires --companion")
    arguments.extend(
        [
            "--failure-pattern=OSTEST: FAIL",
            "--success-marker=GUEST READY",
            f"--hostfwd={HOST_FORWARD}",
            f"--host-companion={companion}",
        ]
    )
    if case == "companion-failure":
        return (
            arguments
            + [
                "--host-companion-arg=--mode=failure",
                "--host-companion-arg=alpha",
                "--host-companion-arg=two words",
            ],
            1,
            "host companion exited with status 17",
            "GUEST READY (companion-failure)",
        )
    if case == "companion-timeout":
        return (
            arguments
            + [
                "--host-companion-arg=--mode=timeout",
                "--timeout-seconds=2",
            ],
            1,
            "timed out after 2 seconds",
            "GUEST READY (companion-timeout)",
        )
    raise AssertionError(f"unknown case: {case!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--companion")
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--inner", required=True)
    parser.add_argument("--qemu", required=True)
    args = parser.parse_args()

    locator = runfiles.Create()
    if locator is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    inner_path = locator.Rlocation(args.inner)
    if inner_path is None:
        raise RuntimeError(f"could not resolve inner runner {args.inner!r}")

    root = pathlib.Path(os.environ["TEST_TMPDIR"]) / args.case
    temporary = root / "tmp"
    outputs = root / "outputs"
    temporary.mkdir(parents=True)
    outputs.mkdir(parents=True)
    environment = dict(os.environ)
    environment.update(
        {
            "TEST_TARGET": f"//tests/runner_negative:{args.case}",
            "TEST_TMPDIR": str(temporary),
            "TEST_UNDECLARED_OUTPUTS_DIR": str(outputs),
        }
    )

    case_arguments, expected_status, expected_reason, expected_serial = _case_arguments(
        args.case,
        companion=args.companion,
    )
    timeout_arguments = [] if any(
        argument.startswith("--timeout-seconds=") for argument in case_arguments
    ) else ["--timeout-seconds=3"]
    completed = subprocess.run(
        [
            inner_path,
            "--arch=x86_64",
            f"--qemu={args.qemu}",
            f"--firmware={args.firmware}",
            *timeout_arguments,
            "--memory-mb=64",
            "--cpus=1",
            *case_arguments,
        ],
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=12,
    )
    if completed.returncode != expected_status:
        raise AssertionError(
            f"{args.case} returned {completed.returncode}, expected "
            f"{expected_status}:\n{completed.stdout}"
        )
    if expected_reason not in completed.stdout:
        raise AssertionError(
            f"{args.case} omitted reason {expected_reason!r}:\n{completed.stdout}"
        )

    command = (outputs / "qemu-command.txt").read_text(encoding="utf-8")
    if f"--negative-mode={args.case}" not in command or "-serial stdio" not in command:
        raise AssertionError(f"incomplete QEMU command artifact: {command!r}")
    serial = (outputs / "qemu.log").read_text(encoding="utf-8")
    if expected_serial not in serial:
        raise AssertionError(
            f"serial artifact omitted {expected_serial!r}: {serial!r}"
        )

    if args.case == "companion-failure":
        companion_log = (outputs / "host-companion.log").read_text(encoding="utf-8")
        if (
            "COMPANION ARGS OK" not in companion_log
            or "COMPANION FAIL SENTINEL" not in companion_log
        ):
            raise AssertionError(f"companion failure evidence is incomplete: {companion_log!r}")
        argv = (outputs / "host-companion" / "argv.txt").read_text(encoding="utf-8")
        if argv != "--mode=failure\nalpha\ntwo words\n":
            raise AssertionError(f"host_companion_args changed in transit: {argv!r}")
    elif args.case == "companion-timeout":
        companion_log = (outputs / "host-companion.log").read_text(encoding="utf-8")
        if "COMPANION TIMEOUT STARTED" not in companion_log:
            raise AssertionError(f"companion was not started: {companion_log!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
