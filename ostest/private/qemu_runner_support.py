"""Reusable support primitives for the serial-driven QEMU test runner.

This module intentionally contains no Bazel/runfiles integration.  The runner
owns command-line parsing and process orchestration; the helpers here model
stream assertions, host capability probes, artifacts, and bounded auxiliary
processes in forms that can be unit tested without QEMU.
"""

from __future__ import annotations

import enum
import os
import pathlib
import platform
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]


Marker = str | bytes


class PhaseTransition(str, enum.Enum):
    """Action required after all markers in an assertion phase match."""

    COMPLETE = "complete"
    REBOOT = "reboot"


class AssertionOutcome(str, enum.Enum):
    """Current terminal state of a serial assertion machine."""

    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"


def _marker_bytes(marker: Marker, *, description: str) -> bytes:
    if isinstance(marker, str):
        encoded = marker.encode("utf-8")
    elif isinstance(marker, bytes):
        encoded = marker
    else:
        raise TypeError(f"{description} must be str or bytes, got {type(marker).__name__}")
    if not encoded:
        raise ValueError(f"{description} must not be empty")
    return encoded


@dataclass(frozen=True)
class AssertionPhase:
    """One ordered set of literal serial markers and its terminal action."""

    markers: tuple[bytes, ...] | Sequence[Marker]
    then: PhaseTransition | str = PhaseTransition.COMPLETE

    def __post_init__(self) -> None:
        markers = tuple(
            _marker_bytes(marker, description="phase marker") for marker in self.markers
        )
        if not markers:
            raise ValueError("an assertion phase must contain at least one marker")
        try:
            transition = PhaseTransition(self.then)
        except ValueError as error:
            raise ValueError(f"unsupported phase transition: {self.then!r}") from error
        object.__setattr__(self, "markers", markers)
        object.__setattr__(self, "then", transition)


@dataclass(frozen=True)
class AssertionResult:
    """Immutable snapshot returned after every assertion-machine event."""

    outcome: AssertionOutcome
    reason: str
    phase_index: int
    marker_index: int
    waiting_for_reboot: bool

    @property
    def terminal(self) -> bool:
        return self.outcome is not AssertionOutcome.RUNNING


