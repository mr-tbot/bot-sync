#!/usr/bin/env python3
"""
BOT-SYNC cross-platform installer.

Run this from a checkout of the bot-sync repository. It interactively asks
which device you're installing on and either deploys BOT-SYNC there or, for
the router target, prints the exact SSH commands to run on the router (and
optionally streams the existing `install/setup.sh` over SSH for you).

Targets:

    1) GL-iNet / OpenWrt router      (primary target — uses install/setup.sh)
    2) Raspberry Pi (Linux ARM)      (systemd, install rclone via official script)
    3) Linux PC / server             (systemd)
    4) macOS                         (launchd LaunchDaemon, brew rclone if available)
    5) Windows                       (Scheduled Task at logon, rclone via winget or zip)

Stdlib only. Requires Python 3.7+.

Usage:
    python3 install.py                  # interactive
    python3 install.py --target linux   # non-interactive install for current host
    python3 install.py --target router --router-host 192.168.8.1 --router-user root
    python3 install.py --uninstall      # remove a previous local install
    python3 install.py --print-only     # print the steps without doing anything

The installer NEVER touches the existing OpenWrt artefacts under install/ —
those still drive `setup.sh`. This file is purely additive.
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import platform
import shutil
import socket
import string
import subprocess
import sys
import textwrap
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # bot-sync/
DAEMON = REPO / "botsyncd.py"
UI_DIR = REPO / "ui"
SETUP_SH = HERE / "setup.sh"

VERSION = "0.7.10"

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str, s: str) -> str:
    if not USE_COLOR:
        return s
    return f"\x1b[{code}m{s}\x1b[0m"

def info(msg: str) -> None:  print(_c("36", "[bot-sync] ") + msg)
def ok(msg: str) -> None:    print(_c("32", "[ok]       ") + msg)
def warn(msg: str) -> None:  print(_c("33", "[warn]     ") + msg)
def err(msg: str) -> None:   print(_c("31", "[error]    ") + msg, file=sys.stderr)
def hr() -> None:            print(_c("90", "-" * 70))


def prompt(question: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            ans = input(f"{question}{suffix}: ").strip()
        except EOFError:
            ans = ""
        if ans:
            return ans
        if default is not None:
            return default
        print("(value required)")


def prompt_yes(question: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{question} [{d}] ").strip().lower()
        except EOFError:
            ans = ""
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_choice(question: str, choices: List[Tuple[str, str]],
                  default: int = 1) -> str:
    print(question)
    for i, (key, desc) in enumerate(choices, 1):
        marker = " (default)" if i == default else ""
        print(f"  {i}) {desc}{marker}")
    while True:
        try:
            ans = input(f"Choice [1-{len(choices)}]: ").strip()
        except EOFError:
            ans = ""
        if not ans:
            return choices[default - 1][0]
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            return choices[int(ans) - 1][0]
        for k, _ in choices:
            if ans.lower() == k.lower():
                return k
        print(f"Enter 1..{len(choices)}.")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def must_have_sources() -> None:
    if not DAEMON.exists():
        err(f"could not find {DAEMON}")
        sys.exit(2)
    if not UI_DIR.is_dir():
        err(f"could not find {UI_DIR}")
        sys.exit(2)


def python_executable() -> str:
    return sys.executable or shutil.which("python3") or shutil.which("python") or "python3"


def is_admin() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]


def run(cmd: List[str], check: bool = True, capture: bool = False,
        cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    info("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, check=check, capture_output=capture, text=True,
                              cwd=cwd, env=env)
    except FileNotFoundError as e:
        err(f"missing executable: {e}")
        raise


def copytree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def random_password(n: int = 16) -> str:
    import secrets
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def lan_ip_guess() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return socket.gethostname()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# rclone install helpers
# ---------------------------------------------------------------------------

def rclone_present() -> Optional[str]:
    p = shutil.which("rclone")
    if not p:
        return None
    try:
        out = subprocess.check_output([p, "version"], text=True, timeout=5).splitlines()
        return f"{p} ({out[0].strip()})" if out else p
    except Exception:
        return p


def install_rclone_unix() -> bool:
    """Install rclone using the official install script (Linux/macOS)."""
    if rclone_present():
        return True
    info("Installing rclone via the official install script (https://rclone.org/install.sh)")
    if shutil.which("curl"):
        try:
            run(["sh", "-c", "curl -fsSL https://rclone.org/install.sh | sudo bash"])
            return rclone_present() is not None
        except subprocess.CalledProcessError:
            return False
    if shutil.which("wget"):
        try:
            run(["sh", "-c", "wget -qO- https://rclone.org/install.sh | sudo bash"])
            return rclone_present() is not None
        except subprocess.CalledProcessError:
            return False
    err("Neither curl nor wget found; install one and re-run, or install rclone manually.")
    return False


def install_rclone_macos() -> bool:
    if rclone_present():
        return True
    if shutil.which("brew"):
        try:
            run(["brew", "install", "rclone"])
            return rclone_present() is not None
        except subprocess.CalledProcessError:
            warn("brew install rclone failed; falling back to the install script")
    return install_rclone_unix()


def install_rclone_windows(target_dir: Path) -> Optional[Path]:
    """Download the latest rclone Windows ZIP and extract rclone.exe."""
    existing = shutil.which("rclone")
    if existing:
        return Path(existing)
    target_dir.mkdir(parents=True, exist_ok=True)
    bundled = target_dir / "rclone.exe"
    if bundled.exists():
        return bundled
    if shutil.which("winget"):
        info("Trying winget install Rclone.Rclone")
        try:
            run(["winget", "install", "--id", "Rclone.Rclone", "-e",
                 "--accept-source-agreements", "--accept-package-agreements"])
            ex = shutil.which("rclone")
            if ex:
                return Path(ex)
        except subprocess.CalledProcessError:
            warn("winget failed; falling back to direct download")
    info("Downloading rclone-current-windows-amd64.zip ...")
    url = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            blob = resp.read()
    except Exception as e:
        err(f"download failed: {e}")
        return None
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if name.lower().endswith("/rclone.exe"):
                with zf.open(name) as srcf, open(bundled, "wb") as dstf:
                    shutil.copyfileobj(srcf, dstf)
                ok(f"rclone.exe extracted to {bundled}")
                return bundled
    err("rclone.exe not found in zip")
    return None


# ---------------------------------------------------------------------------
# Preflight requirement checks
# ---------------------------------------------------------------------------

MIN_PY = (3, 7)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _disk_free_mb(path: Path) -> Optional[int]:
    try:
        p = path
        while not p.exists():
            p = p.parent
        return shutil.disk_usage(p).free // (1024 * 1024)
    except Exception:
        return None


def preflight(target: str, args: argparse.Namespace) -> None:
    """Verify everything needed before we start writing files / services.

    Hard failures call sys.exit(2). Warnings are printed but do not abort.
    """
    info(f"Preflight checks for target: {target}")
    problems: List[str] = []
    warnings: List[str] = []

    # --- Python version (this interpreter) -------------------------------
    if sys.version_info[:2] < MIN_PY:
        problems.append(
            f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required, this is "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    else:
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor}."
           f"{sys.version_info.micro}")

    # --- Sources --------------------------------------------------------
    if not DAEMON.exists():
        problems.append(f"missing daemon source: {DAEMON}")
    if not UI_DIR.is_dir():
        problems.append(f"missing UI source: {UI_DIR}")

    # --- Privilege ------------------------------------------------------
    needs_root = target in ("linux", "pi", "macos", "windows") and not args.uninstall
    if needs_root and not is_admin():
        msg = ("Administrator PowerShell required (right-click > "
               "'Run as administrator')") if target == "windows" else (
               "root required: re-run with sudo")
        if args.print_only:
            warnings.append(msg + " — print-only, continuing")
        else:
            problems.append(msg)
    elif needs_root:
        ok("running with admin/root privileges")

    # --- Network --------------------------------------------------------
    # rclone install + autosync need outbound HTTPS. Best-effort probe.
    try:
        socket.setdefaulttimeout(3)
        with socket.create_connection(("downloads.rclone.org", 443), timeout=3):
            ok("outbound HTTPS reachable (downloads.rclone.org:443)")
    except OSError:
        warnings.append("could not reach downloads.rclone.org:443 — "
                        "rclone download may fail; check your network/proxy")
    finally:
        socket.setdefaulttimeout(None)

    # --- Per-target tooling --------------------------------------------
    # In --print-only mode any "missing tool" condition is downgraded to a
    # warning so users can preview the install plan from a different host
    # (e.g. drafting a Linux install on a Mac, or vice-versa).
    def _missing_tool(msg: str) -> None:
        (warnings if args.print_only else problems).append(msg)
    if target in ("linux", "pi"):
        for c in ("sh",):
            if not _have(c):
                _missing_tool(f"required tool missing: {c}")
        if not _have("systemctl"):
            _missing_tool("systemd not detected (no systemctl on PATH)")
        else:
            ok("systemd present")
        if not (_have("curl") or _have("wget")):
            warnings.append("neither curl nor wget present — "
                            "rclone auto-install will fail. Install one or "
                            "install rclone manually before running.")
        if rclone_present():
            ok(f"rclone: {rclone_present()}")
        else:
            warnings.append("rclone not installed yet — installer will try to "
                            "fetch it")
        # Ports
        if _port_in_use(8585):
            warnings.append("TCP/8585 is already in use on this host")
    elif target == "macos":
        if not _have("launchctl"):
            _missing_tool("launchctl not found (is this really macOS?)")
        else:
            ok("launchd present")
        if not _have("brew") and not (_have("curl") or _have("wget")):
            warnings.append("no brew, curl, or wget — install rclone manually")
        if rclone_present():
            ok(f"rclone: {rclone_present()}")
        if _port_in_use(8585):
            warnings.append("TCP/8585 is already in use on this host")
    elif target == "windows":
        for c in ("schtasks", "netsh"):
            if not _have(c):
                _missing_tool(f"required tool missing: {c}")
        if not _have("powershell") and not _have("pwsh"):
            warnings.append("PowerShell not on PATH (cosmetic — schtasks is "
                            "what we actually use)")
        if rclone_present():
            ok(f"rclone: {rclone_present()}")
        else:
            warnings.append("rclone not installed yet — installer will try to "
                            "fetch via winget or direct download")
        if _port_in_use(8585):
            warnings.append("TCP/8585 is already in use on this host")
    elif target == "router":
        # All real checks happen on the router via setup.sh. Locally we just
        # need a way to push the bundle.
        if not (_have("ssh") or _have("scp")):
            warnings.append("OpenSSH (ssh/scp) not on PATH — installer will "
                            "fall back to paramiko (auto-installed via pip)")
        py = python_executable()
        try:
            r = subprocess.run([py, "-m", "pip", "--version"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                warnings.append("pip is not available in this Python — "
                                "paramiko auto-install will fail. Install "
                                "OpenSSH (ssh/scp) or 'python -m ensurepip'.")
            else:
                ok("pip present (for paramiko fallback)")
        except Exception:
            warnings.append("could not run 'pip --version'")

    # --- Disk space -----------------------------------------------------
    install_dir = Path(args.install_dir or _default_install_dir(target))
    free = _disk_free_mb(install_dir)
    if free is None:
        warnings.append(f"could not determine free space at {install_dir}")
    elif free < 80:
        problems.append(f"need ~80 MB free at {install_dir}, only {free} MB available")
    else:
        ok(f"{free} MB free at {install_dir}")

    # --- Render results -------------------------------------------------
    for w in warnings:
        warn(w)
    if problems:
        err("preflight failed:")
        for pmsg in problems:
            err("  - " + pmsg)
        sys.exit(2)
    ok("preflight checks passed")
    hr()


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _default_install_dir(target: str) -> Path:
    if target in ("linux", "pi"):
        return POSIX_DIRS["linux"]
    if target == "macos":
        return POSIX_DIRS["macos"]
    if target == "windows":
        return WIN_INSTALL_DIR
    return REPO  # router: irrelevant locally


# ---------------------------------------------------------------------------
# Linux / Pi / Mac install — shared helpers (POSIX)
# ---------------------------------------------------------------------------

POSIX_DIRS = {
    "linux": Path("/opt/bot-sync"),
    "macos": Path("/Library/Application Support/bot-sync"),
}

SYSTEMD_UNIT = """\
[Unit]
Description=BOT-SYNC daemon
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
Group={group}
Environment=BOTSYNC_ROOT={root}
Environment=BOTSYNC_BIND={bind}
Environment=BOTSYNC_ALLOW_ROOTFS=1
Environment=BOTSYNC_USER={admin_user}
Environment=BOTSYNC_PASS={admin_pass}
ExecStart={python} {install_dir}/botsyncd.py
Restart=always
RestartSec=5
WorkingDirectory={install_dir}

