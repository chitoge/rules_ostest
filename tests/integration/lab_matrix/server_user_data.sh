#!/bin/sh
exec >/dev/ttyS0 2>&1

fail() {
    echo "OSTEST: FAIL SERVER $*"
    exit 1
}

request=rules-ostest-nonce-v1
ack=rules-ostest-ack-v1
request_file=/tmp/ostest-request
: >"$request_file"

ip link show dev eth0 >/dev/null 2>&1 || fail "ETH0 MISSING"
dhcpcd -x eth0 >/dev/null 2>&1 || :
ip address flush dev eth0 scope global || fail "ADDRESS FLUSH"
ip address add 192.0.2.10/24 dev eth0 || fail "STATIC ADDRESS"
ip link set dev eth0 up || fail "LINK UP"
echo "OSTEST: SERVER NETWORK READY"

nc -u -l -p 39000 >"$request_file" &
listener_pid=$!
attempt=0
while [ ! -s "$request_file" ] && [ "$attempt" -lt 90 ]; do
    attempt=$((attempt + 1))
    sleep 1
done
kill "$listener_pid" 2>/dev/null || :

[ "$(cat "$request_file")" = "$request" ] || fail "NONCE MISMATCH"
echo "OSTEST: SERVER RECEIVED NONCE"

attempt=0
while [ "$attempt" -lt 10 ]; do
    printf '%s' "$ack" | nc -u -w 1 192.0.2.11 39001 || :
    attempt=$((attempt + 1))
    sleep 1
done
echo "OSTEST: SERVER SENT ACK"
