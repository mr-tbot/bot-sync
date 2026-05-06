#!/bin/sh
# BOT-SYNC bootstrap for OpenWrt routers.
#
# Targets:
#   * GL-iNet GL-A1300  (OpenWrt 21.02-SNAPSHOT, ipq40xx, armv7) - primary
#   * Generic OpenWrt 19.07 or newer on any architecture
#
# Run ONCE on the router via SSH. After this, the daemon is run by procd from
# the USB drive; pulling the USB stick removes all sync state from the device.
#
# Usage:
#   sh setup.sh                       # auto-detect model
#   sh setup.sh --model gl-a1300      # force GL-iNet GL-A1300 profile
#   sh setup.sh --model generic       # force generic OpenWrt profile
#   sh setup.sh --no-hostname         # skip the bot.sync hostname feature
#   sh setup.sh --hostname bot.sync   # use a different friendly hostname
#   sh setup.sh --port 8585           # change daemon port (default 8585)
#   sh setup.sh --uninstall           # remove everything this installer added
#
# Idempotent. Re-run safely.

set -e

MODEL="auto"
HOSTNAME_ENABLED=1
HOSTNAME_NAME="bot.sync"
PORT="8585"
DO_UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --model)        MODEL="$2"; shift 2 ;;
        --no-hostname)  HOSTNAME_ENABLED=0; shift ;;
        --hostname)     HOSTNAME_NAME="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        --uninstall)    DO_UNINSTALL=1; shift ;;
        -h|--help)      sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "[bot-sync] $*"; }
SRC="$(cd "$(dirname "$0")" && pwd)"

# ---------- model detection ----------
detect_model() {
    if [ -f /etc/glversion ] || [ -d /etc/config/glconfig ] 2>/dev/null || \
       grep -qi "gl[- ]\?inet" /tmp/sysinfo/model 2>/dev/null; then
        echo "gl-a1300"
    else
        echo "generic"
    fi
}

if [ "$MODEL" = "auto" ]; then
    MODEL="$(detect_model)"
    log "auto-detected model: $MODEL"
fi
case "$MODEL" in
    gl-a1300|generic) ;;
    *) echo "unknown --model '$MODEL' (expected gl-a1300 or generic)" >&2; exit 2 ;;
esac

# ---------- OpenWrt version sanity ----------
if [ -r /etc/openwrt_release ]; then
    . /etc/openwrt_release 2>/dev/null || true
    log "OpenWrt: ${DISTRIB_ID:-?} ${DISTRIB_RELEASE:-?} (${DISTRIB_CODENAME:-?})"
    case "$DISTRIB_RELEASE" in
        17.*|18.*) echo "  WARNING: OpenWrt $DISTRIB_RELEASE is below the supported minimum (19.07). Continuing anyway." ;;
    esac
fi

