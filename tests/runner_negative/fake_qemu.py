#!/usr/bin/env python3
"""QEMU-shaped process for deterministic serial-runner failure tests."""

from __future__ import annotations

import json
import socket
import sys
import time


def _values(arguments: list[str], option: str) -> list[str]:
    return [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == option
    ]


def _mode(arguments: list[str]) -> str:
    values = [
        argument.removeprefix("--negative-mode=")
        for argument in arguments
        if argument.startswith("--negative-mode=")
    ]
    if len(values) != 1:
        raise RuntimeError(f"expected one negative-test mode, got {values!r}")
    return values[0]


def _serve_qmp(arguments: list[str], mode: str) -> int:
    chardevs = _values(arguments, "-chardev")
    if len(chardevs) != 1:
        raise RuntimeError(f"expected one QMP chardev, got {chardevs!r}")
    fields = dict(
        field.split("=", 1)
        for field in chardevs[0].split(",")
        if "=" in field
    )
    listener = socket.socket(fileno=int(fields["fd"]))
    connection, _ = listener.accept()
    with connection, connection.makefile("rwb", buffering=0) as qmp:
        qmp.write(
            json.dumps(
                {
                    "QMP": {
                        "capabilities": [],
                        "version": {
                            "package": "runner-negative",
                            "qemu": {"major": 10, "micro": 0, "minor": 2},
                        },
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        for line in qmp:
            request = json.loads(line)
            command = request["execute"]
            if command == "qmp_capabilities":
                qmp.write(
                    json.dumps({"id": request.get("id"), "return": {}}).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
                continue
            if command != "human-monitor-command":
                raise RuntimeError(f"unexpected QMP command: {command!r}")
            response = (
                "Protocol[State] FD Source Address Port Dest. Address Port RecvQ SendQ\r\n"
                "TCP[HOST_FORWARD] 12 127.0.0.1 34567 10.0.2.15 1234 0 0\r\n"
            )
            qmp.write(
                json.dumps({"id": request.get("id"), "return": response}).encode(
                    "utf-8"
                )
                + b"\n"
            )
            print(f"GUEST READY ({mode})", flush=True)
            while True:
                time.sleep(1)
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    mode = _mode(arguments)
    if mode == "custom-success":
        sys.stdout.write("CUSTOM ")
        sys.stdout.flush()
        time.sleep(0.02)
        print("SUCCESS:42", flush=True)
        return 0
    if mode == "failure-regex":
        print("boot diagnostic: FATAL code=42", flush=True)
        time.sleep(10)
        return 0
    if mode == "forbidden-marker":
        print("NEVER-ALLOWED", flush=True)
        print("GUEST READY", flush=True)
        time.sleep(10)
        return 0
    if mode == "global-timeout":
        print("TIMEOUT STARTED", flush=True)
        time.sleep(10)
        return 0
    if mode == "unexpected-exit":
        print("EARLY EXIT", flush=True)
        return 23
    if mode in ("companion-failure", "companion-timeout"):
        return _serve_qmp(arguments, mode)
    raise RuntimeError(f"unknown negative-test mode: {mode!r}")


if __name__ == "__main__":
    raise SystemExit(main())
