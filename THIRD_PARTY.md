# Third-party software and licensing

This inventory describes the dependency boundaries of the `rules_ostest`
source distribution. It is informational and is not legal advice.

## Included in this repository

No third-party source code or executable is copied into this repository. The
fixtures at `tests/testdata/dummy.efi` and `tests/runner_negative/dummy.fd` are
project-authored plain text, not redistributed firmware binaries.

The Bazel module has two direct build dependencies and one root-only development
dependency. Bazel fetches them from the Bazel Central Registry; they are not
included in a `rules_ostest` source archive:

This registry use applies only to upstream dependencies. `rules_ostest` itself
is consumed from Git/local overrides and is not published to the registry.

| Dependency | Version | License | Purpose |
|---|---:|---|---|
| `rules_python` | 2.2.0 | Apache-2.0 | Python rules and toolchain registration |
| `platforms` | 1.0.0 | Apache-2.0 | Standard CPU constraints |
| `buildifier_prebuilt` | 8.5.1.2 | MIT | Root-module formatting check only |

`rules_python` resolves a CPython 3.12 toolchain at build time. That runtime and
the components packaged with it keep their own license and notice files. A
distributor that includes the resolved runtime in another product must preserve
and review those files; the `rules_ostest` source distribution does not include
the runtime.

`MODULE.bazel.lock` records dependency resolution and integrity data. An entry
in that lock file does not mean the corresponding package is copied into this
repository or a source release.

## QEMU, firmware, and EFI Shell

QEMU and UEFI firmware are consumer-supplied Bazel targets. The `rules_ostest`
source distribution does not contain, link with, or redistribute either one.
It launches the QEMU executable as a separate process and communicates through
command-line arguments, byte streams, QMP sockets, and network sockets.

The real-QEMU CI job temporarily stages the Ubuntu QEMU packages, their dynamic
runtime closure, OVMF and AAVMF, prebuilt TianoCore EFI Shell binaries for
x86-64 and AArch64, and their package notices into a Git-ignored test directory.
Those files are declared test inputs, are not committed or uploaded as release
artifacts, and are discarded with the hosted runner. No EDK II or guest build
toolchain is used by these tests.

The QEMU emulator as a whole is licensed under GNU GPL version 2. QEMU's own
distribution also contains separately licensed components and firmware. Because
QEMU remains a separate program here, using it to run tests does not require
`rules_ostest` itself to be relicensed under the GPL. The project remains
Apache-2.0.

The staged EFI Shell binaries are built from TianoCore EDK II ShellPkg and are
covered by the licenses recorded in Ubuntu's `efi-shell-x64` and
`efi-shell-aa64` copyright notices, principally BSD-2-Clause-Patent for
ShellPkg. Executing them as separate test guests does not change this project's
license.

## Test-only CirrOS guest

The root module declares CirrOS 0.6.3 disk, kernel, and initramfs downloads for
x86-64 and AArch64 as development-only repositories. Each URL and SHA-256 is
pinned to the project's canonical release index. Bazel fetches these inputs
only when the manual real-QEMU cloud tests are selected. They are not copied
into the source tree, uploaded from CI, or included in release archives.

CirrOS is a small test operating-system distribution assembled from components
under heterogeneous licenses, including GPL-covered Linux and BusyBox code.
The project executes the unmodified images as separate QEMU guests and does not
link them into `rules_ostest`. This test use does not change the Apache-2.0
license of `rules_ostest`. Anyone who redistributes the guest images must review
and satisfy the license obligations of the exact CirrOS components they ship.

If you distribute a QEMU executable, a modified QEMU, firmware, a Python
runtime, or shared libraries alongside your own release, that distribution has
additional obligations. Review the exact artifacts' license and notice files,
provide source where their licenses require it, and do not assume this
repository's Apache-2.0 license covers them.

Upstream references:

- QEMU license: <https://gitlab.com/qemu-project/qemu/-/blob/master/LICENSE>
- TianoCore EDK II license: <https://github.com/tianocore/edk2/blob/master/License-History.txt>
- CirrOS 0.6.3 downloads and checksums: <https://download.cirros-cloud.net/0.6.3/>
- CirrOS 0.6.3 source: <https://github.com/cirros-dev/cirros/tree/0.6.3>
- GNU GPL aggregate guidance: <https://www.gnu.org/licenses/gpl-faq.html#MereAggregation>
- `rules_python`: <https://registry.bazel.build/modules/rules_python>
- `platforms`: <https://registry.bazel.build/modules/platforms/1.0.0>
- `buildifier_prebuilt`: <https://registry.bazel.build/modules/buildifier_prebuilt/8.5.1.2>
- Python licensing: <https://docs.python.org/3.12/license.html>
