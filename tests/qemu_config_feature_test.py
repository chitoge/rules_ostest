#!/usr/bin/env python3
"""Focused unit tests for advanced QEMU configuration features."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


# Permit this unregistered test to run from a source checkout. Bazel supplies
# the real rules_python runfiles module when the test is registered.
try:
    from python.runfiles import runfiles as _unused_runfiles
except ModuleNotFoundError:
    python_module = types.ModuleType("python")
    runfiles_module = types.ModuleType("python.runfiles")

    class _RunfilesNamespace:
        class Runfiles:
            pass

        @staticmethod
        def Create():
            return None

    runfiles_module.runfiles = _RunfilesNamespace
    python_module.runfiles = runfiles_module
    sys.modules["python"] = python_module
    sys.modules["python.runfiles"] = runfiles_module

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ostest.python import qemu as qemu_module
from ostest.python.lab import UefiLabConfig
from ostest.python.qemu import (
    HostForwardRequest,
    QemuMedia,
    QemuSession,
    ResolvedHostForward,
    UefiQemuConfig,
    add_uefi_qemu_arguments,
)


class _Locator:
    def __init__(self, root: pathlib.Path):
        self.root = root

    def Rlocation(self, value: str) -> str:
        return str(self.root / value)


class _Process:
    def __init__(self, status):
        self.status = status

    def poll(self):
        return self.status


class QemuConfigFeatureTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        self.tmp = self.root / "tmp"
        self.outputs = self.root / "outputs"
        self.tmp.mkdir()
        self.outputs.mkdir()
        for name, data in {
            "qemu": b"executable",
            "firmware.fd": b"firmware",
            "vars.fd": b"variables",
            "kernel": b"kernel",
            "initrd": b"initrd",
            "disk.img": b"source disk",
            "arch.txt": b"aarch64\n",
        }.items():
            (self.root / name).write_bytes(data)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "TEST_TMPDIR": str(self.tmp),
                "TEST_UNDECLARED_OUTPUTS_DIR": str(self.outputs),
            },
        )
        self.environment.start()
        self.runfiles = mock.patch.object(
            qemu_module.runfiles,
            "Create",
            return_value=_Locator(self.root),
        )
        self.runfiles.start()

    def tearDown(self):
        self.runfiles.stop()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def namespace(self, **overrides) -> argparse.Namespace:
        values = {
            "ostest_arch": "x86_64",
            "ostest_arch_file": None,
            "ostest_cpus": 1,
            "ostest_debugcon": False,
            "ostest_disk": None,
            "ostest_firmware": "firmware.fd",
            "ostest_firmware_vars": None,
            "ostest_gdb": False,
            "ostest_media": [],
            "ostest_memory_mb": 256,
            "ostest_machine_option": [],
            "ostest_pause_at_start": False,
            "ostest_qemu": "qemu",
            "ostest_qemu_arg": [],
            "ostest_require_kvm": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def direct_config(self, **overrides) -> UefiQemuConfig:
        values = {
            "name": "direct",
            "arch": "aarch64",
            "qemu": self.root / "qemu",
            "firmware": None,
            "media": (),
            "firmware_vars": None,
            "memory_mb": 512,
            "cpus": 2,
            "require_kvm": False,
            "debugcon_path": None,
            "gdb": False,
            "pause_at_start": False,
            "machine_options": (),
            "qemu_args": (),
            "boot": "direct-kernel",
            "kernel": self.root / "kernel",
            "initrd": self.root / "initrd",
            "kernel_args": "console=ttyAMA0 test=one two",
        }
        values.update(overrides)
        return UefiQemuConfig(**values)

    def test_old_namespace_defaults_and_command_are_unchanged(self):
        config = UefiQemuConfig.from_namespace(self.namespace())
        self.assertEqual(config.boot, "uefi")
        self.assertFalse(config.allow_reboot)
        self.assertEqual(config.hostfwd, ())
        self.assertEqual(
            config.command(),
            [
                str(self.root / "qemu"),
                "-no-user-config",
                "-nodefaults",
                "-machine",
                "q35",
                "-m",
                "256",
                "-smp",
                "1",
                "-accel",
                "kvm",
                "-accel",
                "tcg",
                "-drive",
                "if=pflash,format=raw,unit=0,readonly=on,file=" + str(self.root / "firmware.fd"),
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "stdio",
                "-nic",
                "none",
                "-no-reboot",
                "-rtc",
                "base=2000-01-01T00:00:00,clock=vm",
                "-uuid",
                "00000000-0000-0000-0000-000000000001",
            ],
        )

    def test_argument_parser_accepts_direct_kernel_without_firmware(self):
        parser = argparse.ArgumentParser()
        add_uefi_qemu_arguments(parser)
        args = parser.parse_args(
            [
                "--ostest-arch=aarch64",
                "--ostest-qemu=qemu",
                "--ostest-boot=direct-kernel",
                "--ostest-kernel=kernel",
                "--ostest-initrd=initrd",
                "--ostest-kernel-args=console=ttyAMA0",
                "--ostest-allow-reboot",
                "--ostest-graphics",
                "--ostest-cpu-model=cortex-a72",
            ]
        )
        config = UefiQemuConfig.from_namespace(args)
        self.assertIsNone(config.firmware)
        self.assertTrue(config.allow_reboot)
        self.assertTrue(config.graphics)
        self.assertEqual(config.cpu_model, "cortex-a72")

    def test_direct_aarch64_defaults_render_kernel_initrd_graphics_and_reboots(self):
        command = self.direct_config(graphics=True, allow_reboot=True).command()
        self.assertEqual(command[command.index("-machine") + 1], "virt,gic-version=2")
        self.assertEqual(command[command.index("-cpu") + 1], "cortex-a53")
        self.assertEqual(command[command.index("-kernel") + 1], str(self.root / "kernel"))
        self.assertEqual(command[command.index("-initrd") + 1], str(self.root / "initrd"))
        self.assertEqual(command[command.index("-append") + 1], "console=ttyAMA0 test=one two")
        self.assertIn("virtio-gpu-pci", command)
        self.assertNotIn("-no-reboot", command)
        self.assertFalse(any("if=pflash" in value for value in command))

    def test_direct_aarch64_machine_and_cpu_overrides_win(self):
        config = self.direct_config(
            cpu_model="max",
            machine_options=("gic-version=3", "highmem=off"),
            graphics=True,
            graphics_device="ramfb",
        )
        command = config.command()
        self.assertEqual(command[command.index("-machine") + 1], "virt,gic-version=3,highmem=off")
        self.assertEqual(command[command.index("-cpu") + 1], "max")
        self.assertIn("ramfb", command)

    def test_gicv2_rejects_more_than_eight_cpus(self):
        with self.assertRaisesRegex(ValueError, "at most 8"):
            self.direct_config(cpus=9).command()

    def test_command_can_allow_reboots_without_mutating_config(self):
        config = self.direct_config()
        self.assertIn("-no-reboot", config.command())
        self.assertNotIn("-no-reboot", config.command(allow_reboot=True))

    def test_boot_validation(self):
        with self.assertRaisesRegex(ValueError, "UEFI boot requires firmware"):
            UefiQemuConfig.from_namespace(self.namespace(ostest_firmware=None))
        with self.assertRaisesRegex(ValueError, "requires a kernel"):
            UefiQemuConfig.from_namespace(
                self.namespace(ostest_boot="direct-kernel", ostest_firmware=None)
            )
        with self.assertRaisesRegex(ValueError, "require direct-kernel"):
            UefiQemuConfig.from_namespace(self.namespace(ostest_kernel="kernel"))

    def test_scratch_media_is_sparse_private_writable_and_has_no_bootindex(self):
        scratch = json.dumps(
            {
                "kind": "scratch",
                "name": "state",
                "interface": "virtio-blk",
                "format": "raw",
                "bootindex": None,
                "size_mb": 2,
                "export": True,
            }
        )
        config = UefiQemuConfig.from_namespace(self.namespace(ostest_media=[scratch]))
        medium = config.media[0]
        self.assertEqual(medium.name, "state")
        self.assertEqual(medium.path.stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(stat.S_IMODE(medium.path.stat().st_mode), 0o600)
        self.assertIsNone(medium.bootindex)
        self.assertTrue(medium.export)
        command = config.command()
        drive = command[command.index("-drive", command.index("-drive") + 1) + 1]
        self.assertNotIn("snapshot=on", drive)
        device = next(value for value in command if value.startswith("virtio-blk-pci,"))
        self.assertNotIn("bootindex", device)

    def test_image_media_export_is_copied_without_mutating_source(self):
        media = json.dumps(
            {
                "kind": "image",
                "name": "state",
                "path": "disk.img",
                "interface": "virtio-blk",
                "format": "raw",
                "compression": "none",
                "readonly": False,
                "snapshot": False,
                "bootindex": 2,
                "export": True,
            }
        )
        config = UefiQemuConfig.from_namespace(self.namespace(ostest_media=[media]))
        self.assertNotEqual(config.media[0].path, self.root / "disk.img")
        config.media[0].path.write_bytes(b"guest mutation")
        self.assertEqual((self.root / "disk.img").read_bytes(), b"source disk")

    def test_export_rejects_readonly_or_snapshot_media(self):
        readonly = json.dumps(
            {
                "name": "read-only",
                "path": "disk.img",
                "interface": "virtio-blk",
                "readonly": True,
                "snapshot": False,
                "export": True,
            }
        )
        with self.assertRaisesRegex(ValueError, "writable and non-snapshot"):
            UefiQemuConfig.from_namespace(self.namespace(ostest_media=[readonly]))

    def test_media_readback_requires_stopped_process_and_exports(self):
        source = self.tmp / "state.img"
        source.write_bytes(b"durable bytes")
        medium = QemuMedia("state", source, "virtio-blk", "raw", False, False, None)
        session = QemuSession(self.direct_config(media=(medium,)))
        with self.assertRaisesRegex(RuntimeError, "before"):
            session.media_path("state")
        session.process = _Process(None)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            session.media_path("state")
        session.process = _Process(0)
        self.assertEqual(session.media_path("state"), source)
        destination = session.export_media("state", self.outputs / "state.img")
        self.assertEqual(destination.read_bytes(), b"durable bytes")
        with self.assertRaises(KeyError):
            session.media_path("missing")

    def test_media_export_rejects_snapshot(self):
        medium = QemuMedia(
            "state",
            self.root / "disk.img",
            "virtio-blk",
            "raw",
            False,
            True,
            1,
        )
        session = QemuSession(self.direct_config(media=(medium,)))
        session.process = _Process(0)
        with self.assertRaisesRegex(RuntimeError, "snapshot-backed"):
            session.export_media("state", self.outputs / "state.img")

    def test_host_forward_json_and_usernet_rendering(self):
        entries = [
            json.dumps({"name": "grpc", "guest": 50051, "host": "auto", "protocol": "tcp"}),
            json.dumps({"name": "dns", "guest": 53, "host": 1053, "protocol": "udp"}),
        ]
        config = UefiQemuConfig.from_namespace(self.namespace(ostest_hostfwd=entries))
        self.assertEqual(
            config.hostfwd,
            (
                HostForwardRequest("grpc", 50051, None, "tcp"),
                HostForwardRequest("dns", 53, 1053, "udp"),
            ),
        )
        self.assertEqual(config.hostfwd[0].qemu_hostfwd, "tcp:127.0.0.1:0-:50051")
        command = config.command()
        usernet = next(value for value in command if value.startswith("user,id=ostest_usernet"))
        self.assertEqual(
            usernet,
            "user,id=ostest_usernet,ipv6=off,restrict=on,"
            "hostfwd=tcp:127.0.0.1:0-:50051,hostfwd=udp:127.0.0.1:1053-:53",
        )
        self.assertIn("virtio-net-pci,netdev=ostest_usernet,mac=52:54:00:ff:00:01", command)

    def test_info_usernet_resolves_only_automatic_forwardings(self):
        config = self.direct_config(
            hostfwd=(
                HostForwardRequest("grpc", 50051, None, "tcp"),
                HostForwardRequest("dns", 53, 1053, "udp"),
            )
        )
        session = QemuSession(config)
        output = """
  Protocol[State]    FD  Source Address  Port   Dest. Address   Port RecvQ SendQ
  TCP [ HOST_FORWARD ]  11    127.0.0.1 43123       10.0.2.15 50051     0     0