class SerialAssertionMachine:
    """Matches ordered literals and forbidden literals over a byte stream.

    Matching is bounded: only the suffix that can begin the next ordered or
    forbidden marker is retained.  Forbidden markers are searched before
    success markers for each input chunk, so a chunk containing both always
    fails.  A reboot phase advances only after an explicit guest RESET event;
    serial data observed while waiting for that event cannot satisfy the next
    phase.
    """

    def __init__(
        self,
        phases: Sequence[AssertionPhase],
        *,
        forbidden_markers: Sequence[Marker] = (),
    ) -> None:
        self._phases = tuple(phases)
        if not self._phases:
            raise ValueError("at least one assertion phase is required")
        for index, phase in enumerate(self._phases):
            if not isinstance(phase, AssertionPhase):
                raise TypeError("phases must contain AssertionPhase values")
            expected = (
                PhaseTransition.REBOOT
                if index < len(self._phases) - 1
                else PhaseTransition.COMPLETE
            )
            if phase.then is not expected:
                raise ValueError(
                    f"phase {index} must end with {expected.value!r}, got {phase.then.value!r}"
                )
        self._forbidden = tuple(
            _marker_bytes(marker, description="forbidden marker")
            for marker in forbidden_markers
        )
        self._forbidden_overlap = max((len(marker) - 1 for marker in self._forbidden), default=0)
        self._forbidden_tail = b""
        self._marker_buffer = b""
        self._phase_index = 0
        self._marker_index = 0
        self._waiting_for_reboot = False
        self._outcome = AssertionOutcome.RUNNING
        self._reason = "waiting for phase 1 marker 1"

    @classmethod
    def ordered(
        cls,
        markers: Sequence[Marker],
        *,
        forbidden_markers: Sequence[Marker] = (),
    ) -> "SerialAssertionMachine":
        """Constructs a single-phase ordered-marker assertion machine."""

        return cls(
            [AssertionPhase(tuple(markers), PhaseTransition.COMPLETE)],
            forbidden_markers=forbidden_markers,
        )

    @property
    def result(self) -> AssertionResult:
        return AssertionResult(
            outcome=self._outcome,
            reason=self._reason,
            phase_index=self._phase_index,
            marker_index=self._marker_index,
            waiting_for_reboot=self._waiting_for_reboot,
        )

    @property
    def retained_bytes(self) -> int:
        """Number of assertion bytes currently retained across stream chunks."""

        return len(self._forbidden_tail) + len(self._marker_buffer)

    def _fail(self, reason: str) -> AssertionResult:
        self._outcome = AssertionOutcome.FAIL
        self._reason = reason
        return self.result

    def feed(self, chunk: bytes | str) -> AssertionResult:
        """Consumes serial data and returns the updated assertion snapshot."""

        if self._outcome is not AssertionOutcome.RUNNING:
            return self.result
        if isinstance(chunk, str):
            data = chunk.encode("utf-8")
        elif isinstance(chunk, bytes):
            data = chunk
        else:
            raise TypeError(f"serial chunk must be str or bytes, got {type(chunk).__name__}")
        if not data:
            return self.result

        forbidden_window = self._forbidden_tail + data
        first_forbidden: tuple[int, int, bytes] | None = None
        for order, marker in enumerate(self._forbidden):
            position = forbidden_window.find(marker)
            if position >= 0:
                candidate = (position, order, marker)
                if first_forbidden is None or candidate[:2] < first_forbidden[:2]:
                    first_forbidden = candidate
        if first_forbidden is not None:
            marker = first_forbidden[2].decode("utf-8", errors="replace")
            return self._fail(f"matched forbidden marker {marker!r}")
        if self._forbidden_overlap:
            self._forbidden_tail = forbidden_window[-self._forbidden_overlap :]
        else:
            self._forbidden_tail = b""

        if self._waiting_for_reboot:
            return self.result

        self._marker_buffer += data
        phase = self._phases[self._phase_index]
        while self._outcome is AssertionOutcome.RUNNING and not self._waiting_for_reboot:
            marker = phase.markers[self._marker_index]
            position = self._marker_buffer.find(marker)
            if position < 0:
                overlap = len(marker) - 1
                self._marker_buffer = self._marker_buffer[-overlap:] if overlap else b""
                self._reason = (
                    f"waiting for phase {self._phase_index + 1} marker "
                    f"{self._marker_index + 1}: {marker.decode('utf-8', errors='replace')!r}"
                )
                break
            self._marker_buffer = self._marker_buffer[position + len(marker) :]
            self._marker_index += 1
            if self._marker_index < len(phase.markers):
                continue
            if phase.then is PhaseTransition.COMPLETE:
                self._outcome = AssertionOutcome.PASS
                self._reason = f"matched all markers in {len(self._phases)} phase(s)"
                self._marker_buffer = b""
            else:
                self._waiting_for_reboot = True
                self._reason = f"phase {self._phase_index + 1} complete; waiting for guest reboot"
                # Bytes emitted before the RESET boundary must not satisfy the
                # next phase, even if they followed the last phase marker.
                self._marker_buffer = b""
        return self.result

    def handle_reset(self, *, guest: bool | None, reason: str | None) -> AssertionResult:
        """Consumes a QMP RESET event and enforces the phase boundary.

        Current QEMU reports guest-initiated resets with ``guest=true`` and a
        ``guest-reset`` reason.  Accepting either signal also supports older
        pinned QEMU versions whose RESET metadata was less consistent.
        """

        if self._outcome is not AssertionOutcome.RUNNING:
            return self.result
        if guest is not True and reason != "guest-reset":
            return self._fail(
                f"received non-guest RESET while in phase {self._phase_index + 1}"
            )
        if not self._waiting_for_reboot:
            return self._fail(
                f"received unexpected guest RESET before phase {self._phase_index + 1} completed"
            )
        self._phase_index += 1
        self._marker_index = 0
        self._marker_buffer = b""
        self._waiting_for_reboot = False
        marker = self._phases[self._phase_index].markers[0]
        self._reason = (
            f"guest reboot entered phase {self._phase_index + 1}; waiting for marker 1: "
            f"{marker.decode('utf-8', errors='replace')!r}"
        )
        return self.result

    def handle_qmp_event(self, event: Mapping[str, object]) -> AssertionResult:
        """Consumes RESET events from a decoded QMP event object.

        Non-RESET events do not change the assertion state.
        """

        if event.get("event") != "RESET":
            return self.result
        raw_data = event.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else {}
        guest_value = data.get("guest")
        reason_value = data.get("reason")
        return self.handle_reset(
            guest=guest_value if isinstance(guest_value, bool) else None,
            reason=reason_value if isinstance(reason_value, str) else None,
        )

    def process_exited(self, returncode: int) -> AssertionResult:
        """Fails a still-running assertion plan after an early QEMU exit."""

        if self._outcome is AssertionOutcome.RUNNING:
            return self._fail(
                f"QEMU exited with status {returncode} before completing assertion phases"
            )
        return self.result

    def timed_out(self, timeout_seconds: float) -> AssertionResult:
        """Fails a still-running assertion plan at its runner-owned deadline."""

        if self._outcome is AssertionOutcome.RUNNING:
            return self._fail(f"timed out after {timeout_seconds:g} seconds")
        return self.result


