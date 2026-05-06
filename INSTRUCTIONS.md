# 🤖 BOT-SYNC — Install Instructions (v0.7.10)

A USB-drive-backed cloud-sync appliance for OpenWrt routers. Watches
**Google Drive, Dropbox, Box, OneDrive, FTP / FTPS, SFTP, and plain HTTP
folder links** and keeps a USB drive plugged into the router in sync with
them. FTP / FTPS / SFTP work the same as the cloud backends — plain host +
credentials on the **☁️ Accounts** tab, no OAuth, then point a Download
at a remote path. Almost everything lives on the USB drive itself: the
only files this installer drops onto the router's flash are an init
script, a UCI config, a hotplug hook, and an optional firewall include
for the friendly hostname redirect.

> The same content is available inside the web UI under the **📖 Help** tab.

BOT-SYNC also runs on a regular **Raspberry Pi / Linux box / macOS / Windows
PC** via the unified `install/install.py` dispatcher — see
[Non-OpenWrt platforms](#non-openwrt-platforms) at the bottom of this file.

---

## Supported targets

| Profile      | Tested on                                             | Min OpenWrt |
|--------------|-------------------------------------------------------|-------------|
| `gl-a1300`   | GL-iNet GL-A1300 (ipq40xx, armv7), v4.7.2 / 21.02-SNAPSHOT | 19.07  |
| `generic`    | Any OpenWrt router with USB host + ~6 MB free in `/overlay` | 19.07  |

Hard requirements (any target):

- OpenWrt 19.07 or newer (procd init, UCI, dnsmasq, hotplug.d).
- USB host port (`kmod-usb-storage`).
- ~30 MB free on the USB drive after rclone (which is ~50 MB on its own).
- A fw3/iptables-based firewall for the friendly hostname feature
  (the redirect uses `iptables -t nat ... -j REDIRECT`, present on every
  fw3 OpenWrt build). Pure fw4/nftables firmware (OpenWrt 22.03+ default)
  works for the daemon itself, but the redirect needs to be expressed in
  nftables — see *Caveats* below.

The full daemon (`botsyncd.py`) runs from the USB drive, not from `/overlay`,
so the architecture/RAM constraints come from the USB drive's filesystem and
your willingness to run Python on the router (the daemon is single-file
stdlib, no PyPI dependencies).

---

## What gets installed where

| Path                                       | Purpose                                  |
|--------------------------------------------|------------------------------------------|
| `/etc/init.d/botsync`                      | procd init script                        |
| `/etc/config/botsync`                      | UCI config (master switch, port, creds)  |
| `/etc/hotplug.d/block/90-botsync`          | auto-mount + daemon (re)start on USB hotplug |
| `/etc/firewall.botsync`                    | iptables PREROUTING rule (Host: bot.sync → :8585) |
| `<usb>/bin/botsyncd.py`                    | the daemon                               |
| `<usb>/bin/rclone`                         | rclone binary (you supply)               |
| `<usb>/ui/`                                | static SPA                               |
| `<usb>/etc/botsync.json`                   | live state                               |
| `<usb>/var/log/botsync.log`                | rolling log                              |
| `<usb>/.botsync_marker`                    | "this drive is a BOT-SYNC drive" marker  |

Nothing is written outside those paths.

---

## Quick install

From your workstation, push the install bundle to the router (any method
works — `scp`, the bundled `push_files.py`, or copy via USB). On the GL-A1300:

```powershell
# from the repo root on your machine
python push_files.py _push_install.txt   # copies setup.sh + botsync.{init,uci} + 90-botsync to /tmp/botsync-install/
python push_files.py _push_app.txt       # copies botsyncd.py + ui/* to the USB drive
```

Then SSH in and run:

```sh
ssh root@<router-ip>
sh /tmp/botsync-install/setup.sh
```

That's it. The installer is idempotent — re-run any time.

### Optional flags

```sh
sh setup.sh --model gl-a1300         # force the GL-iNet profile
sh setup.sh --model generic          # force generic OpenWrt profile
sh setup.sh --hostname bot.sync      # change the friendly hostname (default: bot.sync)
sh setup.sh --no-hostname            # skip DNS + redirect entirely
sh setup.sh --port 8585              # daemon port (default 8585)
sh setup.sh --uninstall              # remove everything (USB drive untouched)
```

`--model auto` (the default) detects GL-iNet by looking for `/etc/glversion`
or `/etc/config/glconfig`, and falls back to `generic`.

---

## How `http://bot.sync/` works

The friendly hostname is a LAN-only convenience. To get a port-free URL
without disturbing whatever else uses port 80 on the router (LuCI, etc.),
the installer dedicates a **secondary IP address** to BOT-SYNC and bends
port 80 on that IP to the daemon's port:

1. **Alias IP on `br-lan`** — derived from the LAN IP by replacing the last
   octet with `.244` (e.g. `192.168.8.1` → `192.168.8.244`). Override with
   `BOTSYNC_ALIAS_IP=...` when calling the installer. The address is added
   to `br-lan` by the firewall include on every reload, so it survives
   reboots and `firewall reload`s.

2. **DNS** — dnsmasq is configured with `address=/bot.sync/<alias-ip>` so
   any LAN client using the router as resolver gets the alias IP when it
   looks up `bot.sync`. (Stock OpenWrt DHCP hands out the router as the
   resolver, so this Just Works on most networks.)

3. **Port-80 redirect** — a firewall include (`/etc/firewall.botsync`,
   hooked into fw3 via `firewall.@include`) adds:

   ```
   iptables -t nat -A PREROUTING -d <alias-ip> -p tcp --dport 80 \
       -j REDIRECT --to-ports 8585
   ```

   Because this targets a *destination IP* (not the HTTP `Host:` header),
   the SYN itself gets rewritten and the connection actually establishes.
   Only traffic addressed to the alias IP is touched; the router's main
   IP (and its port 80 LuCI) is unaffected.

The router's actual hostname (`hostname` UCI, mDNS, etc.) is **not**
changed.

### Caveats

- **Plain HTTP only.** Using `https://bot.sync/` won't work because there
  is no TLS terminator on port 443 of the alias. If you want HTTPS, run a
  reverse proxy (uhttpd-mod-tls, nginx, Caddy) on `<alias-ip>:443` and
  point it at `127.0.0.1:8585`.
- **LAN-only by design.** dnsmasq is not authoritative beyond your router
  and the alias IP is bound to `br-lan` only.
- **fw4 / nftables.** OpenWrt 22.03+ defaults to nftables. The provided
  firewall include uses iptables; on those builds either install
  `iptables-nft` (compatibility shim, fw3 still works) or translate the
  rule to:

  ```
  nft add rule inet fw4 dstnat ip daddr <alias-ip> tcp dport 80 redirect to :8585
  ```

  Pass `--no-hostname` to skip the redirect entirely — the daemon still
  works on `http://<router-ip>:8585/`.

---

## Verify

```sh
# Service status
/etc/init.d/botsync status            # -> running
pgrep -fa botsyncd

# UCI
uci show botsync
cat /etc/config/botsync

# Firewall + alias
uci show firewall | grep -E 'botsync|firewall\.botsync'
ip -4 addr show br-lan | grep -E 'inet '
iptables -t nat -L PREROUTING -n -v | grep 8585

# DNS
nslookup bot.sync <router-ip>          # from any LAN client
```

From a LAN client (laptop / phone on the same network):

```
http://bot.sync/                # should land on the BOT-SYNC login page
http://<router-ip>:8585/        # always works as a fallback
```

---

## Email (SMTP) notifications

BOT-SYNC can email you when a sync job completes/fails, a drive comes online
or offline, etc. Configure it on the **🔔 Notifications** tab → *Add channel*
→ kind **Email (SMTP)**. Fields:

| Field | Notes |
|---|---|
| SMTP host | e.g. `smtp.gmail.com`, `smtp.office365.com`, `smtp.fastmail.com` |
| SMTP port | `587` (STARTTLS), `465` (SSL/implicit TLS), `25` (plaintext) |
| TLS mode | `STARTTLS` (default), `SSL`, or `None`. Auto-set to `SSL` when port is 465. |
| Username | usually your full email address |
| Password | use an **app password** for Gmail / iCloud / Outlook |
| From / To | envelope addresses |
| Subject prefix | optional, default `BOT-SYNC` |

Subjects look like `[BOT-SYNC error] job.failed`. Hit **Send test** on the
channel row after saving to fire a synthetic event; errors are surfaced
inline.

Provider-specific quick reference:

| Provider | Host | Port | Mode | Notes |
|---|---|---|---|---|
| Gmail | `smtp.gmail.com` | `465` | SSL | Requires a 16-char *App password* (account → Security → 2-Step → App passwords). |
| Outlook / O365 | `smtp.office365.com` | `587` | STARTTLS | App password if 2FA is on. |
| iCloud | `smtp.mail.me.com` | `587` | STARTTLS | App-specific password. |
| Fastmail | `smtp.fastmail.com` | `465` | SSL | App password. |
| Self-hosted | your-host | varies | varies | `None` is OK for `localhost`-only relays. |

Sending email needs outbound traffic from the router. The default `wan` zone
already permits outbound TCP, so no firewall rule is needed.

---

## Reliability &amp; watchdog

BOT-SYNC v0.5.0+ self-recovers from crashes, killed processes, runaway
network connections, OOM kills, and unclean reboots. v0.6.6 layers on top an
**auto-sync** loop that re-attempts every active download/upload until it
succeeds — see *Auto-sync &amp; outage recovery* below.
jobs, network hangs, and unclean reboots. The mechanisms layered together:

| Layer | What it watches | Kicks in when |
|---|---|---|
| `procd` respawn | Process exit code | Daemon exits for any reason — crash, OOM, unhandled exception. Restart limit is `respawn 60 5 0` (unlimited tries within 60s window, 5s gap). |
| Cron watchdog (`/usr/sbin/botsync-watchdog`) | HTTP responsiveness | 3 consecutive failed pings to `POST /api/watchdog/ping`. Catches the rare “alive but hung” case where procd alone wouldn't notice. |
| Internal stuck-job thread | Job duration | Any sync job in `running` state for more than `settings.stuck_job_hours` (default 6h) is auto-cancelled and `job.stuck` fires. |
| Crash-recovery marker | Unclean shutdowns | `<usb>/var/run/botsyncd.running` survives a crash; on next start BOT-SYNC emits `system.crash_recovered` (or `system.watchdog_restart` if cron triggered it) and resets any “running” jobs to error. |
| Heartbeat thread | Daemon health | Writes `<usb>/var/run/botsyncd.heartbeat` every 30s. Surfaced in the UI as *last heartbeat*. |
| Crash logger | Fatal exceptions | Top-level `sys.excepthook` writes `<usb>/var/log/crash/crash-<ts>.log` and emits `system.error` before procd respawns. |

### Configure

- **⚙️ Settings → Reliability &amp; watchdog** in the web UI shows daemon
  PID, uptime, last heartbeat, and whether the cron watchdog has pinged
  recently.
- **Stuck-job timeout (hours)** — same panel. Default `6`. Set to `0` to
  disable the timer (not recommended for unattended boxes). Min `0`,
  max `168` (one week).

### Verify the watchdog is running

```sh
# entry should be present
grep botsync-watchdog /etc/crontabs/root
# should print roughly: * * * * * BOTSYNC_PORT=8585 /usr/sbin/botsync-watchdog

# check syslog for hits (only logs on failure or restart)
logread -e botsync-watchdog | tail

# manual test
/usr/sbin/botsync-watchdog && echo OK
```

The watchdog does **not** fight `uci botsync.main.enabled=0`; if you
disable BOT-SYNC via UCI, the watchdog stays out of its way.

### Notification events emitted

Wire any of these to your channel of choice (Discord / Slack / Email …):

- `system.crash_recovered` — daemon restarted after an unclean shutdown.
- `system.watchdog_restart` — cron watchdog forced a restart.
- `system.shutdown` — clean stop (SIGTERM or service stop).
- `system.error` — fatal unhandled exception (also written to `crash/`).
- `job.interrupted` — a job was running when the daemon went down.
- `job.stuck` — stuck-job timeout cancelled a long-running job.

---

## Auto-sync &amp; outage recovery

BOT-SYNC v0.6.6+ keeps every download/upload trying until it lands at least
one successful sync, with no user input required:

- **Auto-start on add.** When you add a folder via the UI (or
  `POST /api/downloads` / `POST /api/uploads`), the first sync is queued
  immediately. The response includes either `job_id` (started right now)
  or `queued: true` + `queued_reason` (e.g. drive not mounted, syncs
  disabled, drive paused) \u2014 the background loop will pick it up.
- **Retry across reboots / outages.** A daemon-side loop re-checks every
  active item every 15&nbsp;s and re-submits anything that has never logged
  a successful sync. After a power loss, ISP drop, OOM kill, or clean
  upgrade, the very next boot resumes pending transfers. Because rclone
  `copy` is idempotent, already-transferred files are skipped \u2014
  half-completed runs continue from where they died.
- **Backoff on hard failures.** rclone-style errors (auth, network,
  exit&nbsp;code 3, \u2026) escalate the next attempt: 30&nbsp;s \u2192 1&nbsp;m \u2192 2&nbsp;m \u2192
  5&nbsp;m \u2192 15&nbsp;m \u2192 30&nbsp;m \u2192 1&nbsp;h cap. A successful sync resets the schedule.
- **Soft blockers don't inflate backoff.** Drive unmounted, drive paused,
  BOT-SYNC master switch off, or per-direction switch off keeps the item
  in the loop and re-checks each tick (~30&nbsp;s) without delaying the
  retry.
- **Honours pause &amp; master switches.** Auto-sync respects per-drive
  pause, the global *BOT-SYNC enabled* switch, and the per-direction
  *Downloads enabled* / *Uploads enabled* switches. Disabling any of
  these pauses retries cleanly; flipping them back on resumes within a
  tick.
- **Self-terminating.** Once the first job records `done` in `sync_log`,
  the loop stops re-queuing that item. Subsequent runs come from the
  schedule field on the item or from a manual *Sync* click.

Verify it from the router:

```sh
logread | grep -iE 'autosync|job .* start' | tail
# expect lines like:
#   autosync queued download <id> (<label>)
#   job <jid> start: download <label>
```

There is nothing to install for this feature beyond the v0.6.6 daemon \u2014
the installer in this repo already pushes the matching `botsyncd.py`.
### Cross-platform behaviour

The auto-sync loop is plain Python (`threading` + `urllib`) and works
identically on every supported target. The "retry across reboots and
crashes" promise depends on the OS-level service manager bringing the
daemon back up:

| Target | Service manager | Auto-restart on crash | Auto-start on boot |
|---|---|---|---|
| OpenWrt router | procd (`respawn 60 5 0`) | yes | yes |
| Linux / Pi | systemd (`Restart=always`, `StartLimitIntervalSec=0`) | yes | yes |
| macOS | launchd (`KeepAlive=true`, `RunAtLoad=true`) | yes | yes |
| Windows 10/11 | Task Scheduler (`BootTrigger` + `RestartOnFailure`) | yes (1 min interval, 999 retries) | yes |

All targets honour the same JSON state file (`<root>/etc/botsync.json`)
and `<root>/var/log/botsync.log`. On a non-router target the daemon
runs with `BOTSYNC_ALLOW_ROOTFS=1` so the "data path must be a
mountpoint" guard (which exists to protect a router's tiny flash) is
disabled.
---

## Master switch

There are now **three** independent switches:

| Switch | Where | Effect when off |
|---|---|---|
| **BOT-SYNC enabled** (master) | UCI `botsync.main.enabled` or \u2699\ufe0f Settings | Daemon is stopped (or refuses all sync calls). Nothing runs. |
| **\u2b07\ufe0f Downloads enabled** | \u2699\ufe0f Settings (`settings.downloads_enabled` in `botsync.json`) | Daemon stays up but cloud \u2192 drive sync calls return an error. Existing files are untouched. |
| **\u2b06\ufe0f Uploads enabled** | \u2699\ufe0f Settings (`settings.uploads_enabled` in `botsync.json`) | Daemon stays up but drive \u2192 cloud sync calls return an error. |

Master switch from the shell:

```sh
# disable cleanly (survives reboot, leaves USB intact)
uci set botsync.main.enabled=0; uci commit botsync; /etc/init.d/botsync stop

# re-enable
uci set botsync.main.enabled=1; uci commit botsync; /etc/init.d/botsync start
```

The GL.iNet "show / normal" mode-switch slider does **not** affect BOT-SYNC.

---

## Adding a Dropbox shared link as a download source

This is a **Dropbox + rclone limitation**, not something bot-sync can change:
the rclone `dropbox:` backend talks to the Dropbox API, which **cannot fetch
a folder by public shared-link URL**. It can only see folders that live
inside the authenticated Dropbox account. So pasting a `dropbox.com/scl/fo/…`
or `dropbox.com/sh/…` link is not enough on its own.

To add such a download:

1. **Open the shared link in a browser** while signed in to the same
   Dropbox account that bot-sync's `TBOT-DROPBOX` remote is connected to.
2. Click **Add to my Dropbox** (sometimes labelled **Save to Dropbox**).
   Dropbox creates a mount of that shared folder inside your account; no
   data is duplicated.
3. The folder now appears at the top level of your Dropbox under the name
   the original owner gave it. You can verify from a workstation:
   ```sh
   ssh root@<router-ip> \
     /tmp/mountd/disk1_part1/bin/rclone \
     --config /tmp/mountd/disk1_part1/etc/rclone.conf \
     lsd TBOT-DROPBOX:
   ```
4. In the bot-sync UI, edit the download (or add it again) and set
   **Remote path** to that exact folder name. **Do not leave Remote path
   blank** — leaving it blank tells rclone to copy your *entire* Dropbox
   account, which on the 256 MB router will OOM-kill rclone (and may
   reboot the device) within seconds.
5. You only have to do this once. Subsequent syncs and reboots reuse the
   path. If the share owner removes you from the share, the folder
   disappears from your Dropbox and the sync starts logging
   `directory not found` — at which point you re-do step 1.

Google Drive folders **do not** have this limitation: paste the
`drive.google.com/drive/folders/<id>` URL and bot-sync resolves it via
the Drive folder ID directly.

---

## Connecting Google Drive (one-time Google Cloud setup)

Google deprecated rclone's bundled OAuth client, so bot-sync requires
**your own** OAuth 2.0 client in **your** Google Cloud project. The
client_id and client_secret you create here are stored in
`rclone.conf` on the router and re-used on every reauthentication; you
do not redo these cloud-console steps when a token expires later.

1. **Create a Google Cloud project** at
   <https://console.cloud.google.com/> → top-bar project picker →
   **New Project** → name it (e.g. *bot-sync*) → **Create**.
2. **Enable the Google Drive API**:
   <https://console.cloud.google.com/apis/library/drive.googleapis.com>
   with that project selected → **Enable**.
3. **Configure the OAuth consent screen**:
   <https://console.cloud.google.com/apis/credentials/consent>
   - User type: **External** → **Create**.
   - App name + support email → **Save and continue**.
   - Scopes: leave default → **Save and continue**.
   - **Test users → Add users**: add the Gmail/Workspace address that
     owns the Drive bot-sync will sync. Without this, sign-in fails
     with *Access blocked: …has not completed Google's verification
     process*.
   - **Save and continue** → **Back to dashboard**.
4. **Create OAuth client credentials**:
   <https://console.cloud.google.com/apis/credentials>
   → **Create Credentials → OAuth client ID** →
   Application type **Desktop app** → **Create**.
5. **Copy the Client ID and Client secret** Google shows you.
6. **In bot-sync**: Accounts → **➕ Connect account** → Provider
   *Google Drive* → name *gdrive* (or anything) → paste both values
   → **🌐 Sign in with browser**. A new tab opens to Google; sign in,
   approve, return to bot-sync. The remote flips to **OK** when the
   refresh token is written.

When a token later expires/revokes, the UI shows a red **🔐
Reauthenticate** button — click it, then **🌐 Sign in with browser**.
The existing client_id/secret are re-used; you don't revisit Google
Cloud Console.

If your OAuth client ever gets deleted from Google Cloud Console (or
you switch Google projects), update the `client_id` / `client_secret`
fields under `[<remote name>]` in
`/tmp/mountd/disk1_part1/etc/rclone.conf` and re-run the
Reauthenticate flow. The `rclone.conf.bak` integrity guard (v0.6.9+)
will preserve the new values across reboots.

---

## Uninstall

```sh
sh /tmp/botsync-install/setup.sh --uninstall
```

This removes:

- the init script, UCI config, hotplug hook
- the firewall rule and include
- the dnsmasq `bot.sync` entry

It does **not** touch the USB drive — pop the drive into another router that
has `setup.sh` installed and the same daemon will pick up the existing
`/etc/botsync.json` and resume right where it left off.

---

## Non-OpenWrt platforms

The same daemon runs on **Raspberry Pi / Linux / macOS / Windows** via the
unified Python installer at `install/install.py` (stdlib only, Python 3.7+).
Wrappers `install/install.sh` (POSIX) and `install/install.ps1` (Windows)
just enforce the Python version and hand off.

### Quick start

```sh
# Raspberry Pi / Linux / macOS  (needs sudo)
sudo sh install/install.sh

# Windows  (Administrator PowerShell)
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install\install.ps1
```

To preview the install plan from any host (no privileges required) use
`--print-only`:

```sh
python3 install/install.py --target linux   --print-only
python3 install/install.py --target pi      --print-only
python3 install/install.py --target macos   --print-only
python3 install/install.py --target windows --print-only
python3 install/install.py --target router  --print-only
```

### Where the daemon lives per platform

| Platform | Install dir | Service / startup | Notes |
|---|---|---|---|
| OpenWrt router | `<usb>/bin/botsyncd.py` | procd `/etc/init.d/botsync` | This document covers it. |
| Raspberry Pi / Linux | `/opt/bot-sync/botsyncd.py` | systemd unit `bot-sync.service` at `/etc/systemd/system/` | Creates an unprivileged `botsync` user when `useradd` is present. |
| macOS | `/Library/Application Support/bot-sync/botsyncd.py` | launchd plist `com.botsync.daemon` at `/Library/LaunchDaemons/` | rclone via Homebrew if present, otherwise `rclone.org/install.sh`. |
| Windows | `%ProgramData%\bot-sync\botsyncd.py` | Scheduled Task `BOT-SYNC` (runs at startup) | rclone via `winget` or direct ZIP. Defender Firewall rule for TCP/8585 is added via `netsh`; third-party firewalls need a manual rule. |

On non-router targets the daemon runs with `BOTSYNC_ALLOW_ROOTFS=1` so the
"data path must be a mountpoint" guard (which exists to protect a router's
tiny flash) is disabled.

### Per-platform requirements

- **Linux / Pi:** `systemctl` on PATH (systemd), and either `curl` or `wget`
  on PATH for the rclone install fallback. Distros without systemd (e.g.
  Alpine with OpenRC) need a manual init wrapper.
- **macOS:** `launchctl` on PATH; Homebrew is preferred but optional.
- **Windows:** `schtasks.exe` and `netsh.exe` on PATH (both are stock on
  Windows 10/11). Run from an Administrator PowerShell.

The installer's preflight reports any missing pieces with actionable hints.

### Uninstall

```sh
sudo python3 install/install.py --target linux  --uninstall
sudo python3 install/install.py --target macos  --uninstall
python3 install/install.py --target windows     --uninstall   # from Admin PowerShell
```

User data (the rclone config, downloaded folders, state file) is **not**
removed unless you also delete the install directory afterwards.
