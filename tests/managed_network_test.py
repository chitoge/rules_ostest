#!/usr/bin/env python3
"""Unit tests for managed QEMU forwards and host companions."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import sys
import tempfile
import textwrap
import time
import unittest

from ostest.private.managed_network import (
    CompanionExitError,
    CompanionStartError,
    CompanionTimeoutError,
    HostForward,
    OneShotCompanion,
    ResolvedHostForward,
    canonical_mapping_json,
    companion_environment,
    parse_host_forwards,
    parse_info_usernet,
    resolve_host_forwards,
    run_one_shot_companion,
)


QEMU_8_OUTPUT = """\
VLAN -1 (net0):
  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP[HOST_FORWARD]  13       127.0.0.1 38117       10.0.2.15    22     0     0
  UDP[236 sec]       24       10.0.2.15 35061   91.189.89.198   123     0     0
"""

QEMU_9_OUTPUT = """\
VLAN -1 (ostest_usernet):
  Protocol[State]          FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP [ HOST_FORWARD ]     7   127.0.0.1       45001  10.0.2.15     50051 0     0
  TCP[ESTABLISHED]         9   10.0.2.15       40512  10.0.2.2      443   0     0
  UDP[HOST_FORWARD]       11   127.0.0.1       45002  10.0.2.15     5353  0     0
"""

QEMU_10_OUTPUT = """\
Hub -1 (ostest_usernet):
 Protocol[State] FD Source Address Port Dest. Address Port RecvQ SendQ
 TCP[HOST_FORWARD] 101 127.0.0.1 46001 10.0.2.15 8080 0 0
 UDP [HOST_FORWARD]  102  127.0.0.1  46002  10.0.2.15  8081  0  0
