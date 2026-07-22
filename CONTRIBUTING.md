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
QEMU, UEFI firmware, or guest images. The manual integration targets stage
their system runtime and prebuilt EFI Shell and fetch pinned test-only CirrOS
inputs through Bazel; no EFI or guest build toolchain is required.

Do not commit Bazel output symlinks, Python bytecode, generated disk images,
guest operating-system images, or real firmware images.

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
