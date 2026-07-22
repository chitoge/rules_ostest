#!/usr/bin/env python3
"""Compose a deterministic MBR-partitioned disk from ordered images."""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import struct


SECTOR_SIZE = 512


def _compression(path: pathlib.Path, requested: str) -> str:
    if requested not in ("auto", "gzip", "none"):
        raise ValueError(f"unsupported image compression {requested!r}")
    if requested != "auto":
        return requested
    with path.open("rb") as source:
        return "gzip" if source.read(2) == b"\x1f\x8b" else "none"


def _logical_size(path: pathlib.Path, compression: str) -> int:
    if compression == "none":
        return path.stat().st_size
    size = 0
    with gzip.open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
    return size


def _chs(lba: int) -> bytes:
    sectors_per_track = 63
    heads = 255
    cylinder = lba // (sectors_per_track * heads)
    if cylinder > 1023:
        return b"\xfe\xff\xff"
    head = (lba // sectors_per_track) % heads
    sector = (lba % sectors_per_track) + 1
    return bytes((head, sector | ((cylinder >> 2) & 0xC0), cylinder & 0xFF))


def _patch_fat_hidden_sectors(
    output,
    partition_offset: int,
    partition_size: int,
    start_lba: int,
) -> None:
    output.seek(partition_offset)
    boot = output.read(SECTOR_SIZE)
    if len(boot) != SECTOR_SIZE or boot[510:512] != b"\x55\xaa":
        raise ValueError("partition marked for FAT patching has no boot-sector signature")
    if struct.unpack_from("<H", boot, 11)[0] != SECTOR_SIZE:
        raise ValueError("partition marked for FAT patching does not use 512-byte sectors")
    backup_sector = struct.unpack_from("<H", boot, 50)[0]
    output.seek(partition_offset + 28)
    output.write(struct.pack("<I", start_lba))
    if backup_sector:
        if (backup_sector + 1) * SECTOR_SIZE > partition_size:
            raise ValueError("FAT backup boot sector is outside its partition")
        output.seek(partition_offset + backup_sector * SECTOR_SIZE + 28)
        output.write(struct.pack("<I", start_lba))


def create_mbr_image(
    specs: list[dict[str, object]],
    output_path: pathlib.Path,
    size_mb: int,
) -> None:
    if not 1 <= len(specs) <= 4:
        raise ValueError("an MBR disk requires one to four primary partitions")
    total_bytes = size_mb * 1024 * 1024
    total_lbas = total_bytes // SECTOR_SIZE
    if total_bytes <= 0 or total_bytes % SECTOR_SIZE or total_lbas > 0xFFFFFFFF:
        raise ValueError("MBR disk size must be a positive 512-byte multiple below 2 TiB")
    layout = []
    cursor = 1
    for index, spec in enumerate(specs):
        image = pathlib.Path(str(spec["image"]))
        compression = _compression(image, str(spec.get("image_compression", "auto")))
        size = _logical_size(image, compression)
        if size <= 0 or size % SECTOR_SIZE:
            raise ValueError(f"partition {index + 1} image is not sector aligned")
        alignment = int(spec.get("alignment_lba", 2048))
        requested_start = int(spec.get("start_lba", 0))
        if alignment <= 0 or requested_start < 0:
            raise ValueError(f"partition {index + 1} has an invalid alignment or start")
        start = requested_start or ((cursor + alignment - 1) // alignment) * alignment
        if start < cursor or start % alignment:
            raise ValueError(f"partition {index + 1} overlaps or is not aligned")
        sectors = size // SECTOR_SIZE
        end = start + sectors - 1
        if end >= total_lbas or sectors > 0xFFFFFFFF:
            raise ValueError(f"partition {index + 1} exceeds the MBR disk")
        type_id = int(spec.get("type_id", 0xEF))
        if not 1 <= type_id <= 0xFF:
            raise ValueError(f"partition {index + 1} has an invalid MBR type")
        layout.append((
            image,
            compression,
            size,
            start,
            end,
            sectors,
            type_id,
            bool(spec.get("bootable", False)),
            bool(spec.get("patch_fat_hidden_sectors", False)),
        ))
        cursor = end + 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w+b") as output:
        output.truncate(total_bytes)
        mbr = bytearray(SECTOR_SIZE)
        for index, (_, _, _, start, end, sectors, type_id, bootable, _) in enumerate(layout):
            struct.pack_into(
                "<B3sB3sII",
                mbr,
                446 + index * 16,
                0x80 if bootable else 0,
                _chs(start),
                type_id,
                _chs(end),
                start,
                sectors,
            )
        mbr[510:512] = b"\x55\xaa"
        output.write(mbr)
        for image, compression, size, start, _, _, _, _, patch_fat in layout:
            output.seek(start * SECTOR_SIZE)
            opener = gzip.open if compression == "gzip" else open
            with opener(image, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            if patch_fat:
                _patch_fat_hidden_sectors(output, start * SECTOR_SIZE, size, start)


def _gzip_image(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
    with source_path.open("rb") as source, output_path.open("wb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw_output, mtime=0) as compressed:
            while chunk := source.read(1024 * 1024):
                compressed.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--size-mb", required=True, type=int)
    parser.add_argument("--compression", choices=("gzip", "none"), default="gzip")
    args = parser.parse_args()
    specs = json.loads(args.manifest.read_text(encoding="utf-8"))["partitions"]
    if args.compression == "none":
        create_mbr_image(specs, args.output, args.size_mb)
        return
    temporary = args.output.with_name(args.output.name + ".uncompressed.tmp")
    try:
        create_mbr_image(specs, temporary, args.size_mb)
        _gzip_image(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