"""


class HostForwardTest(unittest.TestCase):
    def test_strict_json_and_qemu_value(self) -> None:
        forwards = parse_host_forwards(
            [
                '{"guest":50051,"host":"auto","name":"grpc","protocol":"tcp"}',
                '{"guest":5353,"host":45353,"protocol":"udp"}',
            ]
        )
        self.assertEqual(
            forwards,
            (
                HostForward("grpc", "tcp", 50051, None),
                HostForward("forward1", "udp", 5353, 45353),
            ),
        )
        self.assertEqual(forwards[0].qemu_hostfwd, "tcp:127.0.0.1:0-:50051")
        self.assertEqual(forwards[1].qemu_hostfwd, "udp:127.0.0.1:45353-:5353")

    def test_defaults_and_empty_name(self) -> None:
        self.assertEqual(
            parse_host_forwards(['{"guest":80,"name":""}']),
            (HostForward("forward0", "tcp", 80, None),),
        )

    def test_rejects_invalid_json_fields_and_types(self) -> None:
        invalid = [
            "[]",
            "not json",
            "{}",
            '{"guest":80,"surprise":true}',
            '{"guest":true}',
            '{"guest":0}',
            '{"guest":65536}',
            '{"guest":80,"host":false}',
            '{"guest":80,"host":0}',
            '{"guest":80,"host":"dynamic"}',
            '{"guest":80,"protocol":"TCP"}',
            '{"guest":80,"name":"not valid"}',
        ]
        for encoded in invalid:
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                parse_host_forwards([encoded])

    def test_rejects_cross_forward_collisions(self) -> None:
        cases = [
            [
                '{"guest":80,"name":"same"}',
                '{"guest":81,"name":"same"}',
            ],
            [
                '{"guest":80,"name":"first"}',
                '{"guest":80,"name":"second"}',
            ],
            [
                '{"guest":80,"host":4000,"name":"first"}',
                '{"guest":81,"host":4000,"name":"second"}',
            ],
        ]
        for encoded in cases:
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                parse_host_forwards(encoded)
        # TCP and UDP occupy independent endpoint namespaces.
        parse_host_forwards(
            [
                '{"guest":80,"host":4000,"name":"tcp","protocol":"tcp"}',
                '{"guest":80,"host":4000,"name":"udp","protocol":"udp"}',
            ]
        )


class InfoUsernetTest(unittest.TestCase):
    def test_qemu_8_spacing_and_extra_connection(self) -> None:
        rows = parse_info_usernet(QEMU_8_OUTPUT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].protocol, "tcp")
        self.assertEqual(rows[0].host_port, 38117)
        self.assertEqual(rows[0].guest_port, 22)

    def test_qemu_9_spacing_multiple_protocols(self) -> None:
        rows = parse_info_usernet(QEMU_9_OUTPUT)
        self.assertEqual(
            [(row.protocol, row.host_port, row.guest_port) for row in rows],
            [("tcp", 45001, 50051), ("udp", 45002, 5353)],
        )

    def test_qemu_10_spacing(self) -> None:
        rows = parse_info_usernet(QEMU_10_OUTPUT)
        self.assertEqual(
            [(row.protocol, row.host_port, row.guest_port) for row in rows],
            [("tcp", 46001, 8080), ("udp", 46002, 8081)],
        )

    def test_malformed_forward_row_is_not_silently_ignored(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not parse"):
            parse_info_usernet("TCP[HOST_FORWARD] malformed")

    def test_resolves_auto_static_multiple_and_ignores_extra(self) -> None:
        requested = (
            HostForward("grpc", "tcp", 50051, None),
            HostForward("dns", "udp", 5353, 45002),
        )
        resolved = resolve_host_forwards(requested, QEMU_9_OUTPUT)
        self.assertEqual(
            resolved,
            (
                ResolvedHostForward("grpc", "tcp", 50051, 45001),
                ResolvedHostForward("dns", "udp", 5353, 45002),
            ),
        )
        # An unrelated forwarding row is no more relevant than an established
        # usernet connection.
        self.assertEqual(
            resolve_host_forwards(
                (HostForward("grpc", "tcp", 50051, None),), QEMU_9_OUTPUT
            ),
            (ResolvedHostForward("grpc", "tcp", 50051, 45001),),
        )

    def test_missing_and_ambiguous_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not report"):
            resolve_host_forwards(
                (HostForward("missing", "tcp", 9999, None),), QEMU_8_OUTPUT
            )
        duplicate = QEMU_8_OUTPUT + (
            "  TCP[HOST_FORWARD] 14 127.0.0.1 38118 10.0.2.15 22 0 0\n"
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_host_forwards(
                (HostForward("ssh", "tcp", 22, None),), duplicate
            )

    def test_rejects_wrong_bind_guest_and_static_port(self) -> None:
        wrong_bind = QEMU_8_OUTPUT.replace("127.0.0.1", "0.0.0.0")
        with self.assertRaisesRegex(ValueError, "expected loopback"):
            resolve_host_forwards(
                (HostForward("ssh", "tcp", 22, None),), wrong_bind
            )
        wrong_guest = QEMU_8_OUTPUT.replace("10.0.2.15", "10.0.2.16", 1)
        with self.assertRaisesRegex(ValueError, "expected 10.0.2.15"):
            resolve_host_forwards(
                (HostForward("ssh", "tcp", 22, None),), wrong_guest
            )
        with self.assertRaisesRegex(ValueError, "expected static port"):
            resolve_host_forwards(
                (HostForward("ssh", "tcp", 22, 2222),), QEMU_8_OUTPUT
            )


class EnvironmentTest(unittest.TestCase):
    def test_canonical_json_and_single_forward_environment(self) -> None:
        forwards = (ResolvedHostForward("grpc", "tcp", 50051, 45001),)
        expected_json = (
            '{"grpc":{"guest_port":50051,"host":"127.0.0.1",'
            '"host_port":45001,"protocol":"tcp"}}'
        )
        self.assertEqual(canonical_mapping_json(forwards), expected_json)
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = pathlib.Path(temporary) / "companion"
            environment = companion_environment(
                {
                    "PATH": "/bin",
                    "OSTEST_PORT": "stale",
                    "XML_OUTPUT_FILE": "/do/not/overwrite.xml",
                    "TEST_PREMATURE_EXIT_FILE": "/do/not/overwrite",
                    "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR": "/do/not/annotate",
                    "RUNFILES_DIR": "/keep/runfiles",
                },
                forwards,
                artifacts,
            )
            self.assertEqual(environment["OSTEST_HOSTFWD_JSON"], expected_json)
            self.assertEqual(environment["OSTEST_HOST"], "127.0.0.1")
            self.assertEqual(environment["OSTEST_PORT"], "45001")
            self.assertEqual(environment["OSTEST_GUEST_PORT"], "50051")
            self.assertEqual(environment["OSTEST_PROTOCOL"], "tcp")
            self.assertEqual(environment["OSTEST_ARTIFACTS_DIR"], str(artifacts.resolve()))
            self.assertEqual(
                environment["TEST_UNDECLARED_OUTPUTS_DIR"], str(artifacts.resolve())
            )
            self.assertNotIn("XML_OUTPUT_FILE", environment)
            self.assertNotIn("TEST_PREMATURE_EXIT_FILE", environment)
            self.assertNotIn("TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR", environment)
            self.assertEqual(environment["RUNFILES_DIR"], "/keep/runfiles")
            self.assertTrue(artifacts.is_dir())

    def test_multiple_forwards_have_only_canonical_map(self) -> None:
        forwards = (
            ResolvedHostForward("zeta", "udp", 53, 45002),
            ResolvedHostForward("alpha", "tcp", 80, 45001),
        )
        with tempfile.TemporaryDirectory() as temporary:
            environment = companion_environment(
                {
                    "OSTEST_HOST": "stale",
                    "OSTEST_PORT": "stale",
                    "OSTEST_GUEST_PORT": "stale",
                    "OSTEST_PROTOCOL": "stale",
                },
                forwards,
                temporary,
            )
        for key in (
            "OSTEST_HOST",
            "OSTEST_PORT",
            "OSTEST_GUEST_PORT",
            "OSTEST_PROTOCOL",
        ):
            self.assertNotIn(key, environment)
        self.assertEqual(
            list(json.loads(environment["OSTEST_HOSTFWD_JSON"])),
            ["alpha", "zeta"],
        )


class CompanionTest(unittest.TestCase):
    def _run(
        self,
        code: str,
        temporary: str,
        *,
        timeout: float = 2,
        tail_bytes: int = 64 * 1024,
    ):
        return run_one_shot_companion(
            [sys.executable, "-c", textwrap.dedent(code)],
            environment=dict(os.environ),
            working_directory=temporary,
            log_path=pathlib.Path(temporary) / "companion.log",
            timeout_seconds=timeout,
            tail_bytes=tail_bytes,
        )

    def test_success_merged_log_and_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                """
                import sys
                print("A" * 64, flush=True)
                print("stderr-marker", file=sys.stderr, flush=True)
                """,
                temporary,
                tail_bytes=32,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("stderr-marker", result.output_tail)
            self.assertLessEqual(len(result.output_tail.encode()), 32)
            complete = result.log_path.read_text(encoding="utf-8")
            self.assertIn("A" * 64, complete)
            self.assertIn("stderr-marker", complete)

    def test_nonzero_includes_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CompanionExitError) as caught:
                self._run(
                    """
                    import sys
                    print("probe failed", file=sys.stderr, flush=True)
                    raise SystemExit(7)
                    """,
                    temporary,
                )
            self.assertEqual(caught.exception.result.returncode, 7)
            self.assertIn("probe failed", caught.exception.result.output_tail)
            self.assertIn("probe failed", str(caught.exception))

    def test_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CompanionTimeoutError) as caught:
                self._run(
                    """
                    import time
                    print("waiting forever", flush=True)
                    time.sleep(60)
                    """,
                    temporary,
                    timeout=0.1,
                )
            self.assertIn("waiting forever", caught.exception.result.output_tail)
            self.assertLess(caught.exception.result.returncode, 0)

    def test_exec_error_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            companion = OneShotCompanion(
                [pathlib.Path(temporary) / "does-not-exist"],
                environment=dict(os.environ),
                working_directory=temporary,
                log_path=pathlib.Path(temporary) / "companion.log",
            )
            with self.assertRaisesRegex(CompanionStartError, "could not start"):
                companion.start()

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_success_cleans_up_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = pathlib.Path(temporary) / "child-terminated"
            child_code = """
                import pathlib
                import signal
                import sys
                import time

                marker = pathlib.Path(sys.argv[1])
                def terminated(_signal, _frame):
                    marker.write_text("terminated", encoding="utf-8")
                    raise SystemExit(0)
                signal.signal(signal.SIGTERM, terminated)
                print("child-ready", flush=True)
                time.sleep(60)
            """
            parent_code = """
                import subprocess
                import sys
                import time

                subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
                time.sleep(0.15)
                print("parent-done", flush=True)
            """
            result = run_one_shot_companion(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(parent_code),
                    textwrap.dedent(child_code),
                    str(marker),
                ],
                environment=dict(os.environ),
                working_directory=temporary,
                log_path=pathlib.Path(temporary) / "companion.log",
                timeout_seconds=2,
            )
            self.assertEqual(result.returncode, 0)
            deadline = time.monotonic() + 1
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(marker.read_text(encoding="utf-8"), "terminated")


if __name__ == "__main__":
    unittest.main()
