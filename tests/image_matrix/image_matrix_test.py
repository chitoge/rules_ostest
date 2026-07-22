#!/usr/bin/env python3
"""Black-box coverage for deterministic image-rule attributes and modes."""

from __future__ import annotations

import argparse
import mmap
import os
import pathlib
import struct
import unittest
import uuid

from ostest.private.raw_image import parse_offset
from python.runfiles import runfiles


SECTOR_SIZE = 512
ISO_SECTOR_SIZE = 2048
EFI_SYSTEM_PARTITION_GUID = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")


def _resolve(locator: runfiles.Runfiles, value: str) -> pathlib.Path:
    path = locator.Rlocation(value)
    if path is None:
        raise AssertionError(f"runfile not found: {value}")
    return pathlib.Path(path)


def _guid(data: mmap.mmap, offset: int) -> uuid.UUID:
    return uuid.UUID(bytes_le=data[offset : offset + 16])


def _gpt_entry(data: mmap.mmap, index: int) -> tuple[int, bytes]:
    entries_lba = struct.unpack_from("<Q", data, SECTOR_SIZE + 72)[0]
    entry_size = struct.unpack_from("<I", data, SECTOR_SIZE + 84)[0]
    offset = entries_lba * SECTOR_SIZE + index * entry_size
    return offset, data[offset : offset + entry_size]


class ImageMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--fat", required=True)
        parser.add_argument("--gpt", required=True)
        parser.add_argument("--iso", required=True)
        parser.add_argument("--mbr", required=True)
        parser.add_argument("--raw", required=True)
        parser.add_argument("--uefi-disk", required=True)
        args, _ = parser.parse_known_args()
        locator = runfiles.Create()
        assert locator is not None
        cls.fat_path = _resolve(locator, args.fat)
        cls.gpt_path = _resolve(locator, args.gpt)
        cls.iso_path = _resolve(locator, args.iso)
        cls.mbr_path = _resolve(locator, args.mbr)
        cls.raw_path = _resolve(locator, args.raw)
        cls.uefi_disk_path = _resolve(locator, args.uefi_disk)

    def test_fat_metadata_and_uncompressed_custom_output(self) -> None:
        self.assertEqual(self.fat_path.name, "custom-fat.bin")
        self.assertEqual(self.fat_path.stat().st_size, 34 * 1024 * 1024)
        with self.fat_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            self.assertNotEqual(data[:2], b"\x1f\x8b")
            self.assertEqual(data[71:82], b"cidata     ")
            self.assertEqual(struct.unpack_from("<I", data, 67)[0], 0x1234ABCD)
            self.assertEqual(data[:SECTOR_SIZE], data[6 * SECTOR_SIZE : 7 * SECTOR_SIZE])
            self.assertEqual(
                data[SECTOR_SIZE : 2 * SECTOR_SIZE],
                data[7 * SECTOR_SIZE : 8 * SECTOR_SIZE],
            )
            fsinfo = data[SECTOR_SIZE : 2 * SECTOR_SIZE]
            self.assertEqual(struct.unpack_from("<I", fsinfo, 0)[0], 0x41615252)
            self.assertEqual(struct.unpack_from("<I", fsinfo, 484)[0], 0x61417272)
            self.assertEqual(struct.unpack_from("<I", fsinfo, 508)[0], 0xAA550000)
            self.assertGreater(struct.unpack_from("<I", fsinfo, 488)[0], 0)
            self.assertGreaterEqual(struct.unpack_from("<I", fsinfo, 492)[0], 2)

            for alias in (b"LONGDI~1   ", b"MIXEDC~1TXT", b"MIXEDC~2TXT"):
                offset = data.find(alias)
                self.assertNotEqual(offset, -1, alias)
                entry = data[offset : offset + 32]
                self.assertEqual(struct.unpack_from("<H", entry, 14)[0], 0)
                self.assertEqual(struct.unpack_from("<H", entry, 16)[0], 0x21)
                self.assertEqual(struct.unpack_from("<H", entry, 18)[0], 0x21)
                self.assertEqual(struct.unpack_from("<H", entry, 22)[0], 0)
                self.assertEqual(struct.unpack_from("<H", entry, 24)[0], 0x21)

    def test_raw_offsets_uncompressed_custom_output_and_unit_parser(self) -> None:
        self.assertEqual(self.raw_path.name, "custom-raw.bin")
        self.assertEqual(self.raw_path.stat().st_size, 4 * 1024 * 1024)
        with self.raw_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            self.assertEqual(data[17 : 17 + len(b"bytes-unit\n")], b"bytes-unit\n")
            self.assertEqual(data[1024 : 1024 + len(b"kib-unit\n")], b"kib-unit\n")
            self.assertEqual(data[2 * 1024 * 1024 :][:9], b"mib-unit\n")
            sector_marker = b"sector-unit\n"
            self.assertEqual(
                data[6144 * SECTOR_SIZE :][: len(sector_marker)], sector_marker
            )

        self.assertEqual(parse_offset("9"), 9)
        self.assertEqual(parse_offset("9B"), 9)
        self.assertEqual(parse_offset("9KiB"), 9 * 1024)
        self.assertEqual(parse_offset("2MiB"), 2 * 1024 * 1024)
        self.assertEqual(parse_offset("1GiB"), 1024 * 1024 * 1024)
        self.assertEqual(parse_offset("9s"), 9 * SECTOR_SIZE)

    def test_explicit_gpt_metadata_and_automatic_alignment(self) -> None:
        self.assertEqual(self.gpt_path.name, "custom-gpt.bin")
        self.assertEqual(self.gpt_path.stat().st_size, 8 * 1024 * 1024)
        with self.gpt_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            self.assertEqual(data[SECTOR_SIZE : SECTOR_SIZE + 8], b"EFI PART")
            self.assertEqual(
                _guid(data, SECTOR_SIZE + 56),
                uuid.UUID("857a8d1c-fdc4-4f9c-8d3b-df8bf5af9948"),
            )
            first_offset, first = _gpt_entry(data, 0)
            _, second = _gpt_entry(data, 1)
            self.assertEqual(
                uuid.UUID(bytes_le=first[:16]),
                uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4"),
            )
            self.assertEqual(
                uuid.UUID(bytes_le=first[16:32]),
                uuid.UUID("b65ca79b-07b7-4410-97d6-2ab79022a9a8"),
            )
            self.assertEqual(struct.unpack_from("<QQQ", first, 32), (128, 2175, 5))
            self.assertEqual(first[56:128].decode("utf-16le").rstrip("\0"), "Fixed Data")
            self.assertEqual(uuid.UUID(bytes_le=second[:16]), EFI_SYSTEM_PARTITION_GUID)
            second_start, second_end, second_attributes = struct.unpack_from("<QQQ", second, 32)
            self.assertEqual((second_start, second_end, second_attributes), (2304, 4351, 0))
            self.assertEqual(second_start % 256, 0)
            self.assertEqual(data[128 * SECTOR_SIZE :][:14], b"partition-one\n")
            self.assertEqual(data[2304 * SECTOR_SIZE :][:14], b"partition-two\n")
            self.assertEqual(data[first_offset : first_offset + 128], first)

    def test_uefi_wrapper_explicit_metadata_and_fat_patch(self) -> None:
        self.assertEqual(self.uefi_disk_path.name, "custom-uefi-disk.bin")
        self.assertEqual(self.uefi_disk_path.stat().st_size, 40 * 1024 * 1024)
        with self.uefi_disk_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            self.assertEqual(
                _guid(data, SECTOR_SIZE + 56),
                uuid.UUID("5f4bb398-7e44-4189-bf70-9de57bb2d75d"),
            )
            _, entry = _gpt_entry(data, 0)
            self.assertEqual(uuid.UUID(bytes_le=entry[:16]), EFI_SYSTEM_PARTITION_GUID)
            self.assertEqual(
                uuid.UUID(bytes_le=entry[16:32]),
                uuid.UUID("4b8aa8c8-68f7-4b8a-9c7c-e12aa7fc157a"),
            )
            start_lba, end_lba, attributes = struct.unpack_from("<QQQ", entry, 32)
            self.assertEqual((start_lba, end_lba, attributes), (128, 69759, 0))
            self.assertEqual(entry[56:128].decode("utf-16le").rstrip("\0"), "Custom ESP")
            partition_offset = start_lba * SECTOR_SIZE
            self.assertEqual(struct.unpack_from("<I", data, partition_offset + 28)[0], start_lba)
            backup_sector = struct.unpack_from("<H", data, partition_offset + 50)[0]
            self.assertEqual(
                struct.unpack_from(
                    "<I", data, partition_offset + backup_sector * SECTOR_SIZE + 28
                )[0],
                start_lba,
            )

    def test_mbr_active_multiple_explicit_and_aligned_partitions(self) -> None:
        self.assertEqual(self.mbr_path.name, "custom-mbr.bin")
        self.assertEqual(self.mbr_path.stat().st_size, 8 * 1024 * 1024)
        with self.mbr_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            self.assertEqual(data[510:512], b"\x55\xaa")
            first = data[446:462]
            second = data[462:478]
            self.assertEqual((first[0], first[4]), (0x80, 0x0C))
            self.assertEqual(struct.unpack_from("<II", first, 8), (64, 2048))
            self.assertEqual((second[0], second[4]), (0, 0x83))
            self.assertEqual(struct.unpack_from("<II", second, 8), (2176, 2048))
            self.assertEqual(2176 % 128, 0)
            self.assertEqual(data[64 * SECTOR_SIZE :][:14], b"partition-one\n")
            self.assertEqual(data[2176 * SECTOR_SIZE :][:14], b"partition-two\n")

    def test_iso_label_and_uncompressed_custom_output(self) -> None:
        self.assertEqual(self.iso_path.name, "custom-uefi.iso")
        with self.iso_path.open("rb") as image, mmap.mmap(
            image.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            descriptor = 16 * ISO_SECTOR_SIZE
            self.assertNotEqual(data[:2], b"\x1f\x8b")
            self.assertEqual(data[descriptor + 1 : descriptor + 6], b"CD001")
            self.assertEqual(
                data[descriptor + 40 : descriptor + 72],
                b"MATRIX_BOOT".ljust(32, b" "),
            )
            boot_record = 17 * ISO_SECTOR_SIZE
            catalog_lba = struct.unpack_from("<I", data, boot_record + 71)[0]
            catalog = catalog_lba * ISO_SECTOR_SIZE
            self.assertEqual(data[catalog + 32], 0x88)
            image_lba = struct.unpack_from("<I", data, catalog + 40)[0]
            self.assertEqual(
                data[image_lba * ISO_SECTOR_SIZE + 71 : image_lba * ISO_SECTOR_SIZE + 82],
                b"cidata     ",
            )


if __name__ == "__main__":
    unittest.main(argv=[os.path.basename(__file__)])
