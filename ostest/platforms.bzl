"""Guest-platform transitions and platform-aware UEFI image helpers."""

def _guest_platform_transition_impl(_settings, attr):
    return {
        "//command_line_option:platforms": [str(attr.guest_platform)],
    }

_guest_platform_transition = transition(
    implementation = _guest_platform_transition_impl,
    inputs = [],
    outputs = ["//command_line_option:platforms"],
)

def _guest_arch(ctx):
    if ctx.target_platform_has_constraint(ctx.attr._x86_64[platform_common.ConstraintValueInfo]):
        return "x86_64"
    if ctx.target_platform_has_constraint(ctx.attr._aarch64[platform_common.ConstraintValueInfo]):
        return "aarch64"
    fail("guest_platform %s must contain @platforms//cpu:x86_64 or @platforms//cpu:aarch64" % ctx.attr.guest_platform.label)

def _one_file(target, attribute_name):
    files = target[DefaultInfo].files.to_list()
    if len(files) != 1:
        fail("%s entry %s must produce exactly one file, got %d" % (
            attribute_name,
            target.label,
            len(files),
        ))
    return files[0]

def _destination_sort_key(entry):
    return entry["destination"].lower()

def _guest_arch_file_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.write(output, _guest_arch(ctx) + "\n")
    return [DefaultInfo(files = depset([output]))]

guest_arch_file = rule(
    implementation = _guest_arch_file_impl,
    cfg = _guest_platform_transition,
    attrs = {
        "guest_platform": attr.label(mandatory = True, doc = "Standard Bazel platform describing the guest."),
        "_aarch64": attr.label(default = "@platforms//cpu:aarch64"),
        "_x86_64": attr.label(default = "@platforms//cpu:x86_64"),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
    doc = "Produces the normalized guest architecture selected by guest_platform.",
)

def _platform_uefi_esp_image_impl(ctx):
    if ctx.attr.size_mb < 34 or ctx.attr.size_mb > 4096:
        fail("size_mb must be between 34 and 4096")
    arch = _guest_arch(ctx)
    boot_filename = "BOOTX64.EFI" if arch == "x86_64" else "BOOTAA64.EFI"
    output = ctx.outputs.out
    if output == None:
        suffix = ".fat.gz" if ctx.attr.compression == "gzip" else ".fat"
        output = ctx.actions.declare_file(ctx.label.name + suffix)

    entries = [{
        "destination": "EFI/BOOT/" + boot_filename,
        "source": ctx.file.efi_binary.path,
    }]
    inputs = [ctx.file.efi_binary]
    destinations = {("EFI/BOOT/" + boot_filename).lower(): True}
    for target, destination in ctx.attr.files.items():
        source = _one_file(target, "files")
        normalized = destination.replace("\\", "/").strip("/")
        if not normalized:
            fail("files entry %s has an empty destination" % target.label)
        key = normalized.lower()
        if key in destinations:
            fail("duplicate case-insensitive FAT destination %r" % normalized)
        destinations[key] = True
        entries.append({"destination": normalized, "source": source.path})
        inputs.append(source)

    manifest = ctx.actions.declare_file(ctx.label.name + ".fat_manifest.json")
    ctx.actions.write(manifest, json.encode({
        "files": sorted(entries, key = _destination_sort_key),
    }))
    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--output", output)
    args.add("--size-mb", ctx.attr.size_mb)
    args.add("--volume-label", ctx.attr.volume_label)
    args.add("--volume-id", ctx.attr.volume_id)
    args.add("--compression", ctx.attr.compression)
    ctx.actions.run(
        executable = ctx.executable._fat_image_tool,
        arguments = [args],
        inputs = depset([manifest] + inputs),
        tools = [ctx.attr._fat_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "PlatformUefiEspImage",
        progress_message = "Assembling %{label} for " + arch,
    )
    return [DefaultInfo(files = depset([output]))]

platform_uefi_esp_image = rule(
    implementation = _platform_uefi_esp_image_impl,
    cfg = _guest_platform_transition,
    attrs = {
        "guest_platform": attr.label(mandatory = True, doc = "Standard Bazel platform describing the guest."),
        "efi_binary": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "UEFI binary built under the guest-platform transition.",
        ),
        "files": attr.label_keyed_string_dict(allow_files = True),
        "out": attr.output(),
        "compression": attr.string(default = "gzip", values = ["gzip", "none"]),
        "size_mb": attr.int(default = 64),
        "volume_id": attr.int(default = 0x4f535456),
        "volume_label": attr.string(default = "OSTEST"),
        "_aarch64": attr.label(default = "@platforms//cpu:aarch64"),
        "_x86_64": attr.label(default = "@platforms//cpu:x86_64"),
        "_fat_image_tool": attr.label(
            default = Label("//ostest/private:fat_image_tool"),
            cfg = "exec",
            executable = True,
        ),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
    doc = "Builds an ESP and infers its UEFI fallback name from guest_platform.",
)
