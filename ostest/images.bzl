"""Deterministic filesystem and raw disk image rules."""

GptPartitionInfo = provider(
    doc = "Metadata and contents for one partition consumed by gpt_image.",
    fields = {
        "alignment_lba": "Required starting-LBA alignment.",
        "attributes": "64-bit GPT partition attributes.",
        "image": "Sector-aligned partition image File.",
        "image_compression": "Compression of the partition image: auto, gzip, or none.",
        "partition_name": "GPT partition name.",
        "patch_fat_hidden_sectors": "Whether to patch a FAT BPB for its final LBA.",
        "start_lba": "Explicit starting LBA, or zero for automatic layout.",
        "type_guid": "GPT partition type UUID.",
        "unique_guid": "Explicit unique UUID, or empty for a deterministic derived UUID.",
    },
)

MbrPartitionInfo = provider(
    doc = "Metadata and contents for one primary MBR partition.",
    fields = {
        "alignment_lba": "Required starting-LBA alignment.",
        "bootable": "Whether the legacy active flag is set.",
        "image": "Sector-aligned partition image File.",
        "image_compression": "Compression of the partition image.",
        "patch_fat_hidden_sectors": "Whether to patch a FAT BPB for its final LBA.",
        "start_lba": "Explicit starting LBA, or zero for automatic layout.",
        "type_id": "One-byte MBR partition type.",
    },
)

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

def _fat_image_impl(ctx):
    if ctx.attr.size_mb < 34 or ctx.attr.size_mb > 4096:
        fail("size_mb must be between 34 and 4096")
    output = ctx.outputs.out
    if output == None:
        suffix = ".fat.gz" if ctx.attr.compression == "gzip" else ".fat"
        output = ctx.actions.declare_file(ctx.label.name + suffix)

    destinations = {}
    entries = []
    inputs = []
    for target, destination in ctx.attr.files.items():
        source = _one_file(target, "files")
        normalized = destination.replace("\\", "/").strip("/")
        if not normalized:
            fail("files entry %s has an empty destination" % target.label)
        destination_key = normalized.lower()
        if destination_key in destinations:
            fail("duplicate case-insensitive FAT destination %r" % normalized)
        destinations[destination_key] = True
        entries.append({
            "destination": normalized,
            "source": source.path,
        })
        inputs.append(source)

    entries = sorted(entries, key = _destination_sort_key)
    manifest = ctx.actions.declare_file(ctx.label.name + ".fat_manifest.json")
    ctx.actions.write(manifest, json.encode({"files": entries}))

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
        mnemonic = "Fat32Image",
        progress_message = "Assembling FAT32 filesystem %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

