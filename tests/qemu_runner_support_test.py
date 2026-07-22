#!/usr/bin/env python3
"""Unit tests for runner support primitives; runnable without Bazel."""

from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from ostest.private.qemu_runner_support import (  # noqa: E402
    AssertionOutcome,
    AssertionPhase,
    KVM_CREATE_VM,
    KVM_EXPECTED_API_VERSION,
    KVM_GET_API_VERSION,
    PhaseTransition,
    PpmError,
    SerialAssertionMachine,
    normalize_architecture,
    probe_linux_kvm,
    run_failure_hook,
    validate_ppm_distinct_pixels,
    write_skipped_junit_xml,
)


class SerialAssertionMachineTest(unittest.TestCase):
    def test_ordered_markers_match_across_fragment_boundaries(self) -> None:
        machine = SerialAssertionMachine.ordered(["alpha", "beta", "gamma"])
        for chunk in (b"noise-al", b"pha-and-be", b"ta-and-ga", b"mma"):
            result = machine.feed(chunk)
        self.assertEqual(AssertionOutcome.PASS, result.outcome)

    def test_duplicate_markers_require_distinct_occurrences(self) -> None:
        machine = SerialAssertionMachine.ordered(["READY", "READY"])
        self.assertEqual(AssertionOutcome.RUNNING, machine.feed(b"READY").outcome)
        self.assertEqual(AssertionOutcome.PASS, machine.feed(b"READY").outcome)

    def test_out_of_order_occurrence_is_not_reused(self) -> None:
        machine = SerialAssertionMachine.ordered(["first", "second"])
        machine.feed(b"second first")
        self.assertEqual(AssertionOutcome.RUNNING, machine.result.outcome)
        self.assertEqual(AssertionOutcome.FAIL, machine.process_exited(0).outcome)

    def test_forbidden_marker_spans_chunks_and_wins_over_success(self) -> None:
        fragmented = SerialAssertionMachine.ordered(
            ["done"], forbidden_markers=["KERNEL PANIC"]
        )
        fragmented.feed(b"KERNEL PA")
        result = fragmented.feed(b"NIC and done")
        self.assertEqual(AssertionOutcome.FAIL, result.outcome)
        self.assertIn("KERNEL PANIC", result.reason)

        same_chunk = SerialAssertionMachine.ordered(["done"], forbidden_markers=["bad"])
        self.assertEqual(
            AssertionOutcome.FAIL,
            same_chunk.feed(b"done but also bad").outcome,
        )

    def test_retained_state_is_bounded(self) -> None:
        machine = SerialAssertionMachine.ordered(
            ["0123456789"], forbidden_markers=["abcdefgh"]
        )
        machine.feed(b"x" * 1_000_000)
        self.assertLessEqual(machine.retained_bytes, 9 + 7)

    def test_reboot_phase_requires_guest_reset_boundary(self) -> None:
        machine = SerialAssertionMachine(
            [
                AssertionPhase(("slot-a", "applied"), PhaseTransition.REBOOT),
                AssertionPhase(("slot-b", "committed")),
            ],
            forbidden_markers=("panic",),
        )
        machine.feed(b"slot-a applied slot-b committed")
        self.assertTrue(machine.result.waiting_for_reboot)
        # Pre-reset bytes after the last phase-one marker are intentionally not reusable.
        machine.handle_qmp_event({"event": "RESET", "data": {"guest": True}})
        self.assertEqual(AssertionOutcome.RUNNING, machine.result.outcome)
        self.assertEqual(1, machine.result.phase_index)
        self.assertEqual(AssertionOutcome.PASS, machine.feed(b"slot-b committed").outcome)

    def test_reset_before_phase_completion_fails(self) -> None:
        machine = SerialAssertionMachine(
            [
                AssertionPhase(("ready",), "reboot"),
                AssertionPhase(("verified",)),
            ]
        )
        result = machine.handle_reset(guest=True, reason="guest-reset")
        self.assertEqual(AssertionOutcome.FAIL, result.outcome)
        self.assertIn("unexpected", result.reason)

    def test_non_guest_reset_fails_and_non_reset_qmp_event_is_ignored(self) -> None:
        machine = SerialAssertionMachine(
            [
                AssertionPhase(("ready",), "reboot"),
                AssertionPhase(("verified",)),
            ]
        )
        machine.feed("ready")
        machine.handle_qmp_event({"event": "STOP"})
        self.assertEqual(AssertionOutcome.RUNNING, machine.result.outcome)
        result = machine.handle_qmp_event(
            {"event": "RESET", "data": {"guest": False, "reason": "host-qmp-system-reset"}}
        )
        self.assertEqual(AssertionOutcome.FAIL, result.outcome)
        self.assertIn("non-guest", result.reason)

    def test_guest_reset_reason_supports_older_qemu_metadata(self) -> None:
        machine = SerialAssertionMachine(
            [
                AssertionPhase(("ready",), "reboot"),
                AssertionPhase(("verified",)),
            ]
        )
        machine.feed("ready")
        result = machine.handle_reset(guest=False, reason="guest-reset")
        self.assertEqual(AssertionOutcome.RUNNING, result.outcome)
        self.assertEqual(1, result.phase_index)

    def test_phase_shape_is_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "must end with 'reboot'"):
            SerialAssertionMachine(
                [AssertionPhase(("one",)), AssertionPhase(("two",))]
            )
        with self.assertRaisesRegex(ValueError, "must end with 'complete'"):
            SerialAssertionMachine([AssertionPhase(("one",), "reboot")])


