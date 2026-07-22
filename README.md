# rules_ostest

`rules_ostest` builds deterministic UEFI media and runs OS tests with QEMU and
Bazel. It supports x86-64 and AArch64 guests, UEFI and direct-kernel boot,
ordered and reboot-spanning serial verdicts, Python/QMP test scripts,
graphical checks, writable-disk readback, managed host forwards, persistent
UEFI variables, and isolated multi-VM networks.

The rules are suitable for sandboxed and remote execution:

- QEMU, firmware, media, and variable-store templates are declared Bazel
  inputs.
- Image actions use the Python toolchain registered through `rules_python`.
- FAT32, GPT, primary MBR, fixed-layout, and UEFI El Torito ISO images are
  reproducible.
- Image outputs use deterministic gzip compression by default for compact
  remote-cache storage and transfer.
- Writable state is created below `TEST_TMPDIR`.
- Network labs use isolated, process-local Ethernet segments.

## Installation

For a version published in the Bazel Central Registry, add the module to
`MODULE.bazel`:

```starlark
bazel_dep(name = "rules_ostest", version = "0.1.0")
```

To test an unpublished checkout, use the same dependency with a local override:

```starlark
bazel_dep(name = "rules_ostest", version = "0.1.0")
local_path_override(
    module_name = "rules_ostest",
    path = "../rules_ostest",
)
```

## Requirements

Use Bazel 7.x, 8.x, or 9.x. The module registers a hermetic Python 3.12
toolchain through `rules_python`.

Test targets require declared Bazel targets for:

- a QEMU system emulator built for the execution platform;
- UEFI firmware code for a UEFI boot (optional for direct-kernel boot);
- an optional UEFI variable-store template.

A static QEMU executable is operationally easy to declare as a Bazel input. A
dynamically linked executable must include its loader, shared libraries, and
QEMU data files in runfiles. Firmware and QEMU should be pinned so every worker
receives the same inputs. Redistributing either one also requires compliance
with its own license; see [Third-party software and licensing](THIRD_PARTY.md).

## Quick start

Create an EFI System Partition, wrap it in GPT, and boot it:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "uefi_disk_image",
    "uefi_esp_image",
    "uefi_test",
)

uefi_esp_image(
    name = "kernel_esp",
    arch = "x86_64",
    efi_binary = ":kernel.efi",
    files = {
        ":boot.cfg": "EFI/OSTEST/boot.cfg",
    },
)

uefi_disk_image(
    name = "kernel_disk",
    esp = ":kernel_esp",
    size_mb = 128,
)

uefi_test(
    name = "kernel_test",
    arch = "x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    disk = ":kernel_disk",
    timeout_seconds = 30,
)
```

The default guest protocol is simple:

- `OSTEST: PASS` on the first serial port passes the test.
- `OSTEST: FAIL`, an unexpected QEMU exit, or a timeout fails the test.

`success_pattern` and `failure_pattern` accept Python regular expressions. The
serial log and QEMU command line are saved as Bazel undeclared test outputs.

For multi-step protocols, use literal ordered and forbidden markers:

```starlark
uefi_test(
    # qemu, firmware, disk, and architecture ...
    success_markers = [
        "NETSTACK: INTERFACE UP",
        "FILESERVER: LISTENING",
        "FILESERVER: RPC OK",
    ],
    forbidden_markers = ["PANIC", "KERNEL FAULT", "OOM"],
)
```

Markers may span serial reads. Duplicate markers require distinct
occurrences, and a forbidden marker fails immediately. `success_markers`,
`phases`, and an explicit `success_pattern` are mutually exclusive.

One QEMU process can cover a guest reboot:

```starlark
uefi_test(
    # ...
    phases = [
        {"markers": ["OTA-SLOT=A", "OTA-APPLIED"], "then": "reboot"},
        {"markers": ["OTA-SLOT=B", "OTA-COMMITTED"]},
    ],
)
```

The runner advances only on a QMP guest `RESET` event after the current phase
markers. The timeout covers the complete multi-boot session.

## Acceleration

Tests try KVM first and continue with TCG when KVM is unavailable. This is the
default:

```text
-accel kvm -accel tcg
```

Require KVM for a target with:

```starlark
uefi_test(
    # ...
    require_kvm = True,
    exec_properties = {"requires-kvm": "1"},
)
```

To make the KVM-only target a portable run-or-skip gate, add
`kvm_unavailable = "skip"`. Bazel has no portable skipped-test exit status, so
the runner writes a skipped JUnit testcase, prints `rules_ostest: SKIP`, and
returns success. The macro adds the standard `external` tag to disable result
caching; Bazel's top-level summary may still say `PASSED`. Keep
`exec_properties` when the scheduler must guarantee KVM, or omit it when a
KVM-less worker should execute the skip path.

The execution property is executor-specific. Set test timeouts so the test can
complete under TCG when fallback is enabled.

## Guest architectures

Architecture names and defaults are:

| Architecture | QEMU machine | CPU | UEFI fallback path |
|---|---|---|---|
| `x86_64` or `x64` | `q35` | QEMU default | `EFI/BOOT/BOOTX64.EFI` |
| `aarch64` or `arm64` | `virt` | `max` | `EFI/BOOT/BOOTAA64.EFI` |

Targets may set `arch` directly or use a standard Bazel guest platform with an
`@platforms//cpu:x86_64` or `@platforms//cpu:aarch64` constraint.

