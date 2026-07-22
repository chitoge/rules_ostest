#!/bin/sh
console=/dev/ttyS0
[ -c /dev/ttyAMA0 ] && console=/dev/ttyAMA0
exec >"$console" 2>&1

fail() {
    echo "OSTEST: FAIL $*"
    exit 1
}

echo "OSTEST: CLOUD USERDATA"

cpus="$(grep -c '^processor' /proc/cpuinfo)"
[ "$cpus" = 2 ] || fail "EXPECTED 2 CPUS, FOUND $cpus"
echo "OSTEST: CLOUD CPUS 2"

if [ -e /dev/port ]; then
    printf '\036' | dd of=/dev/port bs=1 seek=1026 conv=notrunc >/dev/null 2>&1 \
        || fail "DEBUGCON WRITE"
    echo "OSTEST: DEBUGCON WRITE"
fi

scratch=/dev/vdc
[ -b "$scratch" ] || fail "SCRATCH MISSING"
payload=OSTEST-SCRATCH-DURABLE
printf '%s\n' "$payload" | dd of="$scratch" conv=fsync >/dev/null 2>&1 \
    || fail "SCRATCH WRITE"
echo "OSTEST: SCRATCH WRITE"

observed="$(dd if="$scratch" bs=23 count=1 2>/dev/null)"
[ "$observed" = "$payload" ] || fail "SCRATCH READBACK"
echo "OSTEST: SCRATCH READBACK"
echo "OSTEST: CLOUD PASS"
