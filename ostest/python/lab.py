"""Single-process orchestration for hermetic multi-VM UEFI network tests."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
from dataclasses import dataclass

from ostest.python.network import EthernetEndpoint, EthernetHub
from ostest.python.qemu import QemuSession, UefiQemuConfig


def add_uefi_lab_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds the configuration argument populated by ``uefi_lab_test``."""

    group = parser.add_argument_group("rules_ostest lab")
    group.add_argument("--ostest-lab-vm", action="append", default=[], required=True)


@dataclass(frozen=True)
class NetworkAttachment:
    name: str
    mac: str
    model: str


@dataclass(frozen=True)
class UefiVmParticipant:
    name: str
    config: UefiQemuConfig
    networks: tuple[NetworkAttachment, ...]


@dataclass(frozen=True)
class UefiLabConfig:
    participants: tuple[UefiVmParticipant, ...]

    @classmethod
    def from_namespace(cls, namespace: argparse.Namespace) -> "UefiLabConfig":
        participants = []
        names = set()
        for encoded in namespace.ostest_lab_vm:
            spec = json.loads(encoded)
            name = str(spec["name"])
            if name in names:
                raise ValueError(f"duplicate lab participant name: {name!r}")
            names.add(name)
            firmware = spec.get("firmware")
            config_namespace = argparse.Namespace(
                ostest_arch=spec.get("arch"),
                ostest_arch_file=spec.get("arch_file"),
                ostest_allow_reboot=bool(spec.get("allow_reboot", False)),
                ostest_boot=str(spec.get("boot", "uefi")),
                ostest_cpus=int(spec.get("cpus", 1)),
                ostest_cpu_model=spec.get("cpu_model"),
                ostest_debugcon=bool(spec.get("debugcon", False)),
                ostest_disk=None,
                ostest_firmware=str(firmware) if firmware is not None else None,
                ostest_firmware_vars=spec.get("firmware_vars"),
                ostest_gdb=bool(spec.get("gdb", False)),
                ostest_graphics=bool(spec.get("graphics", False)),
                ostest_graphics_device=spec.get("graphics_device"),
                ostest_hostfwd=[
                    json.dumps(hostfwd, separators=(",", ":"))
                    for hostfwd in spec.get("hostfwd", [])
                ],
                ostest_initrd=spec.get("initrd"),
                ostest_kernel=spec.get("kernel"),
                ostest_kernel_args=str(spec.get("kernel_args", "")),
                ostest_media=[json.dumps(media, separators=(",", ":")) for media in spec.get("media", [])],
                ostest_memory_mb=int(spec.get("memory_mb", 256)),
                ostest_machine_option=list(spec.get("machine_options", [])),
                ostest_pause_at_start=bool(spec.get("pause_at_start", False)),
                ostest_qemu=str(spec["qemu"]),
                ostest_qemu_arg=list(spec.get("qemu_args", [])),
                ostest_require_kvm=bool(spec.get("require_kvm", False)),
            )
            config = UefiQemuConfig.from_namespace(config_namespace, instance_name=name)
            networks = tuple(
                NetworkAttachment(
                    name=str(network["name"]),
                    mac=str(network["mac"]),
                    model=str(network.get("model", "virtio-net-pci")),
                )
                for network in spec.get("networks", [])
            )
            participants.append(UefiVmParticipant(name=name, config=config, networks=networks))
        if not participants:
            raise ValueError("a UEFI lab must contain at least one VM")
        return cls(tuple(participants))


class QemuLab:
    """Owns all VMs and isolated Ethernet segments in one Bazel test process."""

    def __init__(self, config: UefiLabConfig, *, startup_timeout: float = 10.0):
        self.config = config
        self.startup_timeout = startup_timeout
        self._hubs: dict[str, EthernetHub] = {}
        self._sessions: dict[str, QemuSession] = {}

    @property
    def participant_names(self) -> tuple[str, ...]:
        return tuple(participant.name for participant in self.config.participants)

    def __getitem__(self, name: str) -> QemuSession:
        return self._sessions[name]

    def host_endpoint(self, network: str = "lan") -> EthernetEndpoint:
        """Adds the Python test process as a raw-Ethernet network participant."""

        try:
            hub = self._hubs[network]
        except KeyError as error:
            raise KeyError(f"unknown lab network {network!r}") from error
        return hub.host_endpoint()

    def start(self) -> "QemuLab":
        if self._sessions:
            raise RuntimeError("QEMU lab has already been started")
        network_names = {
            attachment.name
            for participant in self.config.participants
            for attachment in participant.networks
        }
        test_tmpdir = pathlib.Path(os.environ["TEST_TMPDIR"])
        artifacts_dir = pathlib.Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", test_tmpdir))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._hubs = {
            name: EthernetHub(name, pcap_path=artifacts_dir / f"network-{name}.pcap")
            for name in sorted(network_names)
        }
        try:
            for participant in self.config.participants:
                nics = tuple(
                    self._hubs[attachment.name].qemu_nic(
                        mac=attachment.mac,
                        model=attachment.model,
                    )
                    for attachment in participant.networks
                )
                session = QemuSession(
                    participant.config,
                    startup_timeout=self.startup_timeout,
                    nics=nics,
                )
                session.start()
                self._sessions[participant.name] = session
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for session in reversed(tuple(self._sessions.values())):
            session.terminate()
        self._sessions.clear()
        for hub in self._hubs.values():
            hub.close()
        self._hubs.clear()

    def __enter__(self) -> "QemuLab":
        return self.start()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