class PpmError(ValueError):
    """A PPM image is malformed, exceeds bounds, or lacks enough variation."""


@dataclass(frozen=True)
class PpmInfo:
    """Validated metadata for a PPM distinct-pixel assertion."""

    format: str
    width: int
    height: int
    max_value: int
    pixel_count: int
    distinct_pixels: int


_PPM_WHITESPACE = b" \t\r\n\v\f"


def _bounded_source_bytes(
    source: bytes | bytearray | memoryview | os.PathLike[str] | str,
    max_file_bytes: int,
) -> bytes:
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if len(data) > max_file_bytes:
            raise PpmError(f"PPM exceeds the {max_file_bytes}-byte input limit")
        return data
    path = pathlib.Path(source)
    with path.open("rb") as stream:
        data = stream.read(max_file_bytes + 1)
    if len(data) > max_file_bytes:
        raise PpmError(f"PPM exceeds the {max_file_bytes}-byte input limit")
    return data


def _skip_ppm_space_and_comments(data: bytes, position: int) -> int:
    while True:
        while position < len(data) and data[position] in _PPM_WHITESPACE:
            position += 1
        if position >= len(data) or data[position] != ord("#"):
            return position
        newline = data.find(b"\n", position + 1)
        if newline < 0:
            return len(data)
        position = newline + 1


def _ppm_token(data: bytes, position: int) -> tuple[bytes, int]:
    position = _skip_ppm_space_and_comments(data, position)
    start = position
    while (
        position < len(data)
        and data[position] not in _PPM_WHITESPACE
        and data[position] != ord("#")
    ):
        position += 1
    if start == position:
        raise PpmError("PPM header or raster ended before the expected token")
    return data[start:position], position


def _ppm_integer(token: bytes, name: str) -> int:
    if not token or any(byte < ord("0") or byte > ord("9") for byte in token):
        raise PpmError(f"PPM {name} is not an unsigned decimal integer: {token!r}")
    return int(token)


