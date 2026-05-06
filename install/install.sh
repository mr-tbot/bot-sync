#!/bin/sh
# Wrapper for the cross-platform BOT-SYNC installer.
# Usage:
#   sudo sh install.sh                        # interactive
#   sudo sh install.sh --target linux         # non-interactive

set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------- Python check
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
    echo "[bot-sync] Python 3.7+ is required but was not found on PATH." >&2
    echo "" >&2
    echo "Install it with your package manager:" >&2
    echo "  Debian / Ubuntu / Pi : sudo apt update && sudo apt install -y python3" >&2
    echo "  Fedora               : sudo dnf install -y python3" >&2
    echo "  Arch                 : sudo pacman -S --noconfirm python" >&2
    echo "  Alpine               : sudo apk add python3" >&2
    echo "  macOS (Homebrew)     : brew install python" >&2
    echo "  macOS (Xcode CLT)    : xcode-select --install" >&2
    exit 2
fi

# Confirm interpreter is >= 3.7. install.py also checks; this gives a clean
# message before exec'ing.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,7) else 1)'; then
    ver="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo unknown)"
    echo "[bot-sync] Python 3.7+ is required (found $ver at $PY)." >&2
    exit 2
fi

# Root check (skip for --print-only / --uninstall / --target router).
needs_root=1
for a in "$@"; do
    case "$a" in
        --print-only|--uninstall) needs_root=0 ;;
        --target=router|router)   needs_root=0 ;;
    esac
done
# also handle "--target router" split across two args
prev=""
for a in "$@"; do
    if [ "$prev" = "--target" ] && [ "$a" = "router" ]; then
        needs_root=0
    fi
    prev="$a"
done

if [ "$needs_root" = "1" ] && [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
    echo "[bot-sync] This installer writes to /etc/systemd/system or" >&2
    echo "           /Library/LaunchDaemons; root is required." >&2
    echo "           Re-run:  sudo sh $0 $*" >&2
    exit 2
fi

if [ ! -f "$HERE/install.py" ]; then
    echo "[bot-sync] $HERE/install.py is missing." >&2
    echo "           Run from a checkout of the bot-sync repository." >&2
    exit 2
fi

exec "$PY" "$HERE/install.py" "$@"
