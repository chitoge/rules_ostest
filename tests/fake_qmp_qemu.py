#!/usr/bin/env python3
"""QEMU-shaped QMP server used to test the public scripted-test API."""

from __future__ import annotations

import json
import pathlib
import socket
import sys


def _option(arguments: list[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


def main() -> int:
    arguments = sys.argv[1:]
    accelerators = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "-accel"]
    if accelerators != ["kvm", "tcg"]:
        print(f"unexpected accelerators: {accelerators}", flush=True)
        return 2
    if "--fake-require-net" in arguments:
        netdev = _option(arguments, "-netdev")
        if not netdev.startswith("socket,id=ostest_net0,fd="):
            print(f"unexpected isolated netdev: {netdev}", flush=True)
            return 2
        devices = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "-device"]
        if not any("netdev=ostest_net0" in device for device in devices):
            print(f"missing network device: {devices}", flush=True)
            return 2
    chardev = _option(arguments, "-chardev")
    fields = dict(field.split("=", 1) for field in chardev.split(",") if "=" in field)
    descriptor = int(fields["fd"])
    listener = socket.socket(fileno=descriptor)
    connection, _ = listener.accept()
    with connection, connection.makefile("rwb", buffering=0) as qmp:
        qmp.write(
            json.dumps(
                {
                    "QMP": {
                        "version": {"qemu": {"major": 9, "minor": 0, "micro": 0}, "package": "fake"},
                        "capabilities": [],
                    }
                }
            ).encode("utf-8")
            + b"\n"
        )
        for line in qmp:
            request = json.loads(line)
            command = request["execute"]
            if command == "qmp_capabilities":
                result = {}
                print("FRAME_READY", flush=True)
            elif command == "query-status":
                result = {"running": True, "status": "running"}
            elif command == "screendump":
                pathlib.Path(request["arguments"]["filename"]).write_bytes(b"P6\n1 1\n255\n\x12\x34\x56")
                result = {}
            else:
                response = {
                    "error": {"class": "CommandNotFound", "desc": command},
                    "id": request.get("id"),
                }
                qmp.write(json.dumps(response).encode("utf-8") + b"\n")
                continue
            qmp.write(json.dumps({"return": result, "id": request.get("id")}).encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
