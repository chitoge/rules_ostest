#!/usr/bin/env python3
"""Build a deterministic ISO9660 image with a UEFI El Torito boot image."""

from __future__ import annotations

import argparse
import gzip
import pathlib
import struct


ISO_SECTOR_SIZE = 2048
VOLUME_DESCRIPTOR_SECTOR = 16
BOOT_RECORD_SECTOR = 17
TERMINATOR_SECTOR = 18
L_PATH_TABLE_SECTOR = 19
M_PATH_TABLE_SECTOR = 20
ROOT_DIRECTORY_SECTOR = 21
BOOT_CATALOG_SECTOR = 22
BOOT_IMAGE_SECTOR = 23


def _both_endian_16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def _both_endian_32(value: int) -> bytes:
    return struct.pack("<I", value) + struct.pack(">I", value)


def _directory_record(
    identifier: bytes,
    extent: int,
    size: int,
    *,
    directory: bool,
) -> bytes:
    padding = b"\0" if len(identifier) % 2 == 0 else b""
    length = 33 + len(identifier) + len(padding)
    record = bytearray(length)
    record[0] = length
    record[2:10] = _both_endian_32(extent)
    record[10:18] = _both_endian_32(size)
    record[18:25] = bytes((80, 1, 1, 0, 0, 0, 0))
    record[25] = 0x02 if directory else 0
    record[28:32] = _both_endian_16(1)
    record[32] = len(identifier)
    record[33 : 33 + len(identifier)] = identifier
    return bytes(record)


def _primary_volume_descriptor(total_sectors: int, volume_label: str) -> bytes:
    descriptor = bytearray(ISO_SECTOR_SIZE)
    descriptor[0] = 1
    descriptor[1:6] = b"CD001"
    descriptor[6] = 1
    descriptor[8:40] = b"RULES_OSTEST".ljust(32, b" ")
    descriptor[40:72] = volume_label.encode("ascii").ljust(32, b" ")
    descriptor[80:88] = _both_endian_32(total_sectors)
    descriptor[120:124] = _both_endian_16(1)
    descriptor[124:128] = _both_endian_16(1)
    descriptor[128:132] = _both_endian_16(ISO_SECTOR_SIZE)
    descriptor[132:140] = _both_endian_32(10)
    struct.pack_into("<I", descriptor, 140, L_PATH_TABLE_SECTOR)
    struct.pack_into(">I", descriptor, 148, M_PATH_TABLE_SECTOR)
    descriptor[156:190] = _directory_record(
        b"\0",
        ROOT_DIRECTORY_SECTOR,
        ISO_SECTOR_SIZE,
        directory=True,
    )
    descriptor[813:830] = b"1980010100000000\0"
    descriptor[830:847] = b"1980010100000000\0"
    descriptor[847:864] = b"0000000000000000\0"
    descriptor[864:881] = b"0000000000000000\0"
    descriptor[881] = 1
    return bytes(descriptor)


def _boot_record_descriptor() -> bytes:
    descriptor = bytearray(ISO_SECTOR_SIZE)
    descriptor[0] = 0
    descriptor[1:6] = b"CD001"
    descriptor[6] = 1
    descriptor[7:39] = b"EL TORITO SPECIFICATION".ljust(32, b" ")
    struct.pack_into("<I", descriptor, 71, BOOT_CATALOG_SECTOR)
    return bytes(descriptor)


def _terminator() -> bytes:
    descriptor = bytearray(ISO_SECTOR_SIZE)
    descriptor[0] = 255
    descriptor[1:6] = b"CD001"
    descriptor[6] = 1
    return bytes(descriptor)


def _path_table(*, big_endian: bool) -> bytes:
    byte_order = ">" if big_endian else "<"
    entry = bytearray(10)
    entry[0] = 1
    struct.pack_into(byte_order + "I", entry, 2, ROOT_DIRECTORY_SECTOR)
    struct.pack_into(byte_order + "H", entry, 6, 1)
    entry[8] = 0
    return bytes(entry).ljust(ISO_SECTOR_SIZE, b"\0")


