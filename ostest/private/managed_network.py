"""Managed loopback QEMU forwards and one-shot host companions.

This module contains the runtime-only pieces shared by the serial test runner
and its tests.  Managed forwards deliberately bind only IPv4 loopback.  QEMU
owns the listening socket (including dynamic port allocation), while the host
companion is an ordinary declared executable launched without a shell.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence


LOOPBACK_ADDRESS = "127.0.0.1"
DEFAULT_GUEST_ADDRESS = "10.0.2.15"
MAX_COMPANION_TAIL = 64 * 1024

_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_HOST_FORWARD_LINE = re.compile(
    r"^\s*(?P<protocol>TCP|UDP)\s*\[\s*HOST_FORWARD\s*\]"
    r"\s+(?P<fd>\d+)"
    r"\s+(?P<source_address>\S+)"
    r"\s+(?P<host_port>\d+)"
    r"\s+(?P<guest_address>\S+)"
    r"\s+(?P<guest_port>\d+)"
    r"(?:\s+.*)?$"
)

_COMPANION_MAPPING_KEYS = (
    "OSTEST_HOSTFWD_JSON",
    "OSTEST_HOST",
    "OSTEST_PORT",
    "OSTEST_GUEST_PORT",
    "OSTEST_PROTOCOL",
    "OSTEST_ARTIFACTS_DIR",
)

# These variables describe the parent Bazel test rather than a nested tool.
# Passing them through can cause the companion to overwrite runner-owned files.
_PARENT_TEST_OUTPUT_KEYS = (
    "XML_OUTPUT_FILE",
    "TEST_PREMATURE_EXIT_FILE",
    "TEST_WARNINGS_OUTPUT_FILE",
    "TEST_INFRASTRUCTURE_FAILURE_FILE",
)


def _port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer from 1 through 65535")
    if not 1 <= value <= 65535:
        raise ValueError(f"{field} must be an integer from 1 through 65535")
    return value


@dataclass(frozen=True)
class HostForward:
    """One validated host-to-guest QEMU user-network forwarding request."""

    name: str
    protocol: str
    guest_port: int
    host_port: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "host-forward name must start with a letter and contain only "
                "letters, digits, '_', '-', or '.'"
            )
        if self.protocol not in ("tcp", "udp"):
            raise ValueError("host-forward protocol must be 'tcp' or 'udp'")
        _port(self.guest_port, "host-forward guest port")
        if self.host_port is not None:
            _port(self.host_port, "host-forward host port")

    @property
    def qemu_hostfwd(self) -> str:
        """Returns QEMU's hostfwd value, using port zero for auto allocation."""

        host_port = self.host_port if self.host_port is not None else 0
        return (
            f"{self.protocol}:{LOOPBACK_ADDRESS}:{host_port}"
            f"-:{self.guest_port}"
        )

    @classmethod
    def from_json(cls, encoded: str, *, default_name: str) -> "HostForward":
        """Parses the strict JSON object emitted by the Starlark helper."""

        try:
            value = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid host-forward JSON: {error}") from error
        if not isinstance(value, dict):
            raise ValueError("host-forward JSON must encode an object")
        allowed = {"guest", "host", "name", "protocol"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown host-forward fields: {', '.join(unknown)}")
        if "guest" not in value:
            raise ValueError("host-forward JSON is missing required field 'guest'")
        name = value.get("name", default_name)
        if name == "":
            name = default_name
        protocol = value.get("protocol", "tcp")
        host = value.get("host", "auto")
        if host == "auto":
            host_port = None
        else:
            host_port = _port(host, "host-forward host port")
        return cls(
            name=name,
            protocol=protocol,
            guest_port=_port(value["guest"], "host-forward guest port"),
            host_port=host_port,
        )


def parse_host_forwards(encoded: Sequence[str]) -> tuple[HostForward, ...]:
    """Parses and cross-validates a sequence of serialized forwarding specs."""

    forwards = tuple(
        HostForward.from_json(value, default_name=f"forward{index}")
        for index, value in enumerate(encoded)
    )
    names: set[str] = set()
    guest_endpoints: set[tuple[str, int]] = set()
    static_host_endpoints: set[tuple[str, int]] = set()
    for forward in forwards:
        if forward.name in names:
            raise ValueError(f"duplicate host-forward name: {forward.name!r}")
        names.add(forward.name)
        guest_endpoint = (forward.protocol, forward.guest_port)
        if guest_endpoint in guest_endpoints:
            raise ValueError(
                "duplicate host-forward guest endpoint: "
                f"{forward.protocol}/{forward.guest_port}"
            )
        guest_endpoints.add(guest_endpoint)
        if forward.host_port is not None:
            host_endpoint = (forward.protocol, forward.host_port)
            if host_endpoint in static_host_endpoints:
                raise ValueError(
                    "duplicate static host-forward endpoint: "
                    f"{forward.protocol}/{forward.host_port}"
                )
            static_host_endpoints.add(host_endpoint)
    return forwards


@dataclass(frozen=True)
class UsernetHostForward:
    """One HOST_FORWARD row reported by QEMU's ``info usernet``."""

    protocol: str
    fd: int
    source_address: str
    host_port: int
    guest_address: str
    guest_port: int


def parse_info_usernet(output: str) -> tuple[UsernetHostForward, ...]:
    """Extracts forwarding rows while ignoring unrelated usernet connections."""

    if not isinstance(output, str):
        raise ValueError("QEMU info usernet output must be text")
    rows = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if "HOST_FORWARD" not in line:
            continue
        match = _HOST_FORWARD_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"could not parse QEMU HOST_FORWARD row on line {line_number}: {line!r}"
            )
        host_port = int(match.group("host_port"))
        guest_port = int(match.group("guest_port"))
        if not 1 <= host_port <= 65535 or not 1 <= guest_port <= 65535:
            raise ValueError(
                f"QEMU HOST_FORWARD row has an invalid port on line {line_number}: {line!r}"
            )
        rows.append(
            UsernetHostForward(
                protocol=match.group("protocol").lower(),
                fd=int(match.group("fd")),
                source_address=match.group("source_address"),
                host_port=host_port,
                guest_address=match.group("guest_address"),
                guest_port=guest_port,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class ResolvedHostForward:
    """A host-forward request after QEMU owns its concrete loopback port."""

    name: str
    protocol: str
    guest_port: int
    host_port: int
    host: str = LOOPBACK_ADDRESS

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"invalid resolved host-forward name: {self.name!r}")
        if self.protocol not in ("tcp", "udp"):
            raise ValueError(f"invalid resolved host-forward protocol: {self.protocol!r}")
        _port(self.guest_port, "resolved guest port")
        _port(self.host_port, "resolved host port")
        if self.host != LOOPBACK_ADDRESS:
            raise ValueError(
                f"managed host forwards must bind {LOOPBACK_ADDRESS}, got {self.host!r}"
            )

    def as_mapping(self) -> dict[str, str | int]:
        return {
            "guest_port": self.guest_port,
            "host": self.host,
            "host_port": self.host_port,
            "protocol": self.protocol,
        }


def resolve_host_forwards(
    requested: Sequence[HostForward],
    info_usernet_output: str,
) -> tuple[ResolvedHostForward, ...]:
    """Matches requested endpoints to QEMU rows and returns concrete ports."""

    rows = parse_info_usernet(info_usernet_output)
    resolved = []
    for request in requested:
        candidates = [
            row
            for row in rows
            if row.protocol == request.protocol
            and row.guest_port == request.guest_port
        ]
        if not candidates:
            raise ValueError(
                "QEMU did not report requested host forward "
                f"{request.name!r} ({request.protocol}/{request.guest_port})"
            )
        if len(candidates) != 1:
            descriptions = ", ".join(
                f"{row.source_address}:{row.host_port}->{row.guest_address}:{row.guest_port}"
                for row in candidates
            )
            raise ValueError(
                "QEMU reported ambiguous rows for host forward "
                f"{request.name!r}: {descriptions}"
            )
        row = candidates[0]
        if row.source_address != LOOPBACK_ADDRESS:
            raise ValueError(
                f"QEMU host forward {request.name!r} bound {row.source_address!r}; "
                f"expected loopback {LOOPBACK_ADDRESS}"
            )
        if row.guest_address != DEFAULT_GUEST_ADDRESS:
            raise ValueError(
                f"QEMU host forward {request.name!r} targets {row.guest_address!r}; "
                f"expected {DEFAULT_GUEST_ADDRESS}"
            )
        if request.host_port is not None and row.host_port != request.host_port:
            raise ValueError(
                f"QEMU host forward {request.name!r} bound port {row.host_port}; "
                f"expected static port {request.host_port}"
            )
        resolved.append(
            ResolvedHostForward(
                name=request.name,
                protocol=request.protocol,
                guest_port=request.guest_port,
                host_port=row.host_port,
            )
        )
    return tuple(resolved)


def canonical_mapping_json(forwards: Sequence[ResolvedHostForward]) -> str:
    """Returns the stable companion-facing map keyed by forwarding name."""

    mapping: dict[str, dict[str, str | int]] = {}
    for forward in forwards:
        if forward.name in mapping:
            raise ValueError(f"duplicate resolved host-forward name: {forward.name!r}")
        mapping[forward.name] = forward.as_mapping()
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def companion_environment(
    base: Mapping[str, str],
    forwards: Sequence[ResolvedHostForward],
    artifacts_dir: os.PathLike[str] | str,
) -> dict[str, str]:
    """Builds a sanitized environment for a nested host companion process."""

    environment = {str(key): str(value) for key, value in base.items()}
    for key in tuple(environment):
        if key.startswith("TEST_") and "OUTPUT" in key:
            environment.pop(key)
    for key in _COMPANION_MAPPING_KEYS + _PARENT_TEST_OUTPUT_KEYS:
        environment.pop(key, None)
    artifacts = pathlib.Path(artifacts_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    environment["TEST_UNDECLARED_OUTPUTS_DIR"] = str(artifacts)
    environment["OSTEST_ARTIFACTS_DIR"] = str(artifacts)
    environment["OSTEST_HOSTFWD_JSON"] = canonical_mapping_json(forwards)
    if len(forwards) == 1:
        forward = forwards[0]
        environment.update(
            {
                "OSTEST_HOST": forward.host,
                "OSTEST_PORT": str(forward.host_port),
                "OSTEST_GUEST_PORT": str(forward.guest_port),
                "OSTEST_PROTOCOL": forward.protocol,
            }
        )
    return environment


def read_log_tail(
    path: os.PathLike[str] | str,
    *,
    limit: int = MAX_COMPANION_TAIL,
) -> str:
    """Reads at most the final ``limit`` bytes of a companion log."""

    if limit <= 0:
        raise ValueError("log tail limit must be positive")
    log_path = pathlib.Path(path)
    try:
        with log_path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - limit))
            return log.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


