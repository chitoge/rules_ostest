# Getting started and execution environments

This guide explains how to consume `rules_ostest` from Git, when QEMU is
required, and how to package QEMU for sandboxed or remote test execution.

## Use the module from Git

`rules_ostest` is not published to the Bazel Central Registry. Keep a Git
checkout beside or inside the consuming workspace and override the module to
that checkout.

For example, pin a checkout in `third_party`:

```sh
git clone https://github.com/chitoge/rules_ostest.git third_party/rules_ostest
git -C third_party/rules_ostest checkout --detach <full-commit-sha>
```

Then add the dependency and local override to the root `MODULE.bazel`:

```starlark
bazel_dep(name = "rules_ostest", version = "0.1.0")

local_path_override(
    module_name = "rules_ostest",
    path = "third_party/rules_ostest",
)
```

The version is the module's compatibility identity; the override prevents a
registry lookup and makes the selected Git checkout authoritative. Update the
checkout explicitly when adopting a new revision.

If checking out dependencies separately is undesirable, Bzlmod can fetch a
pinned Git commit directly:

```starlark
bazel_dep(name = "rules_ostest", version = "0.1.0")

git_override(
    module_name = "rules_ostest",
    commit = "<full-commit-sha>",
    remote = "https://github.com/chitoge/rules_ostest.git",
)
```

Use a full immutable commit SHA, not a branch name. This is still Git-based
consumption and does not require a BCR release.

## Know when QEMU is required

The image rules are ordinary Bazel build actions and do not invoke QEMU. The
repository's default `bazel test //...` suite uses QEMU-shaped test fixtures,
so it also runs without QEMU, firmware, KVM, or a guest operating system.

Real guest targets are different: every `uefi_test`, `uefi_py_test`, or
`uefi_vm` receives a `qemu` label. A hermetic QEMU bundle that uses external
BIOS, option-ROM, or keymap data also supplies `qemu_firmware_dir`, while UEFI
targets receive their platform firmware labels separately. The rules execute
those declared inputs; they do not search `PATH`, install packages, or download
an emulator automatically.

| Workload | Host installation needed? | Remote-execution status |
|---|---|---|
| Image rules | No | Remote-compatible with the registered Python toolchain |
| Repository default tests | No | Deterministic fixture tests; no VM is started |
| Repository manual real tests | No | Content-pinned; `no-remote` pending worker qualification |
| Wrapper around `/usr/bin/qemu-system-*` | Yes, on every worker | Non-hermetic and unsuitable for general remote execution |
| Declared static QEMU bundle | No | Simplest hermetic remote input |
| Declared dynamic QEMU closure | No | Hermetic when the loader, libraries, modules, data files, and firmware are complete |

## Run this repository's real integration tests locally

No local QEMU, firmware, EFI toolchain, or `apt` installation is needed. Bazel
fetches the same pinned runtime used by CI:

```sh
bazel fetch @qemu_noble_x86_64//:qemu_firmware_dir

bazel test \
  --local_test_jobs=1 \
  --nocache_test_results \
  --test_output=errors \
  //tests/integration:real_qemu_efi_shell_test
```

The first command materializes 90 packages from Ubuntu's official
`20260720T000000Z` Noble snapshot. The package graph, version, URL, and SHA-256
of every archive are checked into
`tests/integration/qemu_noble.lock.json`. The repository rule extracts the
archives under Bazel's external-repository directory; it does not write
binaries into the source checkout. Subsequent workspaces can reuse Bazel's
repository cache.

The launcher resolves QEMU through Bazel runfiles and invokes the pinned
closure's own ELF loader with its declared library path. Each real target sets
`qemu_firmware_dir` to the runtime's marker target, so `rules_ostest` resolves
that marker through runfiles and passes its parent to QEMU with `-L`. The
marker target also carries the complete runtime as runfiles. The resulting
data view includes the closure's SeaBIOS and iPXE option ROMs, so QEMU does not
fall back to host firmware-data paths. These runtime files do not make the
test-level `firmware` label mandatory for direct-kernel boot. The launcher
never searches `PATH`. OVMF/AAVMF and the EFI Shell are labels from the same
repository. This makes the inputs content-pinned; the Linux kernel ABI and the
sandbox's process/socket/resource policy remain execution-platform
requirements.

The example runs one representative x86-64 EFI Shell gate. Real targets are
tagged `manual`, so `bazel test //...` intentionally excludes them. The
`Exercise real QEMU guests` step in
[the CI workflow](../.github/workflows/ci.yml) is the authoritative 13-target
matrix. It covers x86-64 and AArch64 EFI Shell, CirrOS direct boot, QMP/GDB
control, shutdown, writable media, CD-ROM, USB, NVMe, the KVM policy, and a
forced-TCG multi-VM lab.

On a local 2026-07-23 validation, a cold package fetch took about 54 seconds
and the uncached 13-target guest phase took 187 seconds; individual guests took
6–34 seconds. These are orientation numbers, not performance guarantees. In CI,
inspect the separate `Fetch content-pinned QEMU runtime` and
`Exercise real QEMU guests` steps and the 13 per-target results. A fast
default/metadata job is not evidence that real guests ran.