def _root_directory(boot_image_size: int) -> bytes:
    records = b"".join(
        (
            _directory_record(
                b"\0",
                ROOT_DIRECTORY_SECTOR,
                ISO_SECTOR_SIZE,
                directory=True,
            ),
            _directory_record(
                b"\1",
                ROOT_DIRECTORY_SECTOR,
                ISO_SECTOR_SIZE,
                directory=True,
            ),
            _directory_record(
                b"EFI.IMG;1",
                BOOT_IMAGE_SECTOR,
                boot_image_size,
                directory=False,
            ),
        )
    )
    return records.ljust(ISO_SECTOR_SIZE, b"\0")


def _boot_catalog(boot_image_size: int) -> bytes:
    catalog = bytearray(ISO_SECTOR_SIZE)
    catalog[0] = 1
    catalog[1] = 0xEF
    catalog[4:28] = b"RULES_OSTEST UEFI".ljust(24, b" ")
    catalog[30:32] = b"\x55\xAA"
    checksum = (-sum(struct.unpack("<16H", catalog[:32]))) & 0xFFFF
    struct.pack_into("<H", catalog, 28, checksum)

    catalog[32] = 0x88
    catalog[33] = 0
    virtual_sectors = (boot_image_size + 511) // 512
    # UEFI defines 0/1 as extending to the end of the optical medium when the
    # boot image exceeds El Torito's 16-bit sector-count field.
    sector_count = virtual_sectors if virtual_sectors <= 0xFFFF else 0
    struct.pack_into("<H", catalog, 38, sector_count)
    struct.pack_into("<I", catalog, 40, BOOT_IMAGE_SECTOR)
    return bytes(catalog)


def _read_logical_image(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data


def create_iso(esp_path: pathlib.Path, output_path: pathlib.Path, volume_label: str) -> None:
    boot_image = _read_logical_image(esp_path)
    if not boot_image or len(boot_image) % 512:
        raise ValueError("the UEFI boot image must be a non-empty multiple of 512 bytes")
    boot_sectors = (len(boot_image) + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    total_sectors = BOOT_IMAGE_SECTOR + boot_sectors
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        output.truncate(total_sectors * ISO_SECTOR_SIZE)
        for sector, data in (
            (VOLUME_DESCRIPTOR_SECTOR, _primary_volume_descriptor(total_sectors, volume_label)),
            (BOOT_RECORD_SECTOR, _boot_record_descriptor()),
            (TERMINATOR_SECTOR, _terminator()),
            (L_PATH_TABLE_SECTOR, _path_table(big_endian=False)),
            (M_PATH_TABLE_SECTOR, _path_table(big_endian=True)),
            (ROOT_DIRECTORY_SECTOR, _root_directory(len(boot_image))),
            (BOOT_CATALOG_SECTOR, _boot_catalog(len(boot_image))),
        ):
            output.seek(sector * ISO_SECTOR_SIZE)
            output.write(data)
        output.seek(BOOT_IMAGE_SECTOR * ISO_SECTOR_SIZE)
        output.write(boot_image)


def _gzip_image(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
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
    parser.add_argument("--esp", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--volume-label", required=True)
    parser.add_argument("--compression", choices=("gzip", "none"), default="gzip")
    args = parser.parse_args()
    if not args.volume_label or len(args.volume_label) > 32 or not args.volume_label.isascii():
        raise ValueError("volume label must be 1-32 ASCII characters")
    if args.compression == "none":
        create_iso(args.esp, args.output, args.volume_label)
        return
    temporary = args.output.with_name(args.output.name + ".uncompressed.tmp")
    try:
        create_iso(args.esp, temporary, args.volume_label)
        _gzip_image(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
