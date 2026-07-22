#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runtime_dir="${script_dir}/runtime"
runtime_root="${runtime_dir}/root"

qemu="$(command -v qemu-system-x86_64 || true)"
if [[ -z "${qemu}" ]]; then
    echo "qemu-system-x86_64 is missing; install qemu-system-x86" >&2
    exit 1
fi

for command_name in ldd awk cp find; do
    if ! command -v "${command_name}" >/dev/null; then
        echo "${command_name} is required to stage the QEMU runtime" >&2
        exit 1
    fi
done

ovmf_code=/usr/share/OVMF/OVMF_CODE_4M.fd
ovmf_vars=/usr/share/OVMF/OVMF_VARS_4M.fd
qemu_data=/usr/share/qemu
for required_path in "${ovmf_code}" "${ovmf_vars}" "${qemu_data}"; do
    if [[ ! -e "${required_path}" ]]; then
        echo "required runtime input is missing: ${required_path}" >&2
        exit 1
    fi
done

# The destination is fixed relative to this script and is ignored by Git.
rm -rf -- \
    "${runtime_root}" \
    "${runtime_dir}/licenses" \
    "${runtime_dir}/OVMF_CODE_4M.fd" \
    "${runtime_dir}/OVMF_VARS_4M.fd" \
    "${runtime_dir}/PACKAGES.txt"
mkdir -p -- "${runtime_root}" "${runtime_dir}/licenses"

copy_with_parents() {
    local source=$1
    local destination="${runtime_root}/${source#/}"
    mkdir -p -- "$(dirname -- "${destination}")"
    cp -L --preserve=mode,timestamps -- "${source}" "${destination}"
}

copy_elf_closure() {
    local executable=$1
    local dependency

    copy_with_parents "${executable}"
    while IFS= read -r dependency; do
        [[ -n "${dependency}" ]] && copy_with_parents "${dependency}"
    done < <(
        LC_ALL=C ldd "${executable}" 2>/dev/null | awk '
            $2 == "=>" && $3 ~ /^\// { print $3 }
            $1 ~ /^\// { print $1 }
        '
    )
}

resolved_qemu="$(readlink -f -- "${qemu}")"
copy_elf_closure "${resolved_qemu}"
mkdir -p -- "${runtime_root}/usr/bin"
cp -L --preserve=mode,timestamps -- \
    "${resolved_qemu}" \
    "${runtime_root}/usr/bin/qemu-system-x86_64"

module_dir=/usr/lib/x86_64-linux-gnu/qemu
if [[ -d "${module_dir}" ]]; then
    while IFS= read -r -d '' module; do
        copy_elf_closure "${module}"
    done < <(find -L "${module_dir}" -type f -name '*.so' -print0)
fi

mkdir -p -- "${runtime_root}/usr/share/qemu"
cp -aL -- "${qemu_data}/." "${runtime_root}/usr/share/qemu/"
cp -L --preserve=mode,timestamps -- "${ovmf_code}" "${runtime_dir}/OVMF_CODE_4M.fd"
cp -L --preserve=mode,timestamps -- "${ovmf_vars}" "${runtime_dir}/OVMF_VARS_4M.fd"

for package_name in qemu-system-x86 qemu-system-common qemu-system-data ovmf; do
    notice="/usr/share/doc/${package_name}/copyright"
    if [[ -f "${notice}" ]]; then
        cp -L -- "${notice}" "${runtime_dir}/licenses/${package_name}.copyright"
    fi
done

if command -v dpkg-query >/dev/null; then
    dpkg-query -W -f='${binary:Package}\t${Version}\n' \
        qemu-system-x86 qemu-system-common qemu-system-data ovmf \
        >"${runtime_dir}/PACKAGES.txt"
else
    qemu-system-x86_64 --version >"${runtime_dir}/PACKAGES.txt"
fi

echo "staged real-QEMU runtime at ${runtime_dir}"