Platform-aware entry points are:

- `platform_uefi_esp_image`
- `uefi_platform_test`
- `uefi_py_test(guest_platform = ...)`
- `uefi_vm(guest_platform = ...)`

QEMU and firmware remain explicit labels because a CPU constraint does not
identify their repositories.

For a large image or a firmware-independent harness, select direct boot:

```starlark
uefi_test(
    name = "direct_aarch64",
    arch = "aarch64",
    qemu = "@qemu_aarch64//:qemu-system-aarch64",
    firmware = None,
    boot = "direct-kernel",
    kernel = ":boot_shim",
    initrd = ":system_image",
    kernel_args = "console=ttyAMA0",
)
```

AArch64 direct boot defaults to `virt,gic-version=2` and `cortex-a53`;
`machine_options` and `cpu_model` can override them. Existing AArch64 UEFI
tests retain `virt` and `max`. These are real QEMU command paths—the repository
tests them with a QEMU-shaped fixture, while consumers supply and pin the real
`qemu-system-aarch64` and any firmware labels.

## Filesystems and images

### FAT32 and fixed layouts

`fat_image` creates a FAT32 filesystem from one-file targets:

```starlark
load("@rules_ostest//ostest:defs.bzl", "fat_image", "raw_image")

fat_image(
    name = "data_fs",
    files = {
        ":kernel": "boot/kernel.bin",
        ":configuration": "config/Long File Name.cfg",
    },
    size_mb = 64,
    volume_label = "OSTEST",
)

raw_image(
    name = "flash",
    blobs = {
        ":stage1": "0",
        ":stage2": "1MiB",
        ":metadata": "4096s",
    },
    size_mb = 16,
)
```

FAT32 images include deterministic timestamps, VFAT long filenames, stable
8.3 aliases, two FAT copies, FSInfo, and a backup boot sector. `raw_image`
accepts byte, 512-byte sector, `KiB`, `MiB`, and `GiB` offsets.

### Partitioned disks and optical media

- `uefi_disk_image` creates a GPT disk containing one EFI System Partition.
- `gpt_partition` and `gpt_image` compose up to 128 partition images.
- `mbr_partition` and `mbr_image` compose up to four primary partitions.
- `uefi_iso_image` creates an ISO9660 image with a UEFI El Torito boot image.

Partition rules accept sector-aligned images produced by other Bazel targets.
GPT GUIDs can be supplied explicitly or derived deterministically from the
target label. FAT hidden-sector fields can be patched to the assigned start
LBA.

### Attaching media

`qemu_media` controls how each image appears to the guest:

```starlark
load(
    "@rules_ostest//ostest:defs.bzl",
    "qemu_media",
    "uefi_test",
)

uefi_test(
    name = "media_test",
    arch = "x86_64",
    # qemu, firmware, and firmware_vars ...
    media = [
        qemu_media(image = ":boot_iso", interface = "cdrom", bootindex = 1),
        qemu_media(image = ":esp", interface = "usb-storage", bootindex = 2),
        qemu_media(image = ":data_disk", interface = "nvme", bootindex = 3),
    ],
)
```

Supported interfaces are `virtio-blk`, `usb-storage`, `nvme`, and `cdrom`.
Non-`None` boot indices must be unique. The `disk` attribute is shorthand for
one raw, snapshot-backed `virtio-blk` device. A test may omit all media for a
firmware or embedded-shell boot.

Generated images use an explicit raw guest format. Their Bazel outputs are
`.fat.gz`, `.img.gz`, or `.iso.gz` by default. The test harness expands them
below `TEST_TMPDIR` immediately before QEMU starts. A declared qcow2 input can
be attached with `image_format = "qcow2"`.

Use a fresh writable disk when the test needs to inspect durable guest state:

```starlark
load("@rules_ostest//ostest:defs.bzl", "qemu_scratch_disk")

uefi_test(
    # ...
    media = [
        qemu_scratch_disk(name = "state", size_mb = 64, export = True),
    ],
)
```

The sparse raw image is created below `TEST_TMPDIR`; an exported copy named
`ostest-media-<index>-<name>.img` is published after QEMU stops, on both pass
and failure. `qemu_media(..., readonly=False, snapshot=False, export=True)`
does the same for a seeded input without mutating its runfile. In a
`uefi_py_test`, use `session.media_path(name)` after QEMU stops and
`session.export_media(name, path)`. The guest must flush its own caches before
its terminal marker.

