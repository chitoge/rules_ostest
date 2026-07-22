#!/usr/bin/env python3
"""Exercises scripted host forwarding and scratch export with real CirrOS."""

from __future__ import annotations

import argparse
import os
import pathlib
import socket
import time

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments


def _ssh_banner(host: str, port: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as connection:
                connection.settimeout(2.0)
                banner = connection.recv(256)
                if banner:
                    return banner
                last_error = ConnectionError("SSH connection closed before its banner")
        except OSError as error:
            last_error = error
        time.sleep(0.1)
    raise RuntimeError(f"SSH service did not become ready: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_qemu_arguments(parser)
    config = UefiQemuConfig.from_namespace(parser.parse_args())
    outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])

    with QemuSession(config, startup_timeout=15.0) as session:
        session.wait_for_serial("OSTEST: CLOUD PASS", timeout=60.0)
        forwarding = session.host_forward("ssh")
        banner = _ssh_banner(forwarding.host, forwarding.host_port, 60.0)
        if not banner.startswith(b"SSH-2.0-"):
            raise AssertionError(f"unexpected SSH banner: {banner!r}")
        session.execute("quit")
        session.wait_for_exit(timeout=5.0)
        exported = session.export_media("scratch", outputs / "scripted-scratch.img")

    payload = b"OSTEST-SCRATCH-DURABLE\n"
    with exported.open("rb") as scratch:
        if scratch.read(len(payload)) != payload:
            raise AssertionError("exported scratch image omitted the guest payload")
    (outputs / "scripted-ssh-banner.txt").write_bytes(banner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
