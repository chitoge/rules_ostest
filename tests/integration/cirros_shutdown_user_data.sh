#!/bin/sh
console=/dev/ttyS0
[ -c /dev/ttyAMA0 ] && console=/dev/ttyAMA0
exec >"$console" 2>&1

echo "OSTEST: CLOUD SHUTDOWN"
sync
poweroff -f
