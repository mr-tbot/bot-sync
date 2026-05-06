#!/bin/sh
# /usr/sbin/botsync-watchdog
#
# External watchdog for botsyncd. Run from cron once per minute. Pings the
# daemon's /api/watchdog/ping endpoint; on N consecutive failures, restarts
# the service via procd. Logs to syslog (tag: botsync-watchdog).
#
# This is a belt-and-braces complement to procd's own respawn. procd handles
# crashes (process exits); this catches the rarer cases of a hung process
# that's still alive but no longer serving HTTP.

set -u

PORT="${BOTSYNC_PORT:-8585}"
HOST="${BOTSYNC_HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}/api/watchdog/ping"

FAIL_FILE="/tmp/botsync-watchdog.fail"
KICK_MARKER="/tmp/botsync-watchdog.kicked"
MAX_FAIL="${BOTSYNC_WATCHDOG_MAX_FAIL:-3}"
TIMEOUT="${BOTSYNC_WATCHDOG_TIMEOUT:-8}"

log() { logger -t botsync-watchdog "$@"; }

# Check master enable in uci. If the user explicitly disabled botsync, do
# nothing (so the watchdog doesn't fight a deliberate stop).
if [ "$(uci -q get botsync.main.enabled 2>/dev/null)" = "0" ]; then
    rm -f "$FAIL_FILE"
    exit 0
fi

http=$(curl -s -m "$TIMEOUT" -o /dev/null -w "%{http_code}" \
    -X POST -H "Content-Length: 0" "$URL" 2>/dev/null || echo "000")

case "$http" in
    2*)
        rm -f "$FAIL_FILE"
        exit 0
        ;;
esac

# Failure path: increment counter, restart on threshold reached.
n=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAIL_FILE"
log "ping failed (http=$http) [$n/$MAX_FAIL]"

if [ "$n" -ge "$MAX_FAIL" ]; then
    log "restarting botsyncd after $n consecutive ping failures"
    : > "$KICK_MARKER"
    /etc/init.d/botsync restart >/dev/null 2>&1 &
    rm -f "$FAIL_FILE"
fi

exit 0
