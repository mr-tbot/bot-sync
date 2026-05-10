# 🤖 BOT-SYNC


> **💛 Enjoying BOT-SYNC?** It's built and maintained by one developer in their spare time. If it's saved you a venue load-in or a stressful upload, please consider chipping in — donations directly fund new backends, hardware testing, and bug-fix turnaround.
>
> [**💛 Donate via PayPal**](https://www.paypal.com/donate/?business=7DQWLBARMM3FE&no_recurring=0&item_name=Support+the+development+and+growth+of+innovative+MR_TBOT+projects.&currency_code=USD) · [🐛 Report a bug / request a feature](https://github.com/mr-tbot/bot-sync/issues)


USB-drive-backed cloud-folder sync appliance for OpenWrt routers (primary


target: GL-iNet GL-A1300). Watches **Google Drive, Dropbox, Box, OneDrive,


FTP / FTPS, SFTP, and plain HTTP folder links**, and keeps a USB drive


plugged into the router in sync with them — then re-shares the drive over


SMB + Bonjour (and optionally NFS) to the LAN.





FTP, FTPS and SFTP “watch” a remote directory the same way the cloud backends


do: paste the host + credentials once on the **☁️ Accounts** tab (no OAuth),


point a Download at a remote path, and bot-sync polls it on the schedule you


pick (per-entry interval) and pulls anything new onto the drive. Uploads can


run in the other direction — push, mirror or bisync local content up to any


of the same backends.





Almost everything lives on the USB drive: the only files dropped onto the


router's flash are an init script, a UCI config, a hotplug hook, and an


optional firewall include for the friendly `http://bot.sync/` hostname.





**Status:** v0.7.21 — deployed to GL-A1300, running off USB, reachable at


`http://bot.sync/` (and `http://<router-ip>:8585/`). In-app guide now lives


under the 📖 **Help** tab and on the welcome modal.





## ⚠️ Heads-up before you start





**This is currently well-tested only on the GL-iNet GL-A1300 router** running


GL firmware 4.7.x (OpenWrt 21.02 underneath). The Raspberry Pi, generic Linux,


macOS and Windows installers exist, are based on widely-available tools


(Python 3.9 stdlib + rclone + a per-platform service manager), and *should*


work — but they have not been put through anywhere near the same number of


production cycles. **Treat the non-router targets as secondary** for now and


expect to do some manual fix-ups (firewall rules, service permissions, mount


paths) on those platforms. Bug reports and PRs from non-router users are


extremely welcome.





The original goal was a solution that could **run exclusively on a router** —


small power footprint, always-on, plug-in USB drive, no spare PC required —


so the router path is the one this project polishes first.





### Why this exists





I'm a **VJ / Production Specialist / Creative Engineer** who is constantly


wrangling content folders for live shows: media drops from artists, project


archives, prep stems, lighting cue libraries, NDI/Dante config bundles, the


lot. Every show I'm pulling the latest version of someone's "FINAL_v3_real"


folder out of a different cloud locker and getting it onto the gear at the


venue.





BOT-SYNC is my way of automating that. Plug a USB drive into the venue's


router, point bot-sync at the producers' Drive / Dropbox / SFTP / FTP / Box /


OneDrive folders, and **the router does the legwork**: pulls the latest


drops in the background and re-shares them over SMB to every laptop, media


server, and lighting console on the LAN. When an artist updates their folder


from a hotel room across the country, the venue's local copy catches up on


its own. No "is everyone on the same version" Slack thread at 2am.





### Skill level and prerequisites





This is **an intermediate self-host tutorial**, not a one-click consumer


product. To set it up successfully (especially on a router) you should be


comfortable with:





- **SSH** into a Linux box (key or password) and basic shell navigation


  (`cd`, `ls`, `cat`, `tail -f`).


- **OpenWrt / UCI basics** if you're targeting a router — at minimum knowing


  how to read `/etc/config/*`, run `uci show`, and tail `logread`.


- **Mounting a USB drive on Linux/OpenWrt** and finding its mountpoint


  (the installer will discover it for you, but if it doesn't appear you'll


  need to debug it).


- **Python 3.9+** installed on your router or host. You don't need to write


  Python; you just need to be able to run `python3 install/install.py` and


  read its error messages.


- **OAuth or token-based cloud auth.** For Google Drive you'll set up a


  Google Cloud OAuth client (one-time, ~10 minutes; the README walks


  through it). For Dropbox you'll generate a long-lived app token. FTP /


  FTPS / SFTP just need host + credentials.


- **Basic networking knowledge** — what an IP / subnet / port is, how to


  add a DHCP/DNS entry on your router (for the friendly `bot.sync`


  hostname), and how to open a firewall port if you're running on a non-


  router OS.


- **SMB / file sharing** at the level of "I know how to mount a network


  share from Windows / macOS / Linux".





If you're comfortable with most of those, you'll be fine. If "SSH" or


"mountpoint" sound unfamiliar, you'll probably want to pair this with a


tutorial on those fundamentals first — bot-sync is opinionated but it


doesn't hide the underlying Linux machinery from you.





See [INSTRUCTIONS.md](INSTRUCTIONS.md) for the full install + uninstall guide,


or jump to the [Changelog](#changelog).





## Features at a glance





- **Self-hosted on commodity hardware.** Runs on a GL-iNet GL-A1300 / any


  OpenWrt 19.07+ router, a Raspberry Pi, a Linux box, a Mac, or a Windows


  PC. Same daemon binary, platform-aware installer.


- **USB-drive-backed.** Almost everything (state, rclone config, OAuth


  tokens, sync data) lives on a plug-in USB drive — yank it out, plug it


  into another router, you're back in business.


- **7 providers.** Google Drive, Dropbox, Box, OneDrive, FTP / FTPS,


  SFTP (SSH), and plain HTTP folder listings — all powered by


  [rclone](https://rclone.org).


- **Downloads + Uploads + Bisync.** Pull cloud → drive (additive copy or


  full resync), push drive → cloud (push / mirror / bisync), per-entry


  sync interval (Manual / 1 min … 24 h / Custom seconds) with live


  countdown next to *Last sync*.


- **Projects.** Group downloads/uploads into named projects; default


  account per provider; inline project create from the Add panels.


- **LAN re-share.** SMB share `\\<router-ip>\BOT-SYNC`, Bonjour/Avahi


  advertisement, optional NFS export.


- **Friendly hostname.** `http://bot.sync/` works on every device on the


  LAN (alias IP + dnsmasq + iptables PREROUTING REDIRECT, no proxy hop).


- **Web UI.** Vanilla-JS SPA: dashboard, accounts, downloads, uploads,


  files browser, projects, system/jobs, settings, in-app Help tab.


- **Reliability.** Watchdog with auto-restart, OOM-kill recovery, master


  switches (Downloads / Uploads / global), per-drive pause/resume,


  filesystem-aware Eject dialog, hardware performance presets for


  low-RAM routers.


- **Notifications.** Discord, Slack, generic webhook, ntfy, email/SMTP


  — selectable per event type.


- **Mock mode.** `BOTSYNC_MOCK=1` runs the entire daemon (UI included)


  on any host with Python 3.7+, no rclone, no USB drive needed.


- **Pure-stdlib daemon.** No Python dependencies beyond the standard


  library; the only third-party runtime piece is the rclone binary.





## Supported providers





| Provider | Auth | Watch / pull (Download) | Push (Upload) | Notes |


|---|---|---|---|---|


| **Google Drive** | OAuth (your client_id / client_secret) | ✅ folder URL or folder ID | ✅ push / mirror / bisync | Shared folders work via folder ID. |


| **Dropbox** | OAuth | ✅ — see *Dropbox shared links* below | ✅ | Shared links must be added to the user's Dropbox first. |


| **Box** | OAuth (rclone built-in) | ✅ | ✅ | |


| **OneDrive** | OAuth (rclone built-in) | ✅ | ✅ | Personal + Business + SharePoint via rclone backend. |


| **FTP / FTPS** | host + user + password (+ TLS mode) | ✅ remote directory polling | ✅ | Plain FTP / explicit FTPS / implicit FTPS. No OAuth. |


| **SFTP (SSH)** | host + user + password *or* private key | ✅ | ✅ | Optional private key persisted at `<conf-dir>/keys/<name>.pem`. |


| **HTTP** | none / Basic | ✅ folder index pull | (read-only) | Public/listing-style HTTP servers. |





Every provider supports the same Downloads / Uploads workflow: per-entry


**sync interval** (Manual / 1 min … 24 h / Custom seconds), live schedule


countdown next to *Last sync*, default-account-per-provider auto-pick, and


project tagging.





## Layout





```


bot-sync/


  botsyncd.py          # single-file daemon (stdlib only, Python 3.7+)


  ui/                  # static SPA served at /


    index.html


    login.html


    app.js


    style.css


  install/             # OpenWrt deployment artefacts


    setup.sh           # one-shot bootstrap, runs on the router


    botsync.init       # /etc/init.d/botsync (procd)


    botsync.uci        # /etc/config/botsync (master switch + creds + hostname)


    90-botsync         # /etc/hotplug.d/block/ — auto-mount + adopt prompt


    firewall.botsync.sh# /etc/firewall.botsync — alias-IP -> :8585 redirect


  INSTRUCTIONS.md      # end-user install guide


  README.md            # this file


  _mock_root/          # created on first run in mock mode


```





## Run in mock mode (Windows / dev)





```powershell


cd c:\Users\monsi\glinet\bot-sync


$env:BOTSYNC_MOCK = "1"


$env:BOTSYNC_ROOT = "$PWD\_mock_root"


$env:BOTSYNC_USER = "admin"


$env:BOTSYNC_PASS = "admin"


python botsyncd.py


```





Then open <http://127.0.0.1:8585/> and log in as `admin` / `admin`.





In mock mode:


- No real `rclone` is invoked. Jobs simulate progress (random walk to 100%).


- Two fake drives are presented (one adopted, one detected/unadopted).


- "Adopt drive" / mount / eject are no-ops that update the state file.


- SMB/Bonjour/NFS toggles just persist to JSON.


- OAuth flow returns a fake auth URL + accepts any token.





## Cross-platform install (router / Pi / Linux / macOS / Windows)





BOT-SYNC is primarily designed for an OpenWrt router, but the same daemon and


UI also run on a regular Pi / Linux box / Mac / PC. A unified interactive


installer at `install/install.py` (stdlib only, Python 3.7+) walks you


through it:





```sh


# Linux / Pi / macOS — needs sudo for systemd / launchd / firewall edits


sudo sh install/install.sh


```





```powershell


# Windows — run from an Administrator PowerShell


Set-ExecutionPolicy -Scope Process Bypass -Force


.\install\install.ps1


```





The installer:





1. Asks where to install. Picks for you:


   - **Router (GL-iNet / OpenWrt)** — prints the SSH steps and, if you have


     `paramiko` (or `ssh`/`scp` on PATH), can stream the repo to the router


     and run `install/setup.sh` for you. The existing OpenWrt artefacts


     (`setup.sh`, `botsync.init`, `botsync.uci`, hotplug, firewall) are


     unchanged.


   - **Raspberry Pi / Linux** — drops the daemon at `/opt/bot-sync`, creates


     an unprivileged `botsync` user (if `useradd` is available), installs


     `rclone` via the official `rclone.org/install.sh`, and registers a


     systemd unit at `/etc/systemd/system/bot-sync.service`.


   - **macOS** — installs at `/Library/Application Support/bot-sync`, uses


     `brew install rclone` if Homebrew is present (else `rclone.org/install.sh`),


     and registers a LaunchDaemon at `/Library/LaunchDaemons/com.botsync.daemon.plist`.


   - **Windows** — installs at `%ProgramData%\bot-sync`, fetches `rclone.exe`


     via `winget` or the official ZIP, opens TCP/8585 in Defender Firewall,


     and registers a `SCHTASKS` job named `BOT-SYNC` running at startup.





2. Copies `botsyncd.py` and `ui/` into the chosen install dir.





3. Generates a random admin password and prints the URL + credentials.





### Preflight requirement checks





Before writing anything, the installer verifies the host has what it


needs and aborts with an actionable message if not:





- **Python 3.7+** on PATH (the wrappers `install.sh` / `install.ps1`


  also enforce this before delegating).


- Daemon + UI sources present next to the installer.


- Admin / root privileges (skipped for `--print-only`, `--uninstall`,


  and `--target router`).


- Service manager: `systemctl` on Linux/Pi, `launchctl` on macOS,


  `schtasks` + `netsh` on Windows.


- Outbound HTTPS to `downloads.rclone.org:443` (warning only — needed


  to fetch rclone if it's missing).


- rclone presence (warning only — auto-installed via the official


  install script, brew, or winget on the matching target).


- Required tooling on the chosen target (curl/wget on Linux for the


  rclone install script, `pip` on the local machine for the router


  target's paramiko fallback, etc.).


- TCP/8585 not already in use on this host.


- ≥ 80 MB free at the install directory.





Non-interactive examples:





```sh


sudo python3 install/install.py --target linux --admin-user admin


sudo python3 install/install.py --target macos


python3 install/install.py --target router --router-host 192.168.8.1 \


                            --router-user root --router-pass '...'


python3 install/install.py --uninstall --target linux


```





On non-router targets the daemon runs with `BOTSYNC_ALLOW_ROOTFS=1`, which


disables the "data path must be a mountpoint" guard (that guard exists to


protect a router's tiny flash; it doesn't apply on a Pi/PC/Mac).





## Run for real (on the router)





See [INSTRUCTIONS.md](INSTRUCTIONS.md). The short version:





```sh


# from a workstation


python push_files.py _push_install.txt


python push_files.py _push_app.txt


ssh root@<router-ip> sh /tmp/botsync-install/setup.sh


```





Afterwards the daemon is run by procd whenever a `.botsync_marker`-bearing


USB drive is mounted. The friendly hostname `http://bot.sync/` is wired up


automatically (alias IP on `br-lan` + dnsmasq + iptables PREROUTING REDIRECT


from port 80). Add `--model generic` for non-GL.iNet routers, or


`--no-hostname` to skip the friendly URL and stick with


`http://<router-ip>:8585/`.





## Settings & master switches





All three live in **⚙️ Settings** (and as UCI options):





| UI toggle | UCI option | Purpose |


|---|---|---|


| BOT-SYNC enabled (master) | `botsync.main.enabled` | Kill switch — nothing runs when off. |


| ⬇️ Downloads enabled | `settings.downloads_enabled` (state file) | Pause cloud → drive transfers only. |


| ⬆️ Uploads enabled | `settings.uploads_enabled` (state file) | Pause drive → cloud transfers only. |





When a switch is off, `POST /api/(downloads\|uploads)/{id}/sync` returns


`{ok:false, error:"... disabled"}` instead of queuing a job. Existing files


on the drive and the remote are never touched by toggling these.





## Dropbox shared links — extra step required





This is a **Dropbox + rclone limitation**, not something bot-sync can work


around: the rclone `dropbox:` backend talks to the Dropbox API, which has


no way to fetch a folder by public shared-link URL (`dropbox.com/scl/fo/…`


or `dropbox.com/sh/…`). It can only list / copy folders that live inside


the authenticated user's own Dropbox account.





So when you paste a shared link as a download source you must, **once**:





1. Open the shared link in a browser while signed in to the Dropbox


   account that bot-sync is connected to.


2. Click **Add to my Dropbox** (sometimes labelled **Save to Dropbox**).


   Dropbox creates a mount of that shared folder inside your account.


3. The folder will then appear in `rclone lsd TBOT-DROPBOX:` under


   whatever name the original owner gave it (Dropbox does **not** rename


   it to anything in the URL).


4. In the bot-sync UI, edit the download and set **Remote path** to that


   exact folder name. Save and unpause / sync.





You only have to do this the first time the folder is added; subsequent


syncs and reboots reuse the same path. If the original owner removes you


from the share, the folder disappears from your Dropbox and the sync


will start failing — bot-sync will surface that as `directory not found`


in the job log.





Google Drive shared folders **do not** require this step — paste the


`drive.google.com/drive/folders/...` URL and bot-sync resolves it via


the folder ID directly.





## Google Cloud OAuth setup (one-time, per Google account)





Google deprecated rclone's bundled OAuth client in 2024, so to use


Google Drive with bot-sync you must create your **own** OAuth 2.0 client


inside your Google Cloud project. The client_id and client_secret you


generate here are stored on the router (in `rclone.conf`) and re-used


for every reauthentication; you do **not** have to repeat the cloud


console steps the next time the token expires.





1. **Create / pick a Google Cloud project.** Go to


   <https://console.cloud.google.com/>. Click the project drop-down at


   the top, **New Project**, name it (e.g. *bot-sync*), **Create**.


2. **Enable the Google Drive API.**


   <https://console.cloud.google.com/apis/library/drive.googleapis.com>


   → make sure your project is selected → **Enable**.


3. **Configure the OAuth consent screen.**


   <https://console.cloud.google.com/apis/credentials/consent>


   - User type: **External** (then **Create**).


   - App name: anything (e.g. *bot-sync*). Support / dev contact: your


     email. **Save and continue**.


   - Scopes: leave default. **Save and continue**.


   - Test users: add the Google account that owns the Drive bot-sync


     will sync. (If you don't add it, sign-in will fail with *Access


     blocked: bot-sync has not completed Google's verification process*.)


   - **Save and continue** → **Back to dashboard**.


4. **Create the OAuth client.**


   <https://console.cloud.google.com/apis/credentials>


   → **Create Credentials → OAuth client ID**.


   - Application type: **Desktop app** (this is the type rclone and


     bot-sync's in-browser sign-in expect; it permits both the


     loopback-IP flow and Google's device flow).


   - Name: anything. **Create**.


5. **Copy the credentials.** Google shows a dialog with **Client ID**


   (`123…apps.googleusercontent.com`) and **Client secret**


   (`GOCSPX-…`). Keep this tab open — you'll paste both into bot-sync


   in the next step.


6. **Connect bot-sync to Google Drive.** In the bot-sync UI →


   **Accounts** → **➕ Connect account** → Provider **Google Drive** →


   pick a name (e.g. *gdrive*) → paste the client_id / client_secret →


   **🌐 Sign in with browser**. A new tab opens at Google; sign in,


   grant access, return to bot-sync. The badge flips to **OK** when


   the token is written.





### What if Google says *Access blocked: …has not completed Google's verification process*?





Your test user list (step 3) doesn't include the account you're trying


to sign in with. Add it under **OAuth consent screen → Test users →


Add users**. Up to 100 test users are allowed without verification,


which is plenty for personal use.





### Reconnecting (token expired / revoked)





bot-sync detects expired/revoked tokens and shows a red **🔐


Reauthenticate** button next to the affected remote. Click it → **🌐


Sign in with browser** → finish on Google. The existing


client_id/secret are re-used; you do **not** need to revisit Google


Cloud Console.





### Why an "in-browser" Google sign-in works on a router with no browser





bot-sync uses Google's [OAuth 2.0 Device Authorization


Grant](https://developers.google.com/identity/protocols/oauth2/limited-input-device).


The daemon asks Google for a short user code, you sign in on **any**


device with a browser (a phone is fine), and the daemon polls Google


until it receives the access + refresh tokens, then writes them


straight into `rclone.conf`. No rclone install on a laptop, no


copy-pasting a JSON token blob.





Other providers (Dropbox, Box, OneDrive) don't support device flow, so


the **Use rclone token** button still walks you through the classic


"run `rclone authorize` on a laptop" flow.





## API surface





All endpoints require HTTP Basic auth. Responses are JSON unless noted.





| Method | Path | Purpose |


|---|---|---|


| GET | `/api/state` | full state snapshot (drives, downloads, uploads, remotes, sharing, system) |


| GET | `/api/system` | live cpu/ram/uptime/temp |


| GET | `/api/drives` | adopted + detected drives |


| POST | `/api/drives/adopt` | adopt a detected drive (body: device, label, format) |


| POST | `/api/drives/{uuid}/mount` | mount an adopted drive |


| POST | `/api/drives/{uuid}/eject` | sync + unmount |


| POST | `/api/drives/{uuid}/primary` | flag as primary |


| POST | `/api/drives/{uuid}/forget` | drop from pool |


| GET | `/api/remotes` | list rclone remotes with health |


| POST | `/api/remotes/oauth/start` | begin OAuth (body: provider, name) |


| POST | `/api/remotes/oauth/finish` | finish OAuth (body: session_id, token) |


| POST | `/api/remotes/oauth/device/start` | begin Google Drive **in-browser** sign-in (body: name, optional client_id, client_secret) |


| POST | `/api/remotes/oauth/device/poll` | poll device-flow status (body: session_id) |


| DELETE | `/api/remotes/{name}` | remove remote |


| POST | `/api/remotes/{name}/check` | force health check |


| GET | `/api/downloads` | list |


| POST | `/api/downloads` | add (body: label, url, drive_uuid) |


| PATCH | `/api/downloads/{id}` | update (state, schedule, etc.) |


| POST | `/api/downloads/{id}/sync` | start a sync job |


| POST | `/api/downloads/{id}/resync` | wipe local + sync from scratch |


| DELETE | `/api/downloads/{id}?delete_files=1` | remove |


| GET / POST / PATCH / DELETE | `/api/uploads...` | mirror of downloads |


| GET | `/api/sharing` | smb/nfs/bonjour state |


| PATCH | `/api/sharing` | update toggles |


| GET | `/api/settings` | settings (master + uploads/downloads enable, providers) |


| PATCH | `/api/settings` | update settings (`enabled`, `uploads_enabled`, `downloads_enabled`, ...) |


| GET | `/api/jobs` | active + recent jobs with progress |


| POST | `/api/jobs/{id}/cancel` | cancel a running job |


| GET | `/api/logs?tail=N` | tail of botsync.log |





## Config & state





`<BOTSYNC_ROOT>/etc/botsync.json` — full mutable state, atomically written.


Schema is documented inline in [botsyncd.py](botsyncd.py) (`DEFAULT_STATE`).


The daemon deep-merges defaults on load, so adding new settings keys in a


future version does not require wiping state.





## Changelog





### v0.7.21 (current)





- **System tab — BOT-SYNC daemon panel fixed.** The self-update widget on


  *🛠️ System* was rendering into a stale element id (`#botsyncUpdatePanel`)


  while the page actually exposes `#botsyncPanel`, so the panel was


  silently empty. Render target corrected; the installed/latest version,


  last-checked timestamp, *Check for updates* and *Update now* buttons now


  populate as designed. Docs (README, `INSTRUCTIONS.md`, in-app Help)


  brought back in sync after a long stretch of release-only commits.





### v0.7.20





- **Init script picks the primary drive across all mounts.** OpenWrt's


  `/etc/init.d/botsync` now does a two-pass scan of every mount under


  `/mnt/sync` and `/tmp/mountd` for `.botsync_marker` files: pass 1


  prefers any drive whose marker has `"primary": true`, only falling


  back to the first marker of any kind. Prevents the daemon from


  picking up an old / secondary drive (and "losing" config) when USB


  device names shuffle on reboot (e.g. `sda`↔`sdb`).





### v0.7.19





- **Wait for internet at boot before first account/update checks.**


  Cold boots no longer fire a wave of *“account unreachable”* / update-


  check failures into Discord while the WAN or repeater is still


  associating. A new TCP-only probe (`api.github.com:443`,


  `1.1.1.1:443`, `8.8.8.8:53`) gates `_health_loop` and


  `_update_check_loop` for up to 600 s (5 s intervals). Falls back to


  plain `time.sleep` when invoked before `_stop_event` exists in


  `App.__init__`.





### v0.7.18





- **bot-sync self-update notifier and updater.** The daemon now polls


  `https://api.github.com/repos/mr-tbot/bot-sync/commits/main` once per


  24 h on the same loop as the rclone update check. The UI gains an


  alert banner plus a *🛠️ System → BOT-SYNC daemon* panel showing


  installed vs. latest, with **View on GitHub**, **Check now**, and


  **Update now** buttons. *Update now* runs `install/update.sh`


  detached (with an inline bootstrap fallback that fetches the GitHub


  tarball when the local helper is missing); the script also runs


  `rclone selfupdate` when rclone is on `PATH` and restarts the


  daemon. New API endpoints: `GET /api/system/botsync`,


  `POST /api/system/botsync/check`, `POST /api/system/botsync/update`.


  New events: `botsync.update_available` / `installed` / `failed`.


  Stale `updating` flags on `bot_sync_status` and `rclone_status` are


  cleared on every startup so a daemon killed mid-update doesn't lock


  the UI buttons.





### v0.7.17





- **Donation + issue-reporting links.** Footer gains a *💛 Donate*


  (PayPal) and *🐛 Report issue* (GitHub) link next to the existing


  MR-TBOT credit. The 📖 Help tab gets a dedicated support callout


  panel near the top with the same two links styled as primary /


  secondary buttons. README opens with a brief blockquote explaining


  what donations fund and linking to PayPal + the issues tracker.





### v0.7.16





- **Per-entry and per-project sync priority.** High / normal / low


  priority controls the order jobs come out of the worker queue when


  several are queued at once. Lower-priority jobs still run; they just


  yield the per-type and global concurrency slots to higher-priority


  work first. Per-project priority lives on the project record


  (default *normal*); per-entry priority lives on each download / upload


  (*auto* by default). *auto* resolves to the entry's primary project


  priority, then to any tagged project's priority (highest wins), then


  to *normal*. The entry-edit modal gains a *Priority* select with an


  *Inherit from project* option; the Projects tab edit form gains a


  *Priority* select and the project summary shows a 🔥/💤 pill.





### v0.7.15





- **Skip *completed* notifications for no-op repeat syncs.** Once a


  download or upload has completed at least one successful sync,


  per-run `job.started` and `job.completed` events are suppressed when


  the run transferred zero files and zero bytes. Failures and


  cancellations always notify regardless. The first-ever sync of a new


  entry still fires both events so users get the welcome ping. Stops


  Discord from being spammed every schedule tick by polling jobs


  against an unchanged remote folder.





### v0.7.14





- **Responsive UI — mobile-friendly tab bar and layouts.** The 13-button


  top tab strip used to overflow horizontally on phones, either getting


  clipped or wrapping into multiple rows that pushed the page content


  down. The new layout keeps the tabs on a single row but turns it into


  a touch-scrollable strip with scroll-snap so each tab feels like a


  flickable page; the brand and sign-out button stay anchored above on


  narrow screens, and clicking a tab scrolls it into view. Three new


  breakpoints in `ui/style.css`: 1100 px / 720 px / 420 px progressively


  stack the topbar, drop the dashboard grids to a single column, and


  let panels scroll horizontally for wide tables.





### v0.7.13





- **Project list refresh fix + dedicated Projects tab.** Bug fix:


  creating a project from inside the Downloads / Uploads add panel via


  the inline *+ New project* button updated `STATE` but did not


  repopulate the open form's `<select>`, so the new project only


  appeared after closing and reopening the panel. Now every


  project-related select on the page is repopulated and the new id is


  preselected in the originating dropdown. New **🗂️ Projects** tab


  with search, expand-all / collapse-all, and per-project collapsible


  cards (member table, slug, schedule and auto-delete pills, edit form


  with rename / auto-delete datetime / auto-sync schedule preset).


  Backend gains `auto_sync_schedule` on projects and a


  `POST /api/projects/<pid>/sync` endpoint that queues every active


  download / upload tagged with that project.





### v0.7.12





- **Cross-platform CPU temp + health threshold alerts.** The System and


  Dashboard tabs (and the footer status line) now show CPU temperature,


  CPU load %, memory %, and swap %. Reads come from a new cross-platform


  probe — Linux: `/sys/class/thermal` then `/sys/class/hwmon` (including


  `ath10k_hwmon` on GL-iNet routers as a thermal proxy); Windows: WMI


  `MSAcpi_ThermalZoneTemperature`; macOS skipped (powermetrics is


  root-only). New **Health Alert Thresholds** panel under


  *🔔 Notifications* with knobs for `cpu_load_pct`, `mem_used_pct`,


  `swap_used_pct`, `cpu_temp_c`, `sustain_secs`, `cooldown_secs` and an


  enable toggle. The autosync loop runs a per-metric sustain timer and


  emits a single combined `system.health_warning` event (severity *warn*)


  to every enabled webhook channel; after firing, that metric is silenced


  for `cooldown_secs` to avoid spam. New endpoints:


  `GET`/`PATCH /api/notifications/health`.





### v0.7.11





- **Disclaimer + skill prerequisites.** README and `INSTRUCTIONS.md` now


  open with a *Heads-up before you start* section: this is well-tested


  only on the GL-A1300 / GL firmware 4.7.x; Pi / Linux / macOS / Windows


  are explicitly secondary at this stage. Adds a *why this exists* note


  from the author and lists the skills you should already have (SSH,


  OpenWrt / UCI basics, USB mounting on Linux / OpenWrt, Python 3.9+,


  Google Cloud OAuth + Dropbox app token, basic networking, optional


  SMB) so users know this is an intermediate-level setup rather than


  plug-and-play.


- **Multi-project tagging.** Downloads and uploads now carry an ordered


  list of project ids (`project_ids`) in addition to the existing


  primary `project_id`. The primary still controls the on-disk path;


  each additional project is treated as a mirror tag. After every


  successful sync the daemon copies the synced folder into each


  additional project's folder on the same drive (`downloads/<slug>/`


  or `uploads/<provider>/<slug>/`), bounded to the drive's mountpoint.


  Implemented via a new `JobManager.on_complete` hook +


  `App._post_sync_mirror` using `shutil.copytree(dirs_exist_ok=True)`.


  Errors are logged but do not fail the originating job. UI: download


  add, upload add and entry-edit dialogs gained an *Additional projects*


  picker + chip area + *+ Tag* button; project cells in the tables now


  render every tag as a pill (primary highlighted).


- **Auto-purge.** Downloads, uploads and projects gained an


  `auto_delete_at` field (epoch seconds; nullable). The autosync loop


  removes expired downloads (full delete), uploads (rmtree of staging


  dir + record removal) and projects (cascades to every download /


  upload tagged with that project, primary or mirror). Mirror folders


  for purged entries are removed too, bounded to the source drive's


  mountpoint. UI gains a *Auto-delete on* `datetime-local` input on the


  add / edit forms; empty value = keep forever.


- **Bug fix.** The `GET /api/downloads` list endpoint had been orphaned


  (the dispatch line was lost), causing the daemon to 404 on download


  list requests. Restored.





### v0.7.10 (initial public release)





- **Public-release sanitization.** Default committed credentials in


  `install/botsync.uci` are now `admin` / `changeme` (and the installer


  prints a "edit these before exposing" reminder). Lab-specific router IPs


  and the development Discord webhook URL have been removed from the


  shipped config and examples; everything defaults to GL-iNet's stock


  `192.168.8.1`. Stale `install/firewall.botsync.sh.new` work-file removed.


- **Schedule column on Downloads & Uploads.** Each row now has a *Schedule*


  column next to *Last sync* showing the configured interval (e.g. *every 5


  min* / *Manual*) plus a live countdown to the next scheduled run


  (*in 2m 15s* / *due now* / *pending first run* / *paused*). The cell


  refreshes alongside the rest of the table on the existing 3-second poll,


  so the user can see at a glance which folders are about to fire and which


  are sitting idle on manual.


- **Cross-platform installer hardened.** `install/install.py --print-only`


  now works from any host (no sudo / Administrator needed) and prints the


  install plan for the chosen target — useful for previewing a Pi / Linux


  / macOS / Windows install before committing. Per-target tooling checks


  (systemd, launchctl, schtasks) are downgraded to warnings under


  `--print-only`. `INSTRUCTIONS.md` gains a *Non-OpenWrt platforms* section


  documenting install paths, service managers, and per-platform


  requirements for Pi / Linux / macOS / Windows.





### v0.7.9





- **Per-entry sync interval.** Every download and upload now has a *Sync


  interval* dropdown (Manual / 1 min / 5 / 15 / 30 / 1 h / 6 h / 24 h /


  Custom…). Picking *Custom…* reveals a numeric input where the user can


  enter any positive number of seconds. After the first successful sync,


  the autosync loop re-fires the entry whenever `now - last_sync >=


  interval`. Manual entries still run once and then sit idle until the


  user clicks **Sync** — matching the previous behaviour.


- The free-text "cron" field on the *Edit entry* dialog has been replaced


  with the same preset dropdown so all three entry forms (Add Download,


  Add Upload, Edit) speak the same language. The backend


  `_schedule_seconds` parser also accepts `30s`, `5m`, `2h`, `1d` suffixes


  for tooling that hits the API directly.





### v0.7.8





- **Default account per provider.** The first account you connect for each


  provider (Drive / Dropbox / Box / OneDrive / FTP / SFTP) is automatically


  marked as the default. When you paste a share link in *Add Download*, the


  account dropdown jumps to the default for the detected provider so


  single-account setups never need to touch the dropdown. *Add Upload* does


  the same when you change the provider selector.


- **★ default badge + "Make default" button.** The Accounts tab tags the


  default account for each provider with a star pill. When you have more


  than one account for the same provider, every non-default row gets a


  *Make default* button so you can flip the choice. Removing the default


  account auto-promotes the next surviving account of the same provider so


  there's always a sensible fallback.


- **New endpoint:** `POST /api/remotes/<name>/default` sets a remote as the


  default for its provider (clearing the flag on its siblings).


- **`/api/remotes` response** now includes a flat `defaults: {provider:


  name}` map so the UI doesn't have to scan the remotes dict.





### v0.7.7


- **Filesystem-aware Eject dialog.** Replaced the plain `confirm()` with a


  proper modal that shows the drive's filesystem and how to mount it on


  Windows / macOS / Linux after pulling it (FAT32 / exFAT / NTFS / ext*


  / Btrfs / XFS / HFS+ / APFS each have their own guidance card). Ejecting


  the **primary** drive now shows a red warning telling the user to power


  the router off first and walks them through the safe-removal sequence.


- **Eject button on the Files tab.** The file explorer toolbar now has an


  inline `⏏ Eject` button that opens the same dialog, so the user no


  longer has to bounce back to the Drives tab to safely remove a drive.


- **Primary-drive presence watchdog.** The daemon now tracks whether the


  primary drive is present between polls. When it disappears unexpectedly


  (hot-pull / hub reset) we emit a new `drive.primary_missing` event,


  cancel any in-flight rclone children targeting it, and surface a top


  alert banner. When the drive returns we emit `drive.primary_returned`


  and clear the banner. New `STATE.primary_drive` summary on `/api/state`


  exposes presence + missing-since timestamp for the UI.


- **Hardware-layout best-practice notice.** The Drives tab now leads with


  a tip card telling the user to keep BOT-SYNC on a small flash primary


  drive that stays plugged in 24/7 and to put any high-draw download/upload


  drives behind a powered USB hub.





### v0.7.6


- **Inline project creation.** The Downloads and Uploads "Add" panels


  now have a `+ New project` button right next to the Project select,


  and the entry edit dialog has the same select + button. Creating a


  project no longer requires a separate tab — the new project is


  created via `POST /api/projects` and immediately selected in the


  dropdown that opened the prompt.


- **Move entries between projects.** `PATCH /api/downloads/<id>` and


  `PATCH /api/uploads/<id>` now accept `project_id`. The daemon


  recomputes `local_subpath`, validates the slug against existing


  projects, and best-effort `os.rename`s the existing data folder so


  re-shuffling doesn't force a full re-sync. Cross-mount or conflicting


  targets are skipped with a log line; the next sync recreates the


  destination.


- **Removed the standalone Projects tab.** Listing/renaming/deleting


  projects via a dedicated tab was redundant once creation became


  inline. The `/api/projects` CRUD endpoints stay; only the UI tab and


  its renderer are gone.





### v0.7.5


- **Hardware performance presets.** New Settings → ⚡ panel lets you pick


  one of `router` / `pi` / `desktop` / `custom`. Each preset caps the


  *total* number of simultaneously-running rclone children (single global


  limiter on top of the per-type lanes) and dictates `--transfers`,


  `--checkers`, `--buffer-size`, `--multi-thread-streams`,


  `--max-backlog`, optional `--bwlimit`, the per-rclone `RLIMIT_AS`


  ceiling, and process niceness. The previous behaviour (two


  independently-capped lanes that could each spawn an rclone) is gone —


  on the router profile no more than ONE rclone runs at any time.


- **Auto-detect on first run.** The daemon reads `/proc/meminfo` (or


  `GlobalMemoryStatusEx` on Windows) and picks `router` for ≤ 768 MB,


  `pi` for ≤ 3 GB, otherwise `desktop`. The choice is persisted to


  `settings.performance_preset` so it survives reboots.


- **Live preset changes.** Switching a preset immediately resizes the


  JobManager caps and updates the rclone child memory ceiling. Already


  running rclones keep their previous flags until they finish (we will


  not SIGKILL active syncs to apply a preset change).


- **Custom overrides.** When the preset is set to `custom`, you can


  individually tune every flag from the Settings panel. Validation


  rejects non-integer / negative values with a fix-up message.


- **API.** `GET /api/performance` returns presets, active values, and


  detected hardware. `PATCH /api/performance` accepts `{preset, custom}`.





### v0.7.4


- **FTP and SFTP backends.** New providers in the Accounts tab let you


  connect to FTP / FTPS / SFTP servers using host + port + username +


  password (and optionally an SFTP private key) — no OAuth required.


  Passwords are obfuscated via `rclone obscure` before being written to


  `rclone.conf`; SFTP keys are persisted to `<conf-dir>/keys/<name>.pem`


  with mode 0600. Works on every architecture upstream rclone ships


  (armv7, aarch64, mipsle, x86_64) — no extra opkg packages required.


- **Per-provider help in Add / Edit forms.** Each Add Folder / Add


  Upload / Edit dialog now shows an inline help block specific to the


  picked provider, explaining how to obtain the URL, what to type in


  Remote path, and which mistakes (e.g. blank Dropbox remote path)


  would copy your entire account.


- **Field-level validation.** Server-side checks return


  `{ok:false, field, error, fix}` and the UI highlights the offending


  input with the corrective hint inline next to it instead of just a


  toast. Most common footguns now block the request before the daemon


  has to refuse it.





### v0.7.3


- **Projects.** New top-level grouping for downloads and uploads. Create


  a project on the new **📦 Projects** tab and the on-disk slug (spaces


  become `-`) becomes the parent folder for any *future* download or


  upload created with that project picked. Local layout becomes


  `downloads/<slug>/<label>` and `uploads/<provider>/<slug>/<label>`.


  Existing entries keep their flat paths — projects are opt-in per


  entry. Projects persist across daemon restarts in `botsync.json`.


  Remote paths are unchanged: a project is purely a local-organisation


  concept, so an entry's rclone destination stays exactly where you


  pointed it.





### v0.7.2


- **Edit download / upload entries.** Each row now has an **Edit**


  button next to Sync / Pause that opens a modal where you can change


  the label, account (rclone remote), remote path, schedule (cron),


  and (uploads only) push/mirror/bisync mode without removing and


  re-adding the entry. Local files are not touched; the change takes


  effect on the next sync.





### v0.7.1


- **Lockup hardening for low-RAM routers.** Several reinforcing


  changes that together stop the "router becomes unresponsive


  during sync" failure mode on 256MB boxes like the GL-iNet A1300:


  - The swap helper now activates the swapfile with `swapon -p 10`


    instead of the kernel's default priority `-2` (last resort), so


    swap is actually used under pressure rather than only after the


    page cache has already been thrashed.


  - VM sysctls are tuned at boot and on every `botsync-swap ensure`


    invocation: `vm.swappiness=80`, `vm.vfs_cache_pressure=200`,


    `vm.min_free_kbytes=8192`, `vm.overcommit_memory=2`,


    `vm.overcommit_ratio=80`, `vm.panic_on_oom=0`. With strict


    overcommit accounting the kernel returns `ENOMEM` to a runaway


    rclone instead of triggering an OOM cascade that kills dropbear


    and the API daemon.


  - The `botsync` procd init now sets `oom_adj=-500` so the OOM


    killer prefers any other process. `botsyncd` also writes its


    own `/proc/self/oom_score_adj=-500` at startup as a fallback for


    non-procd launches.


  - Every rclone child gets `oom_score_adj=+800`, `nice=+5`, and an


    `RLIMIT_AS` cap (default 128MB, override via


    `BOTSYNC_RCLONE_MEM_MB`) — so when memory really does run out


    rclone is killed first, the daemon stays up, the UI keeps


    responding, and the job is retried on the next schedule.


  - `--max-backlog 1000` added to rclone invocations to bound peak


    listing memory on giant remote folders.





### v0.7.0


- **In-browser Google Drive sign-in.** Reconnecting an expired Google


  Drive remote no longer requires installing rclone on a laptop and


  pasting a JSON token. Click **🔐 Reauthenticate** → **🌐 Sign in


  with browser**, finish the consent on Google in any browser (phone


  is fine), done. Implemented via Google's OAuth 2.0 Device


  Authorization Grant; client_id / client_secret are read out of


  `rclone.conf` for reauth and supplied via the UI for first-time


  Drive setup.


- **Reauthenticate button now actually works.** The previous


  Reauthenticate button switched to the Accounts tab but left the


  OAuth panel hidden, so users saw nothing happen. The flow now


  auto-opens the right panel for the provider (device flow for Drive,


  classic rclone-token paste for everything else).


- **README + INSTRUCTIONS:** new "Google Cloud OAuth setup" section


  walking through project creation, Drive API enablement, OAuth


  consent screen + test users, and Desktop OAuth client creation —


  end-to-end the steps required so the back-end token exchange works.


- New API endpoints: `POST /api/remotes/oauth/device/start` (begins a


  Google device-flow OAuth, returns `verification_url_complete` and


  `user_code`) and `POST /api/remotes/oauth/device/poll` (UI calls


  this on a timer until the user finishes consent).





### v0.6.9


- **rclone.conf integrity guard.** rclone rewrites `rclone.conf` whenever


  it refreshes an OAuth token, and a power-cut / OOM kill / FS hiccup


  mid-write can leave the file 0 bytes — silently destroying every


  configured account. The daemon now keeps an authoritative


  `rclone.conf.bak` next to the live conf, snapshots it whenever the


  live conf is healthy and differs (atomic `tmp` → `os.replace`,


  `fsync`'d), and restores from the backup before every rclone


  invocation when the live conf is missing / empty / has no


  `[section]` with `type =`.


- **Docs.** README + INSTRUCTIONS now document the Dropbox shared-link


  limitation: shared links must first be added to the user's own


  Dropbox via "Add to my Dropbox" before rclone can sync them, and the


  download's *Remote path* must be set to the resulting folder name.





### v0.6.8


- **Hardening pass.** Internal audit follow-up; no user-visible feature


  changes. Highlights:


  - Autosync scheduler is now lock-protected end-to-end with an


    `inflight_submits` set that closes a race where two ticks could


    double-submit the same target between submit and JobManager surface.


  - Job cancel now escalates SIGTERM → SIGKILL with bounded waits so


    wedged rclone children can't pile up as zombies.


  - rclone runner is wrapped in try/finally and always reaps the child;


    log lines are capped at 2 KB so a runaway message can't bloat


    process memory.


  - HTTP 500 responses sanitise error strings (likely tokens / paths


    redacted) and only include tracebacks when `BOTSYNC_DEBUG=1`


    or `BOTSYNC_MOCK=1`.


  - JSON request bodies are capped at 1 MiB; oversized bodies are


    drained safely. File uploads still stream via `/api/files/upload`.


  - Re-activating or rescheduling a download/upload now clears the


    sticky `last_sync` and resets autosync backoff.


  - File deletion re-probes the live drive mountpoint and verifies


    `os.path.ismount` + realpath containment before `rmtree`.


  - State load preserves a `<state>.corrupt-<ts>` backup before


    falling back to defaults.


  - Heartbeat writes carry a monotonic `seq` and escalate to a warning


    if writes fail.


  - Watchdog kick marker removal is now in `finally:` so a crash


    between exists() and remove() can't leave a phantom marker.


  - Firewall include validates IP / port format before invoking


    iptables.


  - Stored release notes capped at 4 KB.


  - UI `el()` helper documents that `html:` writes innerHTML and must


    never receive untrusted strings.





### v0.6.7


- **File explorer.** New `📁 Files` tab to browse, upload, download,


  rename, move (within drive) and delete files on any adopted, mounted,


  non-paused USB drive. Backed by JSON endpoints `GET /api/files`,


  `POST /api/files/mkdir`, `POST /api/files/rename`,


  `DELETE /api/files`, plus binary `GET /api/files/raw` (HTTP `Range`


  supported for video/resume) and `POST /api/files/upload` (streaming,


  `X-Drive` / `X-Path` / `X-Overwrite` headers, 10 GB cap). Every path


  is realpath-normalised and rejected if it escapes the drive


  mountpoint via `..` or symlinks. Auth-gated like every other API.


  The `.botsync_marker` file is protected (deleting it would un-adopt


  the drive).





### v0.6.6


- **Auto-start on add.** `POST /api/downloads` and `POST /api/uploads` now


  immediately submit the first sync. Response carries `job_id` (started)


  or `queued: true` + `queued_reason` (drive not mounted, syncs disabled,


  drive paused, …). The UI toast distinguishes "sync started" vs


  "sync will start automatically".


- **Boot-time + outage retry.** New `_autosync_loop` thread re-attempts


  any active download/upload that has never produced a successful sync


  (`done` in `sync_log`). Survives daemon crash, power loss, internet


  outage — on next boot the loop picks up where it left off. rclone


  `copy` is idempotent, so already-transferred files are skipped.


- Exponential backoff (30s → 1m → 2m → 5m → 15m → 30m → 1h cap) on hard


  failures; transient blockers (drive unmounted/paused, master switch


  off, downloads/uploads disabled) re-check every tick without inflating


  the schedule. The loop honours per-drive pause and the global +


  per-direction master switches.





### v0.6.5


- **Per-drive pause / resume.** Eject auto-pauses the drive, cancels its


  in-flight jobs, and blocks new syncs until the drive is mounted again


  (which auto-resumes). Manual ⏸/▶ buttons in the UI for the same.


- One-shot Dropbox "Add this shared folder…" warning so it stops


  re-appearing on every state read.





### v0.6.4


- **Optional swap helper** for low-RAM routers — adds a swapfile on the


  USB drive, surfaced in the System panel.


- **Dashboard** landing tab.





### v0.6.3


- Cap rclone memory to prevent device-wide OOM reboot on small routers.





### v0.6.2


- RAM hint by concurrency; live system stats in footer.





### v0.6.1


- Bounded `sync_log` ring buffer; non-Drive provider audit pass.





### v0.6.0


- Cross-platform installer (`install/install.py`) for router / Pi /


  Linux / macOS / Windows. Per-type concurrent transfer limits.


  Provider-specific OAuth hints. `RCLONE_NON_INTERACTIVE`. Headless


  OAuth token import.





### v0.5.0


- **Hardening &amp; reliability pass.** BOT-SYNC now self-recovers from


  process crashes, SIGKILL/OOM, network hangs, runaway jobs, and unclean


  reboots.


- **procd respawn** tightened to `respawn 60 5 0` (5s retry, 60s window,


  unlimited tries) plus `term_timeout 15`. Process exits — even due to


  unhandled exceptions — are recovered automatically.


- **External cron watchdog** (`/usr/sbin/botsync-watchdog`) installed by


  `setup.sh`. Pings `POST /api/watchdog/ping` once a minute via curl; on 3


  consecutive failures (HTTP timeout / non-2xx) the daemon is restarted via


  `/etc/init.d/botsync restart`. Catches the rare cases where the daemon is


  alive but no longer serving HTTP. Honours `uci botsync.main.enabled=0` so


  it doesn't fight a deliberate stop.


- **Internal heartbeat** thread writes `<usb>/var/run/botsyncd.heartbeat`


  every 30 s with `{ts, pid, version}`. Surfaced in the new


  *Reliability &amp; watchdog* panel under ⚙️ Settings.


- **Stuck-job watchdog** (every 5 min) auto-cancels any job that has been


  in `running` state longer than `settings.stuck_job_hours` (default `6`,


  set to `0` to disable). Emits `job.stuck` so notifications fire.


- **Crash recovery on startup**:


  - `<usb>/var/run/botsyncd.running` is written on start, removed on clean


    shutdown. If found at the next start, the previous run crashed:


    BOT-SYNC emits `system.crash_recovered` (or `system.watchdog_restart`


    if `/tmp/botsync-watchdog.kicked` is present, indicating the cron


    watchdog forced the restart).


  - Any persisted job left in `running` state is flipped to `error` with


    "interrupted by daemon restart" and a `job.interrupted` event.


- **Last-ditch crash logger.** A top-level `sys.excepthook` writes


  `<usb>/var/log/crash/crash-<ts>.log` and best-effort fires


  `system.error` before procd respawns the daemon.


- **Signal handlers** for `SIGTERM` / `SIGINT` perform a clean shutdown:


  emit `system.shutdown`, remove the running marker, stop the HTTP server.


- **New event types** (all selectable per channel):


  `job.interrupted`, `job.stuck`, `system.shutdown`,


  `system.crash_recovered`, `system.watchdog_restart`,


  `system.health_warning`.


- **New unauthenticated endpoint** `POST /api/watchdog/ping`. Localhost-only


  by design (cron). Body is empty, response is `{ok, ts, version, client}`.


  The daemon records the ping timestamp; the UI surfaces it as


  *Cron watchdog: active / not seen yet*.


- **Settings UI**: new *Reliability &amp; watchdog* panel showing daemon


  PID, uptime, last heartbeat, watchdog activity, and a *Stuck-job


  timeout (hours)* input.





### v0.4.0


- **SMTP email notification channel** hardened. New explicit **TLS mode**


  selector with three options:


  - `starttls` (default, port 587) — plain TCP then upgraded.


  - `ssl` (port 465) — implicit TLS from the first byte. Required by Gmail


    when using app passwords, iCloud, Outlook/Office 365, and most managed


    SMTP relays.


  - `none` (port 25) — plaintext, only for trusted local relays.


  Auto-detects `ssl` when port is 465. Backward compatible with the


  legacy `use_tls` boolean. New optional `subject_prefix` config field


  (default `BOT-SYNC`); subjects become


  `[<prefix> <severity>] <event.type>`.


- The existing `POST /api/notifications/channels/{id}/test` endpoint /


  *Send test* button exercises the same code path real notifications take,


  so a successful test guarantees real events will land.





### v0.3.0


- **Independent Uploads / Downloads master switches** in addition to the


  global BOT-SYNC enable. Set them from ⚙️ Settings or via


  `PATCH /api/settings`.


- New in-app **📖 Help** tab carrying a comprehensive user guide — also


  reachable from the welcome modal via *📖 Read the docs*.


- Footer now displays the running daemon version.


- Daemon now deep-merges defaults so new settings are picked up by existing


  installations without erasing `botsync.json`.





### v0.2.0


- Friendly hostname `http://bot.sync/` via alias IP + dnsmasq + iptables


  PREROUTING REDIRECT (replaces the earlier broken string-match approach,


  which couldn't establish a TCP connection because the SYN carries no


  HTTP payload).


- Model-aware installer: `--model {gl-a1300|generic|auto}`, `--hostname`,


  `--no-hostname`, `--port`, `--uninstall`. Auto-detects GL-iNet routers


  via `/etc/glversion` / `/etc/config/glconfig`.


- Minimum OpenWrt 19.07. Documented fw4/nftables shim path for 22.03+.


- `INSTRUCTIONS.md` end-user install guide.





### v0.1.0


- Initial rebrand from cloudsync. USB-drive-backed daemon (Python 3.7+,


  stdlib only), OAuth-based cloud accounts (Google Drive, Dropbox, Box,


  OneDrive, plain HTTP), download + upload jobs powered by `rclone`,


  SMB / Bonjour / NFS sharing, notifications (Discord, Slack, generic


  webhook, ntfy, email), setup wizard, welcome modal, mock mode for


  desktop development.





## License & acknowledgements





BOT-SYNC is released under the [MIT License](LICENSE).





**Massive thanks to the [rclone](https://rclone.org) project** (MIT, Nick Craig-Wood and contributors)  every cloud / FTP / SFTP / HTTP transfer in BOT-SYNC is a thin wrapper around rclone. BOT-SYNC neither modifies nor redistributes rclone source; the installer downloads an unmodified upstream binary from downloads.rclone.org onto your USB drive at install time.





Full third-party attribution (rclone, Python, OpenWrt platform tools, cloud-vendor API terms) is in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). If you fork BOT-SYNC, please keep that file intact.


