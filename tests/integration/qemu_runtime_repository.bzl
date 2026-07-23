"""Creates the pinned, test-only QEMU runtime used by integration tests."""

_BUILD_FILE = """\
package(default_visibility = ["//visibility:public"])

exports_files([
    "PACKAGES.txt",
    "root/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd",
    "root/usr/share/AAVMF/AAVMF_VARS.fd",
    "root/usr/share/OVMF/OVMF_CODE_4M.fd",
    "root/usr/share/OVMF/OVMF_VARS_4M.fd",
    "root/usr/share/efi-shell-aa64/shellaa64.efi",
    "root/usr/share/efi-shell-x64/shellx64.efi",
])

filegroup(
    name = "runtime",
    srcs = glob([
        "root/lib/x86_64-linux-gnu/**",
        "root/usr/bin/qemu-system-aarch64",
        "root/usr/bin/qemu-system-x86_64",
        "root/usr/lib/ipxe/qemu/**",
        "root/usr/lib/x86_64-linux-gnu/**",
        "root/usr/share/qemu-efi-aarch64/**",
        "root/usr/share/qemu/**",
        "root/usr/share/seabios/**",
    ]),
)

filegroup(
    name = "licenses",
    srcs = glob(["root/usr/share/doc/**/copyright"]),
)
"""

_SNAPSHOT_PREFIX = "https://snapshot.ubuntu.com/ubuntu/20260720T000000Z/"
_FIRMWARE_DATA = {
    "root/usr/lib/ipxe/qemu": [
        "efi-e1000.rom",
        "efi-e1000e.rom",
        "efi-eepro100.rom",
        "efi-ne2k_pci.rom",
        "efi-pcnet.rom",
        "efi-rtl8139.rom",
        "efi-virtio.rom",
        "efi-vmxnet3.rom",
        "pxe-e1000.rom",
        "pxe-e1000e.rom",
        "pxe-eepro100.rom",
        "pxe-ne2k_pci.rom",
        "pxe-pcnet.rom",
        "pxe-rtl8139.rom",
        "pxe-virtio.rom",
        "pxe-vmxnet3.rom",
    ],
    "root/usr/share/seabios": [
        "acpi-dsdt.aml",
        "bios-256k.bin",
        "bios-microvm.bin",
        "bios.bin",
        "vgabios-ati.bin",
        "vgabios-bochs-display.bin",
        "vgabios-cirrus.bin",
        "vgabios-isavga.bin",
        "vgabios-qxl.bin",
        "vgabios-ramfb.bin",
        "vgabios-stdvga.bin",
        "vgabios-virtio.bin",
        "vgabios-vmware.bin",
    ],
}
_REQUIRED_FILES = [
    "root/usr/bin/qemu-system-aarch64",
    "root/usr/bin/qemu-system-x86_64",
    "root/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
    "root/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd",
    "root/usr/share/AAVMF/AAVMF_VARS.fd",
    "root/usr/share/OVMF/OVMF_CODE_4M.fd",
    "root/usr/share/OVMF/OVMF_VARS_4M.fd",
    "root/usr/share/efi-shell-aa64/shellaa64.efi",
    "root/usr/share/efi-shell-x64/shellx64.efi",
    "root/usr/share/qemu/bios-256k.bin",
    "root/usr/share/qemu/efi-virtio.rom",
    "root/usr/share/qemu/pxe-virtio.rom",
]

def _validate_package(package):
    for field in ["arch", "name", "sha256", "urls", "version"]:
        if field not in package:
            fail("QEMU runtime lock package is missing %r" % field)

    sha256 = package["sha256"]
    if len(sha256) != 64:
        fail("invalid SHA-256 for QEMU runtime package %s" % package["name"])

    if package["arch"] != "amd64":
        fail("QEMU runtime package %s is not locked for amd64" % package["name"])
    if len(package["urls"]) != 1:
        fail("QEMU runtime package %s must have exactly one URL" % package["name"])
    if not package["urls"][0].startswith(_SNAPSHOT_PREFIX):
        fail("QEMU runtime package %s is not from the pinned snapshot" % package["name"])

def _find_data_archive(repository_ctx, package_dir, package_name):
    archives = [
        entry
        for entry in repository_ctx.path(package_dir).readdir()
        if entry.basename == "data.tar" or entry.basename.startswith("data.tar.")
    ]
    if len(archives) != 1:
        fail(
            "expected one data archive in QEMU runtime package %s, found %d" %
            (package_name, len(archives)),
        )
    return archives[0]

def _link_firmware_data(repository_ctx):
    """Recreates the distro QEMU firmware lookup view without host paths."""

    for source_dir, filenames in _FIRMWARE_DATA.items():
        for filename in filenames:
            source = "%s/%s" % (source_dir, filename)
            destination = "root/usr/share/qemu/%s" % filename
            if repository_ctx.path(destination).exists:
                continue
            if not repository_ctx.path(source).exists:
                fail("QEMU runtime is missing firmware data file %s" % source)
            repository_ctx.symlink(repository_ctx.path(source), destination)

def _qemu_runtime_repository_impl(repository_ctx):
    lock = json.decode(repository_ctx.read(repository_ctx.attr.lock))
    if lock.get("version") != 1:
        fail("unsupported QEMU runtime lock version")

    packages = lock.get("packages", [])
    if not packages:
        fail("QEMU runtime lock contains no packages")

    manifest = [
        "# Generated from %s; do not edit.\n" % repository_ctx.attr.lock,
        "# NAME\tVERSION\tARCH\tSHA256\tURL\n",
    ]
    for index, package in enumerate(packages):
        _validate_package(package)
        repository_ctx.report_progress(
            "Fetching pinned QEMU runtime package %d/%d: %s" %
            (index + 1, len(packages), package["name"]),
        )
        package_dir = "_packages/%d" % index
        repository_ctx.download_and_extract(
            url = package["urls"],
            output = package_dir,
            sha256 = package["sha256"],
            type = "deb",
        )
        repository_ctx.extract(
            archive = _find_data_archive(
                repository_ctx,
                package_dir,
                package["name"],
            ),
            output = "root",
        )
        repository_ctx.delete(package_dir)
        manifest.append(
            "%s\t%s\t%s\t%s\t%s\n" %
            (
                package["name"],
                package["version"],
                package["arch"],
                package["sha256"],
                package["urls"][0],
            ),
        )

    repository_ctx.delete("_packages")
    _link_firmware_data(repository_ctx)
    for required_file in _REQUIRED_FILES:
        if not repository_ctx.path(required_file).exists:
            fail("QEMU runtime is missing required file %s" % required_file)
    repository_ctx.file("PACKAGES.txt", "".join(manifest), executable = False)
    repository_ctx.file("BUILD.bazel", _BUILD_FILE, executable = False)

qemu_runtime_repository = repository_rule(
    implementation = _qemu_runtime_repository_impl,
    attrs = {
        "lock": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
        ),
    },
)