def validate_ppm_distinct_pixels(
    source: bytes | bytearray | memoryview | os.PathLike[str] | str,
    *,
    min_distinct_pixels: int = 2,
    max_file_bytes: int = 64 * 1024 * 1024,
    max_pixels: int = 16 * 1024 * 1024,
) -> PpmInfo:
    """Validates a bounded P3/P6 PPM and requires distinct pixel colors.

    The returned ``distinct_pixels`` value is capped at
    ``min_distinct_pixels``.  This keeps memory proportional to the requested
    threshold instead of to adversarial image color diversity.
    """

    if min_distinct_pixels <= 0:
        raise ValueError("min_distinct_pixels must be positive")
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    data = _bounded_source_bytes(source, max_file_bytes)
    position = 0
    magic, position = _ppm_token(data, position)
    if magic not in (b"P3", b"P6"):
        raise PpmError(f"unsupported PPM magic {magic!r}; expected P3 or P6")
    width_token, position = _ppm_token(data, position)
    height_token, position = _ppm_token(data, position)
    maximum_token, position = _ppm_token(data, position)
    width = _ppm_integer(width_token, "width")
    height = _ppm_integer(height_token, "height")
    maximum = _ppm_integer(maximum_token, "maximum sample")
    if width <= 0 or height <= 0:
        raise PpmError("PPM width and height must be positive")
    if maximum <= 0 or maximum > 65535:
        raise PpmError("PPM maximum sample must be between 1 and 65535")
    if width > max_pixels or height > max_pixels or width * height > max_pixels:
        raise PpmError(f"PPM exceeds the {max_pixels}-pixel limit")
    pixel_count = width * height
    distinct: set[tuple[int, int, int]] = set()

    def observe(pixel: tuple[int, int, int]) -> None:
        if len(distinct) < min_distinct_pixels:
            distinct.add(pixel)

    if magic == b"P6":
        if position >= len(data) or data[position] not in _PPM_WHITESPACE:
            raise PpmError("P6 maximum sample must be followed by raster whitespace")
        if data[position : position + 2] == b"\r\n":
            position += 2
        else:
            position += 1
        bytes_per_sample = 1 if maximum < 256 else 2
        expected_bytes = pixel_count * 3 * bytes_per_sample
        raster = data[position:]
        if len(raster) != expected_bytes:
            raise PpmError(
                f"P6 raster has {len(raster)} bytes; expected exactly {expected_bytes}"
            )
        if bytes_per_sample == 1:
            for offset in range(0, len(raster), 3):
                pixel = (raster[offset], raster[offset + 1], raster[offset + 2])
                if any(sample > maximum for sample in pixel):
                    raise PpmError("P6 raster contains a sample above its declared maximum")
                observe(pixel)
        else:
            for offset in range(0, len(raster), 6):
                pixel = (
                    (raster[offset] << 8) | raster[offset + 1],
                    (raster[offset + 2] << 8) | raster[offset + 3],
                    (raster[offset + 4] << 8) | raster[offset + 5],
                )
                if any(sample > maximum for sample in pixel):
                    raise PpmError("P6 raster contains a sample above its declared maximum")
                observe(pixel)
    else:
        samples: list[int] = []
        for sample_index in range(pixel_count * 3):
            token, position = _ppm_token(data, position)
            sample = _ppm_integer(token, f"sample {sample_index + 1}")
            if sample > maximum:
                raise PpmError("P3 raster contains a sample above its declared maximum")
            samples.append(sample)
            if len(samples) == 3:
                observe((samples[0], samples[1], samples[2]))
                samples.clear()
        if _skip_ppm_space_and_comments(data, position) != len(data):
            raise PpmError("P3 raster contains more samples than declared dimensions")

    if len(distinct) < min_distinct_pixels:
        raise PpmError(
            f"PPM contains only {len(distinct)} distinct pixel color(s); "
            f"expected at least {min_distinct_pixels}"
        )
    return PpmInfo(
        format=magic.decode("ascii"),
        width=width,
        height=height,
        max_value=maximum,
        pixel_count=pixel_count,
        distinct_pixels=len(distinct),
    )


KVM_GET_API_VERSION = 0xAE00
KVM_CREATE_VM = 0xAE01
KVM_EXPECTED_API_VERSION = 12

_ARCHITECTURE_ALIASES = {
    "aarch64": "aarch64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "x64": "x86_64",
    "x86-64": "x86_64",
    "x86_64": "x86_64",
}


def normalize_architecture(value: str) -> str | None:
    """Normalizes supported host/guest architecture aliases."""

    return _ARCHITECTURE_ALIASES.get(value.strip().lower())


