#!/usr/bin/env bash
set -euo pipefail

runtime_project_root=$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd
)
runtime_temporary_root=$(
    mktemp -d --tmpdir="${TMPDIR:-/tmp}" rules-ostest-qemu-lock.XXXXXXXX
)
runtime_temporary_output=$(
    mktemp -d --tmpdir="${TMPDIR:-/tmp}" rules-ostest-qemu-output.XXXXXXXX
)
trap 'rm -rf -- "${runtime_temporary_root}" "${runtime_temporary_output}"' EXIT

runtime_resolver_root="${runtime_temporary_root}/rules_distroless"
mkdir -- "${runtime_resolver_root}"
git -C "${runtime_resolver_root}" init --quiet
git -C "${runtime_resolver_root}" remote add origin \
    https://github.com/GoogleContainerTools/rules_distroless.git
git -C "${runtime_resolver_root}" fetch --quiet --depth=1 origin \
    9629141cac55da90e4282e36b45e145053227ba3
git -C "${runtime_resolver_root}" checkout --quiet --detach FETCH_HEAD

runtime_manifest_dir="${runtime_resolver_root}/examples/rules_ostest_qemu"
mkdir -- "${runtime_manifest_dir}"
cp -- \
    "${runtime_project_root}/tests/integration/qemu_noble.yaml" \
    "${runtime_manifest_dir}/qemu_noble.yaml"

cat >"${runtime_manifest_dir}/BUILD.bazel" <<'EOF'
exports_files(["qemu_noble.yaml"])
EOF

cat >"${runtime_resolver_root}/MODULE.bazel" <<'EOF'
module(
    name = "rules_distroless",
    version = "0.0.0",
    compatibility_level = 1,
)

bazel_dep(name = "platforms", version = "0.0.10")
bazel_dep(name = "bazel_features", version = "1.20.0")
bazel_dep(name = "bazel_skylib", version = "1.5.0")
bazel_dep(name = "aspect_bazel_lib", version = "2.9.4")
bazel_dep(name = "rules_java", version = "8.8.0")

bazel_lib_toolchains = use_extension(
    "@aspect_bazel_lib//lib:extensions.bzl",
    "toolchains",
)
use_repo(
    bazel_lib_toolchains,
    "bsd_tar_toolchains",
    "yq_linux_amd64",
    "zstd_toolchains",
)

apt = use_extension("@rules_distroless//apt:extensions.bzl", "apt")
apt.install(
    name = "qemu_noble",
    manifest = "//examples/rules_ostest_qemu:qemu_noble.yaml",
    nolock = True,
)
use_repo(apt, "qemu_noble")
EOF

runtime_distdir="${runtime_temporary_root}/distfiles"
mkdir -- "${runtime_distdir}"
curl --fail --location --silent --show-error \
    https://github.com/bats-core/bats-core/archive/refs/tags/v1.10.0.tar.gz \
    --output "${runtime_distdir}/v1.10.0.tar.gz"
curl --fail --location --silent --show-error \
    https://github.com/bats-core/bats-support/archive/refs/tags/v0.3.0.tar.gz \
    --output "${runtime_distdir}/v0.3.0.tar.gz"
curl --fail --location --silent --show-error \
    https://github.com/bats-core/bats-assert/archive/refs/tags/v2.1.0.tar.gz \
    --output "${runtime_distdir}/v2.1.0.tar.gz"
curl --fail --location --silent --show-error \
    https://github.com/bats-core/bats-file/archive/refs/tags/v0.4.0.tar.gz \
    --output "${runtime_distdir}/v0.4.0.tar.gz"

(
    cd -- "${runtime_resolver_root}"
    USE_BAZEL_VERSION=7.0.0 \
        bazel \
        --output_user_root="${runtime_temporary_output}" \
        run \
        --distdir="${runtime_distdir}" \
        --remote_executor= \
        --remote_cache= \
        --bes_backend= \
        @qemu_noble//:lock
)

cp -- \
    "${runtime_manifest_dir}/qemu_noble.lock.json" \
    "${runtime_project_root}/tests/integration/qemu_noble.lock.json"

echo "Updated tests/integration/qemu_noble.lock.json"