fat_image = rule(
    implementation = _fat_image_impl,
    attrs = {
        "files": attr.label_keyed_string_dict(
            allow_files = True,
            doc = "Map of one-file targets to absolute-in-filesystem destination paths.",
        ),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.fat.gz or <name>.fat."),
        "compression": attr.string(
            default = "gzip",
            values = ["gzip", "none"],
            doc = "CAS representation. gzip is deterministic and materialized before use.",
        ),
        "size_mb": attr.int(
            default = 64,
            doc = "Filesystem size in MiB. At least 34 MiB is needed for a conforming FAT32 image.",
        ),
        "volume_id": attr.int(
            default = 0x4f535456,
            doc = "Deterministic 32-bit FAT volume identifier.",
        ),
        "volume_label": attr.string(
            default = "OSTEST",
            doc = "FAT volume label (up to 11 printable ASCII characters).",
        ),
        "_fat_image_tool": attr.label(
            default = Label("//ostest/private:fat_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Builds a deterministic FAT32 filesystem using only declared Bazel inputs.",
)

def _raw_image_impl(ctx):
    output = ctx.outputs.out
    if output == None:
        suffix = ".img.gz" if ctx.attr.compression == "gzip" else ".img"
        output = ctx.actions.declare_file(ctx.label.name + suffix)

    entries = []
    inputs = []
    for target, offset in ctx.attr.blobs.items():
        source = _one_file(target, "blobs")
        entries.append({
            "offset": offset,
            "source": source.path,
        })
        inputs.append(source)

    manifest = ctx.actions.declare_file(ctx.label.name + ".raw_manifest.json")
    ctx.actions.write(manifest, json.encode({"blobs": entries}))

    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--output", output)
    args.add("--size-mb", ctx.attr.size_mb)
    args.add("--compression", ctx.attr.compression)

    ctx.actions.run(
        executable = ctx.executable._raw_image_tool,
        arguments = [args],
        inputs = depset([manifest] + inputs),
        tools = [ctx.attr._raw_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "RawImage",
        progress_message = "Assembling raw image %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

raw_image = rule(
    implementation = _raw_image_impl,
    attrs = {
        "blobs": attr.label_keyed_string_dict(
            allow_files = True,
            doc = "Map of one-file targets to byte offsets (plain bytes, KiB, MiB, or sectors, e.g. 1MiB or 2048s).",
        ),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.img.gz or <name>.img."),
        "compression": attr.string(
            default = "gzip",
            values = ["gzip", "none"],
            doc = "CAS representation; gzip is deterministic.",
        ),
        "size_mb": attr.int(mandatory = True, doc = "Final raw image size in MiB."),
        "_raw_image_tool": attr.label(
            default = Label("//ostest/private:raw_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Places declared blobs at fixed offsets in a deterministic sparse raw image.",
)

def _gpt_partition_impl(ctx):
    if ctx.attr.alignment_lba <= 0:
        fail("alignment_lba must be positive")
    if ctx.attr.start_lba < 0:
        fail("start_lba may not be negative")
    if not ctx.attr.type_guid:
        fail("type_guid must not be empty")
    image = ctx.file.image
    return [
        DefaultInfo(files = depset([image])),
        GptPartitionInfo(
            alignment_lba = ctx.attr.alignment_lba,
            attributes = ctx.attr.attributes,
            image = image,
            image_compression = ctx.attr.image_compression,
            partition_name = ctx.attr.partition_name,
            patch_fat_hidden_sectors = ctx.attr.patch_fat_hidden_sectors,
            start_lba = ctx.attr.start_lba,
            type_guid = ctx.attr.type_guid,
            unique_guid = ctx.attr.unique_guid,
        ),
    ]

gpt_partition = rule(
    implementation = _gpt_partition_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Sector-aligned image containing this partition's bytes.",
        ),
        "image_compression": attr.string(
            default = "auto",
            values = ["auto", "gzip", "none"],
            doc = "How to read image; auto recognizes gzip magic bytes.",
        ),
        "type_guid": attr.string(mandatory = True, doc = "GPT partition type UUID."),
        "unique_guid": attr.string(doc = "Optional fixed unique partition UUID."),
        "partition_name": attr.string(default = "", doc = "GPT partition name, up to 36 UTF-16 code units."),
        "attributes": attr.int(default = 0, doc = "GPT partition attribute bits."),
        "alignment_lba": attr.int(default = 2048, doc = "Required starting-LBA alignment."),
        "start_lba": attr.int(
            default = 0,
            doc = "Optional exact starting LBA; zero lays out the partition after its predecessor.",
        ),
        "patch_fat_hidden_sectors": attr.bool(
            default = False,
            doc = "Patch FAT primary/backup BPB hidden-sector fields to the assigned starting LBA.",
        ),
    },
    doc = "Attaches GPT metadata to an independently generated partition image.",
)

def _gpt_image_impl(ctx):
    if ctx.attr.size_mb <= 0:
        fail("size_mb must be positive")
    if not ctx.attr.partitions:
        fail("partitions must not be empty")
    if len(ctx.attr.partitions) > 128:
        fail("GPT supports at most 128 partitions in this ruleset")

    output = ctx.outputs.out
    if output == None:
        suffix = ".img.gz" if ctx.attr.compression == "gzip" else ".img"
        output = ctx.actions.declare_file(ctx.label.name + suffix)
    entries = []
    inputs = []
    for target in ctx.attr.partitions:
        partition = target[GptPartitionInfo]
        entries.append({
            "alignment_lba": partition.alignment_lba,
            "attributes": partition.attributes,
            "image": partition.image.path,
            "image_compression": partition.image_compression,
            "partition_name": partition.partition_name,
            "patch_fat_hidden_sectors": partition.patch_fat_hidden_sectors,
            "start_lba": partition.start_lba,
            "type_guid": partition.type_guid,
            "unique_guid": partition.unique_guid,
        })
        inputs.append(partition.image)

    manifest = ctx.actions.declare_file(ctx.label.name + ".gpt_manifest.json")
    ctx.actions.write(manifest, json.encode({"partitions": entries}))
    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--output", output)
    args.add("--size-mb", ctx.attr.size_mb)
    args.add("--identity", str(ctx.label))
    args.add("--disk-guid", ctx.attr.disk_guid)
    args.add("--compression", ctx.attr.compression)

    ctx.actions.run(
        executable = ctx.executable._gpt_image_tool,
        arguments = [args],
        inputs = depset([manifest] + inputs),
        tools = [ctx.attr._gpt_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "GptImage",
        progress_message = "Composing GPT disk image %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

gpt_image = rule(
    implementation = _gpt_image_impl,
    attrs = {
        "partitions": attr.label_list(
            mandatory = True,
            providers = [GptPartitionInfo],
            doc = "Ordered gpt_partition targets.",
        ),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.img.gz or <name>.img."),
        "compression": attr.string(
            default = "gzip",
            values = ["gzip", "none"],
            doc = "CAS representation; gzip is deterministic and materialized before QEMU use.",
        ),
        "size_mb": attr.int(mandatory = True, doc = "Final GPT disk size in MiB."),
        "disk_guid": attr.string(doc = "Optional fixed disk UUID; derived from the target label when empty."),
        "_gpt_image_tool": attr.label(
            default = Label("//ostest/private:gpt_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Composes ordered sector-aligned partition images into a deterministic GPT disk.",
)

def _uefi_disk_image_impl(ctx):
    if ctx.attr.start_lba <= 0:
        fail("start_lba must be positive")
    if ctx.attr.size_mb <= 0:
        fail("size_mb must be positive")
    output = ctx.outputs.out
    if output == None:
        suffix = ".img.gz" if ctx.attr.compression == "gzip" else ".img"
        output = ctx.actions.declare_file(ctx.label.name + suffix)
    esp = ctx.file.esp

    manifest = ctx.actions.declare_file(ctx.label.name + ".gpt_manifest.json")
    ctx.actions.write(manifest, json.encode({
        "partitions": [{
            "alignment_lba": ctx.attr.start_lba,
            "attributes": 0,
            "image": esp.path,
            "image_compression": "auto",
            "partition_name": ctx.attr.partition_name,
            "patch_fat_hidden_sectors": True,
            "start_lba": ctx.attr.start_lba,
            "type_guid": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
            "unique_guid": ctx.attr.partition_guid,
        }],
    }))

    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--output", output)
    args.add("--size-mb", ctx.attr.size_mb)
    args.add("--identity", str(ctx.label))
    args.add("--disk-guid", ctx.attr.disk_guid)
    args.add("--compression", ctx.attr.compression)

    ctx.actions.run(
        executable = ctx.executable._gpt_image_tool,
        arguments = [args],
        inputs = [esp, manifest],
        tools = [ctx.attr._gpt_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "UefiGptImage",
        progress_message = "Creating UEFI GPT disk image %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

uefi_disk_image = rule(
    implementation = _uefi_disk_image_impl,
    attrs = {
        "esp": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "FAT filesystem image to place in the EFI System Partition.",
        ),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.img.gz or <name>.img."),
        "compression": attr.string(
            default = "gzip",
            values = ["gzip", "none"],
            doc = "CAS representation; gzip is deterministic and materialized by uefi_test.",
        ),
        "size_mb": attr.int(default = 128, doc = "Final GPT disk size in MiB."),
        "start_lba": attr.int(default = 2048, doc = "First LBA of the EFI System Partition."),
        "disk_guid": attr.string(doc = "Optional fixed GPT disk UUID; derived deterministically from the target label when empty."),
        "partition_guid": attr.string(doc = "Optional fixed ESP UUID; derived deterministically from the target label when empty."),
        "partition_name": attr.string(default = "EFI System", doc = "GPT partition name (at most 36 UTF-16 code units)."),
        "_gpt_image_tool": attr.label(
            default = Label("//ostest/private:gpt_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Wraps a FAT image in a deterministic protective-MBR/GPT disk with one ESP.",
)

def _uefi_iso_image_impl(ctx):
    output = ctx.outputs.out
    if output == None:
        suffix = ".iso.gz" if ctx.attr.compression == "gzip" else ".iso"
        output = ctx.actions.declare_file(ctx.label.name + suffix)
    args = ctx.actions.args()
    args.add("--esp", ctx.file.esp)
    args.add("--output", output)
    args.add("--volume-label", ctx.attr.volume_label)
    args.add("--compression", ctx.attr.compression)
    ctx.actions.run(
        executable = ctx.executable._iso_image_tool,
        arguments = [args],
        inputs = [ctx.file.esp],
        tools = [ctx.attr._iso_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "UefiIsoImage",
        progress_message = "Creating UEFI El Torito ISO %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

uefi_iso_image = rule(
    implementation = _uefi_iso_image_impl,
    attrs = {
        "esp": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "FAT boot image exposed through the UEFI El Torito catalog.",
        ),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.iso.gz or <name>.iso."),
        "compression": attr.string(
            default = "gzip",
            values = ["gzip", "none"],
            doc = "CAS representation; gzip is deterministic and materialized by the test harness.",
        ),
        "volume_label": attr.string(default = "OSTEST", doc = "ISO9660 volume identifier."),
        "_iso_image_tool": attr.label(
            default = Label("//ostest/private:iso_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Builds a deterministic ISO9660 image with a UEFI El Torito FAT boot image.",
)

def _mbr_partition_impl(ctx):
    if ctx.attr.alignment_lba <= 0 or ctx.attr.start_lba < 0:
        fail("alignment_lba must be positive and start_lba must not be negative")
    if ctx.attr.type_id <= 0 or ctx.attr.type_id > 255:
        fail("type_id must be between 1 and 255")
    image = ctx.file.image
    return [
        DefaultInfo(files = depset([image])),
        MbrPartitionInfo(
            alignment_lba = ctx.attr.alignment_lba,
            bootable = ctx.attr.bootable,
            image = image,
            image_compression = ctx.attr.image_compression,
            patch_fat_hidden_sectors = ctx.attr.patch_fat_hidden_sectors,
            start_lba = ctx.attr.start_lba,
            type_id = ctx.attr.type_id,
        ),
    ]

mbr_partition = rule(
    implementation = _mbr_partition_impl,
    attrs = {
        "image": attr.label(mandatory = True, allow_single_file = True),
        "image_compression": attr.string(default = "auto", values = ["auto", "gzip", "none"]),
        "type_id": attr.int(default = 0xEF, doc = "MBR partition type; 0xEF is an EFI System Partition."),
        "bootable": attr.bool(default = False, doc = "Set the legacy active flag when compatibility requires it."),
        "patch_fat_hidden_sectors": attr.bool(
            default = False,
            doc = "Patch FAT primary/backup BPB hidden-sector fields to the assigned starting LBA.",
        ),
        "alignment_lba": attr.int(default = 2048),
        "start_lba": attr.int(default = 0),
    },
    doc = "Attaches primary-MBR metadata to an independently generated partition image.",
)

def _mbr_image_impl(ctx):
    if not ctx.attr.partitions or len(ctx.attr.partitions) > 4:
        fail("partitions must contain one to four mbr_partition targets")
    output = ctx.outputs.out
    if output == None:
        suffix = ".img.gz" if ctx.attr.compression == "gzip" else ".img"
        output = ctx.actions.declare_file(ctx.label.name + suffix)
    entries = []
    inputs = []
    for target in ctx.attr.partitions:
        partition = target[MbrPartitionInfo]
        entries.append({
            "alignment_lba": partition.alignment_lba,
            "bootable": partition.bootable,
            "image": partition.image.path,
            "image_compression": partition.image_compression,
            "patch_fat_hidden_sectors": partition.patch_fat_hidden_sectors,
            "start_lba": partition.start_lba,
            "type_id": partition.type_id,
        })
        inputs.append(partition.image)
    manifest = ctx.actions.declare_file(ctx.label.name + ".mbr_manifest.json")
    ctx.actions.write(manifest, json.encode({"partitions": entries}))
    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--output", output)
    args.add("--size-mb", ctx.attr.size_mb)
    args.add("--compression", ctx.attr.compression)
    ctx.actions.run(
        executable = ctx.executable._mbr_image_tool,
        arguments = [args],
        inputs = depset([manifest] + inputs),
        tools = [ctx.attr._mbr_image_tool[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONHASHSEED": "0"},
        mnemonic = "MbrImage",
        progress_message = "Composing MBR disk image %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

mbr_image = rule(
    implementation = _mbr_image_impl,
    attrs = {
        "partitions": attr.label_list(mandatory = True, providers = [MbrPartitionInfo]),
        "size_mb": attr.int(mandatory = True),
        "out": attr.output(doc = "Optional output filename; defaults to <name>.img.gz or <name>.img."),
        "compression": attr.string(default = "gzip", values = ["gzip", "none"]),
        "_mbr_image_tool": attr.label(
            default = Label("//ostest/private:mbr_image_tool"),
            cfg = "exec",
            executable = True,
        ),
    },
    doc = "Composes one to four primary partitions into a deterministic UEFI-compatible MBR disk.",
)