[Install]
WantedBy=multi-user.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.botsync.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{install_dir}/botsyncd.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BOTSYNC_ROOT</key><string>{root}</string>
    <key>BOTSYNC_BIND</key><string>{bind}</string>
    <key>BOTSYNC_ALLOW_ROOTFS</key><string>1</string>
    <key>BOTSYNC_USER</key><string>{admin_user}</string>
    <key>BOTSYNC_PASS</key><string>{admin_pass}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_dir}/botsync.out.log</string>
  <key>StandardErrorPath</key><string>{log_dir}/botsync.err.log</string>
  <key>WorkingDirectory</key><string>{install_dir}</string>
</dict>
</plist>
"""

LAUNCHD_PATH = Path("/Library/LaunchDaemons/com.botsync.daemon.plist")
SYSTEMD_PATH = Path("/etc/systemd/system/bot-sync.service")


def install_posix(kind: str, args: argparse.Namespace) -> None:
    """kind = 'linux' or 'macos'."""
    install_dir = Path(args.install_dir or POSIX_DIRS[kind])
    if args.print_only:
        info(f"Selected target: {kind}")
        info(f"Install dir       : {install_dir}")
        info(f"Data root         : {Path(args.root or (install_dir / 'data'))}")
        info(f"Service unit      : {LAUNCHD_PATH if kind == 'macos' else SYSTEMD_PATH}")
        info(f"Bind              : {args.bind or '0.0.0.0'}:8585")
        info("rclone            : auto-installed via "
             + ("brew or rclone.org/install.sh" if kind == 'macos'
                else "rclone.org/install.sh (curl or wget required)"))
        info("Service launcher  : "
             + ("launchctl load" if kind == 'macos' else "systemctl enable --now"))
        info("Re-run without --print-only (with sudo) to perform the install.")
        return
    if not is_admin():
        err("This installer needs to run with sudo / root.")
        info("Re-run:  sudo python3 " + " ".join(sys.argv))
        sys.exit(2)

    root_dir = Path(args.root or (install_dir / "data"))
    bind = args.bind or "0.0.0.0"
    admin_user = args.admin_user or prompt("Admin username", "admin")
    admin_pass = args.admin_pass or random_password()

    info(f"Install dir: {install_dir}")
    info(f"Data root  : {root_dir}")
    info(f"Bind       : {bind}:8585")

    install_dir.mkdir(parents=True, exist_ok=True)
    root_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path("/var/log/bot-sync") if kind == "linux" else (install_dir / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    info("Copying daemon + UI ...")
    shutil.copy2(DAEMON, install_dir / "botsyncd.py")
    copytree(UI_DIR, install_dir / "ui")

    info("Ensuring rclone is installed ...")
    if kind == "macos":
        if not install_rclone_macos():
            warn("rclone install failed — install manually then re-run.")
    else:
        if not install_rclone_unix():
            warn("rclone install failed — install manually then re-run.")

    py = python_executable()
    svc_user, svc_group = ("root", "root") if kind == "linux" else ("root", "wheel")
    if kind == "linux":
        # Prefer creating a dedicated unprivileged user 'botsync' if useradd present.
        if shutil.which("useradd") and not _user_exists("botsync"):
            try:
                run(["useradd", "--system", "--home", str(root_dir),
                     "--shell", "/usr/sbin/nologin", "botsync"], check=False)
            except Exception:
                pass
        if _user_exists("botsync"):
            svc_user = svc_group = "botsync"
            run(["chown", "-R", f"{svc_user}:{svc_group}", str(root_dir)], check=False)
            run(["chown", "-R", f"{svc_user}:{svc_group}", str(log_dir)], check=False)
    if kind == "linux":
        unit = SYSTEMD_UNIT.format(
            user=svc_user, group=svc_group,
            root=root_dir, bind=bind,
            admin_user=admin_user, admin_pass=admin_pass,
            python=py, install_dir=install_dir,
        )
        SYSTEMD_PATH.write_text(unit)
        ok(f"Wrote {SYSTEMD_PATH}")
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "bot-sync.service"])
    else:  # macos
        plist = LAUNCHD_PLIST.format(
            python=py, install_dir=install_dir,
            root=root_dir, bind=bind,
            admin_user=admin_user, admin_pass=admin_pass,
            log_dir=log_dir,
        )
        LAUNCHD_PATH.write_text(plist)
        ok(f"Wrote {LAUNCHD_PATH}")
        run(["launchctl", "unload", "-w", str(LAUNCHD_PATH)], check=False)
        run(["launchctl", "load", "-w", str(LAUNCHD_PATH)])

    _print_success(admin_user, admin_pass, bind)


def _user_exists(name: str) -> bool:
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except (KeyError, ImportError):
        return False


def uninstall_posix(kind: str) -> None:
    if not is_admin():
        err("Uninstall needs root."); sys.exit(2)
    if kind == "linux":
        run(["systemctl", "disable", "--now", "bot-sync.service"], check=False)
        if SYSTEMD_PATH.exists():
            SYSTEMD_PATH.unlink()
            run(["systemctl", "daemon-reload"], check=False)
    else:
        run(["launchctl", "unload", "-w", str(LAUNCHD_PATH)], check=False)
        if LAUNCHD_PATH.exists():
            LAUNCHD_PATH.unlink()
    install_dir = POSIX_DIRS[kind]
    if install_dir.exists() and prompt_yes(f"Remove {install_dir}?", False):
        shutil.rmtree(install_dir, ignore_errors=True)
    ok("Uninstalled BOT-SYNC.")


# ---------------------------------------------------------------------------
# Windows install
# ---------------------------------------------------------------------------

WIN_INSTALL_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "bot-sync"
WIN_TASK_NAME = "BOT-SYNC"


def install_windows(args: argparse.Namespace) -> None:
    install_dir = Path(args.install_dir or WIN_INSTALL_DIR)
    if args.print_only:
        info("Selected target: Windows")
        info(f"Install dir       : {install_dir}")
        info(f"Data root         : {Path(args.root or (install_dir / 'data'))}")
        info(f"Scheduled task    : {WIN_TASK_NAME} (runs at startup)")
        info(f"Bind              : {args.bind or '0.0.0.0'}:8585")
        info("rclone            : auto-installed via winget or direct ZIP from rclone.org")
        info("Firewall          : netsh advfirewall add rule for TCP/8585 (Defender only)")
        info("Re-run without --print-only from an Administrator PowerShell to perform the install.")
        return
    if not is_admin():
        err("Windows install needs to run from an Administrator PowerShell.")
        sys.exit(2)
    root_dir = Path(args.root or (install_dir / "data"))
    bind = args.bind or "0.0.0.0"
    admin_user = args.admin_user or prompt("Admin username", "admin")
    admin_pass = args.admin_pass or random_password()

    info(f"Install dir: {install_dir}")
    info(f"Data root  : {root_dir}")

    install_dir.mkdir(parents=True, exist_ok=True)
    root_dir.mkdir(parents=True, exist_ok=True)

    info("Copying daemon + UI ...")
    shutil.copy2(DAEMON, install_dir / "botsyncd.py")
    copytree(UI_DIR, install_dir / "ui")

    info("Ensuring rclone is installed ...")
    rc = install_rclone_windows(install_dir / "bin")
    if not rc:
        warn("rclone is not installed; install it manually and add to PATH.")

    py = python_executable()
    if not py or not Path(py).exists():
        err("Python 3 not found. Install Python from python.org or 'winget install Python.Python.3.12'.")
        sys.exit(2)

    # Write run.cmd that the scheduled task launches.
    run_cmd = install_dir / "run.cmd"
    run_cmd.write_text(textwrap.dedent(f"""\
        @echo off
        set BOTSYNC_ROOT={root_dir}
        set BOTSYNC_BIND={bind}
        set BOTSYNC_ALLOW_ROOTFS=1
        set BOTSYNC_USER={admin_user}
        set BOTSYNC_PASS={admin_pass}
        set BOTSYNC_RCLONE={rc or ''}
        cd /d "{install_dir}"
        "{py}" "{install_dir / 'botsyncd.py'}"
    """))

    # Open Defender Firewall rule for port 8585 (TCP, in).
    try:
        run(["netsh", "advfirewall", "firewall", "add", "rule",
             "name=BOT-SYNC", "dir=in", "action=allow", "protocol=TCP",
             "localport=8585"], check=False)
    except Exception:
        warn("could not add firewall rule — open TCP/8585 manually if needed")

    # Register Scheduled Task (system, runs at startup, restarts on failure
    # and on app crash). SCHTASKS' CLI flags can't express RestartCount /
    # RestartInterval, so we register via XML.
    info("Registering Scheduled Task ...")
    run(["schtasks", "/Delete", "/TN", WIN_TASK_NAME, "/F"], check=False)
    task_xml = install_dir / "_botsync_task.xml"
    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>BOT-SYNC daemon (auto-start + auto-restart on failure)</Description>
          </RegistrationInfo>
          <Triggers>
            <BootTrigger>
              <Enabled>true</Enabled>
            </BootTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <UserId>S-1-5-18</UserId>
              <RunLevel>HighestAvailable</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <AllowHardTerminate>true</AllowHardTerminate>
            <StartWhenAvailable>true</StartWhenAvailable>
            <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
            <AllowStartOnDemand>true</AllowStartOnDemand>
            <Enabled>true</Enabled>
            <Hidden>false</Hidden>
            <RunOnlyIfIdle>false</RunOnlyIfIdle>
            <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
            <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
            <WakeToRun>false</WakeToRun>
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <Priority>7</Priority>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>999</Count>
            </RestartOnFailure>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{run_cmd}</Command>
            </Exec>
          </Actions>
        </Task>
    """)
    # SCHTASKS /XML expects UTF-16 LE BOM.
    task_xml.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
    run(["schtasks", "/Create", "/TN", WIN_TASK_NAME,
         "/XML", str(task_xml), "/F"])
    try:
        task_xml.unlink()
    except OSError:
        pass
    run(["schtasks", "/Run", "/TN", WIN_TASK_NAME], check=False)

    _print_success(admin_user, admin_pass, bind)


