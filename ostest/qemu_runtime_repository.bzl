"""Creates a content-pinned public QEMU/firmware runtime repository."""

_BUILD_FILE = """\
load(":qemu_launcher.bzl", "qemu_launcher")

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
    name = "qemu_firmware_dir",
    srcs = ["root/usr/share/qemu/.rules_ostest_dir"],
    data = [":runtime"],
)

filegroup(
    name = "licenses",
    srcs = glob(["root/usr/share/doc/**/copyright"]),
)

alias(
    name = "ovmf_code",
    actual = ":root/usr/share/OVMF/OVMF_CODE_4M.fd",
)

alias(
    name = "ovmf_vars",
    actual = ":root/usr/share/OVMF/OVMF_VARS_4M.fd",
)

alias(
    name = "aavmf_code",
    actual = ":root/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd",
)

alias(
    name = "aavmf_vars",
    actual = ":root/usr/share/AAVMF/AAVMF_VARS.fd",
)

alias(
    name = "efi_shell_x64",
    actual = ":root/usr/share/efi-shell-x64/shellx64.efi",
)

alias(
    name = "efi_shell_aa64",
    actual = ":root/usr/share/efi-shell-aa64/shellaa64.efi",
)

qemu_launcher(
    name = "qemu_system_x86_64",
    licenses = ":licenses",
    packages = "PACKAGES.txt",
    runtime = ":runtime",
    script = "qemu_system_x86_64.sh",
)

qemu_launcher(
    name = "qemu_system_aarch64",
    licenses = ":licenses",
    packages = "PACKAGES.txt",
    runtime = ":runtime",
    script = "qemu_system_aarch64.sh",
)
"""

_LAUNCHER_RULE = """\
\"\"\"Minimal self-contained executable rule for generated QEMU launchers.\"\"\"

def _qemu_launcher_impl(ctx):
    script = ctx.executable.script
    launcher = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.symlink(
        output = launcher,
        target_file = script,
        is_executable = True,
    )
    runfiles = ctx.runfiles(files = [ctx.file.packages, script])
    runfiles = runfiles.merge(ctx.runfiles(files = ctx.files.runtime))
    runfiles = runfiles.merge(ctx.runfiles(files = ctx.files.licenses))
    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

qemu_launcher = rule(
    implementation = _qemu_launcher_impl,
    executable = True,
    attrs = {
        "licenses": attr.label(allow_files = True),
        "packages": attr.label(allow_single_file = True),
        "runtime": attr.label(allow_files = True),
        "script": attr.label(
            allow_single_file = True,
            cfg = "exec",
            executable = True,
        ),
    },
)
"""

_LAUNCHER = """\
#!/bin/sh
# Executes one QEMU binary using only this repository's declared closure.
set -eu

repository=\"__RULES_OSTEST_REPOSITORY__\"
qemu_binary=\"__RULES_OSTEST_QEMU_BINARY__\"

# bazel run may invoke the executable symlink directly rather than exporting
# RUNFILES_DIR. Derive Bazel's sibling runfiles tree without consulting PATH.
if [ -z \"${RUNFILES_DIR:-}\" ] && [ -d \"$0.runfiles\" ]; then
    RUNFILES_DIR=\"$0.runfiles\"
fi
if [ -z \"${RUNFILES_MANIFEST_FILE:-}\" ] && [ -f \"$0.runfiles_manifest\" ]; then
    RUNFILES_MANIFEST_FILE=\"$0.runfiles_manifest\"
fi

runfile() {
    key=\"$1\"
    if [ -n \"${RUNFILES_DIR:-}\" ] && [ -e \"${RUNFILES_DIR}/${key}\" ]; then
        printf '%s\\n' \"${RUNFILES_DIR}/${key}\"
        return 0
    fi
    if [ -n \"${RUNFILES_MANIFEST_FILE:-}\" ]; then
        while IFS=' ' read -r manifest_key manifest_path; do
            if [ \"${manifest_key}\" = \"${key}\" ]; then
                printf '%s\\n' \"${manifest_path}\"
                return 0
            fi
        done < \"${RUNFILES_MANIFEST_FILE}\"
    fi
    printf '%s\\n' \"QEMU runtime runfile is missing: ${key}\" >&2
    return 1
}

loader=$(runfile \"${repository}/root/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2\")
qemu=$(runfile \"${repository}/root/usr/bin/${qemu_binary}\")
runtime_root=${loader%/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2}
library_path=\"${runtime_root}/lib/x86_64-linux-gnu:${runtime_root}/usr/lib/x86_64-linux-gnu\"
export QEMU_MODULE_DIR=\"${runtime_root}/usr/lib/x86_64-linux-gnu/qemu\"
exec \"${loader}\" --library-path \"${library_path}\" \"${qemu}\" \"$@\"
"""

