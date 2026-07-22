#!/usr/bin/env python3
"""One-shot host companion used by the managed-forward integration test."""

from __future__ import annotations

import json
import os
import pathlib
import socket
import time


def main() -> int:
    mapping = json.loads(os.environ["OSTEST_HOSTFWD_JSON"])
    if set(mapping) != {"grpc"}:
        raise RuntimeError(f"unexpected host-forward mapping: {mapping!r}")
    endpoint = mapping["grpc"]
    if endpoint["host"] != "127.0.0.1" or endpoint["guest_port"] != 50051 or endpoint["protocol"] != "tcp":
        raise RuntimeError(f"unexpected endpoint: {endpoint!r}")
    if os.environ.get("OSTEST_HOST") != endpoint["host"] or os.environ.get("OSTEST_PORT") != str(endpoint["host_port"]):
        raise RuntimeError("single-forward convenience environment is incorrect")
    with socket.create_connection((endpoint["host"], endpoint["host_port"]), timeout=2) as connection:
        connection.sendall(b"probe")
        if connection.recv(16) != b"ok":
            raise RuntimeError("forwarded probe returned the wrong response")
    # Make QEMU exit after the guest verdict but before the companion, covering
    # the runner's either-order completion contract.
    time.sleep(0.1)
    artifacts = pathlib.Path(os.environ["OSTEST_ARTIFACTS_DIR"])
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "probe.txt").write_text("companion passed\n", encoding="utf-8")
    print("host companion passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
