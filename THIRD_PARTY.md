# Third-party software and licensing

This inventory describes the dependency boundaries of the `rules_ostest`
source distribution. It is informational and is not legal advice.

## Included in this repository

No third-party source code or executable is copied into this repository. The
test fixture at `tests/testdata/dummy.efi` is project-authored plain text, not a
redistributed firmware binary.

The Bazel module has two direct build dependencies. Bazel fetches them from the
Bazel Central Registry; they are not included in a `rules_ostest` source
archive:

| Dependency | Version | License | Purpose |
|---|---:|---|---|
| `rules_python` | 2.2.0 | Apache-2.0 | Python rules and toolchain registration |
| `platforms` | 1.0.0 | Apache-2.0 | Standard CPU constraints |

`rules_python` resolves a CPython 3.12 toolchain at build time. That runtime and
the components packaged with it keep their own license and notice files. A
distributor that includes the resolved runtime in another product must preserve
and review those files; the `rules_ostest` source distribution does not include
the runtime.

`MODULE.bazel.lock` records dependency resolution and integrity data. An entry
in that lock file does not mean the corresponding package is copied into this
repository or a source release.

## QEMU and firmware

QEMU and UEFI firmware are consumer-supplied Bazel targets. `rules_ostest` does
not download, copy, link with, or redistribute either one. It launches the QEMU
executable as a separate process and communicates through command-line
arguments, byte streams, QMP sockets, and network sockets.

The QEMU emulator as a whole is licensed under GNU GPL version 2. QEMU's own
distribution also contains separately licensed components and firmware. Because
QEMU remains a separate program here, using it to run tests does not require
`rules_ostest` itself to be relicensed under the GPL. The project remains
Apache-2.0.

If you distribute a QEMU executable, a modified QEMU, firmware, a Python
runtime, or shared libraries alongside your own release, that distribution has
additional obligations. Review the exact artifacts' license and notice files,
provide source where their licenses require it, and do not assume this
repository's Apache-2.0 license covers them.

Upstream references:

- QEMU license: <https://gitlab.com/qemu-project/qemu/-/blob/master/LICENSE>
- GNU GPL aggregate guidance: <https://www.gnu.org/licenses/gpl-faq.html#MereAggregation>
- `rules_python`: <https://registry.bazel.build/modules/rules_python>
- `platforms`: <https://registry.bazel.build/modules/platforms/1.0.0>
- Python licensing: <https://docs.python.org/3.12/license.html>
