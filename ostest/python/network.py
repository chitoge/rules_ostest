"""Process-local Ethernet networks for hermetic QEMU test labs."""

from __future__ import annotations

import os
import pathlib
import socket
import struct
import threading
import time
from dataclasses import dataclass


MAX_FRAME_SIZE = 1024 * 1024


def _connected_tcp_pair() -> tuple[socket.socket, socket.socket]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError
        chunks.extend(chunk)
    return bytes(chunks)


@dataclass
class QemuNic:
    """One socket-backed NIC ready to be handed to QEMU."""

    connection: socket.socket
    mac: str
    model: str

    def fileno(self) -> int:
        return self.connection.fileno()

    def handoff_complete(self) -> None:
        self.connection.close()


class EthernetEndpoint:
    """Host-side participant that sends and receives raw Ethernet frames."""

    def __init__(self, connection: socket.socket):
        self._connection = connection

    def send(self, frame: bytes) -> None:
        if not 0 < len(frame) <= MAX_FRAME_SIZE:
            raise ValueError("Ethernet frame has an invalid size")
        self._connection.sendall(struct.pack("!I", len(frame)) + frame)

    def receive(self, *, timeout: float = 5.0) -> bytes:
        self._connection.settimeout(timeout)
        try:
            size = struct.unpack("!I", _receive_exact(self._connection, 4))[0]
            if size <= 0 or size > MAX_FRAME_SIZE:
                raise RuntimeError(f"network endpoint received invalid frame length {size}")
            return _receive_exact(self._connection, size)
        finally:
            self._connection.settimeout(None)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EthernetEndpoint":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


class EthernetHub:
    """A deterministic in-process learning-free Ethernet broadcast hub.

    QEMU's socket network backend uses a four-byte network-order frame length.
    Each port receives every frame sent by every other port. No TAP device,
    external bridge, privilege, or externally reachable socket is involved.
    """

    def __init__(self, name: str = "lan", *, pcap_path: os.PathLike[str] | str | None = None):
        self.name = name
        self._connections: set[socket.socket] = set()
        self._participant_connections: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._closed = False
        self._pcap = None
        if pcap_path is not None:
            path = pathlib.Path(pcap_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._pcap = path.open("wb")
            # PCAP, microsecond timestamps, Ethernet link type.
            self._pcap.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
            self._pcap.flush()

    def _new_port(self) -> socket.socket:
        if self._closed:
            raise RuntimeError("Ethernet hub is closed")
        participant, hub = _connected_tcp_pair()
        with self._lock:
            self._connections.add(hub)
            self._participant_connections.add(participant)
        thread = threading.Thread(target=self._forward_from, args=(hub,), daemon=True)
        self._threads.append(thread)
        thread.start()
        return participant

    def qemu_nic(self, *, mac: str, model: str = "virtio-net-pci") -> QemuNic:
        return QemuNic(self._new_port(), mac, model)

    def host_endpoint(self) -> EthernetEndpoint:
        return EthernetEndpoint(self._new_port())

    def _forward_from(self, source: socket.socket) -> None:
        try:
            while True:
                size = struct.unpack("!I", _receive_exact(source, 4))[0]
                if size <= 0 or size > MAX_FRAME_SIZE:
                    raise RuntimeError(f"invalid QEMU socket-network frame length {size}")
                frame = _receive_exact(source, size)
                self._capture(frame)
                packet = struct.pack("!I", size) + frame
                with self._lock:
                    destinations = tuple(self._connections - {source})
                for destination in destinations:
                    try:
                        destination.sendall(packet)
                    except OSError:
                        self._remove(destination)
        except (EOFError, OSError):
            pass
        finally:
            self._remove(source)

    def _capture(self, frame: bytes) -> None:
        if self._pcap is None:
            return
        timestamp = time.time_ns()
        seconds, nanoseconds = divmod(timestamp, 1_000_000_000)
        header = struct.pack("<IIII", seconds, nanoseconds // 1000, len(frame), len(frame))
        with self._capture_lock:
            self._pcap.write(header)
            self._pcap.write(frame)
            self._pcap.flush()

    def _remove(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.discard(connection)
        try:
            connection.close()
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            connections = tuple(self._connections | self._participant_connections)
            self._connections.clear()
            self._participant_connections.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in self._threads:
            thread.join(timeout=1)
        if self._pcap is not None:
            self._pcap.close()
            self._pcap = None

    def __enter__(self) -> "EthernetHub":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
