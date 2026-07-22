# Testing, guest platforms, and image composition

`rules_ostest` provides three test entry points:

- `uefi_test` reads pass and failure patterns from the first serial port.
- `uefi_py_test` runs a Python test with serial and QMP access to one VM.
- `uefi_lab_test` runs a Python test that owns several networked VMs.

All three declare QEMU, firmware, media, variable stores, and test programs as
Bazel inputs.

## Serial tests

Use `uefi_test` when the guest can report its own verdict:

```starlark
load("@rules_ostest//ostest:defs.bzl", "uefi_test")

uefi_test(
    name = "allocator_test",
    arch = "x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    disk = ":allocator_disk",
    success_pattern = r"ALLOCATOR: PASS allocations=[1-9][0-9]*",
    failure_pattern = r"(OSTEST: FAIL|PANIC|ASSERTION FAILED)",
    timeout_seconds = 30,
)
```

The runner checks the failure pattern before the success pattern. It also
fails on a timeout or an unexpected QEMU exit. Use `success_exit_codes` for a
guest that exits QEMU by design without printing a success marker.

Use `success_markers` for ordered literal assertions and `forbidden_markers`
for fail-fast global literals. Use `phases` when a completed marker sequence
must be followed by a QMP-observed guest reboot before the next sequence.
These assertion modes keep one overall deadline and one QEMU process.

The serial stream and exact QEMU command are stored as Bazel undeclared test
outputs.

Graphical behavior can be checked inside the guest by reading the GOP or OS
framebuffer and reporting a checksum over serial:

```starlark
uefi_test(
    name = "gop_render_test",
    arch = "x86_64",
    # qemu, firmware, and media ...
    graphics = True,
    success_pattern = r"GOP: PASS sha256=[0-9a-f]{64}",
    screendump_not_blank = True,
)
```

For AArch64, select a display device supported by the UEFI firmware build. A
firmware image containing the virtio GPU driver can use `virtio-gpu-pci`.

## Python and QMP tests

Use `uefi_py_test` when the test process needs to observe or control the VM.
The macro adds `//ostest/python:qemu_testlib`, resolves all runfiles, and
passes the resolved configuration through `--ostest-*` arguments.

```starlark
load("@rules_ostest//ostest:defs.bzl", "uefi_py_test")

uefi_py_test(
    name = "gui_golden_test",
    srcs = ["gui_golden_test.py"],
    main = "gui_golden_test.py",
    guest_platform = "//platforms:guest_x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    disk = ":gui_disk",
    qemu_args = ["-device", "VGA"],
)
```

The Python test constructs a `UefiQemuConfig` and owns the QEMU lifecycle:

```python
import argparse
import hashlib
import os
import pathlib

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments

EXPECTED_SHA256 = "..."

parser = argparse.ArgumentParser()
add_uefi_qemu_arguments(parser)
config = UefiQemuConfig.from_namespace(parser.parse_args())

outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
with QemuSession(config) as vm:
    vm.wait_for_serial(r"FRAME_READY", timeout=30)
    screenshot = vm.screendump(outputs / "screen.ppm")
    digest = hashlib.sha256(screenshot.read_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256
```

Useful `QemuSession` operations include:

- `wait_for_serial(pattern, timeout=...)`
- `execute(command, arguments)` for any QMP command
- `screendump(path)`
- `wait_for_exit(timeout=..., acceptable_codes=...)`
- `export_firmware_vars(path)`
- `host_forwards` and `host_forward(name)` after managed usernet starts
- `media_path(name)` and `export_media(name, path)` after QEMU stops
- `gdb_address` when GDB is enabled

QMP and GDB listeners use ephemeral loopback sockets passed to QEMU as file
descriptors. The session writes a serial log and a shell-quoted command line to
undeclared test outputs. Descriptor passing requires a POSIX test worker.

See [Debugging UEFI tests](debugging-uefi-tests.md) for failure artifacts, QMP
state capture, debugcon, GDB, relocated symbols, and lab PCAPs.

## Guest platforms

An emulated test involves three Bazel platform roles and one guest role:

