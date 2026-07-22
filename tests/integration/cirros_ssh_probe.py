#!/usr/bin/env python3
"""Validates a real QEMU dynamic forward to CirrOS's SSH service."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import time


def _connect(host: str, port: int, deadline: float) -> bytes:
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as connection:
                connection.settimeout(2.0)
                banner = connection.recv(256)
                if banner:
                    return banner
                last_error = ConnectionError("forwarded connection closed before its SSH banner")
        except OSError as error:
            last_error = error
        time.sleep(0.1)
    raise RuntimeError(f"SSH service did not become ready: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debugcon", action="store_true")
    args = parser.parse_args()

    mapping = json.loads(os.environ["OSTEST_HOSTFWD_JSON"])
    if set(mapping) != {"ssh"}:
        raise RuntimeError(f"unexpected host-forward mapping: {mapping!r}")
    endpoint = mapping["ssh"]
    if (
        endpoint.get("host") != "127.0.0.1"
        or endpoint.get("guest_port") != 22
        or endpoint.get("protocol") != "tcp"
    ):
        raise RuntimeError(f"unexpected SSH endpoint: {endpoint!r}")
    port = endpoint.get("host_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError(f"invalid dynamically allocated host port: {port!r}")
    if os.environ.get("OSTEST_HOST") != endpoint["host"]:
        raise RuntimeError("OSTEST_HOST does not match the resolved mapping")
    if os.environ.get("OSTEST_PORT") != str(port):
        raise RuntimeError("OSTEST_PORT does not match the resolved mapping")

    banner = _connect(endpoint["host"], port, time.monotonic() + 60.0)
    if not banner.startswith(b"SSH-2.0-"):
        raise RuntimeError(f"unexpected SSH banner: {banner!r}")

    artifacts = pathlib.Path(os.environ["OSTEST_ARTIFACTS_DIR"])
    artifacts.mkdir(parents=True, exist_ok=True)
    if args.debugcon:
        debugcon = artifacts.parent / "ostest-ovmf-debug.log"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if b"\x1e" in debugcon.read_bytes():
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("guest debug-console sentinel was not captured")
    (artifacts / "mapping.json").write_text(
        json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (artifacts / "ssh-banner.txt").write_bytes(banner)
    print("real CirrOS SSH forward passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
