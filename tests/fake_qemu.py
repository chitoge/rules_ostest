#!/usr/bin/env python3
"""Small QEMU-shaped executable used to test the runner contract."""

from __future__ import annotations

import sys


def values_after(arguments: list[str], option: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == option]


def main() -> int:
    arguments = sys.argv[1:]
    modes = [argument.removeprefix("--fake-mode=") for argument in arguments if argument.startswith("--fake-mode=")]
    errors = []
    if len(modes) != 1:
        errors.append(f"expected one fake mode, got {modes}")
    mode = modes[0] if modes else ""
    accelerators = values_after(arguments, "-accel")
    machines = values_after(arguments, "-machine")
    expected_exit = 0
    emit_success = True
    if mode == "soft-aarch64":
        if accelerators != ["kvm", "tcg"]:
            errors.append(f"unexpected soft accelerators: {accelerators}")
        if machines != ["virt"]:
            errors.append(f"unexpected AArch64 machine: {machines}")
        if values_after(arguments, "-cpu") != ["max"]:
            errors.append("AArch64 did not select the max CPU model")
        pflash = [value for value in values_after(arguments, "-drive") if "if=pflash" in value]
        if len(pflash) != 2:
            errors.append(f"expected code and variable pflash drives, got {pflash}")
    elif mode == "hard-x86_64":
        if accelerators != ["kvm"]:
            errors.append(f"unexpected hard-KVM accelerators: {accelerators}")
        if machines != ["q35"]:
            errors.append(f"unexpected x86_64 machine: {machines}")
        if "-cpu" in arguments:
            errors.append("x86_64 runner unexpectedly forced a CPU model")
    elif mode == "media-x86_64":
        if accelerators != ["kvm", "tcg"]:
            errors.append(f"unexpected media-test accelerators: {accelerators}")
        if machines != ["q35"]:
            errors.append(f"unexpected media-test machine: {machines}")
        drives = [value for value in values_after(arguments, "-drive") if "if=pflash" not in value]
        if len(drives) != 2:
            errors.append(f"expected CD and USB drives, got {drives}")
        elif "media=cdrom" not in drives[0] or "readonly=on" not in drives[0]:
            errors.append(f"first drive is not a read-only CD: {drives[0]}")
        devices = values_after(arguments, "-device")
        if not any(value.startswith("scsi-cd,") for value in devices):
            errors.append(f"missing SCSI CD device: {devices}")
        if not any(value.startswith("usb-storage,") for value in devices):
            errors.append(f"missing USB storage device: {devices}")
    elif mode == "firmware-exit-x86_64":
        if accelerators != ["kvm", "tcg"]:
            errors.append(f"unexpected firmware-only accelerators: {accelerators}")
        if machines != ["q35,smm=on"]:
            errors.append(f"unexpected firmware-only machine: {machines}")
        drives = [value for value in values_after(arguments, "-drive") if "if=pflash" not in value]
        if drives:
            errors.append(f"firmware-only boot unexpectedly has media: {drives}")
        expected_exit = 7
        emit_success = False
    else:
        errors.append(f"unknown fake mode {mode!r}")
    for required in ("-no-user-config", "-nodefaults", "-serial", "-no-reboot"):
        if required not in arguments:
            errors.append(f"missing required option {required}")
    if errors:
        for error in errors:
            print(f"runner contract error: {error}")
        print("OSTEST: FAIL")
        return 1
    if emit_success:
        print("OSTEST: PASS")
    return expected_exit


if __name__ == "__main__":
    raise SystemExit(main())
