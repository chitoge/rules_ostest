#!/usr/bin/env python3
"""Boot two real CirrOS guests and prove bidirectional isolated-LAN traffic."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import pathlib
import socket
import struct
import time

from ostest.python.lab import QemuLab, UefiLabConfig, add_uefi_lab_arguments
from ostest.python.network import EthernetEndpoint


SERVER_MAC = bytes.fromhex("52 54 00 12 34 10")
CLIENT_MAC = bytes.fromhex("52 54 00 12 34 11")
REQUEST = b"rules-ostest-nonce-v1"
ACK = b"rules-ostest-ack-v1"
DHCP_SERVER_MAC = bytes.fromhex("52 54 00 12 34 fe")
DHCP_SERVER_IP = socket.inet_aton("192.0.2.1")
DHCP_ADDRESSES = {
    SERVER_MAC: socket.inet_aton("192.0.2.10"),
    CLIENT_MAC: socket.inet_aton("192.0.2.11"),
}


def _internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _dhcp_type(payload: bytes) -> int | None:
    if len(payload) < 240 or payload[236:240] != b"\x63\x82\x53\x63":
        return None
    offset = 240
    while offset < len(payload):
        option = payload[offset]
        offset += 1
        if option == 255:
            return None
        if option == 0:
            continue
        if offset >= len(payload):
            return None
        length = payload[offset]
        offset += 1
        value = payload[offset : offset + length]
        offset += length
        if option == 53 and len(value) == 1:
            return value[0]
    return None


def _dhcp_reply(request: bytes, message_type: int, address: bytes) -> bytes:
    bootp = bytearray(240)
    bootp[0:4] = b"\x02\x01\x06\x00"
    bootp[4:8] = request[4:8]
    bootp[10:12] = request[10:12]
    bootp[16:20] = address
    bootp[20:24] = DHCP_SERVER_IP
    bootp[28:44] = request[28:44]
    bootp[236:240] = b"\x63\x82\x53\x63"
    options = b"".join(
        (
            bytes((53, 1, message_type)),
            b"\x36\x04" + DHCP_SERVER_IP,
            b"\x33\x04" + struct.pack("!I", 600),
            b"\x01\x04\xff\xff\xff\x00",
            b"\x1c\x04\xc0\x00\x02\xff",
            b"\xff",
        )
    )
    udp_payload = bytes(bootp) + options
    udp = struct.pack("!HHHH", 67, 68, 8 + len(udp_payload), 0) + udp_payload
    ip_header = bytearray(
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(udp),
            0,
            0,
            64,
            socket.IPPROTO_UDP,
            0,
            DHCP_SERVER_IP,
            b"\xff\xff\xff\xff",
        )
    )
    struct.pack_into("!H", ip_header, 10, _internet_checksum(bytes(ip_header)))
    return b"\xff" * 6 + DHCP_SERVER_MAC + b"\x08\x00" + bytes(ip_header) + udp


def _serve_dhcp(endpoint: EthernetEndpoint) -> None:
    acknowledged = set()
    deadline = time.monotonic() + 90
    while acknowledged != set(DHCP_ADDRESSES):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"DHCP did not configure guest MACs: {acknowledged!r}")
        try:
            frame = endpoint.receive(timeout=1)
        except TimeoutError:
            continue
        if len(frame) < 14 + 20 + 8 + 240 or frame[12:14] != b"\x08\x00":
            continue
        ip_offset = 14
        header_length = (frame[ip_offset] & 0x0F) * 4
        udp_offset = ip_offset + header_length
        if frame[ip_offset + 9] != socket.IPPROTO_UDP or len(frame) < udp_offset + 8:
            continue
        source_port, destination_port = struct.unpack_from("!HH", frame, udp_offset)
        if (source_port, destination_port) != (68, 67):
            continue
        request = frame[udp_offset + 8 :]
        client_mac = request[28:34]
        address = DHCP_ADDRESSES.get(client_mac)
        message_type = _dhcp_type(request)
        if address is None or message_type not in (1, 3):
            continue
        reply_type = 2 if message_type == 1 else 5
        endpoint.send(_dhcp_reply(request, reply_type, address))
        if reply_type == 5:
            acknowledged.add(client_mac)


def _pcap_frames(data: bytes) -> tuple[bytes, ...]:
    if len(data) < 24 or data[:4] != b"\xd4\xc3\xb2\xa1":
        raise AssertionError("isolated network capture is not little-endian Ethernet PCAP")
    frames = []
    offset = 24
    while offset < len(data):
        if offset + 16 > len(data):
            raise AssertionError("truncated PCAP packet header")
        _seconds, _microseconds, captured, original = struct.unpack_from("<IIII", data, offset)
        offset += 16
        if captured != original or offset + captured > len(data):
            raise AssertionError("truncated PCAP packet payload")
        frames.append(data[offset : offset + captured])
        offset += captured
    return tuple(frames)


def main() -> int:
    parser = argparse.ArgumentParser()
    add_uefi_lab_arguments(parser)
    config = UefiLabConfig.from_namespace(parser.parse_args())
    assert tuple(participant.name for participant in config.participants) == ("server", "client")

    with QemuLab(config) as lab, lab.host_endpoint("lan") as dhcp_endpoint:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            dhcp = executor.submit(_serve_dhcp, dhcp_endpoint)
            server_serial = executor.submit(
                lab["server"].wait_for_serial,
                "OSTEST: SERVER SENT ACK",
                timeout=120,
            )
            client_serial = executor.submit(
                lab["client"].wait_for_serial,
                "OSTEST: CLIENT RECEIVED ACK",
                timeout=120,
            )
            server_serial.result(timeout=125)
            client_serial.result(timeout=125)
            dhcp.result(timeout=5)
        for marker in (
            "OSTEST: SERVER NETWORK READY",
            "OSTEST: SERVER RECEIVED NONCE",
            "OSTEST: SERVER SENT ACK",
        ):
            assert marker in lab["server"].serial_text
        for marker in (
            "OSTEST: CLIENT NETWORK READY",
            "OSTEST: CLIENT RECEIVED ACK",
        ):
            assert marker in lab["client"].serial_text
        assert "OSTEST: FAIL" not in lab["server"].serial_text
        assert "OSTEST: FAIL" not in lab["client"].serial_text

    capture = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "network-lan.pcap"
    frames = _pcap_frames(capture.read_bytes())
    assert any(len(frame) >= 14 and frame[6:12] == CLIENT_MAC for frame in frames)
    assert any(len(frame) >= 14 and frame[6:12] == SERVER_MAC for frame in frames)
    assert any(frame[6:12] == CLIENT_MAC and REQUEST in frame for frame in frames)
    assert any(frame[6:12] == SERVER_MAC and ACK in frame for frame in frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
