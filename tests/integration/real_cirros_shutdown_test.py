#!/usr/bin/env python3
"""Requires the NoCloud shutdown marker followed by a natural QEMU exit."""

from __future__ import annotations

import argparse

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_qemu_arguments(parser)
    config = UefiQemuConfig.from_namespace(parser.parse_args())
    with QemuSession(config, startup_timeout=15.0) as session:
        session.wait_for_serial("OSTEST: CLOUD SHUTDOWN", timeout=60.0)
        session.wait_for_exit(timeout=15.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