| Role | Purpose | Example |
|---|---|---|
| Guest platform | Architecture of the EFI application or OS | AArch64 UEFI |
| Bazel target platform | Configuration used to build a target | AArch64 `kernel.efi` |
| Bazel execution platform | Worker that runs an action or test | Linux x86-64 worker |
| Bazel host platform | Machine running the Bazel client | macOS AArch64 workstation |

Define guest platforms with standard CPU constraints:

```starlark
platform(
    name = "guest_x86_64",
    constraint_values = ["@platforms//cpu:x86_64"],
)

platform(
    name = "guest_aarch64",
    constraint_values = ["@platforms//cpu:aarch64"],
)
```

Build the EFI binary and ESP under the guest-platform transition:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "platform_uefi_esp_image",
    "uefi_disk_image",
    "uefi_platform_test",
)

platform_uefi_esp_image(
    name = "kernel_esp_aarch64",
    guest_platform = "//platforms:guest_aarch64",
    efi_binary = ":kernel.efi",
)

uefi_disk_image(
    name = "kernel_disk_aarch64",
    esp = ":kernel_esp_aarch64",
)

uefi_platform_test(
    name = "boot_aarch64",
    guest_platform = "//platforms:guest_aarch64",
    qemu = "@qemu_aarch64//:qemu-system-aarch64",
    firmware = "@aavmf//:AAVMF_CODE.fd",
    firmware_vars = "@aavmf//:AAVMF_VARS.fd",
    disk = ":kernel_disk_aarch64",
)
```

The guest transition applies to guest artifacts and architecture metadata. The
Python test remains configured for its execution worker. QEMU and firmware are
explicit labels and must be executable or readable on that worker.

Create one test target per guest architecture and collect them in a suite:

```starlark
test_suite(
    name = "cross_arch_tests",
    tests = [":boot_x86_64", ":boot_aarch64"],
)
```

Direct `arch = "x86_64"` or `arch = "aarch64"` attributes are available when
guest artifacts are already configured by their producing rules.

## Accelerator settings

Guest selection and accelerator selection are independent. Tests use ordered
KVM and TCG accelerators by default. Set `require_kvm = True` to pass only the
KVM accelerator.

A remote KVM test should include the execution properties required by its
remote executor. A test with fallback enabled should have a timeout that
allows completion under TCG.

Set `kvm_unavailable = "skip"` only with `require_kvm = True` for a portable
run-or-skip gate. The runner records a skipped JUnit testcase and returns zero
because Bazel has no portable skipped-test status; the target receives the
`external` tag to prevent caching that environment-dependent result.

## Direct-kernel boot

All single-VM entry points accept `boot = "direct-kernel"`, a required
`kernel`, an optional `initrd`, and literal `kernel_args`. Firmware is optional
in this mode. AArch64 direct boot defaults to `virt,gic-version=2` and
`cortex-a53`, matching a conservative real-QEMU boot profile; callers may set
`machine_options` and `cpu_model`. QEMU and all payloads remain declared
runfiles for the execution worker.

## Composing disks

`gpt_partition` attaches partition metadata to a one-file image target. It
records the type GUID, unique GUID, name, attributes, alignment, exact start
LBA, compression, and optional FAT hidden-sector patch.

`gpt_image` places an ordered list of these partitions on a disk and writes the
protective MBR, both GPT headers, both partition arrays, and their CRCs.

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "gpt_image",
    "gpt_partition",
    "uefi_esp_image",
)

uefi_esp_image(
    name = "esp",
    arch = "x86_64",
    efi_binary = ":boot.efi",
)

gpt_partition(
    name = "esp_partition",
    image = ":esp",
    type_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    partition_name = "EFI System",
    patch_fat_hidden_sectors = True,
)

gpt_partition(
    name = "root_partition",
    image = ":root_ext4",
    type_guid = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
    partition_name = "Root",
)

gpt_image(
    name = "boot_disk",
    partitions = [":esp_partition", ":root_partition"],
    size_mb = 1024,
)
```

Partition images must have a logical length divisible by 512 bytes.
`image_compression = "auto"` recognizes gzip by its magic bytes.

For primary MBR layouts, use `mbr_partition` and `mbr_image`. Set
`patch_fat_hidden_sectors = True` on a FAT partition so both FAT boot sectors
contain the assigned start LBA.

