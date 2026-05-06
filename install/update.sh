#!/bin/sh
# BOT-SYNC update helper.
#
# Pulls the latest source tree from the upstream GitHub repo, drops the
# refreshed botsyncd.py + ui/ files in place, optionally runs `rclone
# selfupdate`, and restarts the daemon.
#
# Designed to work three ways:
#   * Invoked from the daemon's web UI (POST /api/system/botsync/update)
#     with --from-daemon. The daemon spawns us detached so it can be
#     restarted without killing this script.
#   * Run by hand on the router via SSH:
#       sh /tmp/mountd/disk1_part1/install/update.sh
#   * Run by hand on a Linux/macOS dev box:
#       sudo sh install/update.sh
#
# Flags:
#   --from-daemon     Called by the running daemon. Suppresses interactive
#                     prompts and writes progress to update.log.
#   --skip-rclone     Don't try to update rclone.
#   --beta            Use rclone's beta channel.
#   --branch BRANCH   Pull a different git branch (default: main).
#   --repo REPO       Override the upstream repo (default mr-tbot/bot-sync).
#   --no-restart      Update files but don't restart the daemon.

set -eu

BRANCH="main"
REPO="mr-tbot/bot-sync"
DO_RCLONE=1
DO_RESTART=1
RCLONE_BETA=0
FROM_DAEMON=0

while [ $# -gt 0 ]; do
    case "$1" in
        --from-daemon)  FROM_DAEMON=1; shift ;;
        --skip-rclone)  DO_RCLONE=0; shift ;;
        --beta)         RCLONE_BETA=1; shift ;;
        --branch)       BRANCH="$2"; shift 2 ;;
        --repo)         REPO="$2"; shift 2 ;;
        --no-restart)   DO_RESTART=0; shift ;;
        -h|--help)      sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "[bot-sync-update] $*"; }

# Locate the install we're updating. The script lives at
# <install_dir>/install/update.sh, and botsyncd.py is its sibling at
# <install_dir>/bin/botsyncd.py (router layout) or <install_dir>/botsyncd.py
# (dev / Linux layout).
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DAEMON=""
for c in "$ROOT/bin/botsyncd.py" "$ROOT/botsyncd.py"; do
    [ -f "$c" ] && DAEMON="$c" && break
done
if [ -z "$DAEMON" ]; then
    log "ERROR: botsyncd.py not found near $ROOT — abort."
    exit 2
fi
UI_DIR="$(dirname "$DAEMON")/../ui"
[ -d "$UI_DIR" ] || UI_DIR="$ROOT/ui"

