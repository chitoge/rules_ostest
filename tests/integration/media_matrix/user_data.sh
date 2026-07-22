#!/bin/sh
exec >/dev/ttyS0 2>&1

fail() {
    echo "OSTEST: FAIL $*"
    exit 1
}

assert_readonly() {
    device="$1"
    marker="$2"
    if printf X | dd of="$device" bs=1 count=1 conv=notrunc 2>/dev/null; then
        fail "$marker ACCEPTED WRITE"
    fi
    echo "OSTEST: $marker READONLY"
}

echo "OSTEST: MEDIA USERDATA"

cdrom=/dev/sr0
[ -b "$cdrom" ] || fail "CDROM DEVICE"
assert_readonly "$cdrom" "CDROM"
mkdir -p /mnt/ostest-cd
mount -t iso9660 -o ro "$cdrom" /mnt/ostest-cd || fail "CDROM MOUNT"
echo "OSTEST: CDROM MOUNT"
cd_image=/mnt/ostest-cd/efi.img
[ -f "$cd_image" ] || cd_image=/mnt/ostest-cd/EFI.IMG
[ -f "$cd_image" ] || fail "CDROM EFI IMAGE"
echo "OSTEST: CDROM EFI IMAGE"
grep -q OSTEST_CD_MEDIA_SENTINEL "$cd_image" \
    || fail "CDROM SENTINEL"
echo "OSTEST: CDROM SENTINEL"

echo "OSTEST: CDROM PASS"