class PpmValidationTest(unittest.TestCase):
    def test_valid_p6_and_p3_images(self) -> None:
        p6 = b"P6\n2 1\n255\n" + b"\x00\x00\x00\xff\xff\xff"
        info = validate_ppm_distinct_pixels(p6)
        self.assertEqual(("P6", 2, 1, 2), (info.format, info.width, info.height, info.distinct_pixels))

        p3 = b"P3\n# dimensions\n2 1\n15\n0 0 0 # black\n15 0 0\n"
        info = validate_ppm_distinct_pixels(p3)
        self.assertEqual(("P3", 2), (info.format, info.distinct_pixels))

    def test_blank_image_is_rejected(self) -> None:
        blank = b"P6\n2 1\n255\n" + b"\x11\x22\x33" * 2
        with self.assertRaisesRegex(PpmError, "only 1 distinct"):
            validate_ppm_distinct_pixels(blank)

    def test_malformed_and_out_of_range_images_are_rejected(self) -> None:
        cases = (
            b"P5\n1 1\n255\n\0",
            b"P6\n2 1\n255\n\0\0\0",
            b"P3\n1 1\n5\n6 0 0\n",
            b"P3\n1 1\n255\n0 0 0 1\n",
            b"P6\n0 1\n255\n",
        )
        for image in cases:
            with self.subTest(image=image), self.assertRaises(PpmError):
                validate_ppm_distinct_pixels(image, min_distinct_pixels=1)

    def test_file_and_pixel_bounds_are_enforced(self) -> None:
        with self.assertRaisesRegex(PpmError, "input limit"):
            validate_ppm_distinct_pixels(b"P3 " + b"0" * 100, max_file_bytes=20)
        with self.assertRaisesRegex(PpmError, "pixel limit"):
            validate_ppm_distinct_pixels(
                b"P3\n100 100\n255\n", min_distinct_pixels=1, max_pixels=100
            )


class KvmProbeTest(unittest.TestCase):
    def test_architecture_normalization(self) -> None:
        self.assertEqual("x86_64", normalize_architecture("AMD64"))
        self.assertEqual("aarch64", normalize_architecture("arm64"))
        self.assertIsNone(normalize_architecture("riscv64"))

    def test_non_linux_and_architecture_mismatch_are_unavailable(self) -> None:
        self.assertFalse(
            probe_linux_kvm("x86_64", system="Darwin", host_arch="x86_64").available
        )
        result = probe_linux_kvm("aarch64", system="Linux", host_arch="x86_64")
        self.assertFalse(result.available)
        self.assertIn("cannot run", result.reason)

    def test_open_failure_is_unavailable(self) -> None:
        def denied(_path: str, _flags: int) -> int:
            raise PermissionError("denied")

        result = probe_linux_kvm(
            "x86_64", system="Linux", host_arch="amd64", open_fn=denied
        )
        self.assertFalse(result.available)
        self.assertIn("cannot open", result.reason)

    def test_api_mismatch_and_create_failure_close_device(self) -> None:
        closed: list[int] = []
        mismatch = probe_linux_kvm(
            "x86_64",
            system="Linux",
            host_arch="x86_64",
            open_fn=lambda _path, _flags: 10,
            ioctl_fn=lambda _fd, _request, _argument: 11,
            close_fn=closed.append,
        )
        self.assertFalse(mismatch.available)
        self.assertEqual([10], closed)

        closed.clear()

        def create_fails(_fd: int, request: int, _argument: int) -> int:
            if request == KVM_GET_API_VERSION:
                return KVM_EXPECTED_API_VERSION
            raise OSError("no VM slots")

        result = probe_linux_kvm(
            "x86_64",
            system="Linux",
            host_arch="x86_64",
            open_fn=lambda _path, _flags: 10,
            ioctl_fn=create_fails,
            close_fn=closed.append,
        )
        self.assertFalse(result.available)
        self.assertIn("KVM_CREATE_VM", result.reason)
        self.assertEqual([10], closed)

    def test_successful_probe_closes_vm_then_device(self) -> None:
        closed: list[int] = []

        def ioctl(_fd: int, request: int, _argument: int) -> int:
            if request == KVM_GET_API_VERSION:
                return KVM_EXPECTED_API_VERSION
            self.assertEqual(KVM_CREATE_VM, request)
            return 11

        result = probe_linux_kvm(
            "x64",
            system="Linux",
            host_arch="amd64",
            open_fn=lambda _path, _flags: 10,
            ioctl_fn=ioctl,
            close_fn=closed.append,
        )
        self.assertTrue(result.available)
        self.assertEqual(KVM_EXPECTED_API_VERSION, result.api_version)
        self.assertEqual([11, 10], closed)