### Update the pinned runtime

Edit `tests/integration/qemu_noble.yaml` to change the snapshot or requested
packages, then run:

```sh
tools/update_qemu_runtime_lock.sh
bazel test //tests:qemu_runtime_lock_test
```

The updater needs Git, curl, and Bazelisk's `bazel` command. It creates a
temporary checkout of the dependency resolver at an exact commit and invokes
its known-good Bazel version; neither becomes a project dependency or changes
the project's Bazel 8.7+ support floor. Review every changed package, version,
snapshot URL, and digest in the generated lock. Then run the complete real
matrix before committing. Do not hand-edit the JSON lock.

## Supply QEMU from a consuming repository

The root module's development-only QEMU repository is not instantiated when
`rules_ostest` is consumed as a dependency. A consumer should own its emulator
version and execution-platform contract. The following pattern keeps that
runtime outside `rules_ostest` while making every byte needed by the test a
declared, content-pinned input.

### 1. Produce a runtime bundle

Build or stage QEMU once for the remote worker's operating system and CPU. A
dynamic Linux bundle should have a layout similar to:

```text
qemu-linux-x86_64/
  runtime/
    lib64/ld-linux-x86-64.so.2
    lib/x86_64-linux-gnu/*.so.*
    usr/lib/x86_64-linux-gnu/*.so.*
    usr/lib/x86_64-linux-gnu/qemu/*.so
    usr/bin/qemu-system-x86_64
    usr/bin/qemu-system-aarch64
    usr/share/qemu/.rules_ostest_dir
    usr/share/qemu/**
  firmware/
    OVMF_CODE.fd
    OVMF_VARS.fd
    AAVMF_CODE.fd
    AAVMF_VARS.fd
  licenses/**
```

A static QEMU build can omit the loader and shared-library directories. Keep
QEMU data files and firmware explicit even with a static executable. Archive
the directory, publish it to a location accessible during Bazel repository
fetching, and record its SHA-256 and provenance. Review the redistribution
requirements described in [`THIRD_PARTY.md`](../THIRD_PARTY.md).

### 2. Fetch the bundle by digest

In the consuming root module:

```starlark
http_archive = use_repo_rule(
    "@bazel_tools//tools/build_defs/repo:http.bzl",
    "http_archive",
)

http_archive(
    name = "qemu_linux_x86_64",
    build_file = "//tools/qemu:qemu_bundle.BUILD.bazel",
    sha256 = "<bundle-sha256>",
    strip_prefix = "qemu-linux-x86_64",
    urls = ["https://artifacts.example/qemu-linux-x86_64.tar.gz"],
)
```

Repository fetching happens before the test action. Remote workers do not need
network access to the artifact URL; Bazel transfers the resulting declared
runfiles to them.

The overlay `tools/qemu/qemu_bundle.BUILD.bazel` can expose the closure and
firmware:

```starlark
filegroup(
    name = "runtime",
    srcs = glob([
        "licenses/**",
        "runtime/**",
    ]),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "qemu_firmware_dir",
    srcs = ["runtime/usr/share/qemu/.rules_ostest_dir"],
    data = [":runtime"],
    visibility = ["//visibility:public"],
)

exports_files([
    "firmware/AAVMF_CODE.fd",
    "firmware/AAVMF_VARS.fd",
    "firmware/OVMF_CODE.fd",
    "firmware/OVMF_VARS.fd",
])
```

### 3. Add a runfile-aware launcher

A dynamically linked QEMU should be launched with its bundled loader and
library path. Add `rules_python` and `platforms` as direct dependencies of the
consumer if they are not already present:

```starlark
bazel_dep(name = "platforms", version = "1.0.0")
bazel_dep(name = "rules_python", version = "2.2.0")
```

Declare a launcher in `tools/qemu/BUILD.bazel`:

```starlark
load("@rules_python//python:py_binary.bzl", "py_binary")

py_binary(
    name = "qemu_system_x86_64",
    srcs = ["qemu_launcher.py"],
    data = ["@qemu_linux_x86_64//:runtime"],
    deps = ["@rules_python//python/runfiles"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
```

The launcher resolves every path through Bazel runfiles:

```python
#!/usr/bin/env python3
import os
import sys

from python.runfiles import runfiles


def required(locator, logical_path):
    path = locator.Rlocation(logical_path)
    if path is None or not os.path.isfile(path):
        raise SystemExit(f"missing QEMU runfile: {logical_path}")
    return path


locator = runfiles.Create()
if locator is None:
    raise SystemExit("Bazel runfiles are unavailable")

prefix = "qemu_linux_x86_64/runtime"
loader = required(locator, f"{prefix}/lib64/ld-linux-x86-64.so.2")
qemu = required(locator, f"{prefix}/usr/bin/qemu-system-x86_64")
library_path = ":".join(
    [
        os.path.dirname(required(locator, f"{prefix}/lib/x86_64-linux-gnu/libc.so.6")),
        os.path.dirname(required(locator, f"{prefix}/usr/lib/x86_64-linux-gnu/libglib-2.0.so.0")),
    ]
)
module_dir = os.path.join(
    os.path.dirname(os.path.dirname(qemu)),
    "lib/x86_64-linux-gnu/qemu",
)
os.environ["QEMU_MODULE_DIR"] = module_dir
os.execv(
    loader,
    [loader, "--library-path", library_path, qemu, *sys.argv[1:]],
)
```

