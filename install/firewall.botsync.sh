#!/bin/sh
# /etc/firewall.botsync � installed as a fw3 firewall include.
# Runs on every fw3 reload.
#
# 1. Adds a secondary IP on br-lan dedicated to bot.sync so port 80 there
#    can be redirected without affecting the router's main IP.
# 2. Adds an iptables PREROUTING DNAT REDIRECT from :80 on that alias IP
#    to the BOT-SYNC daemon port.
#
# Reads /etc/config/botsync. If hostname_redirect is 0 or hostname_alias_ip
# is empty, this is a no-op.

. /lib/functions.sh

config_load botsync
config_get HR   main hostname_redirect "1"
config_get PORT main port              "8585"
config_get HIP  main hostname_alias_ip ""

[ "$HR" = "1" ]    || exit 0
[ -n "$HIP" ]      || exit 0

# Defensive: refuse anything that doesn't look like an IPv4 / port number
# before passing to iptables, so a malformed UCI value can't smuggle args.
echo "$HIP"  | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || exit 0
echo "$PORT" | grep -qE '^[0-9]+$' || exit 0

# Add the alias IP to br-lan (no-op if already present).
ip addr add "$HIP/24" dev br-lan 2>/dev/null || true

# Redirect port 80 on the alias IP to the BOT-SYNC daemon.
iptables -t nat -C PREROUTING -d "$HIP" -p tcp --dport 80 \
    -j REDIRECT --to-ports "$PORT" 2>/dev/null || \
iptables -t nat -A PREROUTING -d "$HIP" -p tcp --dport 80 \
    -j REDIRECT --to-ports "$PORT"

exit 0