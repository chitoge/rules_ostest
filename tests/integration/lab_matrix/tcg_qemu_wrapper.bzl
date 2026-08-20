"""Creates a TCG-only executable bound to one declared QEMU launcher."""

_ROOT_SYMLINK = "rules_ostest_tcg_wrapped_qemu"
_ROOT_SYMLINK_TOKEN = "__RULES_OSTEST_TCG_WRAPPED_QEMU__"

_SCRIPT = """\
#!/usr/bin/env python3
import os
import sys

_RUNFILE = "__RULES_OSTEST_TCG_WRAPPED_QEMU__"


def _wrapped_qemu():
    runfiles_dir = os.environ.get("RUNFILES_DIR")
    if not runfiles_dir and os.path.isdir(sys.argv[0] + ".runfiles"):
        runfiles_dir = sys.argv[0] + ".runfiles"
    if not runfiles_dir:
        ancestor = os.path.abspath(sys.argv[0])
        while True:
            ancestor = os.path.dirname(ancestor)
            if ancestor.endswith(".runfiles"):
                runfiles_dir = ancestor
                break
            if ancestor == os.path.dirname(ancestor):
                break
    if runfiles_dir:
        candidate = os.path.join(runfiles_dir, _RUNFILE)
        if os.path.isfile(candidate):
            return candidate, ("RUNFILES_DIR", runfiles_dir)

    manifest = os.environ.get("RUNFILES_MANIFEST_FILE")
    if not manifest and os.path.isfile(sys.argv[0] + ".runfiles_manifest"):
        manifest = sys.argv[0] + ".runfiles_manifest"
    if manifest:
        with open(manifest, encoding="utf-8") as entries:
            for entry in entries:
                logical, separator, physical = entry.rstrip("\\n").partition(" ")
                if separator and logical == _RUNFILE and os.path.isfile(physical):
                    return physical, ("RUNFILES_MANIFEST_FILE", manifest)
    raise RuntimeError("pinned x86_64 QEMU launcher binding is absent")


def main():
    arguments = []
    removed_kvm = False
    index = 0
    forwarded = sys.argv[1:]
    while index < len(forwarded):
        if forwarded[index:index + 2] == ["-accel", "kvm"]:
            removed_kvm = True
            index += 2
            continue
        arguments.append(forwarded[index])
        index += 1
    if not removed_kvm or arguments[:2] != ["-no-user-config", "-nodefaults"]:
        raise RuntimeError("unexpected rules_ostest QEMU command shape")
    qemu, runfiles_environment = _wrapped_qemu()
    os.environ[runfiles_environment[0]] = runfiles_environment[1]
    os.execv(qemu, [qemu, *arguments])


if __name__ == "__main__":
    main()
""".replace(_ROOT_SYMLINK_TOKEN, _ROOT_SYMLINK)

def _tcg_qemu_wrapper_impl(ctx):
    launcher = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.write(launcher, _SCRIPT, is_executable = True)
    runfiles = ctx.runfiles(
        files = [ctx.executable.qemu],
        root_symlinks = {_ROOT_SYMLINK: ctx.executable.qemu},
    )
    runfiles = runfiles.merge(ctx.attr.qemu[DefaultInfo].default_runfiles)
    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

tcg_qemu_wrapper = rule(
    implementation = _tcg_qemu_wrapper_impl,
    executable = True,
    attrs = {
        "qemu": attr.label(
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
)