Adjust the representative `libc`, GLib, and module paths to the exact bundle
layout. The launcher deliberately does not add `-L`; the test rule adds the
location-expanded directory from `qemu_firmware_dir`. A static launcher can
resolve `qemu-system-*` and execute it directly. Create a second launcher for
`qemu-system-aarch64` when the guest architecture requires it.

### 4. Use only declared labels in the test

```starlark
uefi_test(
    name = "kernel_test",
    arch = "x86_64",
    qemu = "//tools/qemu:qemu_system_x86_64",
    qemu_firmware_dir = "@qemu_linux_x86_64//:qemu_firmware_dir",
    firmware = "@qemu_linux_x86_64//:firmware/OVMF_CODE.fd",
    firmware_vars = "@qemu_linux_x86_64//:firmware/OVMF_VARS.fd",
    disk = ":kernel_disk",
    timeout_seconds = 60,
)
```

`qemu_firmware_dir` is a label, not a literal directory string. Its target must
have exactly one default output: an empty marker file located directly in the
QEMU data directory. Put the runtime closure in that target's `data`, as above.
The macros location-expand the marker, make the target's runfiles available,
and pass the marker's parent to QEMU as `-L`. This works in sandboxed and
remote execution, where a source-tree or host absolute path would not.
Do not also put `-L` in `qemu_args`; the macros reject that ambiguous
configuration.

Do not give this target a `no-remote`, `local`, or `manual` tag when it is meant
to run normally on remote workers. The QEMU bundle must match the platform on
which the test action runs; guest architecture and worker architecture are
separate concerns.

### 5. Configure the remote executor

Portable remote tests should use TCG. KVM requires the remote worker to expose
`/dev/kvm` and the scheduler to honor a matching execution property. The
worker sandbox must also permit child processes, loopback and Unix sockets,
inherited file descriptors, and writes below `TEST_TMPDIR`.

Hermetic inputs do not grant those capabilities. Select an executor pool and
isolation policy that explicitly permits CPU emulators. If the service uses a
container image, pin that image by digest as well; changing the image can
change the available kernel ABI and sandbox policy. Provider-specific
`exec_properties` belong in the consuming repository's execution platform,
not in `rules_ostest`.

Run the target using the remote executor configuration supplied by the
consumer's Bazel setup, for example:

```sh
bazel test --remote_executor=grpcs://remote.example \
  //path/to:kernel_test
```

A remote cache is not a remote executor. Seeing a remote cache hit proves only
artifact transfer; inspect Bazel execution metadata to confirm the test action
actually ran remotely.

If QEMU is killed before producing serial or stderr output, inspect the remote
action's termination reason, memory limit, seccomp policy, and isolation mode.
An empty `qemu.log` plus a reset QMP connection can be caused by an
executor-level failure or a runtime/worker incompatibility; it does not by
itself distinguish the two.

## What this repository verifies

The project currently verifies three distinct layers:

1. Bazel 8.7.0 and 9.2.0 run the deterministic image, rule, runner, and
   QEMU-shaped fixture suite without a QEMU installation. During the
   2026-07-23 documentation audit, Bazel execution metadata confirmed that the
   configured remote executor ran this suite's eligible actions.
2. Ubuntu 24.04 CI fetches the checked Ubuntu-snapshot lock, verifies every
   archive digest, exposes the dynamic QEMU/firmware closure as declared
   runfiles, and boots all 13 real x86-64 and AArch64 EFI Shell and CirrOS
   targets in local Bazel test actions. It does not install host QEMU.
3. An external-consumer smoke workspace uses `local_path_override` and builds
   image targets on both supported Bazel versions.

The project does **not** currently run real QEMU on a remote-execution backend
in CI. During the same audit, the pinned x86-64 EFI Shell target was dispatched
to a managed remote executor under its default container isolation and its
alternate microVM isolation. In both cases the declared inputs transferred,
but the worker terminated QEMU before it produced serial, stderr, or firmware
debug output. The target remains `no-remote`.

Therefore, deterministic remote actions and the pinned real-QEMU closure are
both tested, but their combination is not a project guarantee. Consumers
should add one representative TCG boot to their own remote-executor CI and
remove `no-remote` only after that gate passes under the selected worker pool
and isolation policy.

## Next steps

- [Testing, guest platforms, and image composition](testing-platforms-and-composition.md)
- [UEFI testing scenarios](osdev-uefi-use-cases.md)
- [Debugging UEFI tests](debugging-uefi-tests.md)
- [Third-party software and licensing](../THIRD_PARTY.md)
