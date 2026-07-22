#!/usr/bin/env python3
"""Assemble declared blobs into a deterministic sparse raw image."""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re


_OFFSET_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(B|KiB|MiB|GiB|s)?$", re.IGNORECASE)
_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "kib": 1024,
    "mib": 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
    "s": 512,
}


def parse_offset(value: str) -> int:
    match = _OFFSET_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"invalid blob offset {value!r}")
    return int(match.group(1)) * _MULTIPLIERS[(match.group(2) or "").lower()]


def create_raw_image(entries: list[dict[str, str]], output_path: pathlib.Path, size_mb: int) -> None:
    total_size = size_mb * 1024 * 1024
    if total_size <= 0:
        raise ValueError("raw image size must be positive")
    blobs = []
    for entry in entries:
        source = pathlib.Path(entry["source"])
        if not source.is_file():
            raise ValueError(f"blob source is not a regular file: {source}")
        offset = parse_offset(entry["offset"])
        size = source.stat().st_size
        if offset + size > total_size:
            raise ValueError(f"blob {source} extends beyond the raw image")
        blobs.append((offset, offset + size, source))
    blobs.sort(key=lambda blob: (blob[0], str(blob[2])))
    for previous, current in zip(blobs, blobs[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"blobs overlap: {previous[2]} and {current[2]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        output.truncate(total_size)
        for offset, _, source_path in blobs:
            output.seek(offset)
            with source_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)


def _gzip_image(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source, output_path.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=6,
            fileobj=raw_output,
            mtime=0,
        ) as compressed:
            while chunk := source.read(1024 * 1024):
                compressed.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--size-mb", required=True, type=int)
    parser.add_argument("--compression", choices=("gzip", "none"), default="gzip")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.compression == "none":
        create_raw_image(manifest["blobs"], args.output, args.size_mb)
    else:
        temporary = args.output.with_name(args.output.name + ".uncompressed.tmp")
        try:
            create_raw_image(manifest["blobs"], temporary, args.size_mb)
            _gzip_image(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
