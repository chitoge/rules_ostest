# Debugging UEFI tests

This guide covers failure diagnosis and source debugging for `uefi_test`,
`uefi_py_test`, and `uefi_lab_test` targets.

Choose the section that matches the symptom:

- QEMU does not start: begin with [the test artifacts](#start-with-the-test-artifacts).
- Firmware starts with no guest result: add [serial diagnostics](#improve-serial-diagnostics) and [debugcon](#capture-x86-firmware-debugcon).
- The VM hangs: capture [QMP state](#inspect-a-running-vm-with-qmp).
- Source breakpoints are needed: configure [a declared GDB debugger](#add-a-declared-gdb-debugger).
- A network lab fails: inspect [per-VM state and PCAPs](#debug-a-multi-vm-network-lab).

## Start with the test artifacts

Run the failing target without a cached result:

```console
bazel test //path/to:boot_test \
  --nocache_test_results \
  --test_output=errors
```

Every QEMU test records its command line under `TEST_UNDECLARED_OUTPUTS_DIR`.
`uefi_test` records the complete combined serial and QEMU output stream.
`QemuSession` records the serial bytes consumed by `wait_for_serial`. Bazel
publishes these files under the target's `bazel-testlogs` directory. Remote
test systems attach the same undeclared outputs to the test result.

The serial rule caps `qemu.log` at 256 MiB and fails the target at that bound
so a malfunctioning guest cannot exhaust the worker with an unbounded log.

For `//path/to:boot_test`, list local undeclared outputs with:

```console
find -L bazel-testlogs/path/to/boot_test/test.outputs \
  -maxdepth 1 \
  -type f
```

A Bazel configuration that zips undeclared outputs stores them in an
`outputs.zip` file in the same test-result tree. List that form with
`unzip -l path/to/outputs.zip`.

Artifact names identify the test interface and VM:

| Interface | Artifacts |
|---|---|
| `uefi_test` | `qemu.log`, `qemu-command.txt` |
| `uefi_py_test` | `ostest-qemu.log`, `ostest-qemu-command.txt` |
| `uefi_lab_test` | `<vm>-qemu.log`, `<vm>-qemu-command.txt`, `network-<segment>.pcap` |
| x86 debugcon | `ostest-ovmf-debug.log` or `<vm>-ovmf-debug.log` |
| NVRAM export | `uefi-vars.fd` or a test-selected filename |
| QMP screenshot | The filename selected by the Python test |
| serial graphical assertion | `screendump.ppm` |
| managed host forward | `hostfwd.json`, `host-companion.log`, `host-companion/` |
| exported writable medium | `ostest-media-<index>-<name>.img` |

The command file is shell-quoted for inspection. QMP, GDB, and network file
descriptor numbers belong to the original test process, so rerun the Bazel
target to create live descriptors.

Read artifacts in this order:

1. `qemu.log` for startup errors, firmware output, and serial messages.
2. `qemu-command.txt` for machine, accelerator, pflash, media, and device
   configuration.
3. debugcon output for early x86 OVMF diagnostics.
4. screenshots for firmware UI and framebuffer state.
5. PCAPs and per-VM logs for a network lab.

## Improve serial diagnostics

Use stable, line-oriented messages that identify the phase and result:

```text
OSTEST: PHASE firmware-entry
OSTEST: PHASE memory-map entries=47
OSTEST: PHASE exit-boot-services
OSTEST: FAIL subsystem=allocator reason=free-list-cycle
```

Match a specific success message and a broad failure family:

```starlark
uefi_test(
    name = "boot_test",
    arch = "x86_64",
    # qemu, firmware, and disk ...
    success_pattern = r"OSTEST: PASS suite=boot$",
    failure_pattern = r"(OSTEST: FAIL|PANIC|ASSERTION FAILED)",
    timeout_seconds = 60,
)
```

The serial runner checks the failure expression before the success expression.
Include enough state in a failure line to identify the failing subsystem
without requiring a second run.

When ordering matters, prefer literal markers over a single broad regular
expression:

```starlark
uefi_test(
    # ...
    success_markers = ["STORAGE: PROVISIONED", "STORAGE: MOUNTED", "READBACK: OK"],
    forbidden_markers = ["PANIC", "ASSERTION FAILED"],
)
```

For a reboot-spanning failure, `qemu.log` contains all phases in one stream and
`qemu-command.txt` omits `-no-reboot`. A phase advances only after its markers
and a QMP guest reset, which distinguishes a real reboot boundary from a
repeated banner.

Use a failure hook to make raw serial addresses actionable while preserving
the original log:

```starlark
uefi_test(
    # ...
    on_failure = "//tools:symbolize_serial",
)
```

The executable receives `qemu.log` as its only argument and as
`OSTEST_SERIAL_LOG`. Its output appears before the final failure report; hook
errors are warnings and do not replace the guest failure.

In a Python test, `session.serial_text` contains the serial text observed so
far:

```python
with QemuSession(config) as vm:
    vm.wait_for_serial(r"OSTEST: PHASE exit-boot-services", timeout=30)
    assert "PANIC" not in vm.serial_text
```

## Capture x86 firmware debugcon

Set `debugcon = True` to capture x86 OVMF output written to I/O port `0x402`:

```starlark
uefi_test(
    name = "ovmf_boot_test",
    arch = "x86_64",
    # qemu, firmware, and disk ...
    debugcon = True,
)
```

The output is stored as `ostest-ovmf-debug.log`. In a lab, each enabled VM
writes `<vm>-ovmf-debug.log`.

The selected firmware build must emit debug messages to port `0x402`.
`debugcon` is available for x86-64 guests.

## Inspect a running VM with QMP

`uefi_py_test` gives the Python test access to QMP through `QemuSession`.
Capture machine state when a phase fails or times out:

```python
import argparse
import json
import os
import pathlib

from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments

parser = argparse.ArgumentParser()
add_uefi_qemu_arguments(parser)
config = UefiQemuConfig.from_namespace(parser.parse_args())

outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])

with QemuSession(config) as vm:
    try:
        vm.wait_for_serial(r"KERNEL_READY", timeout=30)
    except TimeoutError:
        status = vm.execute("query-status")
        cpus = vm.execute("query-cpus-fast")
        (outputs / "qmp-state.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "cpus": cpus,
                    "events": vm.events,
                    "serial": vm.serial_text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        vm.screendump(outputs / "failure-screen.ppm")
        raise
```

Set `graphics = True` on the target before using `screendump`, optionally with
a specialized `graphics_device` override. A serial `uefi_test` can
instead set `screendump_not_blank = True`; its bounded PPM check retains
`screendump.ppm` even when the assertion fails.

`session.events` contains asynchronous QMP events observed while QMP commands
were being processed. `session.execute()` accepts any command supported by the
selected QEMU build.

Pause the VM while collecting a coherent state snapshot:

```python
vm.execute("stop")
try:
    status = vm.execute("query-status")
    cpus = vm.execute("query-cpus-fast")
    vm.screendump(outputs / "paused-screen.ppm")
finally:
    vm.execute("cont")
```

## Add a declared GDB debugger

GDB source debugging uses `uefi_py_test` with `gdb = True`. The ruleset creates
an ephemeral listener on `127.0.0.1` and passes it to QEMU as an inherited file
descriptor. The Python test reads the address from `session.gdb_address`.

Declare the GDB executable and symbol file as test data:

```starlark
load("@rules_ostest//ostest:defs.bzl", "uefi_py_test")

uefi_py_test(
    name = "gdb_boot_test",
    srcs = ["gdb_boot_test.py"],
    main = "gdb_boot_test.py",
    guest_platform = "//platforms:guest_x86_64",
    qemu = "@qemu_x86_64//:qemu-system-x86_64",
    firmware = "@ovmf//:OVMF_CODE.fd",
    firmware_vars = "@ovmf//:OVMF_VARS.fd",
    disk = ":debug_disk",
    gdb = True,
    data = [
        ":boot.efi.debug",
        "@gdb//:gdb",
    ],
    args = [
        "--gdb-bin=$(rlocationpath @gdb//:gdb)",
        "--symbols=$(rlocationpath :boot.efi.debug)",
    ],
)
```

The GDB target must run on the Bazel execution platform and support the guest
architecture. Keep unstripped DWARF symbols in a declared artifact such as
`boot.efi.debug`; the boot medium can continue to contain the firmware-loadable
PE/COFF file.

Resolve the debugger and symbol runfiles in the Python test:

```python
import argparse
import os
import pathlib
import subprocess

from python.runfiles import runfiles
from ostest.python.qemu import QemuSession, UefiQemuConfig, add_uefi_qemu_arguments


def resolve(locator: runfiles.Runfiles, value: str) -> pathlib.Path:
    resolved = locator.Rlocation(value)
    if not resolved:
        raise RuntimeError(f"runfile is unavailable: {value}")
    return pathlib.Path(resolved)


def gdb_quote(path: pathlib.Path) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


parser = argparse.ArgumentParser()
parser.add_argument("--gdb-bin", required=True)
parser.add_argument("--symbols", required=True)
add_uefi_qemu_arguments(parser)
args = parser.parse_args()

locator = runfiles.Create()
if locator is None:
    raise RuntimeError("Bazel runfiles are unavailable")

gdb = resolve(locator, args.gdb_bin)
symbols = resolve(locator, args.symbols)
config = UefiQemuConfig.from_namespace(args)
outputs = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])

with QemuSession(config) as vm:
    marker = vm.wait_for_serial(
        r"DEBUG: SYMBOL_OFFSET=(0x[0-9a-fA-F]+)",
        timeout=30,
    )
    symbol_offset = int(marker.group(1), 16)
    vm.execute("stop")

    host, port = vm.gdb_address
    result = subprocess.run(
        [
            str(gdb),
            "--batch",
            "--nx",
            "-ex", "set pagination off",
            "-ex", "set confirm off",
            "-ex", f"target remote {host}:{port}",
            "-ex", f"add-symbol-file {gdb_quote(symbols)} -o {symbol_offset:#x}",
            "-ex", "break debug_checkpoint",
            "-ex", "continue",
            "-ex", "thread apply all backtrace",
            "-ex", "info registers",
            "-ex", "x/16i $pc",
            "-ex", "set variable debug_release = 1",
            "-ex", "detach",
        ],
        cwd=os.environ["TEST_TMPDIR"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    (outputs / "gdb.log").write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"GDB exited with status {result.returncode}")

    vm.wait_for_serial(r"OSTEST: PASS", timeout=30)
```

This example uses a guest-side debug checkpoint. The guest prints the symbol
relocation offset, then repeatedly calls a visible `debug_checkpoint` function
while a volatile `debug_release` variable remains zero. GDB breaks at that
function, captures state, sets `debug_release`, and detaches.

The debugger, symbols, commands, and resulting `gdb.log` remain part of the
Bazel test result.

## Load symbols for a relocated UEFI image

UEFI firmware may relocate a PE/COFF image during loading. GDB needs the
relocation offset:

```text
relocation offset = loaded image address - preferred image address
```

The application can obtain its loaded address from `EFI_LOADED_IMAGE_PROTOCOL`
and report the computed offset through serial. Load the symbols with:

```gdb
add-symbol-file boot.efi.debug -o 0xRELOCATION_OFFSET
```

When a symbol artifact records section-specific addresses, supply those
addresses explicitly:

```gdb
add-symbol-file boot.efi.debug \
  -s .text 0xTEXT_ADDRESS \
  -s .data 0xDATA_ADDRESS
```

Use `info files` and `maintenance info sections` to inspect the addresses GDB
assigned to each section. Source paths embedded by the compiler can be mapped
to runfiles with:

```gdb
set substitute-path /build/source/root /path/to/current/runfiles/root
```

Keep the debug symbol artifact and the booted PE/COFF image from the same build
action so their sections and types remain synchronized.

## Start paused at firmware reset

Set both GDB options to stop the VM before its first guest instruction:

```starlark
uefi_py_test(
    name = "firmware_entry_test",
    # srcs, main, qemu, firmware, and media ...
    gdb = True,
    pause_at_start = True,
)
```

The Python test must connect GDB before waiting for serial output:

```python
with QemuSession(config) as vm:
    host, port = vm.gdb_address
    # Start the declared GDB client and connect to host:port here.
```

At reset, useful GDB commands include:

```gdb
info registers
x/16i $pc
info threads
continue
```

Firmware symbols and their load addresses are specific to the selected
firmware build. Declare its debug artifacts and load map as test data.

## Diagnose process exits and timeouts

For a serial test, `success_exit_codes` accepts a planned QEMU process status:

```starlark
uefi_test(
    name = "shutdown_test",
    # ...
    success_exit_codes = [0],
)
```

For a Python test, validate the process explicitly:

```python
vm.wait_for_exit(timeout=30, acceptable_codes=(0,))
```

There are two timeout layers:

- `timeout_seconds` or `wait_for_serial(timeout=...)` controls the guest phase.
- The Bazel test `timeout` controls the complete test action.

Give a paused GDB test enough Bazel time to start the debugger, collect state,
and detach. Always apply a timeout to debugger subprocesses.

If QEMU exits before QMP negotiation, inspect `qemu.log` and
`qemu-command.txt`. Common configuration errors include an incompatible
firmware architecture, an invalid pflash size, a missing device model, a
duplicate boot index, and an unsupported machine property.

## Debug a multi-VM network lab

Each lab participant has an independent `QemuSession`:

```python
with QemuLab(config) as lab:
    server = lab["server"]
    client = lab["client"]

    server.wait_for_serial(r"SERVER_READY", timeout=30)
    client.wait_for_serial(r"CLIENT_CONNECTED", timeout=30)

    server_status = server.execute("query-status")
    client_status = client.execute("query-status")
```

Use these outputs together:

- `<vm>-qemu.log` identifies each participant's serial and QEMU output.
- `<vm>-qemu-command.txt` records its architecture, media, NICs, and MAC
  addresses.
- `network-<segment>.pcap` records Ethernet frames seen by the segment.
- a per-VM QMP screenshot records firmware UI or framebuffer state.

Open the PCAP with a declared packet-analysis tool or download it from the test
result for inspection. Generated MAC addresses are deterministic, so display
filters remain stable across runs.

When a network assertion fails, include the participant and phase in its
message:

```python
raise AssertionError("client: timed out waiting for DHCP offer")
```

## Failure checklist

### QEMU does not start

- Check the machine and guest architecture in `qemu-command.txt`.
- Check both pflash paths and the variable-store size.
- Check media formats, interfaces, and unique boot indices.
- Check the worker can execute the declared QEMU target and its runfiles.

### Firmware starts but no application runs

- Capture x86 debugcon when using a debug OVMF build.
- Add a display device and capture a screenshot.
- Confirm the fallback filename is `BOOTX64.EFI` or `BOOTAA64.EFI`.
- Confirm the selected firmware and EFI binary use the same architecture.
- Inspect exported NVRAM for a stale boot selection.

### The guest hangs

- Add serial phase markers around the last completed operation.
- Query QMP status and CPU state.
- Pause the VM and attach GDB.
- Capture registers, instructions at the program counter, and all thread
  backtraces.

### A graphical assertion fails

- Save a QMP screenshot before raising the assertion.
- Record expected and actual image hashes.
- Record the display device and firmware build in the failure output.

### A network test fails

- Check readiness markers for every participant.
- Inspect the PCAP for address assignment and request/response traffic.
- Filter frames by the deterministic MAC addresses in the QEMU command files.
- Capture QMP state and serial output for both endpoints.

## Reference documentation

- [QEMU GDB usage](https://qemu.readthedocs.io/en/master/system/gdb.html)
- [QEMU QMP reference](https://qemu.readthedocs.io/en/master/interop/qemu-qmp-ref.html)
- [GDB symbol files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html)
- [GDB source paths](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Source-Path.html)
- [OSDev UEFI debugging with GDB](https://wiki.osdev.org/Debugging_UEFI_applications_with_GDB)
- [Bazel test environment and undeclared outputs](https://bazel.build/reference/test-encyclopedia)
