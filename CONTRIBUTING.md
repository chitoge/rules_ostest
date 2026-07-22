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
Continuous integration also exercises the supported Bazel major versions.
Tests use fake QEMU executables and do not require KVM, QEMU, or UEFI firmware.

Do not commit Bazel output symlinks, Python bytecode, generated disk images, or
real firmware images.

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
