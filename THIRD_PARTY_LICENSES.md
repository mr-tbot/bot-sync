# Third-party software & attributions

BOT-SYNC is MIT-licensed (see [LICENSE](LICENSE)). Its own runtime code is
pure Python 3 standard library plus vanilla HTML/CSS/JS — no bundled
third-party Python packages or JS libraries.

The daemon, however, **depends on and orchestrates** the following third-party
software at install / run time. Each is downloaded, packaged, or invoked by
BOT-SYNC and retains its original license. Nothing in this list is a fork or
re-distribution of upstream source — BOT-SYNC ships glue code only.

## Required runtime dependencies

### rclone

- **Project:** <https://rclone.org> · <https://github.com/rclone/rclone>
- **License:** MIT License — Copyright © 2012–present Nick Craig-Wood
  ([rclone LICENSE](https://github.com/rclone/rclone/blob/master/COPYING)).
- **Role in BOT-SYNC:** rclone is the actual cloud-transfer engine. Every
  Download, Upload, account OAuth flow, FTP/FTPS/SFTP/HTTP backend, and
  bisync operation in BOT-SYNC is implemented as an `rclone` subprocess
  call. The installer downloads an unmodified upstream rclone binary
  (`rclone-current-linux-arm.zip` on OpenWrt, `rclone-current-*-amd64.zip`
  elsewhere) directly from `downloads.rclone.org` and stores it on the USB
  drive. BOT-SYNC neither modifies nor redistributes rclone source; the
  binary is fetched at install time on the user's own host.
- **Acknowledgement:** Massive thanks to Nick Craig-Wood and the rclone
  contributors. Without rclone, BOT-SYNC would not exist.

### Python 3 standard library

- **Project:** <https://www.python.org>
- **License:** [Python Software Foundation License (PSF-2.0)](https://docs.python.org/3/license.html).
- **Role:** the daemon (`botsyncd.py`) and installer (`install/install.py`)
  use only stdlib modules (`http.server`, `urllib`, `subprocess`,
  `threading`, `json`, `hashlib`, etc.). Python itself is provided by the
  host OS.

### OpenWrt / GL-iNet platform tools

- **Project:** <https://openwrt.org>
- **License:** GPL-2.0 (kernel + most utilities), with individual packages
  under their own licenses (BusyBox GPL-2.0, dnsmasq GPL-2.0, Samba
  GPL-3.0, mDNSResponder Apache-2.0, etc.).
- **Role:** BOT-SYNC drives existing OpenWrt subsystems via the standard
  user-space tools — `uci`, `block`, `mount`, `dnsmasq`, `iptables`,
  `samba`/`smbd`, `mdnsd` (Bonjour/Avahi-compatible) and `nfsd`. None of
  these are bundled; BOT-SYNC simply invokes whatever the platform
  provides.

## Cloud-provider APIs (terms apply, no code redistributed)

The Drive / Dropbox / Box / OneDrive backends call vendor APIs through
rclone. Use of those APIs is governed by each vendor's own Terms of Service
and Developer Agreement:

- Google Drive — <https://developers.google.com/terms>
- Dropbox — <https://www.dropbox.com/developers/reference/tos>
- Box — <https://developer.box.com/platform/agreements/>
- Microsoft OneDrive / Graph — <https://learn.microsoft.com/legal/marketplace/>

Trademarks "Google Drive", "Dropbox", "Box", "OneDrive" and any associated
logos are the property of their respective owners; their mention here is
purely for interoperability documentation.

## Contributing back

If you ship a fork of BOT-SYNC, please keep this file (and the rclone
attribution in particular) intact, and consider upstream-ing improvements
that are not BOT-SYNC-specific to the relevant projects above.
