#!/usr/bin/env python3
"""Exercises a two-VM, single-target, isolated Ethernet lab."""

from __future__ import annotations

import argparse
import os
import pathlib

from ostest.python.lab import QemuLab, UefiLabConfig, add_uefi_lab_arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_lab_arguments(parser)
    config = UefiLabConfig.from_namespace(parser.parse_args())
    assert tuple(participant.name for participant in config.participants) == ("server", "client")
    with QemuLab(config) as lab:
        lab["server"].wait_for_serial("FRAME_READY", timeout=5)
        lab["client"].wait_for_serial("FRAME_READY", timeout=5)
        with lab.host_endpoint("lan") as sender, lab.host_endpoint("lan") as receiver:
            frame = b"\xff" * 6 + b"\x52\x54\x00\x00\x00\x01" + b"\x88\xb5" + b"rules_ostest".ljust(46, b"\0")
            sender.send(frame)
            assert receiver.receive(timeout=5) == frame
    capture = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "network-lan.pcap"
    data = capture.read_bytes()
    assert data[:4] == b"\xd4\xc3\xb2\xa1"
    assert frame in data
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
