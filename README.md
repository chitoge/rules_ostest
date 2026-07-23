# rules_ostest

`rules_ostest` builds deterministic UEFI media and runs OS tests with QEMU and
Bazel. It supports x86-64 and AArch64 guests, UEFI and direct-kernel boot,
serial and Python/QMP tests, writable-media readback, graphics, managed host
forwards, persistent variables, and isolated multi-VM networks.

The library rules keep emulators and firmware outside their implementation.
Image actions do not need QEMU. A real guest test receives QEMU and firmware as
explicit Bazel labels. This repository's manual integration targets use a
test-only, content-pinned runtime; consumers remain free to supply their own.

## Highlights

- Deterministic FAT32, raw, GPT, primary MBR, and UEFI El Torito ISO images.
- Reproducible gzip outputs for remote-cache storage and transfer.
- Ordered and forbidden serial markers, reboot-spanning phases, and bounded
  failure hooks.
- Scripted QMP, GDB, screenshots, keyboard and pointer input, debugcon, and
  persistent UEFI variable stores.
- Virtio, USB mass-storage, NVMe, CD-ROM, qcow2, and writable scratch media.
- Collision-free loopback host forwarding with declared companion programs.
- Process-local Ethernet segments for multi-guest labs and PCAP capture.
- KVM with TCG fallback, or an explicit KVM-required run-or-skip policy.

## Use from Git

`rules_ostest` is not published to the Bazel Central Registry. Clone and pin
the repository, then use a local module override:

```sh
git clone https://github.com/chitoge/rules_ostest.git third_party/rules_ostest
git -C third_party/rules_ostest checkout --detach <full-commit-sha>
```

```starlark
bazel_dep(name = "rules_ostest", version = "0.1.0")

local_path_override(
    module_name = "rules_ostest",
    path = "third_party/rules_ostest",
)
```

The override makes the Git checkout authoritative and prevents a registry
lookup. A full-SHA `git_override` is available when Bazel should fetch the
checkout. See [Getting started and execution environments](docs/getting-started.md)
for both forms.

Use Bazel 8.7.0 or newer, including Bazel 9.x. The module registers a hermetic
Python 3.12 toolchain through `rules_python`.

## QEMU and execution environments

| Workload | Local QEMU installation | Remote execution |
|---|---|---|
| Image rules | Not required | Compatible through the registered Python toolchain |
| Default repository tests | Not required | Deterministic QEMU-shaped fixtures; no VM starts |
| Repository real integration targets | Not required | Pinned inputs; kept local until a worker's VM capabilities are qualified |
| Consumer label wrapping `/usr/bin/qemu-system-*` | Required on every worker | Non-hermetic |
| Consumer label containing a pinned QEMU runtime | Not required | Remote-compatible when the runtime closure and worker capabilities are complete |

A dynamically linked bundle must declare its ELF loader, shared libraries,
QEMU modules and data, firmware, and notices. A static QEMU bundle is simpler,
but its firmware and data files remain explicit inputs. The rules never search
`PATH` or install QEMU.

Project CI fetches an exact Ubuntu snapshot closure for QEMU 8.2.2, SeaBIOS,
iPXE option ROMs, OVMF, AAVMF, and the x86-64/AArch64 EFI Shell. Every one of
the 90 package archives has a checked SHA-256. Bazel extracts the closure
outside the source tree and boots real x86-64 and AArch64 guests in local
sandboxed test actions. No system QEMU installation is used. The integration
targets remain `no-remote` until a remote worker's emulator, process, socket,
and resource policies are qualified.

The [setup guide](docs/getting-started.md) includes:

- commands for the pinned local real-QEMU gate and lock maintenance;
- a content-addressed QEMU bundle layout;
- an `http_archive` overlay and runfile-aware dynamic-loader wrapper;
- remote-worker requirements for TCG, sockets, file descriptors, and scratch
  space; and
- an exact account of what project CI does and does not verify.

## Minimal UEFI test

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
    qemu = "//tools/qemu:qemu_system_x86_64",
    firmware = "@qemu_linux_x86_64//:firmware/OVMF_CODE.fd",
    firmware_vars = "@qemu_linux_x86_64//:firmware/OVMF_VARS.fd",
    disk = ":kernel_disk",
    forbidden_markers = ["PANIC", "KERNEL FAULT"],
    success_markers = [
        "KERNEL: EARLY INIT",
        "KERNEL: SERVICES READY",
        "OSTEST: PASS",
    ],
    timeout_seconds = 60,
)
```

The QEMU and firmware labels are consumer examples from the hermetic bundle
pattern in the setup guide. The repository defines a development-only runtime
for its own integration tests, but does not redistribute its binaries or make
that runtime part of the consumed rules module.

The legacy default protocol accepts `OSTEST: PASS` and rejects `OSTEST: FAIL`.
Tests can instead use regular expressions, ordered markers, or reboot phases.
Every serial test retains `qemu.log` and `qemu-command.txt` as Bazel undeclared
test outputs.

## Capability guides

| Task | Documentation |
|---|---|
| Choose `uefi_test`, `uefi_py_test`, or `uefi_lab_test` | [Test entry points](docs/testing-platforms-and-composition.md#serial-tests) |
| Configure x86-64, AArch64, KVM/TCG, or direct boot | [Guest platforms and acceleration](docs/testing-platforms-and-composition.md#guest-platforms) |
| Build FAT, raw, GPT, MBR, and ISO media | [Composing disks](docs/testing-platforms-and-composition.md#composing-disks) |
| Attach virtio, USB, NVMe, CD-ROM, qcow2, or scratch media | [Attaching boot media](docs/testing-platforms-and-composition.md#attaching-boot-media) |
| Add QMP/GDB scripts, graphics, or persistent variables | [UEFI testing scenarios](docs/osdev-uefi-use-cases.md) |
| Probe a guest service through an automatic host forward | [Managed host-to-guest probes](docs/testing-platforms-and-composition.md#managed-host-to-guest-probes) |
| Run several guests on isolated Ethernet segments | [Local Ethernet labs](docs/osdev-uefi-use-cases.md#local-ethernet-labs) |
| Diagnose serial, QMP, GDB, symbol, graphics, or network failures | [Debugging UEFI tests](docs/debugging-uefi-tests.md) |

See the [documentation index](docs/README.md) for the recommended reading
order.

## Scope and policies

QMP, GDB, host forwarding, and lab networking use POSIX process, descriptor,
loopback, and Unix-socket facilities. Writable state is created below
`TEST_TMPDIR`; diagnostics and exported media use
`TEST_UNDECLARED_OUTPUTS_DIR`.

`rules_ostest` is Apache-2.0. QEMU is a separate, consumer-supplied GPLv2
program. Executing it for tests does not make this project copyleft. A consumer
that redistributes QEMU, firmware, guest images, a Python runtime, or shared
libraries must satisfy those artifacts' own licenses; see
[Third-party software and licensing](THIRD_PARTY.md).

- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [License](LICENSE) and [notices](NOTICE)
