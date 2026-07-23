"""Public helpers for scripted, hermetic UEFI tests under QEMU.

Use ``add_uefi_qemu_arguments`` with a test's ArgumentParser, construct a
``UefiQemuConfig`` from the parsed namespace, and enter ``QemuSession``.  The
session exposes both serial matching and the complete JSON QMP command surface.
"""

from __future__ import annotations

import argparse
import codecs
import gzip
import json
import os
import pathlib
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Pattern

from python.runfiles import runfiles
from ostest.python.network import QemuNic


MAX_SERIAL_BUFFER = 4 * 1024 * 1024

_USERNET_FORWARD_RE = re.compile(
    r"^\s*(TCP|UDP)\s*\[\s*HOST_FORWARD\s*\]"
    r"\s+\d+\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)(?:\s|$)",
    re.MULTILINE,
)
_HOST_FORWARD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


class QmpError(RuntimeError):
    """A QMP command returned an error response."""


class _QmpConnectionClosed(RuntimeError):
    """The QMP peer disconnected before sending a complete response."""


def _locate(locator: runfiles.Runfiles, value: str) -> pathlib.Path:
    if os.path.isabs(value):
        raise ValueError(f"expected a runfiles-relative path, got {value!r}")
    resolved = locator.Rlocation(value)
    if not resolved:
        raise ValueError(f"runfile could not be resolved: {value!r}")
    path = pathlib.Path(resolved)
    if not path.exists():
        raise ValueError(f"resolved runfile does not exist: {path}")
    return path


def _materialize_image(
    source: pathlib.Path,
    destination: pathlib.Path,
    compression: str = "auto",
) -> pathlib.Path:
    with source.open("rb") as input_file:
        is_gzip = input_file.read(2) == b"\x1f\x8b"
    if compression not in ("auto", "gzip", "none"):
        raise ValueError(f"unsupported media compression: {compression!r}")
    if compression == "gzip" and not is_gzip:
        raise ValueError(f"media declared as gzip does not have gzip magic: {source}")
    if compression == "none":
        return source
    if not is_gzip:
        return source
    with gzip.open(source, "rb") as compressed, destination.open("wb") as output:
        shutil.copyfileobj(compressed, output, length=1024 * 1024)
    destination.chmod(0o600)
    return destination


def _qemu_keyval_path(path: pathlib.Path) -> str:
    return str(path).replace(",", ",,")


def _parse_usernet_forwards(output: str) -> list[tuple[str, str, int, str, int]]:
    """Parses forwarding rows from QEMU's human `info usernet` output."""

    return [
        (
            match.group(1).lower(),
            match.group(2),
            int(match.group(3)),
            match.group(4),
            int(match.group(5)),
        )
        for match in _USERNET_FORWARD_RE.finditer(output)
    ]


