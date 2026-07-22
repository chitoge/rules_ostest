#!/usr/bin/env python3
"""Compose deterministic GPT disks from ordered partition images."""

from __future__ import annotations

import argparse
import binascii
import gzip
import json
import pathlib
import struct
import uuid
from dataclasses import dataclass


SECTOR_SIZE = 512
GPT_ENTRY_COUNT = 128
GPT_ENTRY_SIZE = 128
GPT_ENTRY_ARRAY_SECTORS = GPT_ENTRY_COUNT * GPT_ENTRY_SIZE // SECTOR_SIZE
GUID_NAMESPACE = uuid.UUID("a289a8e3-ead1-5e5c-b9d8-cbe4f52385d4")


@dataclass(frozen=True)
class Partition:
    image: pathlib.Path
    image_compression: str
    image_size: int
    type_guid: uuid.UUID
    unique_guid: uuid.UUID
    partition_name: str
    attributes: int
    alignment_lba: int
    requested_start_lba: int
    patch_fat_hidden_sectors: bool
    start_lba: int
    end_lba: int


def _guid(value: str, identity: str, kind: str) -> uuid.UUID:
    if value:
        return uuid.UUID(value)
    return uuid.uuid5(GUID_NAMESPACE, f"rules_ostest:{identity}:{kind}")


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _gpt_header(
    *,
    current_lba: int,
    backup_lba: int,
    first_usable_lba: int,
    last_usable_lba: int,
    disk_guid: uuid.UUID,
    entries_lba: int,
    entries_crc: int,
) -> bytes:
    header = bytearray(SECTOR_SIZE)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header,
        0,
        b"EFI PART",
        0x00010000,
        92,
        0,
        0,
        current_lba,
        backup_lba,
        first_usable_lba,
        last_usable_lba,
        disk_guid.bytes_le,
        entries_lba,
        GPT_ENTRY_COUNT,
        GPT_ENTRY_SIZE,
        entries_crc,
    )
    header_crc = binascii.crc32(header[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, header_crc)
    return bytes(header)


def _protective_mbr(total_lbas: int) -> bytes:
    mbr = bytearray(SECTOR_SIZE)
    partition_size = min(total_lbas - 1, 0xFFFFFFFF)
    mbr[446:462] = struct.pack(
        "<B3sB3sII",
        0,
        b"\x00\x02\x00",
        0xEE,
        b"\xFF\xFF\xFF",
        1,
        partition_size,
    )
    mbr[510:512] = b"\x55\xAA"
    return bytes(mbr)


def _partition_entries(partitions: list[Partition]) -> bytes:
    entries = bytearray(GPT_ENTRY_COUNT * GPT_ENTRY_SIZE)
    for index, partition in enumerate(partitions):
        encoded_name = partition.partition_name.encode("utf-16le")
        if len(encoded_name) > 72:
            raise ValueError(
                f"partition {index + 1} name exceeds 36 UTF-16 code units"
            )
        offset = index * GPT_ENTRY_SIZE
        entries[offset : offset + 16] = partition.type_guid.bytes_le
        entries[offset + 16 : offset + 32] = partition.unique_guid.bytes_le
        struct.pack_into(
            "<QQQ",
            entries,
            offset + 32,
            partition.start_lba,
            partition.end_lba,
            partition.attributes,
        )
        entries[offset + 56 : offset + 56 + len(encoded_name)] = encoded_name
    return bytes(entries)


def _resolve_compression(source: pathlib.Path, requested: str) -> str:
    if requested not in ("auto", "gzip", "none"):
        raise ValueError(f"unsupported image compression {requested!r}")
    if requested != "auto":
        return requested
    with source.open("rb") as input_file:
        return "gzip" if input_file.read(2) == b"\x1f\x8b" else "none"


def _logical_size(source: pathlib.Path, compression: str) -> int:
    if compression == "none":
        return source.stat().st_size
    size = 0
    with gzip.open(source, "rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            size += len(chunk)
    return size


def _copy_file(source: pathlib.Path, compression: str, output, offset: int) -> None:
    output.seek(offset)
    opener = gzip.open if compression == "gzip" else open
    with opener(source, "rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            output.write(chunk)


def _patch_fat_hidden_sectors(
    output,
    partition_offset: int,
    partition_size: int,
    start_lba: int,
) -> None:
    output.seek(partition_offset)
    boot = output.read(SECTOR_SIZE)
    if len(boot) != SECTOR_SIZE or boot[510:512] != b"\x55\xAA":
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


def _layout_partitions(
    specs: list[dict[str, object]],
    identity: str,
    first_usable_lba: int,
    last_usable_lba: int,
) -> list[Partition]:
    if not specs:
        raise ValueError("at least one partition is required")
    if len(specs) > GPT_ENTRY_COUNT:
        raise ValueError(f"at most {GPT_ENTRY_COUNT} partitions are supported")

    partitions = []
    cursor = first_usable_lba
    for index, spec in enumerate(specs):
        image = pathlib.Path(str(spec["image"]))
        if not image.is_file():
            raise ValueError(f"partition {index + 1} image is not a file: {image}")
        image_compression = _resolve_compression(
            image,
            str(spec.get("image_compression", "auto")),
        )
        image_size = _logical_size(image, image_compression)
        if image_size <= 0 or image_size % SECTOR_SIZE:
            raise ValueError(
                f"partition {index + 1} image must be a non-empty multiple of {SECTOR_SIZE} bytes"
            )
        alignment_lba = int(spec.get("alignment_lba", 2048))
        if alignment_lba <= 0:
            raise ValueError(f"partition {index + 1} alignment must be positive")
        requested_start_lba = int(spec.get("start_lba", 0))
        if requested_start_lba < 0:
            raise ValueError(f"partition {index + 1} start LBA may not be negative")
        if requested_start_lba:
            start_lba = requested_start_lba
            if start_lba % alignment_lba:
                raise ValueError(
                    f"partition {index + 1} start LBA {start_lba} is not aligned to {alignment_lba}"
                )
            if start_lba < cursor:
                raise ValueError(
                    f"partition {index + 1} starts at {start_lba}, before available LBA {cursor}"
                )
        else:
            start_lba = _align_up(cursor, alignment_lba)
        sector_count = image_size // SECTOR_SIZE
        end_lba = start_lba + sector_count - 1
        if start_lba < first_usable_lba or end_lba > last_usable_lba:
            raise ValueError(
                f"partition {index + 1} occupies LBAs {start_lba}-{end_lba}, "
                f"outside usable range {first_usable_lba}-{last_usable_lba}"
            )
        name = str(spec.get("partition_name", ""))
        unique_guid = _guid(
            str(spec.get("unique_guid", "")),
            identity,
            f"partition:{index}:{name}",
        )
        attributes = int(spec.get("attributes", 0))
        if not 0 <= attributes <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"partition {index + 1} attributes do not fit in 64 bits")
        partitions.append(
            Partition(
                image=image,
                image_compression=image_compression,
                image_size=image_size,
                type_guid=uuid.UUID(str(spec["type_guid"])),
                unique_guid=unique_guid,
                partition_name=name,
                attributes=attributes,
                alignment_lba=alignment_lba,
                requested_start_lba=requested_start_lba,
                patch_fat_hidden_sectors=bool(spec.get("patch_fat_hidden_sectors", False)),
                start_lba=start_lba,
                end_lba=end_lba,
            )
        )
        cursor = end_lba + 1
    return partitions


def create_gpt_image(
    specs: list[dict[str, object]],
    output_path: pathlib.Path,
    size_mb: int,
    identity: str,
    disk_guid_text: str = "",
) -> None:
    total_bytes = size_mb * 1024 * 1024
    if total_bytes <= 0 or total_bytes % SECTOR_SIZE:
        raise ValueError("disk size must be a positive multiple of 512 bytes")
    total_lbas = total_bytes // SECTOR_SIZE
    first_usable_lba = 2 + GPT_ENTRY_ARRAY_SECTORS
    last_usable_lba = total_lbas - GPT_ENTRY_ARRAY_SECTORS - 2
    partitions = _layout_partitions(
        specs,
        identity,
        first_usable_lba,
        last_usable_lba,
    )

    disk_guid = _guid(disk_guid_text, identity, "disk")
    entries = _partition_entries(partitions)
    entries_crc = binascii.crc32(entries) & 0xFFFFFFFF
    backup_entries_lba = total_lbas - GPT_ENTRY_ARRAY_SECTORS - 1
    primary_header = _gpt_header(
        current_lba=1,
        backup_lba=total_lbas - 1,
        first_usable_lba=first_usable_lba,
        last_usable_lba=last_usable_lba,
        disk_guid=disk_guid,
        entries_lba=2,
        entries_crc=entries_crc,
    )
    backup_header = _gpt_header(
        current_lba=total_lbas - 1,
        backup_lba=1,
        first_usable_lba=first_usable_lba,
        last_usable_lba=last_usable_lba,
        disk_guid=disk_guid,
        entries_lba=backup_entries_lba,
        entries_crc=entries_crc,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w+b") as output:
        output.truncate(total_bytes)
        output.seek(0)
        output.write(_protective_mbr(total_lbas))
        output.write(primary_header)
        output.write(entries)
        for partition in partitions:
            partition_offset = partition.start_lba * SECTOR_SIZE
            _copy_file(
                partition.image,
                partition.image_compression,
                output,
                partition_offset,
            )
            if partition.patch_fat_hidden_sectors:
                _patch_fat_hidden_sectors(
                    output,
                    partition_offset,
                    partition.image_size,
                    partition.start_lba,
                )
        output.seek(backup_entries_lba * SECTOR_SIZE)
        output.write(entries)
        output.seek((total_lbas - 1) * SECTOR_SIZE)
        output.write(backup_header)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--size-mb", required=True, type=int)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--disk-guid", default="")
    parser.add_argument("--compression", choices=("gzip", "none"), default="gzip")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.compression == "none":
        create_gpt_image(
            manifest["partitions"],
            args.output,
            args.size_mb,
            args.identity,
            args.disk_guid,
        )
    else:
        temporary = args.output.with_name(args.output.name + ".uncompressed.tmp")
        try:
            create_gpt_image(
                manifest["partitions"],
                temporary,
                args.size_mb,
                args.identity,
                args.disk_guid,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("rb") as source, args.output.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=6,
                    fileobj=raw_output,
                    mtime=0,
                ) as compressed:
                    while chunk := source.read(1024 * 1024):
                        compressed.write(chunk)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
