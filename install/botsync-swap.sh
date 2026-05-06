#!/bin/sh
# BOT-SYNC swap helper.
#
# Creates and activates a small swapfile on the BOT-SYNC USB drive ONLY when:
#   * the device has < ~470 MB total RAM (i.e. low-RAM router class), AND
#   * no swap is currently active, AND
#   * a writable USB root is given (arg 1, or $BOTSYNC_ROOT).
#
# On higher-RAM hardware (laptops, NAS boxes), this is a no-op.
#
# Usage:
#   botsync-swap ensure [<usb-root>]   # create+activate if needed
#   botsync-swap status                # print current swap state
#   botsync-swap off [<usb-root>]      # swapoff + remove our rc.local hook
#                                      #  (file left on disk; safe to keep)
#
# Idempotent. Safe to call on every boot.

set -e

SIZE_MB="${BOTSYNC_SWAP_MB:-256}"     # default 256 MB swapfile
LOW_RAM_KB="${BOTSYNC_LOW_RAM_KB:-480000}"   # ~470 MB threshold
TAG="botsync-swap"

log() { echo "[$TAG] $*"; }

ram_total_kb() {
    awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0
}

swap_active() {
    # POSIX-friendly: any non-empty line beyond the header in /proc/swaps.
    awk 'NR>1 && NF>0 {found=1} END {exit found?0:1}' /proc/swaps 2>/dev/null
}

