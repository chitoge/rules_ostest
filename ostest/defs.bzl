"""Public API for rules_ostest."""

load("@rules_python//python:py_test.bzl", "py_test")
load(
    ":images.bzl",
    _fat_image = "fat_image",
    _gpt_image = "gpt_image",
    _gpt_partition = "gpt_partition",
    _mbr_image = "mbr_image",
    _mbr_partition = "mbr_partition",
    _raw_image = "raw_image",
    _uefi_disk_image = "uefi_disk_image",
    _uefi_iso_image = "uefi_iso_image",
)
load(
    ":platforms.bzl",
    _guest_arch_file = "guest_arch_file",
    _platform_uefi_esp_image = "platform_uefi_esp_image",
)

fat_image = _fat_image
gpt_image = _gpt_image
gpt_partition = _gpt_partition
mbr_image = _mbr_image
mbr_partition = _mbr_partition
raw_image = _raw_image
uefi_disk_image = _uefi_disk_image
uefi_iso_image = _uefi_iso_image
platform_uefi_esp_image = _platform_uefi_esp_image

SUPPORTED_ARCHITECTURES = ["x86_64", "aarch64"]

_ARCH_ALIASES = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "x64": "x86_64",
    "x86_64": "x86_64",
}

_BOOT_FILENAMES = {
    "aarch64": "BOOTAA64.EFI",
    "x86_64": "BOOTX64.EFI",
}

_QEMU_TEST_RUNNER = Label("//ostest/private:qemu_test_runner.py")
_QEMU_TEST_LIBRARY = Label("//ostest/python:qemu_testlib")
_MANAGED_NETWORK_LIBRARY = Label("//ostest/private:managed_network")
_QEMU_RUNNER_SUPPORT_LIBRARY = Label("//ostest/private:qemu_runner_support")

def _normalize_arch(arch):
    normalized = _ARCH_ALIASES.get(arch.lower())
    if normalized == None:
        fail("unsupported architecture %r; expected one of %s" % (arch, SUPPORTED_ARCHITECTURES))
    return normalized

def _deduplicate_labels(labels):
    result = []
    seen = {}
    for label in labels:
        key = str(label)
        if key not in seen:
            seen[key] = True
            result.append(label)
    return result

def _shell_quote(value):
    """Quotes one py_* args element for rules_python's shell tokenization."""
    return "'" + value.replace("'", "'\"'\"'") + "'"

def qemu_media(
        image,
        name = "",
        interface = "virtio-blk",
        image_format = "raw",
        compression = "auto",
        readonly = None,
        snapshot = None,
        bootindex = 1,
        export = False):
    """Describes one boot medium for uefi_*_test media lists.

    Supported interfaces are virtio-blk, usb-storage, nvme, and cdrom. The
    returned value is configuration metadata, not a standalone Bazel target.

    Args:
      image: Label of the one-file target containing the medium.
      name: Optional logical device name; an indexed name is generated when
        empty.
      interface: QEMU attachment type: virtio-blk, usb-storage, nvme, or cdrom.
      image_format: Explicit QEMU image format, raw or qcow2.
      compression: Input compression: auto, gzip, or none.
      readonly: Whether QEMU must expose the medium read-only. Defaults to true
        for cdrom and false otherwise.
      snapshot: Whether writes use a temporary QEMU snapshot. Defaults to true
        for writable non-cdrom media and is disabled for read-only media.
      bootindex: Positive, VM-unique QEMU boot index, or None for non-bootable
        media.
      export: Publish the post-stop image from a serial uefi_test as a Bazel
        test artifact. This requires writable, non-snapshot media. Scripted
        tests should call QemuSession.export_media instead.

    Returns:
      An immutable media configuration struct consumed by the test macros.
    """
    if interface not in ["virtio-blk", "usb-storage", "nvme", "cdrom"]:
        fail("unsupported QEMU media interface %r" % interface)
    if image_format not in ["raw", "qcow2"]:
        fail("unsupported QEMU image format %r" % image_format)
    if compression not in ["auto", "gzip", "none"]:
        fail("unsupported media compression %r" % compression)
    if bootindex != None and bootindex <= 0:
        fail("media bootindex must be positive")
    if readonly == None:
        readonly = interface == "cdrom"
    if snapshot == None:
        snapshot = interface != "cdrom"
    if readonly:
        snapshot = False
    if export and (readonly or snapshot):
        fail("export requires writable media with snapshot=False")
    return struct(
        bootindex = bootindex,
        compression = compression,
        export = export,
        image = image,
        image_format = image_format,
        interface = interface,
        kind = "image",
        name = name,
        readonly = readonly,
        size_mb = None,
        snapshot = snapshot,
    )

