#!/bin/sh
exec >/dev/ttyS0 2>&1

fail() {
    echo "OSTEST: FAIL CLIENT $*"
    exit 1
}

request=rules-ostest-nonce-v1
ack=rules-ostest-ack-v1
ack_file=/tmp/ostest-ack
: >"$ack_file"

ip link show dev eth0 >/dev/null 2>&1 || fail "ETH0 MISSING"
dhcpcd -x eth0 >/dev/null 2>&1 || :
ip address flush dev eth0 scope global || fail "ADDRESS FLUSH"
ip address add 192.0.2.11/24 dev eth0 || fail "STATIC ADDRESS"
ip link set dev eth0 up || fail "LINK UP"
echo "OSTEST: CLIENT NETWORK READY"

nc -u -l -p 39001 >"$ack_file" &
listener_pid=$!
attempt=0
while [ ! -s "$ack_file" ] && [ "$attempt" -lt 90 ]; do
    printf '%s' "$request" | nc -u -w 1 192.0.2.10 39000 || :
    attempt=$((attempt + 1))
    sleep 1
done
kill "$listener_pid" 2>/dev/null || :

[ "$(cat "$ack_file")" = "$ack" ] || fail "ACK MISMATCH"
echo "OSTEST: CLIENT RECEIVED ACK"
