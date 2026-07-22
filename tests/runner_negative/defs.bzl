"""Private declarations for deterministic serial-runner meta-tests."""

load("@rules_python//python:py_test.bzl", "py_test")
load("//ostest:defs.bzl", "qemu_hostfwd", "uefi_test")

def inner_test(
        name,
        mode,
        timeout_seconds = 3,
        success_pattern = "NEVER MATCH",
        failure_pattern = "OSTEST: FAIL",
        success_markers = [],
        forbidden_markers = [],
        host_companion = None,
        host_companion_args = []):
    """Declares one generated runner that may only be called by its wrapper.

    Args:
      name: Bazel target name.
      mode: Fake-QEMU scenario name.
      timeout_seconds: Global runner deadline.
      success_pattern: Legacy success regular expression.
      failure_pattern: Serial failure regular expression.
      success_markers: Ordered serial success markers.
      forbidden_markers: Serial markers that immediately fail the test.
      host_companion: Optional one-shot companion executable.
      host_companion_args: Arguments forwarded to the companion.
    """
    hostfwd = []
    if host_companion != None:
        hostfwd = [qemu_hostfwd(
            name = "probe",
            guest = 1234,
        )]
    uefi_test(
        name = name,
        size = "small",
        arch = "x86_64",
        failure_pattern = failure_pattern,
        firmware = "dummy.fd",
        forbidden_markers = forbidden_markers,
        host_companion = host_companion,
        host_companion_args = host_companion_args,
        hostfwd = hostfwd,
        qemu = ":fake_qemu",
        qemu_args = ["--negative-mode=" + mode],
        success_markers = success_markers,
        success_pattern = None if success_markers else success_pattern,
        tags = ["manual"],
        timeout_seconds = timeout_seconds,
    )

def meta_test(name, case, inner, companion = False):
    """Declares a passing wrapper around an inner generated runner.

    Args:
      name: Bazel target name.
      case: Runner scenario to verify.
      inner: Generated runner executable label.
      companion: Whether the runner needs the companion fixture.
    """
    args = [
        "--case=" + case,
        "--firmware=$(rlocationpath dummy.fd)",
        "--inner=$(rlocationpath %s)" % inner,
        "--qemu=$(rlocationpath :fake_qemu)",
    ]
    data = [
        "dummy.fd",
        inner,
        ":fake_qemu",
    ]
    if companion:
        args.append("--companion=$(rlocationpath :fake_companion)")
        data.append(":fake_companion")
    py_test(
        name = name,
        size = "small",
        srcs = ["runner_meta_test.py"],
        args = args,
        data = data,
        legacy_create_init = 0,
        main = "runner_meta_test.py",
        python_version = "3.12",
        deps = ["@rules_python//python/runfiles"],
    )
