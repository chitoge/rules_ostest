# Contributing to rules_ostest

Thank you for helping improve `rules_ostest`.

## Before opening a change

- Use the issue tracker for bug reports and feature discussions. Include a
  small reproducer, host platform, guest architecture, Bazel version, and the
  relevant test output when possible.
- Report suspected vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md).
- Keep changes focused. Add or update tests and documentation when behavior or
  public APIs change.

## Build and test

Install Bazelisk, then run:

```sh
bazel test //...
```

The checked-in `.bazelversion` selects the default supported Bazel release.
Continuous integration exercises the supported Bazel 8 and 9 releases, plus
uncached real-QEMU integration tests for x86-64 and AArch64. The real matrix
covers EFI Shell, direct-kernel CirrOS guests, and scripted QMP/GDB control.
The default local suite uses fake QEMU executables and does not require KVM,
QEMU, UEFI firmware, or guest images. The manual integration targets fetch a
SHA-256-locked Ubuntu snapshot closure containing QEMU, SeaBIOS and iPXE
firmware data, OVMF/AAVMF, and prebuilt EFI Shell binaries, plus pinned
test-only CirrOS inputs. No system QEMU, EFI build toolchain, or guest build
toolchain is required.

The real integration job validates local sandbox/runfile execution, not an
actual remote-execution service. See
[Getting started and execution environments](docs/getting-started.md) before
changing QEMU provisioning or remote-execution claims.

Do not commit Bazel output symlinks, Python bytecode, generated disk images,
guest operating-system images, QEMU executables, or real firmware images. The
QEMU YAML manifest and JSON lock are provenance metadata, not binaries.

To change the integration runtime, edit
`tests/integration/qemu_noble.yaml`, run
`tools/update_qemu_runtime_lock.sh`, review every package/version/URL/hash
change, and run the lock test plus the complete real-QEMU matrix documented in
the setup guide. The updater uses a disposable, pinned dependency resolver; it
does not add that resolver or Bazel 7 support to the project module.

## Consumption and release model

Consumers use a pinned Git checkout through `local_path_override` or
`git_override`. This project is not published to the Bazel Central Registry,
and contributions should not add BCR publication metadata or describe an
unpublished registry release as an installation option.

## Licensing contributions

This project uses the Apache License 2.0. Unless you explicitly state otherwise,
contributions intentionally submitted for inclusion are licensed under those
terms, as described by section 5 of the license. By submitting a contribution,
you represent that you have the right to do so.

Do not copy third-party code, test data, firmware, or binaries into a change
without identifying their provenance and confirming that redistribution is
compatible with Apache-2.0. Preserve required notices and update
[THIRD_PARTY.md](THIRD_PARTY.md) when a distributed dependency is added or
changed.

All contributors must follow the [Code of Conduct](CODE_OF_CONDUCT.md).