`raw_image` places blobs at exact offsets and is suitable for flash layouts.
`uefi_disk_image` creates the common one-ESP GPT layout in one rule.

For guest-created state, `qemu_scratch_disk` makes a sparse raw writable image
below `TEST_TMPDIR`. A serial `uefi_test` can set `export = True` to retain its
post-stop contents, or use writable non-snapshot
`qemu_media(..., export=True)` to seed a disk without ever mutating the source
runfile. Scripted and lab tests should leave `export` false and call
`QemuSession.export_media()` after QEMU stops.

## Attaching boot media

Image contents and QEMU device types are configured separately. `qemu_media`
supports `virtio-blk`, `usb-storage`, `nvme`, and `cdrom` interfaces:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "qemu_media",
    "uefi_platform_test",
)

uefi_platform_test(
    name = "media_test",
    guest_platform = "//platforms:guest_x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    media = [
        qemu_media(image = ":esp", interface = "usb-storage", bootindex = 1),
        qemu_media(image = ":boot_iso", interface = "cdrom", bootindex = 2),
        qemu_media(image = ":data_disk", interface = "nvme", bootindex = 3),
    ],
)
```

Non-`None` boot indices must be positive and unique. `readonly` and `snapshot`
control write behavior. Writable non-snapshot inputs are copied below
`TEST_TMPDIR`. The `disk` attribute creates one raw `virtio-blk` medium with
`bootindex = 1` and `snapshot = True`.

## Managed host-to-guest probes

`qemu_hostfwd` adds explicit, loopback-only QEMU user networking to a serial
or Python test. Automatic ports are selected by QEMU with host port zero and
discovered through QMP `info usernet`, so parallel tests do not race over a
reserve/close/rebind window. A serial test can declare a one-shot
`host_companion`; a `uefi_py_test` reads the resolved endpoints from
`QemuSession.host_forwards`.

The companion environment uses `OSTEST_HOSTFWD_JSON` as the canonical mapping.
User networking has `restrict=on`, and the feature is host-to-guest only. A
server that must be reached from the guest requires a separate guest-forward
or pre-opened-listener design.

A FAT image may contain fallback applications for both supported guests:

```starlark
load("@rules_ostest//ostest:defs.bzl", "fat_image")

fat_image(
    name = "multiarch_esp",
    files = {
        ":kernel_x64.efi": "EFI/BOOT/BOOTX64.EFI",
        ":kernel_aarch64.efi": "EFI/BOOT/BOOTAA64.EFI",
    },
)
```

## Image representation and reproducibility

Filesystem and disk rules produce a deterministic gzip artifact by default.
The logical device bytes are expanded below `TEST_TMPDIR` before QEMU starts.
This keeps zero-filled filesystem and disk capacity compact in Bazel's content
addressed storage.

Generated image properties include:

- zeroed unwritten regions;
- deterministic FAT timestamps, aliases, and metadata;
- deterministic MBR entries, GPT arrays, and CRCs;
- deterministic ISO9660 records and El Torito catalogs;
- gzip with an empty stored filename, timestamp zero, and fixed level;
- explicit QEMU image formats with no format probing.

Default GPT disk and partition GUIDs are UUIDv5 values derived from the target
label. Set `disk_guid`, `unique_guid`, or `partition_guid` when the bytes must
remain unchanged after a target is renamed.

Set `compression = "none"` for a direct `.fat`, `.img`, or `.iso` output. A
prebuilt qcow2 target can be attached with `qemu_media(image_format = "qcow2",
compression = "none")`.

## Related documentation

- [Debugging UEFI tests](debugging-uefi-tests.md)
- [Bazel platforms](https://bazel.build/extending/platforms)
- [Starlark transitions](https://bazel.build/extending/config)
- [Bazel remote caching](https://bazel.build/remote/caching)
- [QEMU invocation](https://qemu.readthedocs.io/en/master/system/invocation.html)
- [QMP reference](https://qemu.readthedocs.io/en/master/interop/qemu-qmp-ref.html)
- [QEMU disk images](https://qemu.readthedocs.io/en/master/system/images.html)