CUR_VERSION="$(grep -m1 -E '^VERSION = ' "$DAEMON" | sed -E 's/.*"([^"]+)".*/\1/' || echo unknown)"
log "current install: $DAEMON (v$CUR_VERSION)"
log "upstream repo:   $REPO ($BRANCH)"

# ---------- fetch fresh source ----------
WORK="$(mktemp -d 2>/dev/null || mktemp -d -t botsync-update)"
trap 'rm -rf "$WORK"' EXIT

TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
log "downloading $TARBALL ..."
DL=""
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$TARBALL" -o "$WORK/src.tgz" && DL=1 || DL=""
fi
if [ -z "$DL" ] && command -v wget >/dev/null 2>&1; then
    wget -q -O "$WORK/src.tgz" "$TARBALL" && DL=1 || DL=""
fi
if [ -z "$DL" ]; then
    log "ERROR: neither curl nor wget available - install one and retry."
    exit 2
fi
mkdir -p "$WORK/src"
tar -xzf "$WORK/src.tgz" -C "$WORK/src"
SRC="$(find "$WORK/src" -maxdepth 2 -name botsyncd.py -print -quit)"
if [ -z "$SRC" ]; then
    log "ERROR: botsyncd.py missing in tarball - abort."
    exit 2
fi
SRC_DIR="$(dirname "$SRC")"
NEW_VERSION="$(grep -m1 -E '^VERSION = ' "$SRC" | sed -E 's/.*"([^"]+)".*/\1/' || echo unknown)"
log "downloaded:      v$NEW_VERSION"

if [ "$NEW_VERSION" = "$CUR_VERSION" ] && [ "$FROM_DAEMON" = "0" ]; then
    log "already on latest ($CUR_VERSION) - nothing to do (re-run with --no-restart"
    log "                                    to force overwrite)."
    if [ "$DO_RCLONE" = "1" ]; then
        log "(skipping rclone too because nothing changed; pass --skip-rclone=0 to force)"
    fi
    exit 0
fi

# ---------- swap files in place ----------
log "installing botsyncd.py ..."
cp -f "$SRC" "$DAEMON"
chmod +x "$DAEMON"

if [ -d "$SRC_DIR/ui" ] && [ -d "$UI_DIR" ]; then
    log "installing ui/ ..."
    # Copy each file individually so we don't follow symlinks or wipe
    # local customisations the user may have dropped in.
    for f in "$SRC_DIR/ui"/*; do
        [ -f "$f" ] || continue
        cp -f "$f" "$UI_DIR/"
    done
fi

# Refresh helper scripts (init, watchdog, swap) when we can — these rarely
# change but a router install may benefit from a bug fix here.
if [ -d "$SRC_DIR/install" ]; then
    for n in botsync.init botsync-swap.sh botsync-watchdog.sh firewall.botsync.sh 90-botsync; do
        [ -f "$SRC_DIR/install/$n" ] || continue
        case "$n" in
            botsync.init)
                if [ -f /etc/init.d/botsync ]; then
                    cp -f "$SRC_DIR/install/$n" /etc/init.d/botsync
                    chmod +x /etc/init.d/botsync
                fi ;;
            botsync-swap.sh)
                [ -f /usr/sbin/botsync-swap ] && cp -f "$SRC_DIR/install/$n" /usr/sbin/botsync-swap && chmod +x /usr/sbin/botsync-swap || true ;;
            botsync-watchdog.sh)
                [ -f /usr/sbin/botsync-watchdog ] && cp -f "$SRC_DIR/install/$n" /usr/sbin/botsync-watchdog && chmod +x /usr/sbin/botsync-watchdog || true ;;
            firewall.botsync.sh)
                [ -f /etc/firewall.botsync ] && cp -f "$SRC_DIR/install/$n" /etc/firewall.botsync && chmod +x /etc/firewall.botsync || true ;;
            90-botsync)
                [ -f /etc/hotplug.d/block/90-botsync ] && cp -f "$SRC_DIR/install/$n" /etc/hotplug.d/block/90-botsync && chmod +x /etc/hotplug.d/block/90-botsync || true ;;
        esac
        # Also refresh the install/ folder on the USB drive itself so the
        # next update has the new helper available.
        cp -f "$SRC_DIR/install/$n" "$ROOT/install/$n" 2>/dev/null || true
    done
    # And the script that just ran, so a future call uses the new logic.
    cp -f "$SRC" "$ROOT/install/.." 2>/dev/null || true
    cp -f "$SRC_DIR/install/update.sh" "$ROOT/install/update.sh" 2>/dev/null || true
    chmod +x "$ROOT/install/update.sh" 2>/dev/null || true
fi

# ---------- rclone selfupdate ----------
if [ "$DO_RCLONE" = "1" ]; then
    RCLONE=""
    for c in "$ROOT/bin/rclone" "$(command -v rclone 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] && RCLONE="$c" && break
    done
    if [ -n "$RCLONE" ]; then
        log "running '$RCLONE selfupdate'$( [ $RCLONE_BETA = 1 ] && printf ' --beta' )..."
        if [ "$RCLONE_BETA" = "1" ]; then
            "$RCLONE" selfupdate --beta || log "(rclone selfupdate failed - keeping installed binary)"
        else
            "$RCLONE" selfupdate || log "(rclone selfupdate failed - keeping installed binary)"
        fi
    else
        log "rclone binary not found - skipping selfupdate."
    fi
fi

# ---------- restart daemon ----------
if [ "$DO_RESTART" = "0" ]; then
    log "files updated; --no-restart specified, leaving daemon as-is."
    exit 0
fi

if [ -x /etc/init.d/botsync ]; then
    log "restarting via procd (/etc/init.d/botsync restart) ..."
    /etc/init.d/botsync restart || log "(restart returned non-zero)"
elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files botsync.service >/dev/null 2>&1; then
    log "restarting via systemctl ..."
    systemctl restart botsync.service || log "(systemctl restart returned non-zero)"
elif [ "$(uname 2>/dev/null)" = "Darwin" ] && [ -f /Library/LaunchDaemons/com.mrtbot.botsync.plist ]; then
    log "reloading via launchctl ..."
    launchctl unload /Library/LaunchDaemons/com.mrtbot.botsync.plist 2>/dev/null || true
    launchctl load   /Library/LaunchDaemons/com.mrtbot.botsync.plist 2>/dev/null || true
else
    log "no service manager detected - please restart botsyncd manually."
fi

log "update complete: v$CUR_VERSION -> v$NEW_VERSION"
