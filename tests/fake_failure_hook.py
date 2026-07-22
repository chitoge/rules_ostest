#!/usr/bin/env python3
"""Records the serial log received through the public failure-hook contract."""

from __future__ import annotations

import os
import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("expected the serial-log path as the only argument")
    serial_log = pathlib.Path(sys.argv[1]).resolve()
    output_dir = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "failure-hook-observed.txt").write_text(
        str(serial_log) + "\n" + serial_log.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