def qemu_scratch_disk(
        name = "scratch",
        size_mb = 64,
        interface = "virtio-blk",
        bootindex = None,
        export = False):
    """Describes a fresh writable raw disk created for one QEMU test.

    Args:
      name: Stable logical device name used by readback and artifact APIs.
      size_mb: Positive sparse disk size in MiB.
      interface: QEMU attachment type: virtio-blk, usb-storage, or nvme.
      bootindex: Optional positive, VM-unique QEMU boot index.
      export: Publish the post-stop disk from a serial uefi_test as a Bazel
        test artifact. Scripted tests should call QemuSession.export_media.

    Returns:
      An immutable scratch-media configuration struct.
    """
    if not name:
        fail("scratch disk name must not be empty")
    if size_mb <= 0:
        fail("scratch disk size_mb must be positive")
    if interface not in ["virtio-blk", "usb-storage", "nvme"]:
        fail("unsupported QEMU scratch interface %r" % interface)
    if bootindex != None and bootindex <= 0:
        fail("scratch disk bootindex must be positive")
    return struct(
        bootindex = bootindex,
        compression = "none",
        export = export,
        image = None,
        image_format = "raw",
        interface = interface,
        kind = "scratch",
        name = name,
        readonly = False,
        size_mb = size_mb,
        snapshot = False,
    )

def qemu_hostfwd(guest, host = "auto", protocol = "tcp", name = ""):
    """Describes a loopback host-to-guest QEMU user-network forward.

    Args:
      guest: Guest TCP/UDP port in the range 1..65535.
      host: Host loopback port, or "auto" for a collision-free dynamic port.
      protocol: tcp or udp.
      name: Optional stable mapping name; one is generated when empty.

    Returns:
      An immutable host-forward configuration struct.
    """
    if type(guest) != "int" or guest < 1 or guest > 65535:
        fail("hostfwd guest port must be an integer in 1..65535")
    if host != "auto" and (type(host) != "int" or host < 1 or host > 65535):
        fail("hostfwd host port must be 'auto' or an integer in 1..65535")
    if protocol not in ["tcp", "udp"]:
        fail("hostfwd protocol must be 'tcp' or 'udp'")
    return struct(guest = guest, host = host, name = name, protocol = protocol)

def qemu_network(name = "lan", mac = "", model = "virtio-net-pci"):
    """Describes an isolated Ethernet attachment for a uefi_vm participant.

    Args:
      name: Name of the in-process Ethernet segment to join.
      mac: Optional explicit MAC address; the lab generates one when empty.
      model: QEMU network-device model.

    Returns:
      An immutable network configuration struct consumed by uefi_vm.
    """
    if not name:
        fail("network name must not be empty")
    if not model:
        fail("network device model must not be empty")
    return struct(name = name, mac = mac, model = model)

def _validate_media(disk, media):
    boot_indices = {1: True} if disk != None else {}
    names = {"disk": True} if disk != None else {}
    for index, medium in enumerate(media):
        if medium.bootindex != None:
            if medium.bootindex in boot_indices:
                fail("QEMU media bootindex %d is used more than once" % medium.bootindex)
            boot_indices[medium.bootindex] = True
        name = medium.name if medium.name else "media%d" % index
        if name in names:
            fail("QEMU media name %r is used more than once" % name)
        names[name] = True

def _validate_boot(firmware, boot, kernel, initrd, kernel_args):
    if boot not in ["uefi", "direct-kernel"]:
        fail("boot must be 'uefi' or 'direct-kernel'")
    if boot == "uefi":
        if firmware == None:
            fail("firmware is required for boot='uefi'")
        if kernel != None or initrd != None or kernel_args:
            fail("kernel, initrd, and kernel_args require boot='direct-kernel'")
    elif kernel == None:
        fail("kernel is required for boot='direct-kernel'")