# ---------- uninstall path ----------
if [ "$DO_UNINSTALL" = "1" ]; then
    log "stopping + disabling service..."
    [ -x /etc/init.d/botsync ] && /etc/init.d/botsync stop || true
    [ -x /etc/init.d/botsync ] && /etc/init.d/botsync disable || true
    rm -f /etc/init.d/botsync
    rm -f /etc/config/botsync
    rm -f /etc/hotplug.d/block/90-botsync
    rm -f /etc/avahi/services/botsync-smb.service
    rm -f /etc/firewall.botsync
    rm -f /var/run/botsync.pid
    rm -f /usr/sbin/botsync-watchdog
    # Deactivate swap (file is left on USB; user can remove manually).
    [ -x /usr/sbin/botsync-swap ] && /usr/sbin/botsync-swap off >/dev/null 2>&1 || true
    rm -f /usr/sbin/botsync-swap
    rm -f /tmp/botsync-watchdog.fail /tmp/botsync-watchdog.kicked
    if [ -f /etc/crontabs/root ]; then
        sed -i '/botsync-watchdog/d' /etc/crontabs/root 2>/dev/null || true
        /etc/init.d/cron restart >/dev/null 2>&1 || true
    fi

    log "removing dnsmasq entries for $HOSTNAME_NAME..."
    EXISTING="$(uci -q get dhcp.@dnsmasq[0].address || true)"
    if [ -n "$EXISTING" ]; then
        KEEP=""
        for v in $EXISTING; do
            case "$v" in
                /*"$HOSTNAME_NAME"/*) ;;
                *) KEEP="$KEEP $v" ;;
            esac
        done
        uci -q delete dhcp.@dnsmasq[0].address
        for v in $KEEP; do uci add_list dhcp.@dnsmasq[0].address="$v"; done
        uci commit dhcp
        /etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
    fi

    log "removing firewall rule + include..."
    while IDX=$(uci show firewall 2>/dev/null | grep -oE 'firewall\.@rule\[[0-9]+\]\.name=.botsync_lan.' | grep -oE '[0-9]+' | head -n1); [ -n "$IDX" ]; do
        uci delete firewall.@rule["$IDX"]
    done
    while IDX=$(uci show firewall 2>/dev/null | grep -oE '@include\[[0-9]+\]\.path=.+firewall\.botsync.' | grep -oE '@include\[[0-9]+\]' | grep -oE '[0-9]+' | head -n1); [ -n "$IDX" ]; do
        uci delete firewall.@include["$IDX"]
    done
    uci commit firewall
    # Best-effort drop of the alias IP we may have added.
    OLD_ALIAS="$(uci -q get botsync.main.hostname_alias_ip 2>/dev/null)"
    if [ -n "$OLD_ALIAS" ]; then
        ip addr del "$OLD_ALIAS/24" dev br-lan 2>/dev/null || true
    fi
    /etc/init.d/firewall reload >/dev/null 2>&1 || true

    log "uninstall complete. /mnt/sync/ and the USB drive are untouched."
    exit 0
fi

# ---------- packages ----------
COMMON_REQ="block-mount kmod-usb-storage kmod-fs-exfat \
  python3-light python3-openssl python3-urllib python3-codecs python3-email python3-logging \
  blkid"
COMMON_OPT="samba4-server avahi-daemon kmod-usb3 ntfs-3g"
# Hostname redirect uses iptables NAT REDIRECT (universally available on fw3
# OpenWrt). No extra kernel modules required.

case "$MODEL" in
    gl-a1300)
        REQUIRED_PKGS="$COMMON_REQ"
        OPTIONAL_PKGS="$COMMON_OPT kmod-fs-ntfs3 exfat-utils"
        ;;
    generic)
        REQUIRED_PKGS="$COMMON_REQ"
        OPTIONAL_PKGS="$COMMON_OPT kmod-fs-ntfs3 exfat-utils luci-app-samba"
        ;;
esac

log "updating package lists..."
opkg update >/dev/null

log "installing required packages..."
for p in $REQUIRED_PKGS; do
    opkg list-installed "$p" | grep -q . && continue
    opkg install "$p" || { echo "  ERROR: required package $p failed to install"; exit 2; }
done

log "installing optional packages (best-effort)..."
for p in $OPTIONAL_PKGS; do
    opkg list-installed "$p" | grep -q . && continue
    opkg install "$p" 2>/dev/null || echo "  skipped optional: $p"
done

# ---------- init script + UCI ----------
log "installing init script..."
cp -f "$SRC/botsync.init" /etc/init.d/botsync
chmod +x /etc/init.d/botsync
/etc/init.d/botsync enable

log "installing UCI config (master switch)..."
if [ ! -f /etc/config/botsync ]; then
    cp -f "$SRC/botsync.uci" /etc/config/botsync
    chmod 600 /etc/config/botsync
    log "  created /etc/config/botsync (edit user/password before exposing)"
else
    log "  /etc/config/botsync already present, leaving alone"
fi
uci -q set botsync.main.port="$PORT"
if [ "$HOSTNAME_ENABLED" = "1" ]; then
    LAN_IP="$(uci -q get network.lan.ipaddr)"
    [ -z "$LAN_IP" ] && LAN_IP="$(ip -4 addr show br-lan 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n1)"
    [ -z "$LAN_IP" ] && LAN_IP="192.168.1.1"
    # Pick an alias IP in the same /24 as the router. Override via env var.
    if [ -z "$BOTSYNC_ALIAS_IP" ]; then
        ALIAS_IP="$(echo "$LAN_IP" | awk -F. '{print $1"."$2"."$3".244"}')"
    else
        ALIAS_IP="$BOTSYNC_ALIAS_IP"
    fi
    uci -q set botsync.main.hostname="$HOSTNAME_NAME"
    uci -q set botsync.main.hostname_redirect="1"
    uci -q set botsync.main.hostname_alias_ip="$ALIAS_IP"
else
    uci -q set botsync.main.hostname=""
    uci -q set botsync.main.hostname_redirect="0"
    uci -q delete botsync.main.hostname_alias_ip 2>/dev/null || true
fi
uci -q commit botsync

# ---------- firewall: open port + (optional) hostname redirect include ----------
log "opening firewall port $PORT on LAN..."
if ! uci show firewall | grep -q "name='botsync_lan'"; then
    uci add firewall rule >/dev/null
    uci set   firewall.@rule[-1].name='botsync_lan'
    uci set   firewall.@rule[-1].src='lan'
    uci set   firewall.@rule[-1].proto='tcp'
    uci set   firewall.@rule[-1].dest_port="$PORT"
    uci set   firewall.@rule[-1].target='ACCEPT'
    uci commit firewall
fi

if [ "$HOSTNAME_ENABLED" = "1" ]; then
    log "installing firewall include for $HOSTNAME_NAME -> :$PORT redirect..."
    cp -f "$SRC/firewall.botsync.sh" /etc/firewall.botsync
    chmod +x /etc/firewall.botsync
    if ! uci show firewall | grep -q "/etc/firewall.botsync"; then
        uci add firewall include >/dev/null
        uci set firewall.@include[-1].path='/etc/firewall.botsync'
        uci set firewall.@include[-1].reload='1'
        uci commit firewall
    fi
fi
/etc/init.d/firewall reload >/dev/null 2>&1 || true

# ---------- dnsmasq: resolve <hostname> to the BOT-SYNC alias IP on the LAN ----------
if [ "$HOSTNAME_ENABLED" = "1" ]; then
    log "registering dnsmasq entry: $HOSTNAME_NAME -> $ALIAS_IP"
    ENTRY="/$HOSTNAME_NAME/$ALIAS_IP"
    EXISTING="$(uci -q get dhcp.@dnsmasq[0].address || true)"
    KEEP=""
    for v in $EXISTING; do
        case "$v" in
            /*"$HOSTNAME_NAME"/*) ;;
            *) KEEP="$KEEP $v" ;;
        esac
    done
    uci -q delete dhcp.@dnsmasq[0].address 2>/dev/null || true
    for v in $KEEP; do uci add_list dhcp.@dnsmasq[0].address="$v"; done
    uci add_list dhcp.@dnsmasq[0].address="$ENTRY"
    uci commit dhcp
    /etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
fi

# ---------- hotplug ----------
log "installing hotplug rule..."
mkdir -p /etc/hotplug.d/block
cp -f "$SRC/90-botsync" /etc/hotplug.d/block/90-botsync
chmod +x /etc/hotplug.d/block/90-botsync

# ---------- watchdog (cron) ----------
log "installing watchdog script + cron entry..."
cp -f "$SRC/botsync-watchdog.sh" /usr/sbin/botsync-watchdog
chmod +x /usr/sbin/botsync-watchdog
mkdir -p /etc/crontabs
touch /etc/crontabs/root
# Remove any prior entry then add the new one. Run every minute.
sed -i '/botsync-watchdog/d' /etc/crontabs/root 2>/dev/null || true
echo "* * * * * BOTSYNC_PORT=$PORT /usr/sbin/botsync-watchdog" >> /etc/crontabs/root
/etc/init.d/cron enable >/dev/null 2>&1 || true
/etc/init.d/cron restart >/dev/null 2>&1 || true

# ---------- swap helper (low-RAM routers only) ----------
# Installed unconditionally; the helper itself decides whether swap is
# needed (no-op on devices with >= ~470 MB RAM). On the GL-A1300
# (256 MB, no swap) it creates a 256 MB swapfile on the BOT-SYNC USB
# drive once one is adopted, preventing kernel OOM reboots when rclone +
# samba + adguard run together. botsyncd.py also calls it at startup.
log "installing swap helper..."
cp -f "$SRC/botsync-swap.sh" /usr/sbin/botsync-swap
chmod +x /usr/sbin/botsync-swap
/usr/sbin/botsync-swap ensure 2>&1 | sed 's/^/  /' || true

mkdir -p /mnt/sync

echo
echo "[bot-sync] base install complete."
echo "  model:    $MODEL"
echo "  port:     $PORT"
if [ "$HOSTNAME_ENABLED" = "1" ]; then
    echo "  hostname: $HOSTNAME_NAME (DNS + port-80 redirect active)"
else
    echo "  hostname: disabled"
fi
echo
echo "Open the UI:"
if [ "$HOSTNAME_ENABLED" = "1" ]; then
    echo "  *  http://$HOSTNAME_NAME/         (port-free; LAN clients using router DNS)"
    echo "  *  http://<router-ip>:$PORT/      (always works)"
else
    echo "  *  http://<router-ip>:$PORT/"
fi
cat <<'EOF'

Default credentials are in /etc/config/botsync. Change them before exposing.

Next steps:
  1. Plug a USB drive into the router. The hotplug script auto-mounts it
     and starts the daemon if a .botsync_marker file exists on the drive.
  2. For a brand-new drive: open the UI and follow the Setup wizard.
  3. Copy a static rclone binary to <usb>/bin/rclone (https://rclone.org/downloads/).
  4. Copy botsyncd.py and the ui/ folder to <usb>/bin/ and <usb>/ui/.

Master switch:
    uci set botsync.main.enabled=0; /etc/init.d/botsync stop
    uci set botsync.main.enabled=1; /etc/init.d/botsync start

To uninstall later:
    sh setup.sh --uninstall
The USB drive is left untouched.
EOF