class JunitAndHookTest(unittest.TestCase):
    def test_skipped_junit_xml_escapes_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "nested" / "test.xml"
            write_skipped_junit_xml(
                path,
                test_name="boot<&",
                reason="no KVM <available>",
                suite_name="rules&ostest",
                elapsed_seconds=1.25,
            )
            root = ET.parse(path).getroot()
            self.assertEqual("1", root.attrib["skipped"])
            testcase = root.find("./testsuite/testcase")
            self.assertIsNotNone(testcase)
            assert testcase is not None
            self.assertEqual("boot<&", testcase.attrib["name"])
            skipped = testcase.find("skipped")
            self.assertIsNotNone(skipped)
            assert skipped is not None
            self.assertEqual("no KVM <available>", skipped.attrib["message"])

    def _script(self, directory: pathlib.Path, body: str) -> pathlib.Path:
        script = directory / "hook.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_failure_hook_success_receives_argument_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = pathlib.Path(directory_name)
            log = directory / "serial.log"
            log.write_text("raw serial", encoding="utf-8")
            output = directory / "observed.txt"
            script = self._script(
                directory,
                """
                import os
                import pathlib
                import sys
                pathlib.Path(sys.argv[1]).write_text(
                    sys.argv[-1] + "\\n" + os.environ["OSTEST_SERIAL_LOG"] + "\\n"
                    + pathlib.Path(sys.argv[-1]).read_text()
                )
                """,
            )
            result = run_failure_hook(
                sys.executable,
                log,
                arguments=(script, output),
                environment={"LC_ALL": "C"},
                timeout_seconds=2,
            )
            self.assertTrue(result.succeeded)
            observed = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(str(log.resolve()), observed[0])
            self.assertEqual(str(log.resolve()), observed[1])
            self.assertEqual("raw serial", observed[2])

    def test_failure_hook_nonzero_and_spawn_error_are_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = pathlib.Path(directory_name)
            log = directory / "serial.log"
            log.touch()
            script = self._script(directory, "import sys\nsys.exit(7)\n")
            nonzero = run_failure_hook(
                sys.executable, log, arguments=(script,), timeout_seconds=2
            )
            self.assertFalse(nonzero.succeeded)
            self.assertEqual(7, nonzero.returncode)
            self.assertIsNone(nonzero.error)

            missing = run_failure_hook(directory / "missing", log, timeout_seconds=2)
            self.assertFalse(missing.succeeded)
            self.assertIsNotNone(missing.error)
            self.assertIsNone(missing.returncode)

    def test_failure_hook_timeout_is_bounded_and_non_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = pathlib.Path(directory_name)
            log = directory / "serial.log"
            log.touch()
            script = self._script(
                directory,
                """
                import time
                time.sleep(30)
                """,
            )
            result = run_failure_hook(
                sys.executable,
                log,
                arguments=(script,),
                timeout_seconds=0.05,
            )
            self.assertFalse(result.succeeded)
            self.assertTrue(result.timed_out)
            self.assertIn("timed out", result.error or "")
            self.assertLess(result.duration_seconds, 3)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_successful_failure_hook_cleans_up_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = pathlib.Path(directory_name)
            log = directory / "serial.log"
            log.touch()
            marker = directory / "child-terminated"
            ready = directory / "child-ready"
            script = self._script(
                directory,
                """
                import os
                import pathlib
                import signal
                import sys
                import time

                marker = pathlib.Path(sys.argv[1])
                ready = pathlib.Path(sys.argv[2])
                if os.fork() == 0:
                    def terminated(_signal, _frame):
                        marker.write_text("terminated", encoding="utf-8")
                        os._exit(0)
                    signal.signal(signal.SIGTERM, terminated)
                    ready.write_text("ready", encoding="utf-8")
                    while True:
                        time.sleep(1)
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                """,
            )
            result = run_failure_hook(
                sys.executable,
                log,
                arguments=(script, marker, ready),
                timeout_seconds=3,
            )
            self.assertTrue(result.succeeded)
            self.assertEqual("terminated", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
