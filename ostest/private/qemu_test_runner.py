#!/usr/bin/env python3
"""Bazel test runner for serial-driven UEFI and direct-kernel QEMU tests."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import pathlib
import re
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from python.runfiles import runfiles

from ostest.private.managed_network import (
    CompanionError,
    OneShotCompanion,
    canonical_mapping_json,
    companion_environment,
    parse_host_forwards,
    resolve_host_forwards,
)
from ostest.private.qemu_runner_support import (
    AssertionOutcome,
    AssertionPhase,
    PpmError,
    SerialAssertionMachine,
    probe_linux_kvm,
    run_failure_hook,
    stop_process_group,
    validate_ppm_distinct_pixels,
    write_skipped_junit_xml,
)
from ostest.python.qemu import UefiQemuConfig, process_status_after_disconnect


MAX_MATCH_BUFFER = 4 * 1024 * 1024
MAX_FAILURE_OUTPUT = 64 * 1024
MAX_SERIAL_LOG_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class RunResult:
    passed: bool
    reason: str
    output_tail: str


class _LegacyVerdict:
    def __init__(
        self,
        success: re.Pattern[str],
        failure: re.Pattern[str] | None,
        forbidden: tuple[bytes, ...],
    ) -> None:
        self.success = success
        self.failure = failure
        self.forbidden = forbidden
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.text = ""
        self.forbidden_tail = b""
        self.max_forbidden_overlap = max((len(value) - 1 for value in forbidden), default=0)

    def feed(self, chunk: bytes, *, final: bool = False) -> tuple[bool, str] | None:
        window = self.forbidden_tail + chunk
        for marker in self.forbidden:
            if marker in window:
                return False, f"matched forbidden marker {marker.decode('utf-8', errors='replace')!r}"
        if self.max_forbidden_overlap:
            self.forbidden_tail = window[-self.max_forbidden_overlap :]
        self.text += self.decoder.decode(chunk, final=final)
        if self.failure is not None and self.failure.search(self.text):
            return False, f"matched failure pattern {self.failure.pattern!r}"
        if self.success.search(self.text):
            return True, f"matched success pattern {self.success.pattern!r}"
        if len(self.text) > MAX_MATCH_BUFFER:
            self.text = self.text[-MAX_MATCH_BUFFER:]
        return None


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


def _test_environment(test_tmpdir: pathlib.Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("TEST_") and "OUTPUT" in key and key != "TEST_UNDECLARED_OUTPUTS_DIR":
            environment.pop(key)
    for key in (
        "XML_OUTPUT_FILE",
        "TEST_PREMATURE_EXIT_FILE",
        "TEST_WARNINGS_OUTPUT_FILE",
        "TEST_INFRASTRUCTURE_FAILURE_FILE",
    ):
        environment.pop(key, None)
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
    return environment


def _qmp_write(
    connection: socket.socket,
    request_id: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    request: dict[str, Any] = {"execute": command, "id": request_id}
    if arguments is not None:
        request["arguments"] = arguments
    connection.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")


def _qmp_wait_response(
    connection: socket.socket,
    file,
    request_id: int,
    deadline: float,
    pending_events: list[dict[str, Any]],
) -> Any:
    connection.settimeout(max(0.001, deadline - time.monotonic()))
    while True:
        line = file.readline()
        if not line:
            raise RuntimeError("QMP connection closed before its response")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"QMP returned invalid JSON: {line!r}") from error
        if "event" in message:
            pending_events.append(message)
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"QMP command failed: {message['error']!r}")
        if "return" not in message:
            raise RuntimeError(f"QMP returned a malformed response: {message!r}")
        return message["return"]


def _connect_qmp(
    address: tuple[str, int],
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> tuple[socket.socket, Any, list[dict[str, Any]], int]:
    deadline = time.monotonic() + timeout_seconds
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    file = None
    try:
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with status {process.returncode} before QMP startup"
                )
            try:
                connection.connect(address)
                break
            except (ConnectionRefusedError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out connecting to QMP")
                time.sleep(0.01)
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        file = connection.makefile("rwb", buffering=0)
        try:
            line = file.readline()
        except ConnectionError as error:
            status = process_status_after_disconnect(process)
            raise RuntimeError(
                f"QMP greeting failed; QEMU status is {status}"
            ) from error
        if not line:
            status = process_status_after_disconnect(process)
            raise RuntimeError(
                f"QMP closed before its greeting; QEMU status is {status}"
            )
        greeting = json.loads(line)
        if not isinstance(greeting, dict) or "QMP" not in greeting:
            raise RuntimeError(f"invalid QMP greeting: {greeting!r}")
        pending_events: list[dict[str, Any]] = []
        request_id = 1
        _qmp_write(connection, request_id, "qmp_capabilities")
        try:
            _qmp_wait_response(connection, file, request_id, deadline, pending_events)
        except ConnectionError as error:
            status = process_status_after_disconnect(process)
            raise RuntimeError(
                f"QMP capability negotiation failed; QEMU status is {status}"
            ) from error
        connection.settimeout(None)
        return connection, file, pending_events, request_id
    except BaseException:
        if file is not None:
            file.close()
        connection.close()
        raise


def _append_output_tail(output_tail: bytearray, chunk: bytes) -> None:
    output_tail.extend(chunk)
    if len(output_tail) > MAX_FAILURE_OUTPUT:
        del output_tail[:-MAX_FAILURE_OUTPUT]


def _export_media(config: UefiQemuConfig, artifacts_dir: pathlib.Path) -> list[pathlib.Path]:
    exported = []
    for index, medium in enumerate(config.media):
        if not medium.export:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", medium.name).strip("._") or "media"
        destination = artifacts_dir / f"ostest-media-{index}-{safe_name}.img"
        shutil.copyfile(medium.path, destination)
        exported.append(destination)
    return exported


def _run_qemu(
    *,
    config: UefiQemuConfig,
    command: list[str],
    environment: dict[str, str],
    working_directory: pathlib.Path,
    artifacts_dir: pathlib.Path,
    success_pattern: re.Pattern[str] | None,
    failure_pattern: re.Pattern[str] | None,
    success_markers: tuple[str, ...],
    forbidden_markers: tuple[str, ...],
    phases: tuple[AssertionPhase, ...],
    timeout_seconds: int,
    success_exit_codes: set[int],
    screendump_not_blank: bool,
    screendump_min_distinct_pixels: int,
    hostfwd_encoded: tuple[str, ...],
    host_companion: pathlib.Path | None,
    host_companion_args: tuple[str, ...],
) -> RunResult:
    phase_machine = None
    legacy = None
    try:
        if phases:
            phase_machine = SerialAssertionMachine(phases, forbidden_markers=forbidden_markers)
        elif success_markers:
            phase_machine = SerialAssertionMachine.ordered(
                success_markers, forbidden_markers=forbidden_markers
            )
        else:
            assert success_pattern is not None
            legacy = _LegacyVerdict(
                success_pattern,
                failure_pattern,
                tuple(marker.encode("utf-8") for marker in forbidden_markers),
            )
    except (TypeError, ValueError) as error:
        return RunResult(False, f"invalid serial assertion plan: {error}", "")

    needs_qmp = bool(phases or hostfwd_encoded or screendump_not_blank)
    allow_reboot = any(phase.then.value == "reboot" for phase in phases)
    listener = None
    listener_address = None
    try:
        if needs_qmp:
            if os.name != "posix":
                return RunResult(False, "managed QMP features require a POSIX execution worker", "")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener_address = listener.getsockname()
            command = config.command(
                qmp_listener_fd=listener.fileno(),
                allow_reboot=allow_reboot,
            )
            (artifacts_dir / "qemu-command.txt").write_text(
                shlex.join(command) + "\n", encoding="utf-8"
            )

        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(listener.fileno(),) if listener is not None else (),
        )
    except BaseException as error:
        if listener is not None:
            listener.close()
        return RunResult(False, f"could not start QEMU: {error}", "")
    assert process.stdout is not None
    try:
        if hasattr(os, "set_blocking"):
            os.set_blocking(process.stdout.fileno(), False)
    except OSError as error:
        stop_process_group(process)
        if listener is not None:
            listener.close()
        return RunResult(False, f"could not configure QEMU serial capture: {error}", "")
    output_tail = bytearray()
    log_path = artifacts_dir / "qemu.log"
    qmp_connection = None
    qmp_file = None
    qmp_buffer = bytearray()
    pending_events: list[dict[str, Any]] = []
    qmp_request_id = 0
    qmp_closed_at: float | None = None
    screenshot_request_id: int | None = None
    screenshot_complete = not screendump_not_blank
    screenshot_path = artifacts_dir / "screendump.ppm"
    companion = None
    companion_complete = host_companion is None
    process_exit_handled = False
    deadline = time.monotonic() + timeout_seconds

    match_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    regex_buffer = ""
    guest_complete = False
    outcome: tuple[bool, str] | None = None
    selector = None

    def process_serial(chunk: bytes, *, final: bool = False) -> None:
        nonlocal guest_complete, outcome, regex_buffer, screenshot_request_id
        if not chunk and not final:
            return
        _append_output_tail(output_tail, chunk)
        if legacy is not None:
            verdict = legacy.feed(chunk, final=final)
            if verdict is not None:
                passed, reason = verdict
                if not passed:
                    outcome = (False, reason)
                else:
                    guest_complete = True
        else:
            regex_buffer += match_decoder.decode(chunk, final=final)
            if failure_pattern is not None and failure_pattern.search(regex_buffer):
                outcome = (False, f"matched failure pattern {failure_pattern.pattern!r}")
                return
            if len(regex_buffer) > MAX_MATCH_BUFFER:
                regex_buffer = regex_buffer[-MAX_MATCH_BUFFER:]
            assert phase_machine is not None
            result = phase_machine.feed(chunk)
            if result.outcome is AssertionOutcome.FAIL:
                outcome = (False, result.reason)
            elif result.outcome is AssertionOutcome.PASS:
                guest_complete = True
        if guest_complete and not screenshot_complete and screenshot_request_id is None:
            assert qmp_connection is not None
            qmp_request_id_local = 1000
            screenshot_request_id = qmp_request_id_local
            _qmp_write(
                qmp_connection,
                qmp_request_id_local,
                "screendump",
                {"filename": str(screenshot_path.resolve())},
            )

    try:
        if needs_qmp:
            assert listener_address is not None
            qmp_connection, qmp_file, pending_events, qmp_request_id = _connect_qmp(
                listener_address, process, min(10.0, timeout_seconds)
            )
            listener.close()
            listener = None
            if hostfwd_encoded:
                requested = parse_host_forwards(hostfwd_encoded)
                qmp_request_id += 1
                _qmp_write(
                    qmp_connection,
                    qmp_request_id,
                    "human-monitor-command",
                    {"command-line": "info usernet"},
                )
                response = _qmp_wait_response(
                    qmp_connection, qmp_file, qmp_request_id, deadline, pending_events
                )
                if not isinstance(response, str):
                    raise RuntimeError(f"QEMU info usernet returned {response!r}, expected text")
                resolved = resolve_host_forwards(requested, response)
                (artifacts_dir / "hostfwd.json").write_text(
                    canonical_mapping_json(resolved) + "\n", encoding="utf-8"
                )
                if host_companion is not None:
                    companion_env = companion_environment(
                        environment,
                        resolved,
                        artifacts_dir / "host-companion",
                    )
                    companion = OneShotCompanion(
                        [host_companion, *host_companion_args],
                        environment=companion_env,
                        working_directory=working_directory,
                        log_path=artifacts_dir / "host-companion.log",
                    ).start()
            qmp_connection.setblocking(False)

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "serial")
        if qmp_connection is not None:
            selector.register(qmp_connection, selectors.EVENT_READ, "qmp")

        with log_path.open("wb") as log:
            serial_eof = False
            serial_log_bytes = 0

            def drain_serial() -> None:
                nonlocal outcome, serial_eof, serial_log_bytes
                if serial_eof:
                    return
                while True:
                    if outcome is not None:
                        return
                    if time.monotonic() >= deadline:
                        if phase_machine is not None:
                            phase_machine.timed_out(timeout_seconds)
                        outcome = (False, f"timed out after {timeout_seconds} seconds")
                        return
                    try:
                        serial_chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    except BlockingIOError:
                        return
                    if not serial_chunk:
                        serial_eof = True
                        try:
                            selector.unregister(process.stdout)
                        except KeyError:
                            pass
                        process_serial(b"", final=True)
                        return
                    serial_log_bytes += len(serial_chunk)
                    log.write(serial_chunk)
                    log.flush()
                    if serial_log_bytes > MAX_SERIAL_LOG_BYTES:
                        outcome = (
                            False,
                            f"serial log exceeded {MAX_SERIAL_LOG_BYTES} bytes",
                        )
                        return
                    process_serial(serial_chunk)

            while outcome is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if phase_machine is not None:
                        phase_machine.timed_out(timeout_seconds)
                    outcome = (False, f"timed out after {timeout_seconds} seconds")
                    break
                events = sorted(
                    selector.select(timeout=min(0.1, remaining)),
                    key=lambda event: 0 if event[0].data == "serial" else 1,
                )
                for key, _ in events:
                    if key.data == "serial":
                        drain_serial()
                    else:
                        # RESET is a phase boundary. Drain every serial byte
                        # currently available before consuming QMP so a large
                        # pre-reset burst cannot be misordered behind it.
                        drain_serial()
                        if outcome is not None:
                            break
                        assert qmp_connection is not None
                        try:
                            chunk = qmp_connection.recv(64 * 1024)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            if guest_complete and screenshot_complete:
                                selector.unregister(qmp_connection)
                            else:
                                # QMP can reach EOF a scheduling instant before
                                # the child status becomes observable. Defer a
                                # verdict briefly so accepted exit codes remain
                                # authoritative.
                                selector.unregister(qmp_connection)
                                qmp_closed_at = time.monotonic()
                            continue
                        qmp_buffer.extend(chunk)
                        while b"\n" in qmp_buffer:
                            line, _, remainder = qmp_buffer.partition(b"\n")
                            qmp_buffer[:] = remainder
                            message = json.loads(line)
                            if "event" in message:
                                if phase_machine is not None:
                                    result = phase_machine.handle_qmp_event(message)
                                    if result.outcome is AssertionOutcome.FAIL:
                                        outcome = (False, result.reason)
                                continue
                            if message.get("id") == screenshot_request_id:
                                if "error" in message:
                                    outcome = (
                                        False,
                                        f"QMP screendump failed: {message['error']!r}",
                                    )
                                else:
                                    try:
                                        validate_ppm_distinct_pixels(
                                            screenshot_path,
                                            min_distinct_pixels=screendump_min_distinct_pixels,
                                        )
                                    except (OSError, PpmError, ValueError) as error:
                                        outcome = (False, f"screendump assertion failed: {error}")
                                    else:
                                        screenshot_complete = True

                if outcome is not None:
                    break
                if pending_events:
                    drain_serial()
                    if outcome is not None:
                        break
                    queued, pending_events = pending_events, []
                    for message in queued:
                        if phase_machine is not None:
                            result = phase_machine.handle_qmp_event(message)
                            if result.outcome is AssertionOutcome.FAIL:
                                outcome = (False, result.reason)
                                break

                if companion is not None and not companion_complete:
                    status = companion.poll()
                    if status is not None:
                        try:
                            companion.wait(0.1)
                        except CompanionError as error:
                            outcome = (False, str(error))
                        else:
                            companion_complete = True

                if guest_complete and screenshot_complete and companion_complete:
                    outcome = (True, "guest assertions and host companion completed")
                    break

                if process.poll() is not None and not process_exit_handled:
                    drain_serial()
                    if not serial_eof:
                        continue
                    process_exit_handled = True
                    if outcome is None and process.returncode in success_exit_codes:
                        guest_complete = True
                    if outcome is None and guest_complete and screenshot_complete:
                        if companion_complete:
                            outcome = (
                                True,
                                f"QEMU exited with accepted status {process.returncode}",
                            )
                        # The guest verdict is already final. Keep polling an
                        # in-flight one-shot companion with the remaining
                        # global deadline, regardless of completion order.
                    elif outcome is None:
                        if phase_machine is not None:
                            result = phase_machine.process_exited(process.returncode)
                            reason = result.reason
                        else:
                            reason = (
                                f"QEMU exited with status {process.returncode} before a result marker"
                            )
                        outcome = (False, reason)

                if (
                    outcome is None
                    and qmp_closed_at is not None
                    and process.poll() is None
                    and not (guest_complete and screenshot_complete)
                    and time.monotonic() - qmp_closed_at >= 0.25
                ):
                    outcome = (False, "QMP connection closed unexpectedly")

        stop_process_group(process)
        if companion is not None and not companion_complete:
            companion.terminate()
        return RunResult(
            outcome[0],
            outcome[1],
            bytes(output_tail).decode("utf-8", errors="replace"),
        )
    except BaseException as error:
        stop_process_group(process)
        with log_path.open("ab") as log:
            while True:
                try:
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                log.write(chunk)
                _append_output_tail(output_tail, chunk)
        if companion is not None:
            companion.terminate()
        return RunResult(
            False,
            f"runner orchestration failed: {error}",
            bytes(output_tail).decode("utf-8", errors="replace"),
        )
    finally:
        if qmp_file is not None:
            qmp_file.close()
        if qmp_connection is not None:
            qmp_connection.close()
        if listener is not None:
            listener.close()
        if selector is not None:
            selector.close()
        process.stdout.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    architecture = parser.add_mutually_exclusive_group(required=True)
    architecture.add_argument("--arch", choices=("x86_64", "aarch64"))
    architecture.add_argument("--arch-file")
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--qemu-firmware-dir")
    parser.add_argument("--firmware")
    parser.add_argument("--firmware-vars")
    parser.add_argument("--disk")
    parser.add_argument("--media", action="append", default=[])
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--success-pattern")
    parser.add_argument("--failure-pattern", default="")
    parser.add_argument("--success-marker", action="append", default=[])
    parser.add_argument("--forbidden-marker", action="append", default=[])
    parser.add_argument("--phase", action="append", default=[])
    parser.add_argument("--memory-mb", required=True, type=int)
    parser.add_argument("--cpus", required=True, type=int)
    parser.add_argument("--require-kvm", action="store_true")
    parser.add_argument("--kvm-unavailable", choices=("fail", "skip"), default="fail")
    parser.add_argument("--debugcon", action="store_true")
    parser.add_argument("--export-firmware-vars", action="store_true")
    parser.add_argument("--success-exit-code", action="append", default=[], type=int)
    parser.add_argument("--machine-option", action="append", default=[])
    parser.add_argument("--qemu-arg", action="append", default=[])
    parser.add_argument("--boot", choices=("uefi", "direct-kernel"), default="uefi")
    parser.add_argument("--kernel")
    parser.add_argument("--initrd")
    parser.add_argument("--kernel-args", default="")
    parser.add_argument("--cpu-model")
    parser.add_argument("--graphics", action="store_true")
    parser.add_argument("--graphics-device")
    parser.add_argument("--screendump-not-blank", action="store_true")
    parser.add_argument("--screendump-min-distinct-pixels", type=int, default=2)
    parser.add_argument("--hostfwd", action="append", default=[])
    parser.add_argument("--host-companion")
    parser.add_argument("--host-companion-arg", action="append", default=[])
    parser.add_argument("--on-failure")
    parser.add_argument("--on-failure-timeout-seconds", type=int, default=30)
    return parser


def main() -> int:
    args = _parser().parse_args()
    locator = runfiles.Create()
    if locator is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    if args.arch_file:
        args.arch = _locate(locator, args.arch_file).read_text(encoding="utf-8").strip()
        if args.arch not in ("x86_64", "aarch64"):
            raise ValueError(f"guest architecture file contains unsupported value: {args.arch!r}")

    phase_specs = []
    for encoded in args.phase:
        spec = json.loads(encoded)
        phase_specs.append(AssertionPhase(tuple(spec["markers"]), spec.get("then", "complete")))
    phases = tuple(phase_specs)
    success_pattern = re.compile(args.success_pattern) if args.success_pattern else None
    failure_pattern = re.compile(args.failure_pattern) if args.failure_pattern else None
    if success_pattern is None and not args.success_marker and not phases:
        raise ValueError("one success assertion mode is required")

    test_tmpdir = pathlib.Path(os.environ["TEST_TMPDIR"])
    artifacts_dir = pathlib.Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", test_tmpdir))
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifacts_dir / "qemu.log"
    log_path.touch()
    config = UefiQemuConfig.from_namespace(
        argparse.Namespace(
            ostest_arch=args.arch,
            ostest_arch_file=None,
            ostest_boot=args.boot,
            ostest_cpu_model=args.cpu_model,
            ostest_cpus=args.cpus,
            ostest_debugcon=args.debugcon,
            ostest_disk=args.disk,
            ostest_firmware=args.firmware,
            ostest_firmware_vars=args.firmware_vars,
            ostest_gdb=False,
            ostest_graphics=args.graphics,
            ostest_graphics_device=args.graphics_device,
            ostest_hostfwd=args.hostfwd,
            ostest_initrd=args.initrd,
            ostest_kernel=args.kernel,
            ostest_kernel_args=args.kernel_args,
            ostest_machine_option=args.machine_option,
            ostest_media=args.media,
            ostest_memory_mb=args.memory_mb,
            ostest_pause_at_start=False,
            ostest_qemu=args.qemu,
            ostest_qemu_firmware_dir=args.qemu_firmware_dir,
            ostest_qemu_arg=args.qemu_arg,
            ostest_require_kvm=args.require_kvm,
        )
    )
    command = config.command(
        allow_reboot=any(phase.then.value == "reboot" for phase in phases)
    )
    (artifacts_dir / "qemu-command.txt").write_text(
        shlex.join(command) + "\n", encoding="utf-8"
    )

    if args.require_kvm and args.kvm_unavailable == "skip":
        probe = probe_linux_kvm(args.arch)
        if not probe.available:
            xml_output = os.environ.get("XML_OUTPUT_FILE")
            if xml_output:
                write_skipped_junit_xml(
                    xml_output,
                    test_name=os.environ.get("TEST_TARGET", "uefi_test"),
                    reason=probe.reason,
                )
            print(f"rules_ostest: SKIP: {probe.reason}")
            return 0

    environment = _test_environment(test_tmpdir)
    host_companion = _locate(locator, args.host_companion) if args.host_companion else None
    failure_hook = None
    failure_hook_resolution_error = None
    if args.on_failure:
        try:
            failure_hook = _locate(locator, args.on_failure)
        except (OSError, ValueError) as error:
            failure_hook_resolution_error = str(error)
    result = _run_qemu(
        config=config,
        command=command,
        environment=environment,
        working_directory=test_tmpdir,
        artifacts_dir=artifacts_dir,
        success_pattern=success_pattern,
        failure_pattern=failure_pattern,
        success_markers=tuple(args.success_marker),
        forbidden_markers=tuple(args.forbidden_marker),
        phases=phases,
        timeout_seconds=args.timeout_seconds,
        success_exit_codes=set(args.success_exit_code),
        screendump_not_blank=args.screendump_not_blank,
        screendump_min_distinct_pixels=args.screendump_min_distinct_pixels,
        hostfwd_encoded=tuple(args.hostfwd),
        host_companion=host_companion,
        host_companion_args=tuple(args.host_companion_arg),
    )
    export_errors = []
    try:
        _export_media(config, artifacts_dir)
    except OSError as error:
        export_errors.append(f"media export failed: {error}")
    if args.export_firmware_vars:
        try:
            config.export_firmware_vars(artifacts_dir / "uefi-vars.fd")
        except (OSError, RuntimeError) as error:
            export_errors.append(f"firmware-variable export failed: {error}")
    if export_errors:
        export_reason = "; ".join(export_errors)
        if result.passed:
            result = RunResult(False, export_reason, result.output_tail)
        else:
            print(f"rules_ostest: warning: {export_reason}", file=sys.stderr)
    if result.passed:
        print(f"rules_ostest: PASS: {result.reason}")
        return 0

    if failure_hook is not None:
        hook = run_failure_hook(
            failure_hook,
            log_path,
            timeout_seconds=args.on_failure_timeout_seconds,
            environment=environment,
            cwd=test_tmpdir,
        )
        if not hook.succeeded:
            print(f"rules_ostest: warning: failure hook did not complete: {hook.error or hook.returncode}", file=sys.stderr)
    elif failure_hook_resolution_error is not None:
        print(
            "rules_ostest: warning: failure hook could not be resolved: "
            + failure_hook_resolution_error,
            file=sys.stderr,
        )
    print(f"rules_ostest: FAIL: {result.reason}", file=sys.stderr)
    print("QEMU command:", shlex.join(command), file=sys.stderr)
    if result.output_tail:
        print("--- QEMU output tail ---", file=sys.stderr)
        print(result.output_tail, file=sys.stderr)
    print(f"Full log: {log_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