def add_uefi_qemu_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds the arguments populated by the ``uefi_py_test`` Bazel macro."""

    group = parser.add_argument_group("rules_ostest")
    architecture = group.add_mutually_exclusive_group(required=True)
    architecture.add_argument("--ostest-arch", choices=("x86_64", "aarch64"))
    architecture.add_argument("--ostest-arch-file")
    group.add_argument("--ostest-qemu", required=True)
    group.add_argument("--ostest-firmware")
    group.add_argument("--ostest-firmware-vars")
    group.add_argument("--ostest-boot", choices=("uefi", "direct-kernel"), default="uefi")
    group.add_argument("--ostest-kernel")
    group.add_argument("--ostest-initrd")
    group.add_argument("--ostest-kernel-args", default="")
    group.add_argument("--ostest-disk")
    group.add_argument("--ostest-media", action="append", default=[])
    group.add_argument("--ostest-hostfwd", action="append", default=[])
    group.add_argument("--ostest-memory-mb", type=int, default=256)
    group.add_argument("--ostest-cpus", type=int, default=1)
    group.add_argument("--ostest-require-kvm", action="store_true")
    group.add_argument("--ostest-debugcon", action="store_true")
    group.add_argument("--ostest-gdb", action="store_true")
    group.add_argument("--ostest-pause-at-start", action="store_true")
    group.add_argument("--ostest-allow-reboot", action="store_true")
    group.add_argument("--ostest-cpu-model")
    group.add_argument("--ostest-graphics", action="store_true")
    group.add_argument("--ostest-graphics-device")
    group.add_argument("--ostest-machine-option", action="append", default=[])
    group.add_argument("--ostest-qemu-arg", action="append", default=[])


@dataclass(frozen=True)
class QemuMedia:
    """One resolved boot medium attached to a QEMU guest."""

    name: str
    path: pathlib.Path
    interface: str
    image_format: str
    readonly: bool
    snapshot: bool
    bootindex: int | None
    export: bool = False


@dataclass(frozen=True)
class HostForwardRequest:
    """One requested user-network host-to-guest port forwarding."""

    name: str
    guest_port: int
    host_port: int | None
    protocol: str

    def __post_init__(self) -> None:
        if not _HOST_FORWARD_NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid host forwarding name: {self.name!r}")
        if self.protocol not in ("tcp", "udp"):
            raise ValueError(f"unsupported host forwarding protocol: {self.protocol!r}")
        if isinstance(self.guest_port, bool) or not 1 <= self.guest_port <= 65535:
            raise ValueError("guest forwarding port must be between 1 and 65535")
        if self.host_port is not None and (
            isinstance(self.host_port, bool) or not 1 <= self.host_port <= 65535
        ):
            raise ValueError("host forwarding port must be between 1 and 65535")

    @property
    def qemu_hostfwd(self) -> str:
        """Returns the loopback-only QEMU usernet forwarding value."""

        host_port = self.host_port if self.host_port is not None else 0
        return f"{self.protocol}:127.0.0.1:{host_port}-:{self.guest_port}"


@dataclass(frozen=True)
class ResolvedHostForward:
    """One host forwarding after QEMU has allocated any automatic port."""

    name: str
    host: str
    host_port: int
    guest_port: int
    protocol: str

    def __post_init__(self) -> None:
        if not _HOST_FORWARD_NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid host forwarding name: {self.name!r}")
        if self.host != "127.0.0.1":
            raise ValueError(f"managed host forwarding must bind loopback, got {self.host!r}")
        if self.protocol not in ("tcp", "udp"):
            raise ValueError(f"unsupported host forwarding protocol: {self.protocol!r}")
        if not 1 <= self.host_port <= 65535 or not 1 <= self.guest_port <= 65535:
            raise ValueError("resolved host forwarding ports must be between 1 and 65535")

    def as_mapping(self) -> dict[str, str | int]:
        """Returns a JSON-compatible endpoint mapping."""

        return {
            "guest_port": self.guest_port,
            "host": self.host,
            "host_port": self.host_port,
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class UefiQemuConfig:
    """Resolved QEMU inputs and deterministic machine configuration."""

    name: str
    arch: str
    qemu: pathlib.Path
    firmware: pathlib.Path | None
    media: tuple[QemuMedia, ...]
    firmware_vars: pathlib.Path | None
    memory_mb: int
    cpus: int
    require_kvm: bool
    debugcon_path: pathlib.Path | None
    gdb: bool
    pause_at_start: bool
    machine_options: tuple[str, ...]
    qemu_args: tuple[str, ...]
    boot: str = "uefi"
    kernel: pathlib.Path | None = None
    initrd: pathlib.Path | None = None
    kernel_args: str = ""
    cpu_model: str | None = None
    graphics: bool = False
    graphics_device: str | None = None
    allow_reboot: bool = False
    hostfwd: tuple[HostForwardRequest, ...] = ()

    @classmethod
    def from_namespace(
        cls,
        namespace: argparse.Namespace,
        *,
        instance_name: str = "ostest",
    ) -> "UefiQemuConfig":
        """Resolves runfiles and prepares writable inputs below TEST_TMPDIR."""

        locator = runfiles.Create()
        if locator is None:
            raise RuntimeError("Bazel runfiles are unavailable")
        arch = namespace.ostest_arch
        if namespace.ostest_arch_file:
            arch = _locate(locator, namespace.ostest_arch_file).read_text(encoding="utf-8").strip()
        if arch not in ("x86_64", "aarch64"):
            raise ValueError(f"unsupported guest architecture: {arch!r}")
        if namespace.ostest_memory_mb <= 0 or namespace.ostest_cpus <= 0:
            raise ValueError("memory and CPU counts must be positive")
        machine_options = tuple(namespace.ostest_machine_option)
        if any(not option or "," in option for option in machine_options):
            raise ValueError("machine options must be non-empty single QEMU key/value properties")

        boot = str(getattr(namespace, "ostest_boot", "uefi"))
        if boot not in ("uefi", "direct-kernel"):
            raise ValueError(f"unsupported boot mode: {boot!r}")
        firmware_value = getattr(namespace, "ostest_firmware", None)
        kernel_value = getattr(namespace, "ostest_kernel", None)
        initrd_value = getattr(namespace, "ostest_initrd", None)
        kernel_args = str(getattr(namespace, "ostest_kernel_args", ""))
        if boot == "uefi":
            if not firmware_value:
                raise ValueError("UEFI boot requires firmware")
            if kernel_value or initrd_value or kernel_args:
                raise ValueError("kernel, initrd, and kernel arguments require direct-kernel boot")
        elif not kernel_value:
            raise ValueError("direct-kernel boot requires a kernel")
        if getattr(namespace, "ostest_firmware_vars", None) and not firmware_value:
            raise ValueError("a writable firmware variable store requires firmware")

        cpu_model_value = getattr(namespace, "ostest_cpu_model", None)
        cpu_model = None
        if cpu_model_value is not None:
            cpu_model = str(cpu_model_value)
            if not cpu_model or "," in cpu_model:
                raise ValueError("CPU model must be a non-empty QEMU model name without commas")
        graphics = bool(getattr(namespace, "ostest_graphics", False))
        graphics_device_value = getattr(namespace, "ostest_graphics_device", None)
        graphics_device = None
        if graphics_device_value is not None:
            graphics_device = str(graphics_device_value)
            if not graphics_device:
                raise ValueError("graphics_device must not be empty")
            if not graphics:
                raise ValueError("graphics_device requires graphics to be enabled")

        test_tmpdir = pathlib.Path(os.environ["TEST_TMPDIR"])
        media_specs = [json.loads(encoded) for encoded in namespace.ostest_media]
        if namespace.ostest_disk:
            media_specs.insert(
                0,
                {
                    "bootindex": 1,
                    "compression": "auto",
                    "format": "raw",
                    "interface": "virtio-blk",
                    "name": "disk",
                    "path": namespace.ostest_disk,
                    "readonly": False,
                    "snapshot": True,
                },
            )
        resolved_media = []
        boot_indices = set()
        media_names = set()
        for index, spec in enumerate(media_specs):
            kind = str(spec.get("kind", "image"))
            if kind not in ("image", "scratch"):
                raise ValueError(f"unsupported media kind: {kind!r}")
            interface = str(spec["interface"])
            if interface not in ("virtio-blk", "usb-storage", "nvme", "cdrom"):
                raise ValueError(f"unsupported media interface: {interface!r}")
            image_format = str(spec.get("format", "raw"))
            if image_format not in ("raw", "qcow2"):
                raise ValueError(f"unsupported QEMU image format: {image_format!r}")
            default_bootindex = None if kind == "scratch" else index + 1
            bootindex_value = spec.get("bootindex", default_bootindex)
            bootindex = int(bootindex_value) if bootindex_value is not None else None
            if bootindex is not None:
                if bootindex <= 0:
                    raise ValueError("media bootindex must be positive")
                if bootindex in boot_indices:
                    raise ValueError(f"QEMU media bootindex {bootindex} is used more than once")
                boot_indices.add(bootindex)
            name = str(spec.get("name", f"media{index}"))
            if not name:
                raise ValueError("media name must not be empty")
            if name in media_names:
                raise ValueError(f"duplicate QEMU media name: {name!r}")
            media_names.add(name)
            readonly = bool(spec.get("readonly", False if kind == "scratch" else interface == "cdrom"))
            snapshot = bool(spec.get("snapshot", False if kind == "scratch" else interface != "cdrom"))
            export = bool(spec.get("export", False))
            extension = ".iso" if interface == "cdrom" else ".img"
            destination = test_tmpdir / f"{instance_name}-media-{index}{extension}"
            if kind == "scratch":
                if interface == "cdrom":
                    raise ValueError("scratch media cannot use the cdrom interface")
                if image_format != "raw":
                    raise ValueError("scratch media must use the raw image format")
                if readonly or snapshot:
                    raise ValueError("scratch media must be writable and non-snapshot")
                size_mb = int(spec.get("size_mb", 0))
                if size_mb <= 0:
                    raise ValueError("scratch media size_mb must be positive")
                with destination.open("wb") as scratch:
                    scratch.truncate(size_mb * 1024 * 1024)
                destination.chmod(0o600)
                path = destination
            else:
                source = _locate(locator, str(spec["path"]))
                path = _materialize_image(
                    source,
                    destination,
                    str(spec.get("compression", "auto")),
                )
                if not readonly and not snapshot and path == source:
                    shutil.copyfile(source, destination)
                    destination.chmod(0o600)
                    path = destination
            if export and (readonly or snapshot):
                raise ValueError("exported media must be writable and non-snapshot")
            resolved_media.append(
                QemuMedia(
                    name=name,
                    path=path,
                    interface=interface,
                    image_format=image_format,
                    readonly=readonly,
                    snapshot=snapshot,
                    bootindex=bootindex,
                    export=export,
                )
            )
        hostfwd = []
        hostfwd_names = set()
        hostfwd_guest_endpoints = set()
        hostfwd_static_endpoints = set()
        for encoded in getattr(namespace, "ostest_hostfwd", []):
            spec = json.loads(encoded)
            if not isinstance(spec, dict):
                raise ValueError("host forwarding JSON must encode an object")
            unknown_fields = sorted(set(spec) - {"guest", "host", "name", "protocol"})
            if unknown_fields:
                raise ValueError(f"unknown host forwarding fields: {', '.join(unknown_fields)}")
            if "guest" not in spec:
                raise ValueError("host forwarding is missing required field 'guest'")
            name = str(spec.get("name", f"forward{len(hostfwd)}"))
            if not _HOST_FORWARD_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid host forwarding name: {name!r}")
            if name in hostfwd_names:
                raise ValueError(f"duplicate host forwarding name: {name!r}")
            hostfwd_names.add(name)
            guest_value = spec["guest"]
            if isinstance(guest_value, bool) or not isinstance(guest_value, int):
                raise ValueError("guest forwarding port must be an integer")
            guest_port = guest_value
            if not 1 <= guest_port <= 65535:
                raise ValueError("guest forwarding port must be between 1 and 65535")
            host_value = spec.get("host", "auto")
            if host_value == "auto":
                host_port = None
            elif isinstance(host_value, bool) or not isinstance(host_value, int):
                raise ValueError("host forwarding port must be 'auto' or an integer")
            else:
                host_port = host_value
            if host_port is not None and not 1 <= host_port <= 65535:
                raise ValueError("host forwarding port must be 'auto' or between 1 and 65535")
            protocol = str(spec.get("protocol", "tcp"))
            if protocol not in ("tcp", "udp"):
                raise ValueError(f"unsupported host forwarding protocol: {protocol!r}")
            guest_endpoint = (protocol, guest_port)
            if guest_endpoint in hostfwd_guest_endpoints:
                raise ValueError(f"duplicate guest forwarding endpoint: {protocol}/{guest_port}")
            hostfwd_guest_endpoints.add(guest_endpoint)
            if host_port is not None:
                static_endpoint = (protocol, host_port)
                if static_endpoint in hostfwd_static_endpoints:
                    raise ValueError(f"duplicate static forwarding endpoint: {protocol}/{host_port}")
                hostfwd_static_endpoints.add(static_endpoint)
            hostfwd.append(
                HostForwardRequest(
                    name=name,
                    guest_port=guest_port,
                    host_port=host_port,
                    protocol=protocol,
                )
            )
        firmware_vars = None
        if getattr(namespace, "ostest_firmware_vars", None):
            vars_source = _locate(locator, namespace.ostest_firmware_vars)
            firmware_vars = test_tmpdir / f"{instance_name}-uefi-vars.fd"
            shutil.copyfile(vars_source, firmware_vars)
            firmware_vars.chmod(0o600)
        debugcon_path = None
        if namespace.ostest_debugcon:
            if arch != "x86_64":
                raise ValueError("OVMF debugcon capture is only available for x86_64 guests")
            artifacts_dir = pathlib.Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", test_tmpdir))
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            debugcon_path = artifacts_dir / f"{instance_name}-ovmf-debug.log"
        return cls(
            name=instance_name,
            arch=arch,
            qemu=_locate(locator, namespace.ostest_qemu),
            firmware=_locate(locator, firmware_value) if firmware_value else None,
            media=tuple(resolved_media),
            firmware_vars=firmware_vars,
            memory_mb=namespace.ostest_memory_mb,
            cpus=namespace.ostest_cpus,
            require_kvm=namespace.ostest_require_kvm,
            debugcon_path=debugcon_path,
            gdb=namespace.ostest_gdb,
            pause_at_start=namespace.ostest_pause_at_start,
            machine_options=machine_options,
            qemu_args=tuple(namespace.ostest_qemu_arg),
            boot=boot,
            kernel=_locate(locator, kernel_value) if kernel_value else None,
            initrd=_locate(locator, initrd_value) if initrd_value else None,
            kernel_args=kernel_args,
            cpu_model=cpu_model,
            graphics=graphics,
            graphics_device=graphics_device,
            allow_reboot=bool(getattr(namespace, "ostest_allow_reboot", False)),
            hostfwd=tuple(hostfwd),
        )

    @property
    def disk(self) -> pathlib.Path | None:
        """Returns the first medium for compatibility with the original API."""

        return self.media[0].path if self.media else None

    def command(
        self,
        qmp_listener_fd: int | None = None,
        gdb_listener_fd: int | None = None,
        nics: tuple[QemuNic, ...] = (),
        allow_reboot: bool = False,
    ) -> list[str]:
        """Returns the complete QEMU command for a passed QMP listener FD."""

        machine_options = list(self.machine_options)
        if (
            self.arch == "aarch64"
            and self.boot == "direct-kernel"
            and not any(option.split("=", 1)[0] == "gic-version" for option in machine_options)
        ):
            machine_options.insert(0, "gic-version=2")
        if (
            self.arch == "aarch64"
            and self.cpus > 8
            and any(option == "gic-version=2" for option in machine_options)
        ):
            raise ValueError("AArch64 GICv2 supports at most 8 virtual CPUs")
        machine = "q35" if self.arch == "x86_64" else "virt"
        if machine_options:
            machine += "," + ",".join(machine_options)
        command = [
            str(self.qemu),
            "-no-user-config",
            "-nodefaults",
            "-machine",
            machine,
            "-m",
            str(self.memory_mb),
            "-smp",
            str(self.cpus),
        ]
        cpu_model = self.cpu_model
        if cpu_model is None and self.arch == "aarch64":
            cpu_model = "cortex-a53" if self.boot == "direct-kernel" else "max"
        if cpu_model is not None:
            command.extend(["-cpu", cpu_model])
        if self.require_kvm:
            command.extend(["-accel", "kvm"])
        else:
            command.extend(["-accel", "kvm", "-accel", "tcg"])
        if self.firmware is not None:
            command.extend(
                [
                    "-drive",
                    "if=pflash,format=raw,unit=0,readonly=on,file=" + _qemu_keyval_path(self.firmware),
                ]
            )
        if self.firmware_vars is not None:
            command.extend(
                [
                    "-drive",
                    "if=pflash,format=raw,unit=1,file=" + _qemu_keyval_path(self.firmware_vars),
                ]
            )
        if self.boot == "direct-kernel":
            if self.kernel is None:
                raise ValueError("direct-kernel boot requires a kernel")
            command.extend(["-kernel", str(self.kernel)])
            if self.initrd is not None:
                command.extend(["-initrd", str(self.initrd)])
            if self.kernel_args:
                command.extend(["-append", self.kernel_args])
        needs_usb = any(medium.interface == "usb-storage" for medium in self.media)
        needs_scsi = any(medium.interface == "cdrom" for medium in self.media)
        if needs_usb:
            command.extend(["-device", "qemu-xhci,id=ostest_usb"])
        if needs_scsi:
            command.extend(["-device", "virtio-scsi-pci,id=ostest_scsi"])
        for index, medium in enumerate(self.media):
            drive_id = f"ostest_media{index}"
            drive_options = [
                "if=none",
                "id=" + drive_id,
                "format=" + medium.image_format,
                "file=" + _qemu_keyval_path(medium.path),
            ]
            if medium.interface == "cdrom":
                drive_options.append("media=cdrom")
            if medium.readonly:
                drive_options.append("readonly=on")
            if medium.snapshot and not medium.readonly:
                drive_options.append("snapshot=on")
            command.extend(["-drive", ",".join(drive_options)])
            bootindex = f",bootindex={medium.bootindex}" if medium.bootindex is not None else ""
            if medium.interface == "virtio-blk":
                device = f"virtio-blk-pci,drive={drive_id}{bootindex}"
            elif medium.interface == "usb-storage":
                device = f"usb-storage,bus=ostest_usb.0,drive={drive_id}{bootindex}"
            elif medium.interface == "nvme":
                device = f"nvme,drive={drive_id},serial=OSTEST{index:04d}{bootindex}"
            else:
                device = f"scsi-cd,bus=ostest_scsi.0,drive={drive_id}{bootindex}"
            command.extend(["-device", device])
        if self.graphics:
            graphics_device = self.graphics_device
            if graphics_device is None:
                graphics_device = "VGA" if self.arch == "x86_64" else "virtio-gpu-pci"
            command.extend(["-device", graphics_device])
        command.extend(
            [
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "stdio",
                "-nic",
                "none",
            ]
        )
        if not (self.allow_reboot or allow_reboot):
            command.append("-no-reboot")
        command.extend(
            [
                "-rtc",
                "base=2000-01-01T00:00:00,clock=vm",
                "-uuid",
                "00000000-0000-0000-0000-000000000001",
            ]
        )
        if qmp_listener_fd is not None:
            command.extend(
                [
                    "-chardev",
                    f"socket,id=ostest_qmp,fd={qmp_listener_fd},server=on,wait=off",
                    "-mon",
                    "chardev=ostest_qmp,mode=control",
                ]
            )
        if self.debugcon_path is not None:
            command.extend(
                [
                    "-debugcon",
                    "file:" + str(self.debugcon_path),
                    "-global",
                    "isa-debugcon.iobase=0x402",
                ]
            )
        if self.gdb:
            if gdb_listener_fd is None:
                raise ValueError("GDB was enabled without a listener descriptor")
            command.extend(
                [
                    "-chardev",
                    f"socket,id=ostest_gdb,fd={gdb_listener_fd},server=on,wait=off",
                    "-gdb",
                    "chardev:ostest_gdb",
                ]
            )
        if self.pause_at_start:
            command.append("-S")
        for index, nic in enumerate(nics):
            netdev_id = f"ostest_net{index}"
            command.extend(
                [
                    "-netdev",
                    f"socket,id={netdev_id},fd={nic.fileno()}",
                    "-device",
                    f"{nic.model},netdev={netdev_id},mac={nic.mac}",
                ]
            )
        if self.hostfwd:
            usernet = ["user", "id=ostest_usernet", "ipv6=off", "restrict=on"]
            for forwarding in self.hostfwd:
                usernet.append("hostfwd=" + forwarding.qemu_hostfwd)
            command.extend(
                [
                    "-netdev",
                    ",".join(usernet),
                    "-device",
                    "virtio-net-pci,netdev=ostest_usernet,mac=52:54:00:ff:00:01",
                ]
            )
        command.extend(self.qemu_args)
        return command

    def export_firmware_vars(self, output: os.PathLike[str] | str) -> pathlib.Path:
        """Copies the mutated per-test variable store to a declared artifact path."""

        if self.firmware_vars is None:
            raise RuntimeError("this configuration has no writable firmware variable store")
        destination = pathlib.Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.firmware_vars, destination)
        return destination


class QemuSession:
    """A QEMU process with serial matching and a negotiated QMP channel."""

    def __init__(
        self,
        config: UefiQemuConfig,
        *,
        startup_timeout: float = 10.0,
        nics: tuple[QemuNic, ...] = (),
    ):
        self.config = config
        self.startup_timeout = startup_timeout
        self.nics = nics
        self.process: subprocess.Popen[bytes] | None = None
        self._qmp_socket: socket.socket | None = None
        self._qmp_file = None
        self._qmp_id = 0
        self._events: list[dict[str, Any]] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._serial_buffer = ""
        self._serial_log = None
        self._gdb_address: tuple[str, int] | None = None
        self._host_forwards: tuple[ResolvedHostForward, ...] = ()

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def serial_text(self) -> str:
        return self._serial_buffer

    @property
    def gdb_address(self) -> tuple[str, int]:
        """Returns the loopback GDB endpoint created for this session."""

        if self._gdb_address is None:
            raise RuntimeError("GDB is not enabled for this session")
        return self._gdb_address

    @property
    def host_forwards(self) -> tuple[ResolvedHostForward, ...]:
        """Returns the static and QEMU-allocated host forwarding endpoints."""

        if self.config.hostfwd and self.process is None:
            raise RuntimeError("host forwardings are unavailable before the QEMU session starts")
        return self._host_forwards

    def host_forward(self, name: str) -> ResolvedHostForward:
        """Returns one resolved host forwarding by its declared name."""

        for forwarding in self.host_forwards:
            if forwarding.name == name:
                return forwarding
        raise KeyError(f"unknown host forwarding {name!r}")

    def __enter__(self) -> "QemuSession":
        return self.start()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.terminate()

    def start(self) -> "QemuSession":
        if self.process is not None:
            raise RuntimeError("QEMU session has already been started")
        if os.name != "posix":
            raise RuntimeError("QMP descriptor passing currently requires a POSIX execution worker")

        test_tmpdir = pathlib.Path(os.environ["TEST_TMPDIR"])
        artifacts_dir = pathlib.Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", test_tmpdir))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        gdb_listener = None
        if self.config.gdb:
            gdb_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            gdb_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            gdb_listener.bind(("127.0.0.1", 0))
            gdb_listener.listen(1)
            self._gdb_address = gdb_listener.getsockname()
        command = self.config.command(
            listener.fileno(),
            gdb_listener.fileno() if gdb_listener is not None else None,
            self.nics,
        )
        (artifacts_dir / f"{self.config.name}-qemu-command.txt").write_text(
            shlex.join(command) + "\n",
            encoding="utf-8",
        )
        self._serial_log = (artifacts_dir / f"{self.config.name}-qemu.log").open("wb")
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(test_tmpdir),
                "TMPDIR": str(test_tmpdir),
                "XDG_CACHE_HOME": str(test_tmpdir / "cache"),
                "XDG_CONFIG_HOME": str(test_tmpdir / "config"),
                "XDG_DATA_HOME": str(test_tmpdir / "data"),
                "LC_ALL": "C",
                "TZ": "UTC",
            }
        )
        try:
            self.process = subprocess.Popen(
                command,
                cwd=test_tmpdir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=tuple(
                    descriptor
                    for descriptor in (
                        listener.fileno(),
                        gdb_listener.fileno() if gdb_listener is not None else None,
                        *(nic.fileno() for nic in self.nics),
                    )
                    if descriptor is not None
                ),
            )
            for nic in self.nics:
                nic.handoff_complete()
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(self.startup_timeout)
            client.connect(listener.getsockname())
            self._qmp_socket = client
            self._qmp_file = client.makefile("rwb", buffering=0)
            greeting = self._read_qmp_message(time.monotonic() + self.startup_timeout)
            if "QMP" not in greeting:
                raise RuntimeError(f"invalid QMP greeting: {greeting!r}")
            self.execute("qmp_capabilities", timeout=self.startup_timeout)
            self._host_forwards = self._resolve_host_forwards(timeout=self.startup_timeout)
            return self
        except BaseException:
            self.terminate()
            raise
        finally:
            listener.close()
            if gdb_listener is not None:
                gdb_listener.close()

    def _read_qmp_message(self, deadline: float) -> dict[str, Any]:
        if self._qmp_socket is None or self._qmp_file is None:
            raise RuntimeError("QMP is not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for a QMP response")
        self._qmp_socket.settimeout(remaining)
        try:
            line = self._qmp_file.readline()
        except ConnectionError as error:
            status = self.process.poll() if self.process is not None else None
            raise _QmpConnectionClosed(
                f"QMP connection failed; QEMU status is {status}"
            ) from error
        if not line:
            status = self.process.poll() if self.process is not None else None
            raise _QmpConnectionClosed(
                f"QMP connection closed; QEMU status is {status}"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"invalid QMP JSON: {line!r}") from error
        if not isinstance(message, dict):
            raise RuntimeError(f"invalid QMP message: {message!r}")
        return message

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> Any:
        """Executes an arbitrary QMP command and returns its ``return`` value."""

        if self._qmp_file is None:
            raise RuntimeError("QMP is not connected")
        self._qmp_id += 1
        request: dict[str, Any] = {"execute": command, "id": self._qmp_id}
        if arguments is not None:
            request["arguments"] = arguments
        self._qmp_file.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = self._read_qmp_message(deadline)
            except _QmpConnectionClosed as error:
                # QEMU can close a TCP-backed QMP monitor immediately after
                # accepting "quit", before the empty success response reaches
                # the client. Treat that race as success only after the process
                # itself exits cleanly.
                if command != "quit" or self.process is None:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                try:
                    status = self.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise error
                if status != 0:
                    raise RuntimeError(
                        f"QEMU exited with unexpected status {status} after QMP quit"
                    ) from error
                return {}
            if "event" in response:
                self._events.append(response)
                continue
            if response.get("id") != self._qmp_id:
                continue
            if "error" in response:
                raise QmpError(f"QMP {command!r} failed: {response['error']!r}")
            if "return" not in response:
                raise RuntimeError(f"malformed QMP response: {response!r}")
            return response["return"]

    def _resolve_host_forwards(self, *, timeout: float) -> tuple[ResolvedHostForward, ...]:
        if not self.config.hostfwd:
            return ()
        discovered: list[tuple[str, str, int, str, int]] = []
        if any(forwarding.host_port is None for forwarding in self.config.hostfwd):
            output = self.execute(
                "human-monitor-command",
                {"command-line": "info usernet"},
                timeout=timeout,
            )
            if not isinstance(output, str):
                raise RuntimeError(f"QEMU info usernet returned a non-string result: {output!r}")
            discovered = _parse_usernet_forwards(output)
        resolved = []
        for request in self.config.hostfwd:
            if request.host_port is not None:
                host = "127.0.0.1"
                host_port = request.host_port
            else:
                matches = [
                    (index, row)
                    for index, row in enumerate(discovered)
                    if row[0] == request.protocol and row[4] == request.guest_port
                ]
                if not matches:
                    raise RuntimeError(
                        "QEMU did not report an automatically allocated "
                        f"{request.protocol} forwarding for guest port {request.guest_port}"
                    )
                if len(matches) != 1:
                    raise RuntimeError(
                        "QEMU reported ambiguous automatically allocated "
                        f"{request.protocol} forwarding for guest port {request.guest_port}"
                    )
                match_index, row = matches[0]
                _protocol, host, host_port, guest, _guest_port = row
                discovered.pop(match_index)
                if host != "127.0.0.1":
                    raise RuntimeError(
                        f"QEMU host forwarding bound {host!r}; expected loopback '127.0.0.1'"
                    )
                if guest != "10.0.2.15":
                    raise RuntimeError(
                        f"QEMU host forwarding targets {guest!r}; expected '10.0.2.15'"
                    )
            resolved.append(
                ResolvedHostForward(
                    name=request.name,
                    host=host,
                    host_port=host_port,
                    guest_port=request.guest_port,
                    protocol=request.protocol,
                )
            )
        return tuple(resolved)

    def wait_for_serial(self, pattern: str | Pattern[str], *, timeout: float = 60.0) -> re.Match[str]:
        """Waits for a regular expression in the first serial port's output."""

        if self.process is None or self.process.stdout is None:
            raise RuntimeError("QEMU is not running")
        expression = re.compile(pattern) if isinstance(pattern, str) else pattern
        existing = expression.search(self._serial_buffer)
        if existing:
            return existing
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for serial pattern {expression.pattern!r}")
                for key, _ in selector.select(timeout=min(0.2, remaining)):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(self.process.stdout)
                        continue
                    if self._serial_log is not None:
                        self._serial_log.write(chunk)
                        self._serial_log.flush()
                    self._serial_buffer += self._decoder.decode(chunk)
                    if len(self._serial_buffer) > MAX_SERIAL_BUFFER:
                        self._serial_buffer = self._serial_buffer[-MAX_SERIAL_BUFFER:]
                    match = expression.search(self._serial_buffer)
                    if match:
                        return match
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited with status {self.process.returncode} before serial pattern {expression.pattern!r}"
                    )
        finally:
            selector.close()

    def screendump(
        self,
        output: os.PathLike[str] | str,
        *,
        device: str | None = None,
        head: int | None = None,
        image_format: str | None = None,
        timeout: float = 10.0,
    ) -> pathlib.Path:
        """Captures a display through QMP and returns the requested output path."""

        path = pathlib.Path(output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        arguments: dict[str, Any] = {"filename": str(path)}
        if device is not None:
            arguments["device"] = device
        if head is not None:
            if device is None:
                raise ValueError("head requires a display device")
            arguments["head"] = head
        if image_format is not None:
            arguments["format"] = image_format
        self.execute("screendump", arguments, timeout=timeout)
        if not path.is_file():
            raise RuntimeError(f"QMP screendump did not create {path}")
        return path

    def wait_for_exit(
        self,
        *,
        timeout: float = 10.0,
        acceptable_codes: tuple[int, ...] = (0,),
    ) -> int:
        """Waits for QEMU to exit and validates its process status."""

        if self.process is None:
            raise RuntimeError("QEMU is not running")
        try:
            status = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"QEMU did not exit within {timeout} seconds") from error
        if status not in acceptable_codes:
            raise RuntimeError(f"QEMU exited with unexpected status {status}")
        return status

    def export_firmware_vars(self, output: os.PathLike[str] | str) -> pathlib.Path:
        """Exports the current NVRAM image after a guest boot."""

        return self.config.export_firmware_vars(output)

    def media_path(self, name: str) -> pathlib.Path:
        """Returns a medium's path after QEMU has stopped writing to it."""

        if self.process is None:
            raise RuntimeError("media read-back is unavailable before the QEMU session starts")
        if self.process.poll() is None:
            raise RuntimeError("media read-back requires QEMU to be stopped")
        for medium in self.config.media:
            if medium.name == name:
                return medium.path
        raise KeyError(f"unknown QEMU medium {name!r}")

    def export_media(
        self,
        name: str,
        output: os.PathLike[str] | str,
    ) -> pathlib.Path:
        """Copies writable non-snapshot media to a post-boot artifact path."""

        source = self.media_path(name)
        medium = next(medium for medium in self.config.media if medium.name == name)
        if medium.readonly:
            raise RuntimeError(f"QEMU medium {name!r} is read-only")
        if medium.snapshot:
            raise RuntimeError(f"QEMU medium {name!r} is snapshot-backed and cannot be exported")
        destination = pathlib.Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination:
            shutil.copyfile(source, destination)
        return destination

    def terminate(self) -> None:
        """Stops QEMU and closes test-owned descriptors."""

        if self._qmp_file is not None:
            try:
                self._qmp_file.close()
            except OSError:
                pass
            self._qmp_file = None
        if self._qmp_socket is not None:
            try:
                self._qmp_socket.close()
            except OSError:
                pass
            self._qmp_socket = None
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
        if self._serial_log is not None:
            self._serial_log.close()
            self._serial_log = None