def _launcher_source(repository_name, qemu_binary):
    return _LAUNCHER.replace("__RULES_OSTEST_REPOSITORY__", repository_name).replace("__RULES_OSTEST_QEMU_BINARY__", qemu_binary)

_SNAPSHOT_PREFIX = "https://snapshot.ubuntu.com/ubuntu/20260720T000000Z/"
_EXPECTED_PACKAGE_COUNT = 90
_LOWER_HEX = "0123456789abcdef"
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
    "root/usr/share/qemu/.rules_ostest_dir",
    "root/usr/share/qemu/bios-256k.bin",
    "root/usr/share/qemu/efi-virtio.rom",
    "root/usr/share/qemu/pxe-virtio.rom",
]

def _validate_package(package):
    for field in ["arch", "dependencies", "key", "name", "sha256", "urls", "version"]:
        if field not in package:
            fail("QEMU runtime lock package is missing %r" % field)

    sha256 = package["sha256"]
    if len(sha256) != 64:
        fail("invalid SHA-256 for QEMU runtime package %s" % package["name"])
    for index in range(len(sha256)):
        if sha256[index] not in _LOWER_HEX:
            fail("invalid SHA-256 for QEMU runtime package %s" % package["name"])

    if package["arch"] != "amd64":
        fail("QEMU runtime package %s is not locked for amd64" % package["name"])
    if len(package["urls"]) != 1:
        fail("QEMU runtime package %s must have exactly one URL" % package["name"])
    if not package["urls"][0].startswith(_SNAPSHOT_PREFIX):
        fail("QEMU runtime package %s is not from the pinned snapshot" % package["name"])

def _validate_lock(packages):
    """Rejects a truncated or ambiguous package closure before downloading it."""

    if len(packages) != _EXPECTED_PACKAGE_COUNT:
        fail(
            "QEMU runtime lock must preserve the %d-package Noble closure, got %d" %
            (_EXPECTED_PACKAGE_COUNT, len(packages)),
        )

    keys = {}
    identities = {}
    for package in packages:
        _validate_package(package)
        key = package["key"]
        identity = "%s|%s|%s" % (package["name"], package["version"], package["arch"])
        if key in keys:
            fail("QEMU runtime lock has duplicate package key %s" % key)
        if identity in identities:
            fail("QEMU runtime lock has duplicate package identity %s" % identity)
        keys[key] = True
        identities[identity] = True

    for package in packages:
        for dependency in package["dependencies"]:
            if "key" not in dependency or dependency["key"] not in keys:
                fail(
                    "QEMU runtime package %s has an unlocked dependency %s" %
                    (package["name"], dependency.get("name", "<unnamed>")),
                )

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
    _validate_lock(packages)

    manifest = [
        "# Generated from %s; do not edit.\n" % repository_ctx.attr.lock,
        "# NAME\tVERSION\tARCH\tSHA256\tURL\n",
    ]
    for index, package in enumerate(packages):
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
    repository_ctx.file(
        "root/usr/share/qemu/.rules_ostest_dir",
        "",
        executable = False,
    )
    for required_file in _REQUIRED_FILES:
        if not repository_ctx.path(required_file).exists:
            fail("QEMU runtime is missing required file %s" % required_file)
    repository_ctx.file("PACKAGES.txt", "".join(manifest), executable = False)
    repository_ctx.file(
        "qemu_system_x86_64.sh",
        _launcher_source(repository_ctx.name, "qemu-system-x86_64"),
        executable = True,
    )
    repository_ctx.file(
        "qemu_system_aarch64.sh",
        _launcher_source(repository_ctx.name, "qemu-system-aarch64"),
        executable = True,
    )
    repository_ctx.file("qemu_launcher.bzl", _LAUNCHER_RULE, executable = False)
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