def _validate_hostfwd(hostfwd, qemu_args):
    seen_names = {}
    seen_guest = {}
    seen_static = {}
    for index, forward in enumerate(hostfwd):
        name = forward.name if forward.name else "forward%d" % index
        if not name:
            fail("hostfwd name must not be empty")
        if name[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
            fail("hostfwd name %r must start with a letter" % name)
        for character in name.elems():
            if character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-":
                fail("hostfwd name %r contains an unsupported character" % name)
        if name in seen_names:
            fail("hostfwd name %r is used more than once" % name)
        seen_names[name] = True
        guest_key = "%s:%d" % (forward.protocol, forward.guest)
        if guest_key in seen_guest:
            fail("hostfwd guest endpoint %s is used more than once" % guest_key)
        seen_guest[guest_key] = True
        if forward.host != "auto":
            host_key = "%s:%d" % (forward.protocol, forward.host)
            if host_key in seen_static:
                fail("hostfwd static endpoint %s is used more than once" % host_key)
            seen_static[host_key] = True
    if hostfwd:
        for argument in qemu_args:
            if argument in ["-net", "-netdev", "-nic"]:
                fail("managed hostfwd cannot be combined with %s in qemu_args" % argument)

def uefi_vm(
        name,
        qemu,
        firmware,
        arch = None,
        guest_platform = None,
        disk = None,
        media = [],
        firmware_vars = None,
        networks = [qemu_network()],
        debugcon = False,
        gdb = False,
        pause_at_start = False,
        require_kvm = False,
        memory_mb = 256,
        cpus = 1,
        machine_options = [],
        qemu_args = [],
        boot = "uefi",
        kernel = None,
        initrd = None,
        kernel_args = "",
        cpu_model = None,
        graphics = False,
        graphics_device = None):
    """Describes one VM captured by a uefi_lab_test target.

    Args:
      name: Unique participant name within the lab.
      qemu: Label of the QEMU system executable for the execution platform.
      firmware: Label of the read-only UEFI firmware-code image.
      arch: Guest architecture name. Exactly one of arch and guest_platform is
        required.
      guest_platform: Bazel platform used to infer and transition guest inputs.
      disk: Optional raw boot disk shorthand with boot index 1.
      media: List of qemu_media configurations.
      firmware_vars: Optional writable UEFI variable-store template.
      networks: List of qemu_network attachments.
      debugcon: Capture the x86 OVMF debug console when true.
      gdb: Expose an ephemeral loopback QEMU GDB endpoint when true.
      pause_at_start: Start paused for GDB; requires gdb.
      require_kvm: Disable TCG fallback and require KVM when true.
      memory_mb: Positive guest memory size in MiB.
      cpus: Positive guest virtual CPU count.
      machine_options: QEMU machine key/value properties without commas.
      qemu_args: Additional arguments appended to the QEMU command.
      boot: Boot mode, uefi or direct-kernel.
      kernel: Direct-boot kernel label.
      initrd: Optional direct-boot initial RAM disk label.
      kernel_args: Literal direct-boot kernel command line.
      cpu_model: Optional QEMU CPU model override.
      graphics: Attach a guest display device while remaining host-headless.
      graphics_device: Optional QEMU display-device model override.

    Returns:
      An immutable VM configuration struct consumed by uefi_lab_test.
    """
    if not name:
        fail("VM name must not be empty")
    if (arch == None) == (guest_platform == None):
        fail("uefi_vm requires exactly one of arch and guest_platform")
    if pause_at_start and not gdb:
        fail("pause_at_start requires gdb=True")
    if memory_mb <= 0 or cpus <= 0:
        fail("memory_mb and cpus must be positive")
    if firmware_vars != None and firmware == None:
        fail("firmware_vars requires firmware")
    if graphics_device != None and not graphics:
        fail("graphics_device requires graphics=True")
    if cpu_model != None and (not cpu_model or "," in cpu_model):
        fail("cpu_model must be a non-empty QEMU model name without commas")
    _validate_boot(firmware, boot, kernel, initrd, kernel_args)
    _validate_media(disk, media)
    for medium in media:
        if medium.export:
            fail("uefi_vm does not auto-export media; call QemuSession.export_media from the lab test")
    for option in machine_options:
        if not option or "," in option:
            fail("machine_options entries must be non-empty single QEMU key/value properties")
    return struct(
        arch = arch,
        boot = boot,
        cpus = cpus,
        cpu_model = cpu_model,
        debugcon = debugcon,
        disk = disk,
        firmware = firmware,
        firmware_vars = firmware_vars,
        gdb = gdb,
        graphics = graphics,
        graphics_device = graphics_device,
        guest_platform = guest_platform,
        initrd = initrd,
        kernel = kernel,
        kernel_args = kernel_args,
        media = media,
        machine_options = machine_options,
        memory_mb = memory_mb,
        name = name,
        networks = networks,
        pause_at_start = pause_at_start,
        qemu = qemu,
        qemu_args = qemu_args,
        require_kvm = require_kvm,
    )

def _append_media_runtime(runtime_args, runtime_data, disk, media, argument_prefix):
    if disk != None:
        runtime_args.append("--%sdisk=" % argument_prefix + _shell_quote("$(rlocationpath %s)" % disk))
        runtime_data.append(disk)
    for index, medium in enumerate(media):
        entry = {
            "bootindex": medium.bootindex,
            "compression": medium.compression,
            "export": medium.export,
            "format": medium.image_format,
            "interface": medium.interface,
            "kind": medium.kind,
            "name": medium.name if medium.name else "media%d" % index,
            "readonly": medium.readonly,
            "size_mb": medium.size_mb,
            "snapshot": medium.snapshot,
        }
        if medium.kind == "image":
            runtime_data.append(medium.image)
            entry["path"] = "$(rlocationpath %s)" % medium.image
        encoded = json.encode(entry)
        runtime_args.append("--%smedia=" % argument_prefix + _shell_quote(encoded))

def _append_boot_runtime(runtime_args, runtime_data, argument_prefix, boot, kernel, initrd, kernel_args, cpu_model, graphics, graphics_device):
    runtime_args.append("--%sboot=%s" % (argument_prefix, boot))
    if kernel != None:
        runtime_data.append(kernel)
        runtime_args.append("--%skernel=" % argument_prefix + _shell_quote("$(rlocationpath %s)" % kernel))
    if initrd != None:
        runtime_data.append(initrd)
        runtime_args.append("--%sinitrd=" % argument_prefix + _shell_quote("$(rlocationpath %s)" % initrd))
    if kernel_args:
        runtime_args.append("--%skernel-args=" % argument_prefix + _shell_quote(kernel_args))
    if cpu_model != None:
        runtime_args.append("--%scpu-model=" % argument_prefix + _shell_quote(cpu_model))
    if graphics:
        runtime_args.append("--%sgraphics" % argument_prefix)
    if graphics_device != None:
        runtime_args.append("--%sgraphics-device=" % argument_prefix + _shell_quote(graphics_device))

def _append_hostfwd_runtime(runtime_args, hostfwd, argument_prefix):
    for index, forward in enumerate(hostfwd):
        runtime_args.append("--%shostfwd=" % argument_prefix + _shell_quote(json.encode({
            "guest": forward.guest,
            "host": forward.host,
            "name": forward.name if forward.name else "forward%d" % index,
            "protocol": forward.protocol,
        })))

def uefi_esp_image(name, arch, efi_binary, files = {}, **kwargs):
    """Builds a FAT32 ESP with the UEFI fallback boot filename.

    Args:
      name: Bazel target name.
      arch: x86_64/x64 or aarch64/arm64.
      efi_binary: One-file target containing the UEFI PE executable.
      files: Additional label-to-destination mapping passed to fat_image.
      **kwargs: Additional fat_image attributes.
    """
    normalized_arch = _normalize_arch(arch)
    mappings = dict(files)
    if efi_binary in mappings:
        fail("efi_binary %s is also present in files" % efi_binary)
    mappings[efi_binary] = "EFI/BOOT/" + _BOOT_FILENAMES[normalized_arch]
    fat_image(
        name = name,
        files = mappings,
        **kwargs
    )

def _define_uefi_test(
        name,
        qemu,
        firmware,
        disk = None,
        media = [],
        arch = None,
        arch_file = None,
        firmware_vars = None,
        debugcon = False,
        export_firmware_vars = False,
        require_kvm = False,
        timeout_seconds = 60,
        success_pattern = None,
        failure_pattern = "OSTEST: FAIL",
        memory_mb = 256,
        cpus = 1,
        machine_options = [],
        qemu_args = [],
        success_exit_codes = [],
        test_data = [],
        tags = [],
        success_markers = [],
        forbidden_markers = [],
        phases = [],
        on_failure = None,
        on_failure_timeout_seconds = 30,
        kvm_unavailable = "fail",
        boot = "uefi",
        kernel = None,
        initrd = None,
        kernel_args = "",
        cpu_model = None,
        graphics = False,
        graphics_device = None,
        screendump_not_blank = False,
        screendump_min_distinct_pixels = 2,
        hostfwd = [],
        host_companion = None,
        host_companion_args = [],
        **kwargs):
    if (arch == None) == (arch_file == None):
        fail("exactly one of arch and arch_file must be supplied")
    if success_markers and phases:
        fail("success_markers and phases are mutually exclusive")
    if (success_markers or phases) and success_pattern != None:
        fail("success_pattern cannot be combined with success_markers or phases")
    if (success_markers or phases) and success_exit_codes:
        fail("success_exit_codes cannot replace ordered marker or phase assertions")
    if success_pattern == None and not success_markers and not phases:
        success_pattern = "OSTEST: PASS"
    if success_pattern != None and not success_pattern:
        fail("success_pattern must not be empty")
    for marker_group in [success_markers, forbidden_markers]:
        if type(marker_group) not in ["list", "tuple"]:
            fail("success_markers and forbidden_markers must be lists of strings")
        for marker in marker_group:
            if type(marker) != "string" or not marker:
                fail("success and forbidden markers must be non-empty strings")
    if type(phases) not in ["list", "tuple"]:
        fail("phases must be a list of dictionaries")
    for index, phase in enumerate(phases):
        if type(phase) != "dict":
            fail("phase %d must be a dictionary" % index)
        for key in phase.keys():
            if key not in ["markers", "then"]:
                fail("phase %d has unsupported key %r" % (index, key))
        markers = phase.get("markers", [])
        if type(markers) not in ["list", "tuple"] or not markers:
            fail("phase %d markers must be a non-empty list" % index)
        for marker in markers:
            if type(marker) != "string" or not marker:
                fail("phase markers must be non-empty strings")
        then = phase.get("then", "complete")
        if index < len(phases) - 1 and then != "reboot":
            fail("non-final phase %d must use then='reboot'" % index)
        if index == len(phases) - 1 and then != "complete":
            fail("the final phase must complete rather than reboot")
    if timeout_seconds <= 0:
        fail("timeout_seconds must be positive")
    if on_failure_timeout_seconds <= 0:
        fail("on_failure_timeout_seconds must be positive")
    if memory_mb <= 0:
        fail("memory_mb must be positive")
    if cpus <= 0:
        fail("cpus must be positive")
    if kvm_unavailable not in ["fail", "skip"]:
        fail("kvm_unavailable must be 'fail' or 'skip'")
    if kvm_unavailable == "skip" and not require_kvm:
        fail("kvm_unavailable='skip' requires require_kvm=True")
    if screendump_min_distinct_pixels < 2:
        fail("screendump_min_distinct_pixels must be at least 2")
    if screendump_not_blank and success_exit_codes:
        fail("screendump_not_blank cannot be combined with success_exit_codes")
    if firmware_vars != None and firmware == None:
        fail("firmware_vars requires firmware")
    if graphics_device != None and not graphics:
        fail("graphics_device requires graphics=True")
    if cpu_model != None and (not cpu_model or "," in cpu_model):
        fail("cpu_model must be a non-empty QEMU model name without commas")
    _validate_boot(firmware, boot, kernel, initrd, kernel_args)
    _validate_media(disk, media)
    _validate_hostfwd(hostfwd, qemu_args)
    if phases:
        for argument in qemu_args:
            if argument in ["-action", "-mon", "-monitor", "-no-reboot", "-qmp", "-serial"]:
                fail("reboot phases cannot be combined with %s in qemu_args" % argument)
    if host_companion != None and not hostfwd:
        fail("host_companion requires at least one hostfwd")
    if host_companion_args and host_companion == None:
        fail("host_companion_args requires host_companion")
    for option in machine_options:
        if not option or "," in option:
            fail("machine_options entries must be non-empty single QEMU key/value properties")
    if "args" in kwargs or "data" in kwargs or "main" in kwargs or "srcs" in kwargs:
        fail("uefi_test owns args, data, main, and srcs; use qemu_args or test_data")

    runner_args = []
    if arch != None:
        runner_args.append("--arch=" + _normalize_arch(arch))
    else:
        runner_args.append("--arch-file=" + _shell_quote("$(rlocationpath %s)" % arch_file))
    runner_args.extend([
        "--qemu=" + _shell_quote("$(rlocationpath %s)" % qemu),
        "--timeout-seconds=%d" % timeout_seconds,
        "--failure-pattern=" + _shell_quote(failure_pattern),
        "--memory-mb=%d" % memory_mb,
        "--cpus=%d" % cpus,
    ])
    if success_pattern != None:
        runner_args.append("--success-pattern=" + _shell_quote(success_pattern))
    for marker in success_markers:
        runner_args.append("--success-marker=" + _shell_quote(marker))
    for marker in forbidden_markers:
        runner_args.append("--forbidden-marker=" + _shell_quote(marker))
    for phase in phases:
        runner_args.append("--phase=" + _shell_quote(json.encode({
            "markers": phase["markers"],
            "then": phase.get("then", "complete"),
        })))
    runtime_data = [qemu] + test_data
    if firmware != None:
        runner_args.append("--firmware=" + _shell_quote("$(rlocationpath %s)" % firmware))
        runtime_data.append(firmware)
    _append_boot_runtime(runner_args, runtime_data, "", boot, kernel, initrd, kernel_args, cpu_model, graphics, graphics_device)
    _append_media_runtime(runner_args, runtime_data, disk, media, "")
    _append_hostfwd_runtime(runner_args, hostfwd, "")
    if arch_file != None:
        runtime_data.append(arch_file)
    if firmware_vars != None:
        runner_args.append("--firmware-vars=" + _shell_quote("$(rlocationpath %s)" % firmware_vars))
        runtime_data.append(firmware_vars)
    if debugcon:
        runner_args.append("--debugcon")
    if export_firmware_vars:
        if firmware_vars == None:
            fail("export_firmware_vars requires firmware_vars")
        runner_args.append("--export-firmware-vars")
    if require_kvm:
        runner_args.append("--require-kvm")
    if kvm_unavailable == "skip":
        runner_args.append("--kvm-unavailable=skip")
        if "external" not in tags:
            tags = tags + ["external"]
    if screendump_not_blank:
        runner_args.append("--screendump-not-blank")
        runner_args.append("--screendump-min-distinct-pixels=%d" % screendump_min_distinct_pixels)
    if on_failure != None:
        runtime_data.append(on_failure)
        runner_args.append("--on-failure=" + _shell_quote("$(rlocationpath %s)" % on_failure))
        runner_args.append("--on-failure-timeout-seconds=%d" % on_failure_timeout_seconds)
    if host_companion != None:
        runtime_data.append(host_companion)
        runner_args.append("--host-companion=" + _shell_quote("$(rlocationpath %s)" % host_companion))
        for argument in host_companion_args:
            runner_args.append("--host-companion-arg=" + _shell_quote(argument))
    for option in machine_options:
        runner_args.append("--machine-option=" + _shell_quote(option))
    for arg in qemu_args:
        runner_args.append("--qemu-arg=" + _shell_quote(arg))
    for exit_code in success_exit_codes:
        runner_args.append("--success-exit-code=%d" % exit_code)
    if "legacy_create_init" not in kwargs:
        kwargs["legacy_create_init"] = 0

    py_test(
        name = name,
        srcs = [_QEMU_TEST_RUNNER],
        main = _QEMU_TEST_RUNNER,
        args = runner_args,
        data = _deduplicate_labels(runtime_data),
        deps = [
            _MANAGED_NETWORK_LIBRARY,
            _QEMU_RUNNER_SUPPORT_LIBRARY,
            _QEMU_TEST_LIBRARY,
        ],
        python_version = "3.12",
        tags = tags,
        **kwargs
    )

def uefi_test(
        name,
        arch,
        qemu,
        firmware,
        disk = None,
        media = [],
        firmware_vars = None,
        debugcon = False,
        export_firmware_vars = False,
        require_kvm = False,
        timeout_seconds = 60,
        success_pattern = None,
        failure_pattern = "OSTEST: FAIL",
        memory_mb = 256,
        cpus = 1,
        machine_options = [],
        qemu_args = [],
        success_exit_codes = [],
        test_data = [],
        tags = [],
        success_markers = [],
        forbidden_markers = [],
        phases = [],
        on_failure = None,
        on_failure_timeout_seconds = 30,
        kvm_unavailable = "fail",
        boot = "uefi",
        kernel = None,
        initrd = None,
        kernel_args = "",
        cpu_model = None,
        graphics = False,
        graphics_device = None,
        screendump_not_blank = False,
        screendump_min_distinct_pixels = 2,
        hostfwd = [],
        host_companion = None,
        host_companion_args = [],
        **kwargs):
    """Defines a serial-driven, remote-executable QEMU py_test.

    The guest may pass through a legacy regular expression, ordered literal
    markers, or reboot-aware phases. Forbidden markers, timeouts, unexpected
    exits, failed screendump assertions, and failed host companions fail the
    test. QEMU and every boot input are runfiles; writable state is
    materialized below TEST_TMPDIR and selected media can be exported after
    QEMU stops.

    KVM is attempted first and QEMU falls back to TCG by default. Set
    require_kvm=True to omit TCG. kvm_unavailable="skip" records a skipped
    JUnit testcase and returns success when the host cannot provide KVM.
    """
    _define_uefi_test(
        name = name,
        arch = arch,
        qemu = qemu,
        firmware = firmware,
        disk = disk,
        media = media,
        firmware_vars = firmware_vars,
        debugcon = debugcon,
        export_firmware_vars = export_firmware_vars,
        require_kvm = require_kvm,
        timeout_seconds = timeout_seconds,
        success_pattern = success_pattern,
        failure_pattern = failure_pattern,
        memory_mb = memory_mb,
        cpus = cpus,
        machine_options = machine_options,
        qemu_args = qemu_args,
        success_exit_codes = success_exit_codes,
        test_data = test_data,
        tags = tags,
        success_markers = success_markers,
        forbidden_markers = forbidden_markers,
        phases = phases,
        on_failure = on_failure,
        on_failure_timeout_seconds = on_failure_timeout_seconds,
        kvm_unavailable = kvm_unavailable,
        boot = boot,
        kernel = kernel,
        initrd = initrd,
        kernel_args = kernel_args,
        cpu_model = cpu_model,
        graphics = graphics,
        graphics_device = graphics_device,
        screendump_not_blank = screendump_not_blank,
        screendump_min_distinct_pixels = screendump_min_distinct_pixels,
        hostfwd = hostfwd,
        host_companion = host_companion,
        host_companion_args = host_companion_args,
        **kwargs
    )

def uefi_platform_test(
        name,
        guest_platform,
        qemu,
        firmware,
        disk = None,
        media = [],
        firmware_vars = None,
        debugcon = False,
        export_firmware_vars = False,
        require_kvm = False,
        timeout_seconds = 60,
        success_pattern = None,
        failure_pattern = "OSTEST: FAIL",
        memory_mb = 256,
        cpus = 1,
        machine_options = [],
        qemu_args = [],
        success_exit_codes = [],
        test_data = [],
        tags = [],
        success_markers = [],
        forbidden_markers = [],
        phases = [],
        on_failure = None,
        on_failure_timeout_seconds = 30,
        kvm_unavailable = "fail",
        boot = "uefi",
        kernel = None,
        initrd = None,
        kernel_args = "",
        cpu_model = None,
        graphics = False,
        graphics_device = None,
        screendump_not_blank = False,
        screendump_min_distinct_pixels = 2,
        hostfwd = [],
        host_companion = None,
        host_companion_args = [],
        **kwargs):
    """Defines a UEFI test whose guest architecture comes from a Bazel platform.

    Only guest inputs use the platform transition. The Python/QEMU harness keeps
    its normal configuration so that the test remains executable on its worker.
    """
    arch_target = name + "__guest_arch"
    _guest_arch_file(
        name = arch_target,
        guest_platform = guest_platform,
        visibility = ["//visibility:private"],
    )
    _define_uefi_test(
        name = name,
        arch_file = ":" + arch_target,
        qemu = qemu,
        firmware = firmware,
        disk = disk,
        media = media,
        firmware_vars = firmware_vars,
        debugcon = debugcon,
        export_firmware_vars = export_firmware_vars,
        require_kvm = require_kvm,
        timeout_seconds = timeout_seconds,
        success_pattern = success_pattern,
        failure_pattern = failure_pattern,
        memory_mb = memory_mb,
        cpus = cpus,
        machine_options = machine_options,
        qemu_args = qemu_args,
        success_exit_codes = success_exit_codes,
        test_data = test_data,
        tags = tags,
        success_markers = success_markers,
        forbidden_markers = forbidden_markers,
        phases = phases,
        on_failure = on_failure,
        on_failure_timeout_seconds = on_failure_timeout_seconds,
        kvm_unavailable = kvm_unavailable,
        boot = boot,
        kernel = kernel,
        initrd = initrd,
        kernel_args = kernel_args,
        cpu_model = cpu_model,
        graphics = graphics,
        graphics_device = graphics_device,
        screendump_not_blank = screendump_not_blank,
        screendump_min_distinct_pixels = screendump_min_distinct_pixels,
        hostfwd = hostfwd,
        host_companion = host_companion,
        host_companion_args = host_companion_args,
        **kwargs
    )

def uefi_py_test(
        name,
        srcs,
        main,
        qemu,
        firmware,
        disk = None,
        media = [],
        arch = None,
        guest_platform = None,
        firmware_vars = None,
        debugcon = False,
        gdb = False,
        pause_at_start = False,
        require_kvm = False,
        memory_mb = 256,
        cpus = 1,
        machine_options = [],
        qemu_args = [],
        args = [],
        data = [],
        deps = [],
        tags = [],
        boot = "uefi",
        kernel = None,
        initrd = None,
        kernel_args = "",
        cpu_model = None,
        graphics = False,
        graphics_device = None,
        hostfwd = [],
        **kwargs):
    """Defines a custom rules_python test with the public QEMU/QMP library.

    Exactly one of arch or guest_platform is required. The macro adds resolved
    QEMU inputs as --ostest-* arguments; the test should call
    add_uefi_qemu_arguments() before parsing its command line.

    Args:
      name: Bazel test target name.
      srcs: Python source labels passed to py_test.
      main: Python entry point passed to py_test.
      qemu: Label of the QEMU system executable for the execution platform.
      firmware: Label of the read-only UEFI firmware-code image.
      disk: Optional raw boot disk shorthand with boot index 1.
      media: List of qemu_media configurations.
      arch: Guest architecture name. Exactly one of arch and guest_platform is
        required.
      guest_platform: Bazel platform used to infer and transition guest inputs.
      firmware_vars: Optional writable UEFI variable-store template.
      debugcon: Capture the x86 OVMF debug console when true.
      gdb: Expose an ephemeral loopback QEMU GDB endpoint when true.
      pause_at_start: Start paused for GDB; requires gdb.
      require_kvm: Disable TCG fallback and require KVM when true.
      memory_mb: Positive guest memory size in MiB.
      cpus: Positive guest virtual CPU count.
      machine_options: QEMU machine key/value properties without commas.
      qemu_args: Additional arguments appended to the QEMU command.
      args: Additional arguments passed to the Python test after owned
        --ostest-* arguments.
      data: Additional runfiles for the Python test.
      deps: Additional Python dependencies for the test.
      tags: Bazel test tags.
      boot: Boot mode, uefi or direct-kernel.
      kernel: Direct-boot kernel label.
      initrd: Optional direct-boot initial RAM disk label.
      kernel_args: Literal direct-boot kernel command line.
      cpu_model: Optional QEMU CPU model override.
      graphics: Attach a guest display device while remaining host-headless.
      graphics_device: Optional QEMU display-device model override.
      hostfwd: List of qemu_hostfwd configurations exposed by QemuSession.
      **kwargs: Remaining attributes forwarded to py_test. The macro owns srcs,
        main, args, data, deps, python_version, and tags.
    """
    if (arch == None) == (guest_platform == None):
        fail("exactly one of arch and guest_platform must be supplied")
    if memory_mb <= 0 or cpus <= 0:
        fail("memory_mb and cpus must be positive")
    if firmware_vars != None and firmware == None:
        fail("firmware_vars requires firmware")
    if graphics_device != None and not graphics:
        fail("graphics_device requires graphics=True")
    if cpu_model != None and (not cpu_model or "," in cpu_model):
        fail("cpu_model must be a non-empty QEMU model name without commas")
    _validate_boot(firmware, boot, kernel, initrd, kernel_args)
    _validate_media(disk, media)
    for medium in media:
        if medium.export:
            fail("uefi_py_test does not auto-export media; call QemuSession.export_media from the Python test")
    _validate_hostfwd(hostfwd, qemu_args)
    for option in machine_options:
        if not option or "," in option:
            fail("machine_options entries must be non-empty single QEMU key/value properties")

    runtime_data = [qemu] + data
    runtime_args = []
    if arch != None:
        runtime_args.append("--ostest-arch=" + _normalize_arch(arch))
    else:
        arch_target = name + "__guest_arch"
        _guest_arch_file(
            name = arch_target,
            guest_platform = guest_platform,
            visibility = ["//visibility:private"],
        )
        arch_label = ":" + arch_target
        runtime_data.append(arch_label)
        runtime_args.append("--ostest-arch-file=" + _shell_quote("$(rlocationpath %s)" % arch_label))
    runtime_args.extend([
        "--ostest-qemu=" + _shell_quote("$(rlocationpath %s)" % qemu),
        "--ostest-memory-mb=%d" % memory_mb,
        "--ostest-cpus=%d" % cpus,
    ])
    if firmware != None:
        runtime_data.append(firmware)
        runtime_args.append("--ostest-firmware=" + _shell_quote("$(rlocationpath %s)" % firmware))
    _append_boot_runtime(runtime_args, runtime_data, "ostest-", boot, kernel, initrd, kernel_args, cpu_model, graphics, graphics_device)
    _append_media_runtime(runtime_args, runtime_data, disk, media, "ostest-")
    _append_hostfwd_runtime(runtime_args, hostfwd, "ostest-")
    if firmware_vars != None:
        runtime_data.append(firmware_vars)
        runtime_args.append("--ostest-firmware-vars=" + _shell_quote("$(rlocationpath %s)" % firmware_vars))
    if debugcon:
        runtime_args.append("--ostest-debugcon")
    if gdb:
        runtime_args.append("--ostest-gdb")
    if pause_at_start:
        if not gdb:
            fail("pause_at_start requires gdb=True")
        runtime_args.append("--ostest-pause-at-start")
    if require_kvm:
        runtime_args.append("--ostest-require-kvm")
    for option in machine_options:
        runtime_args.append("--ostest-machine-option=" + _shell_quote(option))
    for qemu_arg in qemu_args:
        runtime_args.append("--ostest-qemu-arg=" + _shell_quote(qemu_arg))
    runtime_args.extend(args)

    if "legacy_create_init" not in kwargs:
        kwargs["legacy_create_init"] = 0
    py_test(
        name = name,
        srcs = srcs,
        main = main,
        args = runtime_args,
        data = _deduplicate_labels(runtime_data),
        deps = _deduplicate_labels(deps + [_QEMU_TEST_LIBRARY]),
        python_version = "3.12",
        tags = tags,
        **kwargs
    )

def _hex_byte(value):
    digits = "0123456789abcdef"
    return digits[(value // 16) % 16] + digits[value % 16]

def _lab_media_entries(vm, runtime_data):
    entries = []
    if vm.disk != None:
        runtime_data.append(vm.disk)
        entries.append({
            "bootindex": 1,
            "compression": "auto",
            "export": False,
            "format": "raw",
            "interface": "virtio-blk",
            "kind": "image",
            "name": "disk",
            "path": "$(rlocationpath %s)" % vm.disk,
            "readonly": False,
            "size_mb": None,
            "snapshot": True,
        })
    for index, medium in enumerate(vm.media):
        entry = {
            "bootindex": medium.bootindex,
            "compression": medium.compression,
            "export": medium.export,
            "format": medium.image_format,
            "interface": medium.interface,
            "kind": medium.kind,
            "name": medium.name if medium.name else "media%d" % index,
            "readonly": medium.readonly,
            "size_mb": medium.size_mb,
            "snapshot": medium.snapshot,
        }
        if medium.kind == "image":
            runtime_data.append(medium.image)
            entry["path"] = "$(rlocationpath %s)" % medium.image
        entries.append(entry)
    return entries

def uefi_lab_test(
        name,
        srcs,
        main,
        vms,
        args = [],
        data = [],
        deps = [],
        tags = [],
        **kwargs):
    """Defines one hermetic test owning all VMs in an isolated local network.

    Args:
      name: Bazel test target name.
      srcs: Python source labels passed to py_test.
      main: Python entry point passed to py_test.
      vms: Non-empty list of uniquely named uefi_vm configurations.
      args: Additional arguments passed to the Python test.
      data: Additional runfiles for the Python test.
      deps: Additional Python dependencies for the test.
      tags: Bazel test tags.
      **kwargs: Remaining attributes forwarded to py_test. The macro owns srcs,
        main, args, data, deps, python_version, and tags.
    """
    if not vms:
        fail("uefi_lab_test requires at least one VM")
    runtime_args = []
    runtime_data = list(data)
    seen_names = {}
    for vm_index, vm in enumerate(vms):
        if vm.name in seen_names:
            fail("duplicate VM name %r" % vm.name)
        seen_names[vm.name] = True
        runtime_data.append(vm.qemu)
        firmware = None
        if vm.firmware != None:
            runtime_data.append(vm.firmware)
            firmware = "$(rlocationpath %s)" % vm.firmware
        kernel = None
        if vm.kernel != None:
            runtime_data.append(vm.kernel)
            kernel = "$(rlocationpath %s)" % vm.kernel
        initrd = None
        if vm.initrd != None:
            runtime_data.append(vm.initrd)
            initrd = "$(rlocationpath %s)" % vm.initrd
        arch = None
        arch_file = None
        if vm.arch != None:
            arch = _normalize_arch(vm.arch)
        else:
            arch_target = "%s__%s_guest_arch" % (name, vm.name)
            _guest_arch_file(
                name = arch_target,
                guest_platform = vm.guest_platform,
                visibility = ["//visibility:private"],
            )
            arch_file = ":" + arch_target
            runtime_data.append(arch_file)
        firmware_vars = None
        if vm.firmware_vars != None:
            runtime_data.append(vm.firmware_vars)
            firmware_vars = "$(rlocationpath %s)" % vm.firmware_vars
        networks = []
        for network_index, network in enumerate(vm.networks):
            mac = network.mac
            if not mac:
                mac = "52:54:00:%s:%s:%s" % (
                    _hex_byte(vm_index),
                    _hex_byte(network_index),
                    _hex_byte(vm_index + network_index + 1),
                )
            networks.append({
                "mac": mac,
                "model": network.model,
                "name": network.name,
            })
        spec = {
            "arch": arch,
            "arch_file": "$(rlocationpath %s)" % arch_file if arch_file else None,
            "boot": vm.boot,
            "cpus": vm.cpus,
            "cpu_model": vm.cpu_model,
            "debugcon": vm.debugcon,
            "firmware": firmware,
            "firmware_vars": firmware_vars,
            "gdb": vm.gdb,
            "graphics": vm.graphics,
            "graphics_device": vm.graphics_device,
            "initrd": initrd,
            "kernel": kernel,
            "kernel_args": vm.kernel_args,
            "media": _lab_media_entries(vm, runtime_data),
            "machine_options": vm.machine_options,
            "memory_mb": vm.memory_mb,
            "name": vm.name,
            "networks": networks,
            "pause_at_start": vm.pause_at_start,
            "qemu": "$(rlocationpath %s)" % vm.qemu,
            "qemu_args": vm.qemu_args,
            "require_kvm": vm.require_kvm,
        }
        runtime_args.append("--ostest-lab-vm=" + _shell_quote(json.encode(spec)))
    runtime_args.extend(args)
    if "legacy_create_init" not in kwargs:
        kwargs["legacy_create_init"] = 0
    py_test(
        name = name,
        srcs = srcs,
        main = main,
        args = runtime_args,
        data = _deduplicate_labels(runtime_data),
        deps = _deduplicate_labels(deps + [_QEMU_TEST_LIBRARY]),
        python_version = "3.12",
        tags = tags,
        **kwargs
    )
