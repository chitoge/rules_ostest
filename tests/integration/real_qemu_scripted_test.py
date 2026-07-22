#!/usr/bin/env python3
"""Exercises the public scripted QEMU API against a real EFI Shell guest."""

from __future__ import annotations

import argparse
import os
import pathlib
import socket

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments


def _rsp_packet(payload: bytes) -> bytes:
    return b"$" + payload + b"#" + f"{sum(payload) & 0xff:02x}".encode("ascii")


def _read_rsp(connection: socket.socket) -> bytes:
    if connection.recv(1) != b"+":
        raise AssertionError("QEMU GDB stub did not acknowledge the request")
    if connection.recv(1) != b"$":
        raise AssertionError("QEMU GDB stub returned an invalid packet prefix")
    payload = bytearray()
    while True:
        byte = connection.recv(1)
        if not byte:
            raise AssertionError("QEMU GDB stub closed mid-packet")
        if byte == b"#":
            break
        payload.extend(byte)
    checksum = connection.recv(2)
    expected = f"{sum(payload) & 0xff:02x}".encode("ascii")
    if checksum.lower() != expected:
        raise AssertionError(f"invalid GDB response checksum: {checksum!r}")
    connection.sendall(b"+")
    return bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_qemu_arguments(parser)
    args = parser.parse_args()
    config = UefiQemuConfig.from_namespace(args)
    outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])

    with QemuSession(config, startup_timeout=15.0) as session:
        status = session.execute("query-status")
        if status.get("running") is not False or status.get("status") not in {
            "paused",
            "prelaunch",
        }:
            raise AssertionError(f"QEMU did not start paused: {status!r}")

        with socket.create_connection(session.gdb_address, timeout=3.0) as gdb:
            gdb.settimeout(3.0)
            gdb.sendall(_rsp_packet(b"qSupported:multiprocess+"))
            response = _read_rsp(gdb)
        if b"PacketSize=" not in response:
            raise AssertionError(f"unexpected GDB qSupported response: {response!r}")

        session.execute("cont")
        session.wait_for_serial("OSTEST: SCRIPTED QMP GDB READY", timeout=15.0)
        screenshot = session.screendump(outputs / "scripted-control.ppm")
        data = screenshot.read_bytes()
        if not data.startswith(b"P6\n") or len(data) < 1024:
            raise AssertionError("QMP screendump was not a nontrivial binary PPM")
        session.execute("quit")
        if session.wait_for_exit(timeout=5.0) != 0:
            raise AssertionError("QEMU did not exit cleanly after QMP quit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
