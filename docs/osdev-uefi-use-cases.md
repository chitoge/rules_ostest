# UEFI testing scenarios

This guide covers x86-64 and AArch64 UEFI applications, loaders, and operating
systems running under QEMU.

Supported scenarios include:

| Scenario | Configuration |
|---|---|
| Removable-media fallback boot | Install `BOOTX64.EFI` or `BOOTAA64.EFI` with `uefi_esp_image` or `platform_uefi_esp_image`. |
| FAT or USB storage | Attach a FAT32 image with `qemu_media(interface = "usb-storage")`. |
| UEFI hard disk | Create a one-ESP GPT disk with `uefi_disk_image`, or compose partitions with `gpt_image`. |
| Primary MBR | Compose up to four partitions with `mbr_partition` and `mbr_image`. |
| UEFI CD/DVD | Create a UEFI El Torito ISO with `uefi_iso_image` and attach it as `cdrom`. |
| Multiple device paths | Attach `virtio-blk`, `usb-storage`, `nvme`, and `cdrom` media with unique boot indices. |
| Firmware or embedded shell | Run a test with no `disk` or `media` attributes. |
| UEFI protocols | Place fixtures and EFI applications on declared media and report protocol assertions from the guest. |
| GOP and graphical output | Check framebuffer contents in the guest or capture the display through QMP. |
| Runtime Services and reset | Report over serial or accept a deliberate QEMU exit with `success_exit_codes`. |
| Persistent variables | Reuse a writable variable store across several `QemuSession` instances. |
| Secure Boot | Supply Secure-Boot firmware, an enrolled variable store, and signed EFI binaries. |
| Source debugging | Enable the loopback GDB endpoint and optionally start the VM paused. |
| Local networking | Run several VMs and isolated Ethernet segments in one `uefi_lab_test`. |

The UEFI application or OS performs protocol-level assertions. The test rule
provides firmware, media, virtual devices, lifecycle control, logging, and
host-side observation.

## Boot media

Create one ESP and package it as a GPT disk and a UEFI CD:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "qemu_media",
    "uefi_disk_image",
    "uefi_esp_image",
    "uefi_iso_image",
    "uefi_test",
)

uefi_esp_image(
    name = "esp",
    arch = "x86_64",
    efi_binary = ":boot.efi",
    files = {
        ":fixture": "fixtures/input.bin",
    },
)

uefi_disk_image(
    name = "gpt_disk",
    esp = ":esp",
)

uefi_iso_image(
    name = "boot_cd",
    esp = ":esp",
)
```

The same ESP can be tested through several device types:

```starlark
uefi_test(
    name = "boot_as_usb",
    arch = "x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    media = [
        qemu_media(
            image = ":esp",
            interface = "usb-storage",
            bootindex = 1,
        ),
    ],
)

uefi_test(
    name = "boot_as_cd",
    arch = "x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    media = [
        qemu_media(
            image = ":boot_cd",
            interface = "cdrom",
            bootindex = 1,
        ),
    ],
)
```

Attach several media to exercise UEFI device paths and boot selection:

```starlark
uefi_test(
    name = "device_path_test",
    arch = "x86_64",
    # qemu, firmware, and firmware_vars ...
    media = [
        qemu_media(image = ":gpt_disk", interface = "virtio-blk", bootindex = 1),
        qemu_media(image = ":nvme_data", interface = "nvme", bootindex = 2),
        qemu_media(image = ":boot_cd", interface = "cdrom", bootindex = 3),
    ],
)
```

The ISO contains an El Torito entry with UEFI platform identifier `0xEF` and
an ISO9660-visible `EFI.IMG` boot image. It contains no legacy BIOS boot entry.

### Primary MBR media

Use a primary MBR when a firmware test requires that partitioning scheme:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "mbr_image",
    "mbr_partition",
)

mbr_partition(
    name = "mbr_esp_partition",
    image = ":esp",
    type_id = 0xEF,
    patch_fat_hidden_sectors = True,
)

mbr_image(
    name = "mbr_disk",
    partitions = [":mbr_esp_partition"],
    size_mb = 128,
)
```

`patch_fat_hidden_sectors = True` updates the primary and backup FAT boot
sectors with the partition's assigned start LBA.

## UEFI protocols and fixtures

Files added to a FAT image are available to guest tests through the UEFI Simple
File System protocol. Additional EFI applications can be placed at stable
paths for `LoadImage` and `StartImage` tests. Multiple attached disks provide
Block I/O and device-path fixtures.

Examples of guest-reported assertions include:

- opened and read a known file;
- enumerated the expected block devices;
- loaded and started a child EFI application;
- located a protocol and validated its revision;
- exited boot services with a current memory-map key;
- called Runtime Services after virtual address conversion;
- reset or shut down through `ResetSystem`.

Use regular expressions in `uefi_test` for a serial result protocol. Use
`uefi_py_test` for host-side QMP commands or a custom protocol.

## Persistent variables and reboot tests

The variable-store template is copied below `TEST_TMPDIR` at test startup. The
copy remains available to every session created from the same
`UefiQemuConfig`:

```python
import argparse
import os
import pathlib

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments

parser = argparse.ArgumentParser()
add_uefi_qemu_arguments(parser)
config = UefiQemuConfig.from_namespace(parser.parse_args())

with QemuSession(config) as first_boot:
    first_boot.wait_for_serial(r"VARIABLE_WRITTEN", timeout=30)
    first_boot.wait_for_exit(timeout=30)

with QemuSession(config) as second_boot:
    second_boot.wait_for_serial(r"VARIABLE_PERSISTED", timeout=30)
    second_boot.wait_for_exit(timeout=30)

outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
config.export_firmware_vars(outputs / "final-vars.fd")
```