resolve_root() {
    R="${1:-$BOTSYNC_ROOT}"
    if [ -z "$R" ]; then
        # Auto-detect: first directory under /mnt/sync or /tmp/mountd that
        # carries a .botsync_marker.
        for base in /mnt/sync /tmp/mountd; do
            [ -d "$base" ] || continue
            for d in "$base"/*; do
                [ -d "$d" ] || continue
                [ -f "$d/.botsync_marker" ] || continue
                R="$d"; break 2
            done
        done
    fi
    [ -n "$R" ] && [ -d "$R" ] && [ -w "$R" ] && echo "$R"
}

ensure_rc_local_line() {
    # Make swap survive reboot via /etc/rc.local. The activation line may be
    # 'swapon /path' (works on ext*) or a small block that sets up a loop
    # device first (needed for exfat/ntfs/fat hosts).
    SWAP_PATH="$1"
    USE_LOOP="$2"   # "1" = wrap with losetup
    RC=/etc/rc.local
    [ -f "$RC" ] || printf '#!/bin/sh\nexit 0\n' > "$RC"
    # Wipe any prior botsync-swap block to avoid duplicates.
    if grep -q '# botsync-swap' "$RC"; then
        sed -i '/# botsync-swap BEGIN/,/# botsync-swap END/d' "$RC"
    fi
    if [ "$USE_LOOP" = "1" ]; then
        # Boot-time activation: re-uses the helper itself to avoid duplicating
        # the (busybox-losetup-or-python ioctl) detection logic. The helper's
        # `ensure` path also re-applies the VM sysctl tuning.
        BLOCK=$(printf '%s\n' \
            "# botsync-swap BEGIN" \
            "[ -x /usr/sbin/botsync-swap ] && /usr/sbin/botsync-swap ensure '$(dirname "$(dirname "$SWAP_PATH")")' >/dev/null 2>&1 || true" \
            "# botsync-swap END")
    else
        # Native-swapfile boot path: re-enter the helper so VM tuning is
        # applied alongside the swapon, instead of skipping it entirely on
        # subsequent boots when swap is already active.
        BLOCK=$(printf '%s\n' \
            "# botsync-swap BEGIN" \
            "swapon -p 10 '$SWAP_PATH' 2>/dev/null || true" \
            "[ -x /usr/sbin/botsync-swap ] && /usr/sbin/botsync-swap ensure '$(dirname "$(dirname "$SWAP_PATH")")' >/dev/null 2>&1 || true" \
            "# botsync-swap END")
    fi
    if grep -q '^exit 0' "$RC"; then
        awk -v block="$BLOCK" '/^exit 0/ && !done {print block; done=1} {print}' "$RC" > "$RC.new"
        mv "$RC.new" "$RC"
    else
        printf '%s\n' "$BLOCK" >> "$RC"
    fi
    chmod +x "$RC" 2>/dev/null || true
}

remove_rc_local_line() {
    RC=/etc/rc.local
    [ -f "$RC" ] || return 0
    sed -i '/# botsync-swap BEGIN/,/# botsync-swap END/d' "$RC" 2>/dev/null || true
}

fs_supports_swapfile() {
    # Returns 0 if the filesystem hosting $1 supports plain swapfiles.
    # ext2/3/4, btrfs and xfs are fine. exfat, ntfs, vfat, fuseblk, tmpfs are
    # NOT - those need the loop-device workaround.
    DIR="$1"
    FS=$(df -T "$DIR" 2>/dev/null | awk 'NR==2 {print $2}')
    case "$FS" in
        ext2|ext3|ext4|btrfs|xfs) return 0 ;;
        *) return 1 ;;
    esac
}

tune_vm() {
    # Apply low-RAM-router VM knobs. Idempotent; safe to call every boot.
    # Notes on the choices:
    #   swappiness=80         — prefer paging out anonymous pages over
    #                           evicting fs cache, since rclone touches a
    #                           huge amount of dentries/page-cache during a
    #                           listing pass. Evicting that cache mid-sync
    #                           causes I/O thrash & the \"router locks up\"
    #                           failure mode users see.
    #   min_free_kbytes=8192  — keep ~8MB head-room so allocations from
    #                           atomic context (network RX, mmc IRQs) don't
    #                           fail and cascade.
    #   vfs_cache_pressure=200 — let the kernel reclaim dentry/inode caches
    #                            faster under pressure.
    #   overcommit_memory=2   — strict accounting; malloc() returns NULL\n    #                           instead of producing reservations the box\n    #                           can't back. Combined with overcommit_ratio=80\n    #                           this lets the kernel use ~80% of (RAM+swap)\n    #                           before refusing further commits, which fails\n    #                           rclone allocations gracefully instead of\n    #                           triggering OOM kills mid-write.\n    #   panic_on_oom=0        — leave userspace OOM-kill enabled (default);\n    #                           we explicitly ensure the kernel does not\n    #                           panic on OOM, which would reboot the box.
    sysctl -w \
        vm.swappiness=80 \
        vm.min_free_kbytes=8192 \
        vm.vfs_cache_pressure=200 \
        vm.overcommit_memory=2 \
        vm.overcommit_ratio=80 \
        vm.panic_on_oom=0 \
        kernel.panic_on_oops=0 \
        >/dev/null 2>&1 || true
}

cmd="${1:-ensure}"
shift 2>/dev/null || true

case "$cmd" in
    status)
        echo "ram_total_kb=$(ram_total_kb)"
        echo "low_ram_threshold_kb=$LOW_RAM_KB"
        echo "swap_active=$(swap_active && echo yes || echo no)"
        cat /proc/swaps 2>/dev/null
        ;;
    ensure)
        tune_vm
        RAM=$(ram_total_kb)
        if [ "$RAM" -ge "$LOW_RAM_KB" ]; then
            log "ram=${RAM}kB >= threshold ${LOW_RAM_KB}kB; not creating swap"
            exit 0
        fi
        if swap_active; then
            log "swap already active; vm tuned, nothing else to do"
            exit 0
        fi
        ROOT="$(resolve_root "$1" || true)"
        if [ -z "$ROOT" ]; then
            log "no writable BOTSYNC_ROOT; cannot create swapfile (will retry next boot)"
            exit 0
        fi
        SWAP="$ROOT/etc/swapfile"
        mkdir -p "$ROOT/etc"
        if [ ! -s "$SWAP" ]; then
            log "creating ${SIZE_MB}MB swapfile at $SWAP"
            # Prefer fallocate (instant); fall back to dd.
            if command -v fallocate >/dev/null 2>&1 && fallocate -l "${SIZE_MB}M" "$SWAP" 2>/dev/null; then
                :
            else
                dd if=/dev/zero of="$SWAP" bs=1M count="$SIZE_MB" status=none 2>/dev/null \
                    || dd if=/dev/zero of="$SWAP" bs=1024 count=$((SIZE_MB*1024)) >/dev/null 2>&1
            fi
            chmod 600 "$SWAP"
        fi

        # Tune VM knobs for low-RAM routers (already done at top of `ensure`,
        # but harmless to repeat — sysctl is idempotent and we want the
        # tuning to apply even if the swap-creation path below was skipped
        # on a previous boot).
        tune_vm

        if fs_supports_swapfile "$ROOT"; then
            mkswap "$SWAP" >/dev/null 2>&1 || { log "mkswap failed"; exit 1; }
            if swapon -p 10 "$SWAP" 2>/dev/null; then
                log "swap ON: $SWAP (${SIZE_MB}MB, native, prio 10)"
                ensure_rc_local_line "$SWAP" 0
                exit 0
            fi
            log "swapon failed on native fs; trying loop device"
        fi

        # Loop-device path (exfat/ntfs/vfat/fuse/etc).
        # OpenWrt routers often ship without `losetup` in busybox, but
        # /dev/loop* and the loop kernel module are present. Fall back to a
        # tiny Python helper that performs LOOP_SET_FD via ioctl. Python is
        # always present on the daemon host.
        modprobe loop 2>/dev/null || true
        LOOP=""
        if command -v losetup >/dev/null 2>&1; then
            losetup -j "$SWAP" 2>/dev/null | awk -F: '{print $1}' | while read L; do
                [ -n "$L" ] && losetup -d "$L" 2>/dev/null || true
            done
            LOOP=$(losetup -f --show "$SWAP" 2>/dev/null) || LOOP=""
        fi
        if [ -z "$LOOP" ] && command -v python3 >/dev/null 2>&1; then
            LOOP=$(python3 - "$SWAP" <<'PY'
import sys, os, fcntl, glob
LOOP_SET_FD       = 0x4C00
LOOP_CLR_FD       = 0x4C01
LOOP_CTL_GET_FREE = 0x4C82
path = sys.argv[1]
# Try /dev/loop-control first.
ldev = None
try:
    ctl = os.open("/dev/loop-control", os.O_RDWR)
    n = fcntl.ioctl(ctl, LOOP_CTL_GET_FREE)
    os.close(ctl)
    if n >= 0: ldev = "/dev/loop%d" % n
except Exception:
    pass
candidates = [ldev] if ldev else []
candidates += sorted(glob.glob("/dev/loop[0-9]*"))
backing = os.open(path, os.O_RDWR)
for c in candidates:
    if not c: continue
    try:
        lo = os.open(c, os.O_RDWR)
        try:
            fcntl.ioctl(lo, LOOP_SET_FD, backing)
            print(c); os.close(lo); sys.exit(0)
        except OSError:
            os.close(lo); continue
    except OSError:
        continue
sys.exit(1)
PY
            ) || LOOP=""
        fi
        if [ -z "$LOOP" ]; then
            log "no usable loop device (need losetup or python3 with /dev/loop*); cannot create swap on this filesystem"
            exit 1
        fi
        if ! mkswap "$LOOP" >/dev/null 2>&1; then
            log "mkswap on $LOOP failed"
            losetup -d "$LOOP" 2>/dev/null || true
            exit 1
        fi
        if swapon -p 10 "$LOOP" 2>/dev/null; then
            log "swap ON: $SWAP via $LOOP (${SIZE_MB}MB, loop, prio 10)"
            ensure_rc_local_line "$SWAP" 1
            exit 0
        fi
        log "swapon failed even via loop device"
        losetup -d "$LOOP" 2>/dev/null || true
        exit 1
        ;;
    off)
        ROOT="$(resolve_root "$1" || true)"
        # swapoff every active swap entry (we only ever activate one).
        awk 'NR>1 {print $1}' /proc/swaps 2>/dev/null | while read SW; do
            [ -n "$SW" ] && swapoff "$SW" 2>/dev/null || true
        done
        if [ -n "$ROOT" ] && [ -f "$ROOT/etc/swapfile" ]; then
            remove_rc_local_line
            log "swap deactivated; file left at $SWAP (delete manually if you want)"
        fi
        ;;
    *)
        echo "usage: $0 {ensure|status|off} [usb-root]" >&2
        exit 2
        ;;
esac
