#!/usr/bin/env python3
"""QEMU-shaped executable for advanced serial-runner integration tests."""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import time


def _values(arguments: list[str], option: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == option]


def _mode(arguments: list[str]) -> str:
    values = [value.removeprefix("--fake-advanced=") for value in arguments if value.startswith("--fake-advanced=")]
    if len(values) != 1:
        raise RuntimeError(f"expected one advanced fake mode, got {values}")
    return values[0]


def _direct(arguments: list[str]) -> int:
    errors = []
    firmware_directories = _values(arguments, "-L")
    if len(firmware_directories) != 1:
        errors.append(f"expected one declared QEMU firmware directory, got {firmware_directories}")
    elif pathlib.Path(firmware_directories[0]).name != "testdata":
        errors.append(f"unexpected QEMU firmware directory: {firmware_directories[0]}")
    if _values(arguments, "-machine") != ["virt,gic-version=2"]:
        errors.append(f"unexpected machine: {_values(arguments, '-machine')}")
    if _values(arguments, "-cpu") != ["cortex-a53"]:
        errors.append(f"unexpected CPU: {_values(arguments, '-cpu')}")
    if len(_values(arguments, "-kernel")) != 1 or len(_values(arguments, "-initrd")) != 1:
        errors.append("direct kernel/initrd inputs were not attached")
    if _values(arguments, "-append") != ["console=ttyAMA0 ostest.direct=1"]:
        errors.append(f"unexpected kernel arguments: {_values(arguments, '-append')}")
    if any("if=pflash" in value for value in _values(arguments, "-drive")):
        errors.append("firmware-free direct boot unexpectedly attached pflash")
    if "-no-reboot" not in arguments:
        errors.append("single-phase direct boot omitted -no-reboot")
    data_drives = [value for value in _values(arguments, "-drive") if "if=none" in value]
    if len(data_drives) != 1:
        errors.append(f"expected one scratch drive, got {data_drives}")
    else:
        fields = dict(field.split("=", 1) for field in data_drives[0].split(",") if "=" in field)
        scratch = pathlib.Path(fields["file"])
        if scratch.stat().st_size != 1024 * 1024:
            errors.append(f"scratch disk has wrong size: {scratch.stat().st_size}")
        with scratch.open("r+b") as disk:
            if disk.read(16) != b"\0" * 16:
                errors.append("scratch disk was not zero initialized")
            disk.seek(0)
            disk.write(b"DURABLE-OSTEST\n")
            disk.flush()
    if errors:
        for error in errors:
            print(error, flush=True)
        print("ADVANCED-FORBIDDEN", flush=True)
        return 1
    print("DIRECT-START", flush=True)
    sys.stdout.write("D" * (192 * 1024))
    print("DIRECT-DONE", flush=True)
    return 0


def _qmp(arguments: list[str], mode: str) -> int:
    chardev = _values(arguments, "-chardev")
    if len(chardev) != 1:
        print(f"expected one QMP chardev, got {chardev}", flush=True)
        return 2
    fields = dict(field.split("=", 1) for field in chardev[0].split(",") if "=" in field)
    forwarded_listener = None
    if mode in ("hostfwd", "hostfwd-exit"):
        forwarded_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        forwarded_listener.bind(("127.0.0.1", 0))
        forwarded_listener.listen(1)
    listener = socket.socket(fileno=int(fields["fd"]))
    connection, _ = listener.accept()
    with connection, connection.makefile("rwb", buffering=0) as qmp:
        qmp.write(
            json.dumps(
                {
                    "QMP": {
                        "version": {"qemu": {"major": 10, "minor": 2, "micro": 0}, "package": "fake"},
                        "capabilities": [],
                    }
                }
            ).encode("utf-8")
            + b"\n"
        )
        for line in qmp:
            request = json.loads(line)
            command = request["execute"]
            response: dict[str, object]
            if command == "qmp_capabilities":
                response = {"return": {}, "id": request.get("id")}
                qmp.write(json.dumps(response).encode("utf-8") + b"\n")
                if mode == "phases":
                    if "-no-reboot" in arguments:
                        print("PHASE-FORBIDDEN", flush=True)
                    sys.stdout.write("P" * (192 * 1024))
                    print("PHASE-ONE-WRITE", flush=True)
                    time.sleep(0.1)
                    qmp.write(
                        json.dumps(
                            {
                                "event": "RESET",
                                "data": {"guest": True, "reason": "guest-reset"},
                                "timestamp": {"seconds": 1, "microseconds": 0},
                            }
                        ).encode("utf-8")
                        + b"\n"
                    )
                    time.sleep(0.05)
                    print("PHASE-TWO-READ", flush=True)
                elif mode == "graphics":
                    devices = _values(arguments, "-device")
                    if "VGA" not in devices:
                        print("GRAPHICS-FORBIDDEN", flush=True)
                    print("GRAPHICS-READY", flush=True)
                continue
            if command == "human-monitor-command" and mode in ("hostfwd", "hostfwd-exit"):
                assert forwarded_listener is not None
                forwarded_port = forwarded_listener.getsockname()[1]
                usernet = next(
                    (value for value in _values(arguments, "-netdev") if value.startswith("user,")),
                    "",
                )
                if "restrict=on" not in usernet or "hostfwd=tcp:127.0.0.1:0-:50051" not in usernet:
                    print("NETWORK-FORBIDDEN", flush=True)
                output = (
                    "VLAN -1 (ostest_usernet):\r\n"
                    "  Protocol[State] FD Source Address Port Dest. Address Port RecvQ SendQ\r\n"
                    f"  TCP[HOST_FORWARD] 12 127.0.0.1 {forwarded_port} 10.0.2.15 50051 0 0\r\n"
                )
                qmp.write(
                    json.dumps({"return": output, "id": request.get("id")}).encode("utf-8")
                    + b"\n"
                )
                if mode == "hostfwd":
                    print("NETWORK-READY", flush=True)
                forwarded_listener.settimeout(5)
                probe, _ = forwarded_listener.accept()
                with probe:
                    if probe.recv(16) != b"probe":
                        return 3
                    probe.sendall(b"ok")
                forwarded_listener.close()
                return 7 if mode == "hostfwd-exit" else 0
            if command == "screendump" and mode == "graphics":
                pathlib.Path(request["arguments"]["filename"]).write_bytes(
                    b"P6\n2 1\n255\n\x00\x00\x00\xff\xff\xff"
                )
                response = {"return": {}, "id": request.get("id")}
            else:
                response = {
                    "error": {"class": "CommandNotFound", "desc": command},
                    "id": request.get("id"),
                }
            qmp.write(json.dumps(response).encode("utf-8") + b"\n")
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    mode = _mode(arguments)
    if mode == "direct":
        return _direct(arguments)
    if mode not in ("phases", "graphics", "hostfwd", "hostfwd-exit"):
        raise RuntimeError(f"unknown advanced fake mode {mode!r}")
    return _qmp(arguments, mode)


if __name__ == "__main__":
    raise SystemExit(main())