@dataclass(frozen=True)
class KvmProbeResult:
    """Result of a side-effect-minimal Linux KVM availability probe."""

    available: bool
    reason: str
    guest_arch: str
    host_arch: str | None
    api_version: int | None = None


def probe_linux_kvm(
    guest_arch: str,
    *,
    host_arch: str | None = None,
    system: str | None = None,
    device_path: os.PathLike[str] | str = "/dev/kvm",
    open_fn: Callable[[str, int], int] = os.open,
    ioctl_fn: Callable[[int, int, int], int] | None = None,
    close_fn: Callable[[int], None] = os.close,
) -> KvmProbeResult:
    """Checks architecture, KVM API compatibility, and VM creation.

    The syscall functions and host metadata are injectable so callers can test
    every outcome without requiring or mutating host KVM state.
    """

    normalized_guest = normalize_architecture(guest_arch)
    if normalized_guest is None:
        raise ValueError(f"unsupported guest architecture: {guest_arch!r}")
    system_name = platform.system() if system is None else system
    machine_name = platform.machine() if host_arch is None else host_arch
    normalized_host = normalize_architecture(machine_name)
    if system_name.lower() != "linux":
        return KvmProbeResult(
            False,
            f"KVM requires Linux; execution host reports {system_name!r}",
            normalized_guest,
            normalized_host,
        )
    if normalized_host is None:
        return KvmProbeResult(
            False,
            f"unsupported KVM host architecture {machine_name!r}",
            normalized_guest,
            None,
        )
    if normalized_host != normalized_guest:
        return KvmProbeResult(
            False,
            f"KVM host architecture {normalized_host} cannot run {normalized_guest} guest code",
            normalized_guest,
            normalized_host,
        )
    if ioctl_fn is None:
        if fcntl is None:
            return KvmProbeResult(
                False,
                "the Python fcntl module is unavailable",
                normalized_guest,
                normalized_host,
            )
        ioctl_fn = fcntl.ioctl

    path = os.fspath(device_path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    try:
        device_fd = open_fn(path, flags)
    except OSError as error:
        return KvmProbeResult(
            False,
            f"cannot open {path}: {error}",
            normalized_guest,
            normalized_host,
        )

    vm_fd: int | None = None
    api_version: int | None = None
    try:
        try:
            api_version = ioctl_fn(device_fd, KVM_GET_API_VERSION, 0)
        except OSError as error:
            return KvmProbeResult(
                False,
                f"KVM_GET_API_VERSION failed for {path}: {error}",
                normalized_guest,
                normalized_host,
            )
        if api_version != KVM_EXPECTED_API_VERSION:
            return KvmProbeResult(
                False,
                f"KVM API version is {api_version}; expected {KVM_EXPECTED_API_VERSION}",
                normalized_guest,
                normalized_host,
                api_version,
            )
        try:
            vm_fd = ioctl_fn(device_fd, KVM_CREATE_VM, 0)
        except OSError as error:
            return KvmProbeResult(
                False,
                f"KVM_CREATE_VM failed for {path}: {error}",
                normalized_guest,
                normalized_host,
                api_version,
            )
        if not isinstance(vm_fd, int) or vm_fd < 0:
            return KvmProbeResult(
                False,
                f"KVM_CREATE_VM returned invalid descriptor {vm_fd!r}",
                normalized_guest,
                normalized_host,
                api_version,
            )
        return KvmProbeResult(
            True,
            f"KVM API {api_version} created a probe VM",
            normalized_guest,
            normalized_host,
            api_version,
        )
    finally:
        if vm_fd is not None and isinstance(vm_fd, int) and vm_fd >= 0:
            try:
                close_fn(vm_fd)
            except OSError:
                pass
        try:
            close_fn(device_fd)
        except OSError:
            pass


def write_skipped_junit_xml(
    output: os.PathLike[str] | str,
    *,
    test_name: str,
    reason: str,
    suite_name: str = "rules_ostest",
    elapsed_seconds: float = 0.0,
) -> pathlib.Path:
    """Writes a one-test JUnit document whose only testcase is skipped."""

    if elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must not be negative")
    path = pathlib.Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    elapsed = f"{elapsed_seconds:.6f}"
    suites = ET.Element(
        "testsuites",
        tests="1",
        failures="0",
        errors="0",
        skipped="1",
        time=elapsed,
    )
    suite = ET.SubElement(
        suites,
        "testsuite",
        name=suite_name,
        tests="1",
        failures="0",
        errors="0",
        skipped="1",
        time=elapsed,
    )
    testcase = ET.SubElement(
        suite,
        "testcase",
        name=test_name,
        classname=suite_name,
        time=elapsed,
    )
    skipped = ET.SubElement(testcase, "skipped", message=reason)
    skipped.text = reason
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)
    return path