"""
        with mock.patch.object(session, "execute", return_value=output) as execute:
            resolved = session._resolve_host_forwards(timeout=1)
        execute.assert_called_once_with(
            "human-monitor-command",
            {"command-line": "info usernet"},
            timeout=1,
        )
        self.assertEqual(
            resolved,
            (
                ResolvedHostForward("grpc", "127.0.0.1", 43123, 50051, "tcp"),
                ResolvedHostForward("dns", "127.0.0.1", 1053, 53, "udp"),
            ),
        )
        self.assertEqual(
            resolved[0].as_mapping(),
            {
                "guest_port": 50051,
                "host": "127.0.0.1",
                "host_port": 43123,
                "protocol": "tcp",
            },
        )

    def test_static_host_forward_does_not_query_qmp(self):
        config = self.direct_config(hostfwd=(HostForwardRequest("web", 80, 8080, "tcp"),))
        session = QemuSession(config)
        with mock.patch.object(session, "execute") as execute:
            resolved = session._resolve_host_forwards(timeout=1)
        execute.assert_not_called()
        self.assertEqual(resolved[0].host_port, 8080)

    def test_host_forward_property_and_named_lookup(self):
        config = self.direct_config(hostfwd=(HostForwardRequest("web", 80, 8080, "tcp"),))
        session = QemuSession(config)
        with self.assertRaisesRegex(RuntimeError, "before"):
            _ = session.host_forwards
        session.process = _Process(None)
        session._host_forwards = (
            ResolvedHostForward("web", "127.0.0.1", 8080, 80, "tcp"),
        )
        self.assertEqual(session.host_forward("web").host_port, 8080)
        with self.assertRaises(KeyError):
            session.host_forward("missing")

    def test_lab_json_propagates_new_configuration(self):
        spec = {
            "name": "arm",
            "arch": "aarch64",
            "qemu": "qemu",
            "firmware": None,
            "boot": "direct-kernel",
            "kernel": "kernel",
            "initrd": "initrd",
            "kernel_args": "console=ttyAMA0",
            "cpu_model": "cortex-a72",
            "graphics": True,
            "graphics_device": "ramfb",
            "allow_reboot": True,
            "hostfwd": [
                {"name": "web", "guest": 80, "host": "auto", "protocol": "tcp"}
            ],
        }
        lab = UefiLabConfig.from_namespace(
            argparse.Namespace(ostest_lab_vm=[json.dumps(spec)])
        )
        config = lab.participants[0].config
        self.assertEqual(config.boot, "direct-kernel")
        self.assertEqual(config.kernel, self.root / "kernel")
        self.assertTrue(config.allow_reboot)
        self.assertEqual(config.graphics_device, "ramfb")
        self.assertIsNone(config.hostfwd[0].host_port)


if __name__ == "__main__":
    unittest.main()