Complete a graceful guest shutdown before inspecting exact variable-store
bytes. The serial `uefi_test` interface can set
`export_firmware_vars = True`; the exported store is written to undeclared test
outputs.

## Secure Boot

A Secure Boot test declares these inputs:

- a Secure-Boot-capable firmware code image;
- a variable-store template containing the required PK, KEK, db, and dbx
  state;
- signed EFI applications and drivers;
- any machine properties required by the firmware build.

An x86 OVMF configuration that uses SMM can be started as follows:

```starlark
load("@rules_ostest//ostest:defs.bzl", "uefi_test")

uefi_test(
    name = "secure_boot_test",
    arch = "x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf_secure//:OVMF_CODE.secboot.fd",
    firmware_vars = ":enrolled_vars.fd",
    disk = ":disk_containing_signed_boot_efi",
    machine_options = ["smm=on"],
    qemu_args = [
        "-global",
        "driver=cfi.pflash01,property=secure,value=on",
    ],
)
```

Signing keys, signed binaries, and enrolled variable stores should be produced
by declared Bazel targets or checked-in public test fixtures. Use the machine
properties documented by the selected firmware build.

## Graphical tests and source debugging

Add a display device through `qemu_args`, then use guest-side framebuffer
checks or QMP screenshots:

```starlark
load("@rules_ostest//ostest:defs.bzl", "uefi_py_test")

uefi_py_test(
    name = "source_debug_test",
    srcs = ["source_debug_test.py"],
    main = "source_debug_test.py",
    guest_platform = "//platforms:guest_x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    disk = ":debug_disk",
    gdb = True,
    pause_at_start = True,
    debugcon = True,
    qemu_args = ["-device", "VGA"],
)
```

The Python test reads `session.gdb_address` and may start a declared GDB
executable. `session.screendump(...)` captures the display. `debugcon` captures
x86 OVMF output from port `0x402` and is available for x86-64 guests.

QMP, GDB, and debugcon use standard QEMU interfaces. Loopback listeners use
ephemeral ports and are passed to QEMU as inherited file descriptors.

See [Debugging UEFI tests](debugging-uefi-tests.md) for artifact triage,
automated GDB sessions, symbol relocation, and failure checklists.

## Local Ethernet labs

`uefi_lab_test` declares every VM, QEMU executable, firmware image, variable
store, boot medium, test dependency, and network attachment in one Bazel test
target.

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "qemu_network",
    "uefi_lab_test",
    "uefi_vm",
)

uefi_lab_test(
    name = "dhcp_boot_lab",
    srcs = ["dhcp_boot_lab.py"],
    main = "dhcp_boot_lab.py",
    vms = [
        uefi_vm(
            name = "server",
            arch = "x86_64",
            qemu = "@qemu_x86_64//:qemu-system-x86_64",
            firmware = "@ovmf//:OVMF_CODE.fd",
            disk = ":server_disk",
            networks = [qemu_network("boot_lan")],
        ),
        uefi_vm(
            name = "client",
            guest_platform = "//platforms:guest_aarch64",
            qemu = "@qemu_aarch64//:qemu-system-aarch64",
            firmware = "@aavmf//:AAVMF_CODE.fd",
            firmware_vars = "@aavmf//:AAVMF_VARS.fd",
            networks = [qemu_network("boot_lan")],
        ),
    ],
)
```

The Python test starts the lab and addresses sessions by participant name:

```python
import argparse

from ostest.python.lab import QemuLab, UefiLabConfig, add_uefi_lab_arguments

parser = argparse.ArgumentParser()
add_uefi_lab_arguments(parser)
config = UefiLabConfig.from_namespace(parser.parse_args())

with QemuLab(config) as lab:
    lab["server"].wait_for_serial(r"DHCP_TFTP_READY", timeout=30)
    lab["client"].wait_for_serial(r"NETBOOT: PASS", timeout=60)
```

Each named network is an in-process Ethernet broadcast segment. QEMU receives
a connected socket descriptor for every NIC. The test process can join a
segment with `lab.host_endpoint("boot_lan")` to send and receive raw Ethernet
frames. DHCP, TFTP, and other services can run in a VM or in the Python test.

The lab uses no TAP interface, host bridge, fixed port, external connection, or
special privilege. It requires no `requires-network`, `local`, or `no-remote`
tag. Every segment writes `network-<name>.pcap` to undeclared test outputs.

Multiple networks and multiple NICs per VM are supported. MAC addresses are
generated deterministically when `qemu_network` does not specify one.

## Supported scope

The generated media and QEMU harness cover x86-64 and AArch64 UEFI application,
loader, kernel, storage, graphics, variable, Secure Boot, debugger, and local
network tests.

The image rules generate FAT32 filesystems and UEFI-only optical media.
Prebuilt declared images can supply FAT12 or FAT16 fixtures. Legacy BIOS, CSM,
hybrid BIOS/UEFI optical boot, and external network access are outside this
scope.

Physical firmware and device qualification should run the same guest binaries
and generated media in a hardware test environment.

## UEFI references

- [OSDev UEFI overview](https://wiki.osdev.org/EFI)
- [OSDev UEFI application media](https://wiki.osdev.org/UEFI_App_Bare_Bones)
- [OSDev EFI System Partition](https://wiki.osdev.org/EFI_System_Partition)
- [OSDev GPT](https://wiki.osdev.org/GPT)
- [OSDev UEFI NVRAM](https://wiki.osdev.org/UEFI_NVRAM)
- [OSDev UEFI debugging with GDB](https://wiki.osdev.org/Debugging_UEFI_applications_with_GDB)
- [UEFI media access](https://uefi.org/specs/UEFI/2.11/13_Protocols_Media_Access.html)