@dataclass(frozen=True)
class CompanionResult:
    command: tuple[str, ...]
    returncode: int
    log_path: pathlib.Path
    output_tail: str
    elapsed_seconds: float


class CompanionError(RuntimeError):
    """Base class for actionable host-companion failures."""


class CompanionStartError(CompanionError):
    pass


class CompanionExitError(CompanionError):
    def __init__(self, result: CompanionResult):
        self.result = result
        super().__init__(
            f"host companion exited with status {result.returncode}"
            + (f"\n{result.output_tail}" if result.output_tail else "")
        )


class CompanionTimeoutError(CompanionError):
    def __init__(self, timeout_seconds: float, result: CompanionResult):
        self.timeout_seconds = timeout_seconds
        self.result = result
        super().__init__(
            f"host companion timed out after {timeout_seconds:g} seconds"
            + (f"\n{result.output_tail}" if result.output_tail else "")
        )


class OneShotCompanion:
    """A bounded, process-group-owned host companion launched without a shell."""

    def __init__(
        self,
        command: Sequence[os.PathLike[str] | str],
        *,
        environment: Mapping[str, str],
        working_directory: os.PathLike[str] | str,
        log_path: os.PathLike[str] | str,
        tail_bytes: int = MAX_COMPANION_TAIL,
    ):
        if not command:
            raise ValueError("host companion command must not be empty")
        if tail_bytes <= 0:
            raise ValueError("host companion tail size must be positive")
        self.command = tuple(os.fspath(argument) for argument in command)
        self.environment = {str(key): str(value) for key, value in environment.items()}
        self.working_directory = pathlib.Path(working_directory)
        self.log_path = pathlib.Path(log_path)
        self.tail_bytes = tail_bytes
        self.process: subprocess.Popen[bytes] | None = None
        self._log = None
        self._started_at: float | None = None
        self._result: CompanionResult | None = None

    def start(self) -> "OneShotCompanion":
        if self.process is not None or self._started_at is not None:
            raise RuntimeError("host companion has already been started")
        if os.name != "posix":
            raise CompanionStartError(
                "managed host companions require a POSIX execution worker"
            )
        self.working_directory.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("wb")
        self._started_at = time.monotonic()
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.working_directory,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                shell=False,
            )
        except OSError as error:
            self._close_log()
            raise CompanionStartError(
                f"could not start host companion {self.command[0]!r}: {error}"
            ) from error
        return self

    def poll(self) -> int | None:
        if self.process is None:
            raise RuntimeError("host companion has not been started")
        return self.process.poll()

    def wait(self, timeout_seconds: float) -> CompanionResult:
        if timeout_seconds <= 0:
            raise ValueError("host companion timeout must be positive")
        if self._result is not None:
            if self._result.returncode != 0:
                raise CompanionExitError(self._result)
            return self._result
        if self.process is None:
            raise RuntimeError("host companion has not been started")
        try:
            returncode = self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self.terminate()
            result = self._make_result()
            self._result = result
            raise CompanionTimeoutError(timeout_seconds, result) from None
        # A nominally finished wrapper may have left descendants in its process
        # group.  Always clear them before returning control to the test runner.
        self._terminate_process_group(grace_seconds=0.25)
        self._close_log()
        result = self._make_result(returncode=returncode)
        self._result = result
        if returncode != 0:
            raise CompanionExitError(result)
        return result

    def terminate(self, *, grace_seconds: float = 2.0) -> None:
        if grace_seconds < 0:
            raise ValueError("termination grace period must not be negative")
        if self.process is None:
            self._close_log()
            return
        self._terminate_process_group(grace_seconds=grace_seconds)
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=max(0.1, grace_seconds))
            except subprocess.TimeoutExpired:
                self._signal_process_group(signal.SIGKILL)
                self.process.wait(timeout=2)
        self._close_log()

    def _group_exists(self) -> bool:
        assert self.process is not None
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal_process_group(self, requested_signal: int) -> None:
        assert self.process is not None
        try:
            os.killpg(self.process.pid, requested_signal)
        except ProcessLookupError:
            pass

    def _terminate_process_group(self, *, grace_seconds: float) -> None:
        if self.process is None or not self._group_exists():
            return
        self._signal_process_group(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while self._group_exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._group_exists():
            self._signal_process_group(signal.SIGKILL)

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.flush()
            self._log.close()
            self._log = None

    def _make_result(self, *, returncode: int | None = None) -> CompanionResult:
        assert self.process is not None
        assert self._started_at is not None
        if returncode is None:
            returncode = self.process.poll()
        if returncode is None:
            raise RuntimeError("host companion is still running")
        return CompanionResult(
            command=self.command,
            returncode=returncode,
            log_path=self.log_path.resolve(),
            output_tail=read_log_tail(self.log_path, limit=self.tail_bytes),
            elapsed_seconds=time.monotonic() - self._started_at,
        )

    def __enter__(self) -> "OneShotCompanion":
        return self.start()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.terminate()


def run_one_shot_companion(
    command: Sequence[os.PathLike[str] | str],
    *,
    environment: Mapping[str, str],
    working_directory: os.PathLike[str] | str,
    log_path: os.PathLike[str] | str,
    timeout_seconds: float,
    tail_bytes: int = MAX_COMPANION_TAIL,
) -> CompanionResult:
    """Starts a companion, waits for status zero, and owns all descendants."""

    companion = OneShotCompanion(
        command,
        environment=environment,
        working_directory=working_directory,
        log_path=log_path,
        tail_bytes=tail_bytes,
    )
    try:
        companion.start()
        return companion.wait(timeout_seconds)
    finally:
        companion.terminate()