## Scripted and graphical tests

Use `uefi_py_test` for QMP commands, screenshots, keyboard or mouse input,
source debugging, custom protocols, and multi-boot variable tests. It supplies
the public `QemuSession` library and the complete set of declared QEMU inputs.

```python
import argparse
import os
import pathlib

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments

parser = argparse.ArgumentParser()
add_uefi_qemu_arguments(parser)
config = UefiQemuConfig.from_namespace(parser.parse_args())

outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
with QemuSession(config) as vm:
    vm.wait_for_serial(r"FRAME_READY", timeout=30)
    vm.screendump(outputs / "screen.ppm")
```

`QemuSession.execute()` accepts arbitrary QMP commands. Optional target
features include:

- `gdb = True` for an ephemeral loopback GDB endpoint;
- `pause_at_start = True` to start the VM paused;
- `debugcon = True` for x86 OVMF debug output on port `0x402`;
- a writable variable store that can be reused across sessions and exported.

For a simple serial framebuffer gate, the lower-level QMP script is optional:

```starlark
uefi_test(
    # ...
    graphics = True,
    success_markers = ["FRAMEBUFFER READY"],
    screendump_not_blank = True,
    screendump_min_distinct_pixels = 2,
)
```

The guest gets `VGA` on x86-64 or `virtio-gpu-pci` on AArch64 while QEMU stays
host-headless. `screendump.ppm` is retained, and malformed, blank, or
insufficiently varied images fail the test.

Descriptor-based QMP, GDB, and network connections require a POSIX execution
worker.

## Managed host-to-guest forwards

`qemu_hostfwd` lets a declared one-shot host probe connect to a guest service
without hard-coded ports:

```starlark
load("@rules_ostest//ostest:defs.bzl", "qemu_hostfwd", "uefi_test")

uefi_test(
    # ...
    hostfwd = [qemu_hostfwd(name = "grpc", guest = 50051)],
    host_companion = "//tools:grpc_probe",
    success_markers = ["FILESERVER READY"],
)
```

QEMU binds port zero on `127.0.0.1` and retains the selected socket; the
runner discovers the concrete port through QMP, avoiding a reserve/close/rebind
race. User networking is `restrict=on`. The companion receives
`OSTEST_HOSTFWD_JSON`, plus `OSTEST_HOST`, `OSTEST_PORT`,
`OSTEST_GUEST_PORT`, and `OSTEST_PROTOCOL` for one mapping. PASS requires both
the guest verdict and companion exit zero. Diagnostics include
`hostfwd.json`, `host-companion.log`, and a companion artifact directory.

This is a host-to-guest client/probe facility. It does not let a guest reach a
host package server; that opposite direction needs a separate guest-forward
or pre-opened-listener design.

## Failure diagnostics

`qemu.log` and `qemu-command.txt` are stable undeclared test artifacts on every
serial-test outcome. The runner fails and stops a guest that produces more
than 256 MiB of serial output, preventing an unbounded diagnostic artifact. A
declared diagnostic tool can process the closed serial log before failure is
reported:

```starlark
uefi_test(
    # ...
    on_failure = "//tools:symbolize_serial",
    on_failure_timeout_seconds = 30,
)
```

The tool receives the absolute log path as its only argument and through
`OSTEST_SERIAL_LOG`; it runs without a shell. A hook timeout or nonzero status
is reported as a warning and never replaces the original failure.

See [Debugging UEFI tests](docs/debugging-uefi-tests.md) for artifact triage,
QMP state capture, relocated symbols, automated GDB sessions, and multi-VM
failure diagnosis.

## Local network labs

`uefi_lab_test` owns several `uefi_vm` participants in one Bazel test. Named
`qemu_network` attachments connect them through isolated Ethernet segments.
The Python test can address every `QemuSession` and join a segment as a raw
Ethernet endpoint.

This supports client/server tests, DHCP/TFTP services, network boot, multiple
NICs, multiple segments, and x86-64/AArch64 guest combinations. Each segment
produces a PCAP in undeclared test outputs.

See [UEFI testing scenarios](docs/osdev-uefi-use-cases.md#local-ethernet-labs)
for a complete BUILD and Python example.

## Guides

- [Debugging UEFI tests](docs/debugging-uefi-tests.md)
- [Testing, guest platforms, and image composition](docs/testing-platforms-and-composition.md)
- [UEFI testing scenarios](docs/osdev-uefi-use-cases.md)

## License and project policies

`rules_ostest` is licensed under Apache-2.0. QEMU is a separate,
consumer-supplied GNU GPL version 2 program: this project executes it as a
child process and does not link with or redistribute it. Using QEMU for tests
does not make `rules_ostest` copyleft. Distributing a QEMU binary or firmware
alongside a product does create separate compliance duties; see
[THIRD_PARTY.md](THIRD_PARTY.md) for the boundary and upstream references.

- [License](LICENSE) and [notices](NOTICE)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
# rules_ostest