def stop_process_group(
    process: subprocess.Popen[object],
    *,
    terminate_timeout_seconds: float = 2.0,
) -> int | None:
    """Stops a child started in its own process group, escalating to SIGKILL."""

    if terminate_timeout_seconds < 0:
        raise ValueError("terminate_timeout_seconds must not be negative")
    if os.name != "posix":  # pragma: no cover - the QEMU runner requires POSIX.
        if process.poll() is None:
            process.terminate()
            try:
                return process.wait(timeout=terminate_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            return process.wait(timeout=max(terminate_timeout_seconds, 0.1))
        except subprocess.TimeoutExpired:
            return process.poll()

    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    original_status = process.poll()
    if group_exists():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + terminate_timeout_seconds
    while group_exists() and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.01)
    if group_exists():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + max(terminate_timeout_seconds, 0.1)
        while group_exists() and time.monotonic() < kill_deadline:
            process.poll()
            time.sleep(0.01)
    if process.poll() is None:
        try:
            process.wait(timeout=max(terminate_timeout_seconds, 0.1))
        except subprocess.TimeoutExpired:
            pass
    return original_status if original_status is not None else process.poll()


@dataclass(frozen=True)
class FailureHookResult:
    """Non-throwing result of a failure diagnostic hook invocation."""

    command: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    error: str | None
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.error is None and self.returncode == 0


def run_failure_hook(
    executable: os.PathLike[str] | str,
    serial_log: os.PathLike[str] | str,
    *,
    arguments: Sequence[os.PathLike[str] | str] = (),
    timeout_seconds: float = 30.0,
    environment: Mapping[str, str] | None = None,
    cwd: os.PathLike[str] | str | None = None,
    stdout: int | BinaryIO | None = None,
    stderr: int | BinaryIO | None = None,
) -> FailureHookResult:
    """Runs a bounded diagnostic hook without raising hook failures.

    The absolute serial-log path is both the final positional argument and the
    ``OSTEST_SERIAL_LOG`` environment value.  Standard output and error inherit
    the runner's streams by default.  Spawn errors, nonzero exits, and timeouts
    are represented in ``FailureHookResult`` so they cannot mask the original
    QEMU failure.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    log_path = pathlib.Path(serial_log).resolve()
    command = (
        os.fspath(executable),
        *(os.fspath(argument) for argument in arguments),
        str(log_path),
    )
    hook_environment = dict(os.environ if environment is None else environment)
    hook_environment["OSTEST_SERIAL_LOG"] = str(log_path)
    start = time.monotonic()
    try:
        process: subprocess.Popen[object] = subprocess.Popen(
            command,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=hook_environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except OSError as error:
        return FailureHookResult(
            command=command,
            returncode=None,
            timed_out=False,
            error=f"could not start failure hook: {error}",
            duration_seconds=time.monotonic() - start,
        )
    try:
        returncode = process.wait(timeout=timeout_seconds)
        stop_process_group(process, terminate_timeout_seconds=0.25)
        return FailureHookResult(
            command=command,
            returncode=returncode,
            timed_out=False,
            error=None,
            duration_seconds=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired:
        returncode = stop_process_group(process)
        return FailureHookResult(
            command=command,
            returncode=returncode,
            timed_out=True,
            error=f"failure hook timed out after {timeout_seconds:g} seconds",
            duration_seconds=time.monotonic() - start,
        )
