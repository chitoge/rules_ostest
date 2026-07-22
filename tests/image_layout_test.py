#!/usr/bin/env python3
"""Black-box validation of images produced by the public Bazel rules."""

from __future__ import annotations

import argparse
import binascii
import gzip
import hashlib
import os
import pathlib
import struct
import unittest
import uuid

from python.runfiles import runfiles


SECTOR_SIZE = 512
EFI_SYSTEM_PARTITION_GUID = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")


def _read_image(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data


def _lfn_units(entry: bytes) -> list[int]:
    data = entry[1:11] + entry[14:26] + entry[28:32]
    return list(struct.unpack("<13H", data))


class Fat32:
    def __init__(self, data: bytes):
        self.data = data
        self.bytes_per_sector = struct.unpack_from("<H", data, 11)[0]
        self.sectors_per_cluster = data[13]
        self.reserved_sectors = struct.unpack_from("<H", data, 14)[0]
        self.fat_count = data[16]
        self.fat_sectors = struct.unpack_from("<I", data, 36)[0]
        self.root_cluster = struct.unpack_from("<I", data, 44)[0]
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.data_offset = (
            self.reserved_sectors + self.fat_count * self.fat_sectors
        ) * self.bytes_per_sector

    def cluster(self, number: int) -> bytes:
        offset = self.data_offset + (number - 2) * self.cluster_size
        return self.data[offset : offset + self.cluster_size]

    def chain(self, first_cluster: int) -> bytes:
        chunks = []
        cluster = first_cluster
        seen = set()
        while cluster >= 2 and cluster < 0x0FFFFFF8:
            if cluster in seen:
                raise AssertionError("FAT cluster-chain loop")
            seen.add(cluster)
            chunks.append(self.cluster(cluster))
            cluster = struct.unpack_from("<I", self.data, self.fat_offset + cluster * 4)[0] & 0x0FFFFFFF
        return b"".join(chunks)

    def directory(self, first_cluster: int) -> dict[str, tuple[int, int, int]]:
        result = {}
        long_parts: dict[int, list[int]] = {}
        for offset in range(0, len(self.chain(first_cluster)), 32):
            entry = self.chain(first_cluster)[offset : offset + 32]
            if not entry or entry[0] == 0:
                break
            if entry[0] == 0xE5:
                long_parts = {}
                continue
            if entry[11] == 0x0F:
                long_parts[entry[0] & 0x1F] = _lfn_units(entry)
                continue
            if entry[11] & 0x08:
                long_parts = {}
                continue
            if long_parts:
                units = []
                for sequence in sorted(long_parts):
                    units.extend(long_parts[sequence])
                units = units[: units.index(0)] if 0 in units else [unit for unit in units if unit != 0xFFFF]
                name = b"".join(struct.pack("<H", unit) for unit in units).decode("utf-16le")
            else:
                base = entry[:8].decode("ascii").rstrip()
                extension = entry[8:11].decode("ascii").rstrip()
                name = base + (("." + extension) if extension else "")
            cluster = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]
            size = struct.unpack_from("<I", entry, 28)[0]
            result[name] = (entry[11], cluster, size)
            long_parts = {}
        return result

    def read_path(self, path: str) -> bytes:
        components = path.split("/")
        cluster = self.root_cluster
        for component in components[:-1]:
            attributes, cluster, _ = self.directory(cluster)[component]
            if not attributes & 0x10:
                raise AssertionError(f"{component} is not a directory")
        attributes, cluster, size = self.directory(cluster)[components[-1]]
        if attributes & 0x10:
            raise AssertionError(f"{path} is a directory")
        return self.chain(cluster)[:size] if size else b""


class ImageLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = argparse.ArgumentParser()
        parser.add_argument("--esp", required=True)
        parser.add_argument("--esp-copy", required=True)
        parser.add_argument("--disk", required=True)
        parser.add_argument("--disk-copy", required=True)
        parser.add_argument("--composed-disk", required=True)
        parser.add_argument("--raw", required=True)
        parser.add_argument("--platform-aarch64-esp", required=True)
        parser.add_argument("--iso", required=True)
        parser.add_argument("--mbr", required=True)
        cls.args, remaining = parser.parse_known_args()
        unittest_args = [os.path.basename(__file__)] + remaining
        unittest.main_argv = unittest_args
        locator = runfiles.Create()
        assert locator is not None
        cls.esp_path = pathlib.Path(locator.Rlocation(cls.args.esp))
        cls.esp_copy_path = pathlib.Path(locator.Rlocation(cls.args.esp_copy))
        cls.disk_path = pathlib.Path(locator.Rlocation(cls.args.disk))
        cls.disk_copy_path = pathlib.Path(locator.Rlocation(cls.args.disk_copy))
        cls.composed_disk_path = pathlib.Path(locator.Rlocation(cls.args.composed_disk))
        cls.raw_path = pathlib.Path(locator.Rlocation(cls.args.raw))
        cls.platform_aarch64_esp_path = pathlib.Path(locator.Rlocation(cls.args.platform_aarch64_esp))
        cls.iso_path = pathlib.Path(locator.Rlocation(cls.args.iso))
        cls.mbr_path = pathlib.Path(locator.Rlocation(cls.args.mbr))

    def test_fat32_layout_and_contents(self):
        data = _read_image(self.esp_path)
        self.assertEqual(len(data), 64 * 1024 * 1024)
        self.assertEqual(data[510:512], b"\x55\xAA")
        self.assertEqual(data[82:90], b"FAT32   ")
        self.assertEqual(data[71:82], b"OSTEST     ")
        self.assertEqual(struct.unpack_from("<H", data, 14)[0], 32)
        filesystem = Fat32(data)
        self.assertEqual(
            filesystem.read_path("EFI/BOOT/BOOTX64.EFI"),
            b"This is a fixture standing in for a PE/COFF UEFI executable.\n",
        )
        self.assertEqual(
            filesystem.read_path("config/Long File Name.txt"),
            b"serial_baud=115200\nresult_protocol=OSTEST\n",
        )
        first_fat = data[filesystem.fat_offset : filesystem.fat_offset + filesystem.fat_sectors * SECTOR_SIZE]
        second_fat_offset = filesystem.fat_offset + filesystem.fat_sectors * SECTOR_SIZE
        second_fat = data[second_fat_offset : second_fat_offset + filesystem.fat_sectors * SECTOR_SIZE]
        self.assertEqual(first_fat, second_fat)

    def test_fat_output_is_reproducible_across_target_names(self):
        self.assertEqual(
            hashlib.sha256(self.esp_path.read_bytes()).digest(),
            hashlib.sha256(self.esp_copy_path.read_bytes()).digest(),
        )

    def test_platform_selects_aarch64_uefi_fallback_name(self):
        filesystem = Fat32(_read_image(self.platform_aarch64_esp_path))
        self.assertEqual(
            filesystem.read_path("EFI/BOOT/BOOTAA64.EFI"),
            b"This is a fixture standing in for a PE/COFF UEFI executable.\n",
        )

    def test_gpt_layout_and_crcs(self):
        data = _read_image(self.disk_path)
        self.assertEqual(len(data), 96 * 1024 * 1024)
        self.assertEqual(data[450], 0xEE)
        self.assertEqual(data[510:512], b"\x55\xAA")
        header = bytearray(data[SECTOR_SIZE : 2 * SECTOR_SIZE])
        self.assertEqual(header[:8], b"EFI PART")
        header_size = struct.unpack_from("<I", header, 12)[0]
        stored_header_crc = struct.unpack_from("<I", header, 16)[0]
        struct.pack_into("<I", header, 16, 0)
        self.assertEqual(binascii.crc32(header[:header_size]) & 0xFFFFFFFF, stored_header_crc)
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_count, entry_size, entries_crc = struct.unpack_from("<III", header, 80)
        entries = data[
            entries_lba * SECTOR_SIZE : entries_lba * SECTOR_SIZE + entry_count * entry_size
        ]
        self.assertEqual(binascii.crc32(entries) & 0xFFFFFFFF, entries_crc)
        self.assertEqual(uuid.UUID(bytes_le=entries[:16]), EFI_SYSTEM_PARTITION_GUID)
        start_lba, end_lba = struct.unpack_from("<QQ", entries, 32)
        self.assertEqual(start_lba, 2048)
        self.assertEqual(end_lba - start_lba + 1, 64 * 1024 * 1024 // SECTOR_SIZE)
        partition_offset = start_lba * SECTOR_SIZE
        self.assertEqual(struct.unpack_from("<I", data, partition_offset + 28)[0], start_lba)
        self.assertEqual(data[-SECTOR_SIZE : -SECTOR_SIZE + 8], b"EFI PART")

    def test_gpt_and_gzip_output_is_reproducible(self):
        first = self.disk_path.read_bytes()
        second = self.disk_copy_path.read_bytes()
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        self.assertEqual(first[:3], b"\x1f\x8b\x08")
        self.assertEqual(first[3], 0)  # No original filename or other variable fields.
        self.assertEqual(first[4:8], b"\0\0\0\0")

    def test_raw_blob_offsets(self):
        data = _read_image(self.raw_path)
        self.assertEqual(len(data), 2 * 1024 * 1024)
        config = b"serial_baud=115200\nresult_protocol=OSTEST\n"
        self.assertEqual(
            data[1024 * 1024 : 1024 * 1024 + len(config)],
            config,
        )
        executable = b"This is a fixture standing in for a PE/COFF UEFI executable.\n"
        self.assertEqual(
            data[3072 * SECTOR_SIZE : 3072 * SECTOR_SIZE + len(executable)],
            executable,
        )

    def test_composed_gpt_partitions(self):
        data = _read_image(self.composed_disk_path)
        header = data[SECTOR_SIZE : 2 * SECTOR_SIZE]
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        entries = data[entries_lba * SECTOR_SIZE :]
        first = entries[:entry_size]
        second = entries[entry_size : 2 * entry_size]
        self.assertEqual(uuid.UUID(bytes_le=first[:16]), EFI_SYSTEM_PARTITION_GUID)
        self.assertEqual(
            uuid.UUID(bytes_le=second[:16]),
            uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4"),
        )
        first_start, first_end = struct.unpack_from("<QQ", first, 32)
        second_start, second_end = struct.unpack_from("<QQ", second, 32)
        self.assertEqual(first_start, 2048)
        self.assertEqual(first_end - first_start + 1, 64 * 1024 * 1024 // SECTOR_SIZE)
        self.assertEqual(second_start % 2048, 0)
        self.assertGreater(second_start, first_end)
        self.assertEqual(second_end - second_start + 1, 2 * 1024 * 1024 // SECTOR_SIZE)
        name = second[56:128].decode("utf-16le").rstrip("\0")
        self.assertEqual(name, "Test Data")
        config = b"serial_baud=115200\nresult_protocol=OSTEST\n"
        data_offset = second_start * SECTOR_SIZE + 1024 * 1024
        self.assertEqual(data[data_offset : data_offset + len(config)], config)

    def test_cache_artifacts_are_compressed(self):
        self.assertLess(self.esp_path.stat().st_size, 1024 * 1024)
        self.assertLess(self.disk_path.stat().st_size, 2 * 1024 * 1024)
        self.assertLess(self.composed_disk_path.stat().st_size, 2 * 1024 * 1024)
        self.assertLess(self.raw_path.stat().st_size, 128 * 1024)
        self.assertLess(self.iso_path.stat().st_size, 2 * 1024 * 1024)
        self.assertLess(self.mbr_path.stat().st_size, 2 * 1024 * 1024)

    def test_uefi_compatible_primary_mbr(self):
        data = _read_image(self.mbr_path)
        self.assertEqual(len(data), 96 * 1024 * 1024)
        self.assertEqual(data[510:512], b"\x55\xaa")
        entry = data[446:462]
        bootable, type_id, start_lba, sectors = (
            entry[0],
            entry[4],
            struct.unpack_from("<I", entry, 8)[0],
            struct.unpack_from("<I", entry, 12)[0],
        )
        self.assertEqual(bootable, 0)
        self.assertEqual(type_id, 0xEF)
        self.assertEqual(start_lba, 2048)
        self.assertEqual(sectors, 64 * 1024 * 1024 // SECTOR_SIZE)
        partition = data[start_lba * SECTOR_SIZE :]
        self.assertEqual(partition[82:90], b"FAT32   ")
        self.assertEqual(struct.unpack_from("<I", partition, 28)[0], start_lba)
        backup_sector = struct.unpack_from("<H", partition, 50)[0]
        self.assertEqual(
            struct.unpack_from("<I", partition, backup_sector * SECTOR_SIZE + 28)[0],
            start_lba,
        )
        self.assertEqual(
            Fat32(partition).read_path("EFI/BOOT/BOOTX64.EFI"),
            b"This is a fixture standing in for a PE/COFF UEFI executable.\n",
        )

    def test_uefi_el_torito_iso(self):
        data = _read_image(self.iso_path)
        self.assertEqual(data[16 * 2048 + 1 : 16 * 2048 + 6], b"CD001")
        boot_record = data[17 * 2048 : 18 * 2048]
        self.assertEqual(boot_record[7:30], b"EL TORITO SPECIFICATION")
        catalog_lba = struct.unpack_from("<I", boot_record, 71)[0]
        catalog = data[catalog_lba * 2048 : (catalog_lba + 1) * 2048]
        self.assertEqual(catalog[0:2], b"\x01\xef")
        self.assertEqual(sum(struct.unpack("<16H", catalog[:32])) & 0xFFFF, 0)
        self.assertEqual(catalog[30:32], b"\x55\xaa")
        self.assertEqual(catalog[32], 0x88)
        self.assertEqual(catalog[33], 0)
        # The 64 MiB ESP exceeds El Torito's 16-bit sector count; UEFI uses
        # zero to mean the no-emulation image extends to the end of the media.
        self.assertEqual(struct.unpack_from("<H", catalog, 38)[0], 0)
        image_lba = struct.unpack_from("<I", catalog, 40)[0]
        self.assertEqual(data[image_lba * 2048 + 82 : image_lba * 2048 + 90], b"FAT32   ")


if __name__ == "__main__":
    # setUpClass parses the rule-provided arguments before unittest sees them.
    unittest.main(argv=[os.path.basename(__file__)])
