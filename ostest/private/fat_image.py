#!/usr/bin/env python3
"""Create a deterministic, UEFI-compatible FAT32 filesystem image."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import pathlib
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Iterable


SECTOR_SIZE = 512
RESERVED_SECTORS = 32
FAT_COUNT = 2
SECTORS_PER_CLUSTER = 1
FAT32_MIN_CLUSTERS = 65_525
END_OF_CHAIN = 0x0FFFFFFF
VALID_SHORT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&")
RESERVED_SHORT_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


@dataclass
class Node:
    name: str
    is_dir: bool
    source: pathlib.Path | None = None
    parent: "Node | None" = None
    children: dict[str, "Node"] = field(default_factory=dict)
    short_name: bytes = b""
    needs_lfn: bool = False
    first_cluster: int = 0
    cluster_count: int = 0
    size: int = 0

    def sorted_children(self) -> list["Node"]:
        return sorted(self.children.values(), key=lambda child: (child.name.casefold(), child.name))


def _validate_component(component: str) -> None:
    if component in ("", ".", ".."):
        raise ValueError(f"invalid FAT path component {component!r}")
    if component[-1] in (" ", "."):
        raise ValueError(f"FAT path component may not end in a space or dot: {component!r}")
    if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in component):
        raise ValueError(f"FAT path component contains a forbidden character: {component!r}")
    units = len(component.encode("utf-16le")) // 2
    if units > 255:
        raise ValueError(f"FAT long filename exceeds 255 UTF-16 code units: {component!r}")


def _build_tree(entries: Iterable[dict[str, str]]) -> Node:
    root = Node(name="", is_dir=True)
    for entry in entries:
        destination = entry["destination"].replace("\\", "/").strip("/")
        parts = destination.split("/")
        if not parts or any(not part for part in parts):
            raise ValueError(f"invalid FAT destination {destination!r}")
        current = root
        for part in parts[:-1]:
            _validate_component(part)
            key = part.casefold()
            child = current.children.get(key)
            if child is None:
                child = Node(name=part, is_dir=True, parent=current)
                current.children[key] = child
            elif not child.is_dir:
                raise ValueError(f"path traverses through a file: {destination!r}")
            current = child

        filename = parts[-1]
        _validate_component(filename)
        key = filename.casefold()
        if key in current.children:
            raise ValueError(f"duplicate or conflicting FAT destination {destination!r}")
        source = pathlib.Path(entry["source"])
        if not source.is_file():
            raise ValueError(f"source is not a regular file: {source}")
        current.children[key] = Node(
            name=filename,
            is_dir=False,
            source=source,
            parent=current,
            size=source.stat().st_size,
        )
    return root


def _split_filename(name: str) -> tuple[str, str]:
    if "." in name and not name.startswith("."):
        base, extension = name.rsplit(".", 1)
        return base, extension
    return name, ""


def _sanitize_short_part(value: str) -> str:
    result = []
    for char in value.upper():
        if char in VALID_SHORT_CHARS:
            result.append(char)
        elif char not in (" ", "."):
            result.append("_")
    return "".join(result)


def _short_display_name(raw: bytes) -> str:
    base = raw[:8].decode("ascii").rstrip()
    extension = raw[8:].decode("ascii").rstrip()
    return base + (("." + extension) if extension else "")


def _assign_short_names(directory: Node) -> None:
    used: set[bytes] = set()
    for child in directory.sorted_children():
        base, extension = _split_filename(child.name)
        clean_base = _sanitize_short_part(base)
        clean_extension = _sanitize_short_part(extension)[:3]
        direct_base = clean_base[:8]
        direct = (direct_base.ljust(8) + clean_extension.ljust(3)).encode("ascii")
        directly_representable = (
            bool(direct_base)
            and len(base) <= 8
            and len(extension) <= 3
            and all(char.upper() in VALID_SHORT_CHARS for char in base + extension)
            and base.upper() not in RESERVED_SHORT_NAMES
            and direct not in used
        )
        if directly_representable:
            raw = direct
        else:
            stem = clean_base or "_"
            raw = b""
            for suffix_number in range(1, 1_000_000):
                suffix = f"~{suffix_number}"
                candidate_base = (stem[: 8 - len(suffix)] + suffix)[:8]
                candidate = (candidate_base.ljust(8) + clean_extension.ljust(3)).encode("ascii")
                if candidate not in used and candidate_base not in RESERVED_SHORT_NAMES:
                    raw = candidate
                    break
            if not raw:
                raise ValueError(f"could not assign an 8.3 alias for {child.name!r}")
        used.add(raw)
        child.short_name = raw
        child.needs_lfn = child.name != _short_display_name(raw)

    for child in directory.sorted_children():
        if child.is_dir:
            _assign_short_names(child)


def _lfn_entry_count(name: str) -> int:
    if not name:
        return 0
    unit_count = len(name.encode("utf-16le")) // 2
    return math.ceil((unit_count + 1) / 13)


def _directory_entry_count(directory: Node, include_volume_label: bool) -> int:
    count = 1 if include_volume_label else 2  # label in root; dot and dot-dot elsewhere
    for child in directory.sorted_children():
        count += 1
        if child.needs_lfn:
            count += _lfn_entry_count(child.name)
    return count


def _walk_preorder(node: Node) -> Iterable[Node]:
    yield node
    for child in node.sorted_children():
        if child.is_dir:
            yield from _walk_preorder(child)
    for child in node.sorted_children():
        if not child.is_dir:
            yield child


def _assign_clusters(root: Node, cluster_size: int, available_clusters: int) -> int:
    for node in _walk_preorder(root):
        if node.is_dir:
            entries = _directory_entry_count(node, include_volume_label=node.parent is None)
            node.cluster_count = max(1, math.ceil(entries * 32 / cluster_size))
        else:
            node.cluster_count = math.ceil(node.size / cluster_size) if node.size else 0

    next_cluster = 2
    for node in _walk_preorder(root):
        if node.cluster_count:
            node.first_cluster = next_cluster
            next_cluster += node.cluster_count
    used_clusters = next_cluster - 2
    if used_clusters > available_clusters:
        raise ValueError(
            f"filesystem contents require {used_clusters} clusters, but image has only {available_clusters}"
        )
    return next_cluster


def _fat_date() -> int:
    return (1 << 5) | 1  # 1980-01-01


def _short_entry(name: bytes, attributes: int, first_cluster: int, size: int = 0) -> bytes:
    entry = bytearray(32)
    entry[:11] = name
    entry[11] = attributes
    date = _fat_date()
    struct.pack_into("<H", entry, 16, date)
    struct.pack_into("<H", entry, 18, date)
    struct.pack_into("<H", entry, 20, (first_cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", entry, 24, date)
    struct.pack_into("<H", entry, 26, first_cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def _lfn_checksum(short_name: bytes) -> int:
    checksum = 0
    for value in short_name:
        checksum = (((checksum & 1) << 7) | (checksum >> 1)) + value
        checksum &= 0xFF
    return checksum


def _pack_utf16_units(units: list[int]) -> bytes:
    return b"".join(struct.pack("<H", unit) for unit in units)


def _lfn_entries(name: str, short_name: bytes) -> list[bytes]:
    encoded = name.encode("utf-16le")
    units = list(struct.unpack(f"<{len(encoded) // 2}H", encoded))
    units.append(0)
    entry_count = math.ceil(len(units) / 13)
    units.extend([0xFFFF] * (entry_count * 13 - len(units)))
    checksum = _lfn_checksum(short_name)
    entries = []
    for sequence in range(entry_count, 0, -1):
        chunk = units[(sequence - 1) * 13 : sequence * 13]
        entry = bytearray(32)
        entry[0] = sequence | (0x40 if sequence == entry_count else 0)
        entry[1:11] = _pack_utf16_units(chunk[:5])
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        entry[14:26] = _pack_utf16_units(chunk[5:11])
        entry[26:28] = b"\0\0"
        entry[28:32] = _pack_utf16_units(chunk[11:13])
        entries.append(bytes(entry))
    return entries


def _directory_data(directory: Node, volume_label: str, cluster_size: int) -> bytes:
    entries: list[bytes] = []
    if directory.parent is None:
        label = volume_label.upper().ljust(11).encode("ascii")
        entries.append(_short_entry(label, 0x08, 0))
    else:
        entries.append(_short_entry(b".          ", 0x10, directory.first_cluster))
        parent_cluster = directory.parent.first_cluster if directory.parent.parent is not None else 0
        entries.append(_short_entry(b"..         ", 0x10, parent_cluster))

    for child in directory.sorted_children():
        if child.needs_lfn:
            entries.extend(_lfn_entries(child.name, child.short_name))
        entries.append(
            _short_entry(
                child.short_name,
                0x10 if child.is_dir else 0x20,
                child.first_cluster,
                0 if child.is_dir else child.size,
            )
        )
    data = b"".join(entries)
    allocated_size = directory.cluster_count * cluster_size
    if len(data) > allocated_size:
        raise AssertionError("directory cluster calculation was too small")
    return data.ljust(allocated_size, b"\0")


def _calculate_layout(total_sectors: int) -> tuple[int, int]:
    # Solve for the smallest FAT that can address its resulting data region.
    # A fixed-point iteration can oscillate by one sector because both sides
    # round, while this monotonic binary search cannot.
    low = 1
    high = math.ceil((total_sectors - RESERVED_SECTORS) * 4 / SECTOR_SIZE) + 1
    while low < high:
        candidate = (low + high) // 2
        data_sectors = total_sectors - RESERVED_SECTORS - FAT_COUNT * candidate
        clusters = data_sectors // SECTORS_PER_CLUSTER
        fat_entry_capacity = candidate * SECTOR_SIZE // 4
        if fat_entry_capacity >= clusters + 2:
            high = candidate
        else:
            low = candidate + 1
    fat_sectors = low
    data_sectors = total_sectors - RESERVED_SECTORS - FAT_COUNT * fat_sectors
    clusters = data_sectors // SECTORS_PER_CLUSTER
    if clusters < FAT32_MIN_CLUSTERS:
        raise ValueError(
            f"image has {clusters} data clusters; FAT32 requires at least {FAT32_MIN_CLUSTERS}"
        )
    return fat_sectors, clusters


def _boot_sector(total_sectors: int, fat_sectors: int, volume_label: str, volume_id: int) -> bytes:
    sector = bytearray(SECTOR_SIZE)
    sector[0:3] = b"\xEB\x58\x90"
    sector[3:11] = b"OSTEST  "
    struct.pack_into("<H", sector, 11, SECTOR_SIZE)
    sector[13] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", sector, 14, RESERVED_SECTORS)
    sector[16] = FAT_COUNT
    struct.pack_into("<H", sector, 17, 0)
    struct.pack_into("<H", sector, 19, 0)
    sector[21] = 0xF8
    struct.pack_into("<H", sector, 22, 0)
    struct.pack_into("<H", sector, 24, 63)
    struct.pack_into("<H", sector, 26, 255)
    struct.pack_into("<I", sector, 28, 0)  # Patched by uefi_disk_image when embedded.
    struct.pack_into("<I", sector, 32, total_sectors)
    struct.pack_into("<I", sector, 36, fat_sectors)
    struct.pack_into("<H", sector, 40, 0)
    struct.pack_into("<H", sector, 42, 0)
    struct.pack_into("<I", sector, 44, 2)
    struct.pack_into("<H", sector, 48, 1)
    struct.pack_into("<H", sector, 50, 6)
    sector[64] = 0x80
    sector[66] = 0x29
    struct.pack_into("<I", sector, 67, volume_id)
    sector[71:82] = volume_label.upper().ljust(11).encode("ascii")
    sector[82:90] = b"FAT32   "
    sector[510:512] = b"\x55\xAA"
    return bytes(sector)


def _fsinfo_sector(free_clusters: int, next_free: int) -> bytes:
    sector = bytearray(SECTOR_SIZE)
    struct.pack_into("<I", sector, 0, 0x41615252)
    struct.pack_into("<I", sector, 484, 0x61417272)
    struct.pack_into("<I", sector, 488, free_clusters)
    struct.pack_into("<I", sector, 492, next_free)
    struct.pack_into("<I", sector, 508, 0xAA550000)
    return bytes(sector)


def _write_at(output: BinaryIO, offset: int, data: bytes) -> None:
    output.seek(offset)
    output.write(data)


def create_fat32_image(
    entries: Iterable[dict[str, str]],
    output_path: pathlib.Path,
    size_mb: int,
    volume_label: str,
    volume_id: int,
) -> None:
    if not 1 <= len(volume_label) <= 11:
        raise ValueError("volume label must contain between 1 and 11 characters")
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in volume_label):
        raise ValueError("volume label must contain printable ASCII only")
    if not 0 <= volume_id <= 0xFFFFFFFF:
        raise ValueError("volume ID must fit in 32 bits")
    total_bytes = size_mb * 1024 * 1024
    if total_bytes % SECTOR_SIZE:
        raise ValueError("image size must be sector aligned")
    total_sectors = total_bytes // SECTOR_SIZE
    fat_sectors, available_clusters = _calculate_layout(total_sectors)

    root = _build_tree(entries)
    _assign_short_names(root)
    cluster_size = SECTOR_SIZE * SECTORS_PER_CLUSTER
    next_free = _assign_clusters(root, cluster_size, available_clusters)

    fat = bytearray(fat_sectors * SECTOR_SIZE)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
    struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
    for node in _walk_preorder(root):
        for cluster_offset in range(node.cluster_count):
            cluster = node.first_cluster + cluster_offset
            value = END_OF_CHAIN if cluster_offset == node.cluster_count - 1 else cluster + 1
            struct.pack_into("<I", fat, cluster * 4, value)

    used_clusters = next_free - 2
    free_clusters = available_clusters - used_clusters
    next_free_hint = next_free if free_clusters else 0xFFFFFFFF
    boot = _boot_sector(total_sectors, fat_sectors, volume_label, volume_id)
    fsinfo = _fsinfo_sector(free_clusters, next_free_hint)
    data_start = (RESERVED_SECTORS + FAT_COUNT * fat_sectors) * SECTOR_SIZE

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        output.truncate(total_bytes)
        _write_at(output, 0, boot)
        _write_at(output, SECTOR_SIZE, fsinfo)
        _write_at(output, 6 * SECTOR_SIZE, boot)
        _write_at(output, 7 * SECTOR_SIZE, fsinfo)
        for fat_index in range(FAT_COUNT):
            fat_offset = (RESERVED_SECTORS + fat_index * fat_sectors) * SECTOR_SIZE
            _write_at(output, fat_offset, fat)

        for node in _walk_preorder(root):
            if not node.cluster_count:
                continue
            offset = data_start + (node.first_cluster - 2) * cluster_size
            if node.is_dir:
                _write_at(output, offset, _directory_data(node, volume_label, cluster_size))
            else:
                output.seek(offset)
                assert node.source is not None
                with node.source.open("rb") as source:
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
    parser.add_argument("--volume-label", required=True)
    parser.add_argument("--volume-id", required=True, type=int)
    parser.add_argument("--compression", choices=("gzip", "none"), default="gzip")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.compression == "none":
        create_fat32_image(
            manifest["files"],
            args.output,
            args.size_mb,
            args.volume_label,
            args.volume_id,
        )
    else:
        temporary = args.output.with_name(args.output.name + ".uncompressed.tmp")
        try:
            create_fat32_image(
                manifest["files"],
                temporary,
                args.size_mb,
                args.volume_label,
                args.volume_id,
            )
            _gzip_image(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
