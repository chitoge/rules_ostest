#!/usr/bin/env python3
"""Host companion fixture covering argv forwarding and lifecycle failures."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments or not arguments[0].startswith("--mode="):
        raise RuntimeError(f"missing companion mode: {arguments!r}")
    mode = arguments[0].removeprefix("--mode=")
    mapping = json.loads(os.environ["OSTEST_HOSTFWD_JSON"])
    if mapping != {
        "probe": {
            "guest_port": 1234,
            "host": "127.0.0.1",
            "host_port": 34567,
            "protocol": "tcp",
        }
    }:
        raise RuntimeError(f"unexpected forwarding environment: {mapping!r}")
    artifacts = pathlib.Path(os.environ["OSTEST_ARTIFACTS_DIR"])
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "argv.txt").write_text(
        "\n".join(arguments) + "\n",
        encoding="utf-8",
    )
    if mode == "failure":
        if arguments[1:] != ["alpha", "two words"]:
            raise RuntimeError(f"host_companion_args were corrupted: {arguments!r}")
        print("COMPANION ARGS OK", flush=True)
        print("COMPANION FAIL SENTINEL", flush=True)
        return 17
    if mode == "timeout":
        if len(arguments) != 1:
            raise RuntimeError(f"unexpected timeout arguments: {arguments!r}")
        print("COMPANION TIMEOUT STARTED", flush=True)
        time.sleep(20)
        return 0
    raise RuntimeError(f"unknown companion mode: {mode!r}")


if __name__ == "__main__":
    raise SystemExit(main())
