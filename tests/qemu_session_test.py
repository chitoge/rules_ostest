#!/usr/bin/env python3
"""Exercises serial, QMP, screenshots, and compressed-disk materialization."""

from __future__ import annotations

import argparse
import os
import pathlib
import socket

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_qemu_arguments(parser)
    config = UefiQemuConfig.from_namespace(parser.parse_args())
    assert config.arch == "x86_64"
    assert config.disk.parent == pathlib.Path(os.environ["TEST_TMPDIR"])
    assert config.disk.stat().st_size == 96 * 1024 * 1024
    output_dir = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    with QemuSession(config) as session:
        with socket.create_connection(session.gdb_address, timeout=2):
            pass
        session.wait_for_serial(r"FRAME_READY", timeout=5)
        status = session.execute("query-status")
        assert status == {"running": True, "status": "running"}
        screenshot = session.screendump(output_dir / "screen.ppm")
        assert screenshot.read_bytes() == b"P6\n1 1\n255\n\x12\x34\x56"
        exported_vars = session.export_firmware_vars(output_dir / "exported-vars.fd")
        assert exported_vars.read_bytes() == b"serial_baud=115200\nresult_protocol=OSTEST\n"
        assert session.execute("quit") == {}
        assert session.wait_for_exit(timeout=2) == 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