def uninstall_windows() -> None:
    if not is_admin():
        err("Uninstall needs Administrator."); sys.exit(2)
    run(["schtasks", "/Delete", "/TN", WIN_TASK_NAME, "/F"], check=False)
    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=BOT-SYNC"], check=False)
    if WIN_INSTALL_DIR.exists() and prompt_yes(f"Remove {WIN_INSTALL_DIR}?", False):
        shutil.rmtree(WIN_INSTALL_DIR, ignore_errors=True)
    ok("Uninstalled BOT-SYNC.")


# ---------------------------------------------------------------------------
# Router (OpenWrt / GL-iNet) install — runs from this machine over SSH
# ---------------------------------------------------------------------------

ROUTER_INSTRUCTIONS = """
On the router, BOT-SYNC is bootstrapped by install/setup.sh. From the
router's SSH shell:

  1. Plug a USB drive into the router (formatted ext4 or exfat).
  2. SSH into the router as root:
        ssh root@<router-ip>
  3. Make sure SSH access is enabled:
        - GL-iNet web UI -> System -> Advanced Settings -> SSH
          (enable LAN, optionally set a password)
        - or the LuCI advanced UI under System -> Administration
  4. Copy this folder to /tmp on the router (use scp from another shell):
        scp -r {repo} root@<router-ip>:/tmp/bot-sync
  5. Run setup.sh on the router:
        ssh root@<router-ip> "sh /tmp/bot-sync/install/setup.sh"

This installer can do steps 4 and 5 for you if you have an SSH client and
optionally `sshpass` or `paramiko` available. If you'd rather do them by
hand, just follow the lines above.
"""


