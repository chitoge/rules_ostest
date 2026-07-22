#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runtime_dir="${script_dir}/runtime"
runtime_root="${runtime_dir}/root"

qemu_x86_64="$(command -v qemu-system-x86_64 || true)"
if [[ -z "${qemu_x86_64}" ]]; then
    echo "qemu-system-x86_64 is missing; install qemu-system-x86" >&2
    exit 1
fi
qemu_aarch64="$(command -v qemu-system-aarch64 || true)"
if [[ -z "${qemu_aarch64}" ]]; then
    echo "qemu-system-aarch64 is missing; install qemu-system-arm" >&2
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
aavmf_code=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd
aavmf_vars=/usr/share/AAVMF/AAVMF_VARS.fd
qemu_data=/usr/share/qemu
efi_shell_x64=/usr/share/efi-shell-x64/shellx64.efi
efi_shell_aa64=/usr/share/efi-shell-aa64/shellaa64.efi
for required_path in \
    "${ovmf_code}" \
    "${ovmf_vars}" \
    "${aavmf_code}" \
    "${aavmf_vars}" \
    "${qemu_data}" \
    "${efi_shell_x64}" \
    "${efi_shell_aa64}"; do
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
    "${runtime_dir}/AAVMF_CODE.fd" \
    "${runtime_dir}/AAVMF_VARS.fd" \
    "${runtime_dir}/shellx64.efi" \
    "${runtime_dir}/shellaa64.efi" \
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

resolved_qemu_x86_64="$(readlink -f -- "${qemu_x86_64}")"
resolved_qemu_aarch64="$(readlink -f -- "${qemu_aarch64}")"
copy_elf_closure "${resolved_qemu_x86_64}"
copy_elf_closure "${resolved_qemu_aarch64}"
mkdir -p -- "${runtime_root}/usr/bin"
cp -L --preserve=mode,timestamps -- \
    "${resolved_qemu_x86_64}" \
    "${runtime_root}/usr/bin/qemu-system-x86_64"
cp -L --preserve=mode,timestamps -- \
    "${resolved_qemu_aarch64}" \
    "${runtime_root}/usr/bin/qemu-system-aarch64"

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
cp -L --preserve=mode,timestamps -- "${aavmf_code}" "${runtime_dir}/AAVMF_CODE.fd"
cp -L --preserve=mode,timestamps -- "${aavmf_vars}" "${runtime_dir}/AAVMF_VARS.fd"
cp -L --preserve=mode,timestamps -- "${efi_shell_x64}" "${runtime_dir}/shellx64.efi"
cp -L --preserve=mode,timestamps -- "${efi_shell_aa64}" "${runtime_dir}/shellaa64.efi"

packages=(
    qemu-system-x86
    qemu-system-arm
    qemu-system-common
    qemu-system-data
    ovmf
    qemu-efi-aarch64
    efi-shell-x64
    efi-shell-aa64
)
for package_name in "${packages[@]}"; do
    notice="/usr/share/doc/${package_name}/copyright"
    if [[ -f "${notice}" ]]; then
        cp -L -- "${notice}" "${runtime_dir}/licenses/${package_name}.copyright"
    fi
done

if command -v dpkg-query >/dev/null; then
    dpkg-query -W -f='${binary:Package}\t${Version}\n' \
        "${packages[@]}" \
        >"${runtime_dir}/PACKAGES.txt"
else
    qemu-system-x86_64 --version >"${runtime_dir}/PACKAGES.txt"
fi

echo "staged real-QEMU runtime at ${runtime_dir}"