# Common GL-iNet / OpenWrt LAN IPs we can probe to find the router.
ROUTER_DEFAULT_IPS = [
    "192.168.8.1",   # GL-iNet default
    "192.168.1.1",   # generic OpenWrt
    "192.168.0.1",
    "192.168.31.1",
]


def _tcp_open(host: str, port: int = 22, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_router_ip() -> Optional[str]:
    info("Probing common router IPs for SSH (port 22) ...")
    for ip in ROUTER_DEFAULT_IPS:
        if _tcp_open(ip, 22):
            ok(f"found SSH on {ip}")
            return ip
    return None


def _ensure_paramiko() -> bool:
    """Make sure `paramiko` is importable on this machine; install via pip if not."""
    try:
        import paramiko  # type: ignore  # noqa: F401
        return True
    except ImportError:
        pass
    info("Installing paramiko locally (needed to push to the router) ...")
    py = python_executable()
    # Try a user install first, fall back to a system install if that fails
    # (e.g. in a venv or as root where --user is unsupported).
    attempts = [
        [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--user", "paramiko"],
        [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "paramiko"],
    ]
    for cmd in attempts:
        try:
            run(cmd, check=True)
        except Exception:
            continue
        # Make freshly installed user packages discoverable in this process.
        try:
            import site, importlib  # noqa: F401
            for d in site.getsitepackages() + [site.getusersitepackages()]:
                if d and d not in sys.path:
                    sys.path.insert(0, d)
            importlib.invalidate_caches()
            import paramiko  # type: ignore  # noqa: F401
            ok("paramiko installed")
            return True
        except Exception:
            continue
    warn("could not install paramiko automatically — will fall back to ssh/scp on PATH")
    return False


def install_router(args: argparse.Namespace) -> None:
    info("Selected target: GL-iNet / OpenWrt router")

    if args.print_only:
        print(ROUTER_INSTRUCTIONS.format(repo=REPO))
        return

    # 1. Resolve host. Prefer --router-host, else probe common IPs, else ask.
    host = args.router_host
    if not host:
        probed = _probe_router_ip()
        if probed and prompt_yes(f"Use {probed} as the router?", default=True):
            host = probed
        else:
            host = prompt("Router host (IP or name)", "192.168.8.1")
    elif not _tcp_open(host, 22):
        warn(f"{host}:22 is not reachable yet — we'll still try to connect.")

    # 2. SSH credentials.
    user = args.router_user or prompt("SSH user", "root")
    if args.router_pass:
        password: Optional[str] = args.router_pass
    elif sys.stdin.isatty():
        password = getpass.getpass(
            f"SSH password for {user}@{host} (blank = use key auth): "
        ) or None
    else:
        password = None

    # 3. Make sure we have a way to push. paramiko is preferred because it
    #    works unattended with a password and doesn't need OpenSSH installed.
    have_paramiko = _ensure_paramiko()

    # 4. Push + run setup.sh.
    if have_paramiko:
        _router_install_paramiko(host, user, password, args)
    else:
        _router_install_scp(host, user, password, args)


def _router_install_paramiko(host: str, user: str, password: Optional[str],
                             args: argparse.Namespace) -> None:
    import paramiko  # type: ignore

    info(f"Connecting to {user}@{host} via paramiko ...")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=user, password=password,
                    look_for_keys=password is None, allow_agent=password is None,
                    timeout=20)
    except paramiko.AuthenticationException:
        err(f"SSH authentication failed for {user}@{host}.")
        info("Set/verify the router password from the GL-iNet web UI:")
        info("  System -> Advanced Settings -> SSH -> set Root Password")
        return
    except (socket.timeout, OSError) as e:
        err(f"could not reach {host}:22 ({e}).")
        info("Make sure the router is on this network and SSH is enabled:")
        info("  GL-iNet web UI -> System -> Advanced Settings -> SSH -> enable LAN")
        return
    try:
        # Build a tar stream of the repo (excluding _mock_root and __pycache__).
        info("Streaming repo to /tmp/bot-sync.tar.gz ...")
        import tarfile
        # Files that must have LF line endings on the router (shell, init, conf,
        # python, html, etc.). On a Windows checkout these may be CRLF.
        TEXT_EXT = {".sh", ".py", ".js", ".css", ".html", ".json", ".uci",
                    ".conf", ".init", ".md", ".txt", ".yml", ".yaml"}
        TEXT_NAMES = {"botsync.init", "botsync.uci"}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for entry in REPO.rglob("*"):
                rel = entry.relative_to(REPO)
                if any(p in {"_mock_root", "__pycache__", "logs"} for p in rel.parts):
                    continue
                if not entry.is_file():
                    continue
                if entry.suffix.lower() in TEXT_EXT or entry.name in TEXT_NAMES:
                    raw = entry.read_bytes().replace(b"\r\n", b"\n")
                    ti = tarfile.TarInfo(name=str(rel).replace("\\", "/"))
                    ti.size = len(raw)
                    ti.mtime = int(entry.stat().st_mtime)
                    ti.mode = 0o755 if entry.suffix.lower() == ".sh" else 0o644
                    tar.addfile(ti, io.BytesIO(raw))
                else:
                    tar.add(entry, arcname=str(rel).replace("\\", "/"))
        buf.seek(0)
        t = cli.get_transport()
        ch = t.open_session()
        ch.exec_command("mkdir -p /tmp/bot-sync && cd /tmp/bot-sync && cat > __upload.tgz")
        ch.sendall(buf.read()); ch.shutdown_write()
        rc = ch.recv_exit_status()
        if rc != 0:
            err(f"upload failed (rc={rc})"); return
        ch = t.open_session()
        ch.exec_command("cd /tmp/bot-sync && tar -xzf __upload.tgz && rm __upload.tgz && "
                        f"sh install/setup.sh"
                        + (f" --hostname {args.router_hostname}" if args.router_hostname else ""))
        out = ch.makefile().read().decode("utf-8", "replace")
        e = ch.makefile_stderr().read().decode("utf-8", "replace")
        rc = ch.recv_exit_status()
        print(out)
        if e:
            print(_c("33", e), file=sys.stderr)
        if rc == 0:
            ok("Router setup completed.")
        else:
            err(f"setup.sh exited {rc}")
    finally:
        cli.close()


def _router_install_scp(host: str, user: str, password: Optional[str],
                        args: argparse.Namespace) -> None:
    if not shutil.which("scp") or not shutil.which("ssh"):
        err("ssh/scp not found on PATH and paramiko is not installed.")
        info("Install OpenSSH client or `pip install paramiko` and re-run.")
        return
    if password and not shutil.which("sshpass"):
        warn("sshpass not found; you'll be prompted for the password twice.")
    pre = ["sshpass", "-p", password] if (password and shutil.which("sshpass")) else []
    info("Copying repo to router via scp ...")
    run(pre + ["scp", "-r", "-o", "StrictHostKeyChecking=accept-new",
               str(REPO), f"{user}@{host}:/tmp/bot-sync"])
    info("Running setup.sh on router ...")
    args_extra = f" --hostname {args.router_hostname}" if args.router_hostname else ""
    run(pre + ["ssh", "-o", "StrictHostKeyChecking=accept-new",
               f"{user}@{host}", f"sh /tmp/bot-sync/install/setup.sh{args_extra}"])
    ok("Router setup completed.")


# ---------------------------------------------------------------------------
# Success banner
# ---------------------------------------------------------------------------

def _print_success(admin_user: str, admin_pass: str, bind: str) -> None:
    hr()
    ok("BOT-SYNC installed.")
    ip = lan_ip_guess() if bind in ("0.0.0.0", "::", "") else bind
    print(f"   URL : http://{ip}:8585/")
    print(f"   user: {admin_user}")
    print(f"   pass: {admin_pass}")
    print()
    print("   The password above was generated for this install. Save it now;")
    print("   you can change it later from Settings.")
    hr()


# ---------------------------------------------------------------------------
# Target detection / dispatch
# ---------------------------------------------------------------------------

def detect_default_target() -> str:
    sysname = platform.system().lower()
    if sysname == "windows":
        return "windows"
    if sysname == "darwin":
        return "macos"
    if sysname == "linux":
        # Pi has /proc/device-tree/model containing 'Raspberry Pi'
        try:
            model = Path("/proc/device-tree/model").read_text(errors="ignore").lower()
            if "raspberry" in model:
                return "pi"
        except Exception:
            pass
        return "linux"
    return "linux"


def main() -> None:
    p = argparse.ArgumentParser(
        prog="bot-sync-installer",
        description="Cross-platform interactive installer for BOT-SYNC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Targets:
              router  GL-iNet / OpenWrt router (uses install/setup.sh)
              pi      Raspberry Pi OS / Debian / Ubuntu on ARM (systemd)
              linux   Generic Linux with systemd
              macos   macOS with launchd
              windows Windows 10/11 (Scheduled Task)

            Examples:
              python3 install.py
              python3 install.py --target linux
              sudo python3 install.py --target macos --admin-user admin
              python3 install.py --target router --router-host 192.168.8.1 --router-user root
              python3 install.py --uninstall --target linux
        """),
    )
    p.add_argument("--target", choices=["router", "pi", "linux", "macos", "windows"])
    p.add_argument("--install-dir", help="override install directory")
    p.add_argument("--root", help="override BOTSYNC_ROOT (data path)")
    p.add_argument("--bind", help="override bind address (default 0.0.0.0)")
    p.add_argument("--admin-user", help="initial admin username (default 'admin')")
    p.add_argument("--admin-pass", help="initial admin password (random if omitted)")
    p.add_argument("--router-host", help="router hostname / IP for --target router")
    p.add_argument("--router-user", help="router SSH username (default 'root')")
    p.add_argument("--router-pass", help="router SSH password (or use SSH key auth)")
    p.add_argument("--router-hostname", help="friendly hostname (default bot.sync)")
    p.add_argument("--uninstall", action="store_true",
                   help="remove a previous local install for the chosen target")
    p.add_argument("--print-only", action="store_true",
                   help="print steps but don't change anything (useful for routers)")
    args = p.parse_args()

    must_have_sources()

    print(_c("1;36",
        f"\nBOT-SYNC installer v{VERSION}\n"
        "================================\n"))

    if not args.target:
        default = detect_default_target()
        order = ["router", "pi", "linux", "macos", "windows"]
        choices = [
            ("router",  "GL-iNet / OpenWrt router (recommended)"),
            ("pi",      "Raspberry Pi (Linux ARM, systemd)"),
            ("linux",   "Linux PC / server (systemd)"),
            ("macos",   "macOS (launchd)"),
            ("windows", "Windows 10/11 (Scheduled Task)"),
        ]
        default_idx = order.index(default) + 1
        info(f"Detected this machine as: {default}")
        target = prompt_choice("Where do you want to install BOT-SYNC?",
                               choices, default=default_idx)
    else:
        target = args.target

    if args.uninstall:
        if target == "windows":
            uninstall_windows()
        elif target == "macos":
            uninstall_posix("macos")
        elif target in ("linux", "pi"):
            uninstall_posix("linux")
        else:
            info("To uninstall on a router, run on the router:  sh /tmp/bot-sync/install/setup.sh --uninstall")
        return

    preflight(target, args)

    if target == "router":
        install_router(args)
    elif target == "windows":
        install_windows(args)
    elif target == "macos":
        install_posix("macos", args)
    elif target in ("linux", "pi"):
        install_posix("linux", args)
    else:
        err(f"unsupported target {target!r}")
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        err("aborted")
        sys.exit(130)
