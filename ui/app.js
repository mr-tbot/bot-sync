// botsync UI — vanilla JS SPA. No build step.
(() => {
'use strict';

// ----- helpers -----
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    // SECURITY: 'html' writes innerHTML and skips escaping. Only pass strings
    // we authored ourselves; never pass values that came from the daemon, the
    // UCI config, rclone, or any user input.
    else if (k === 'html') n.innerHTML = v;
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k == null) continue;
    n.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return n;
};
const human = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; n = +n;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
};
const since = (ts) => {
  if (!ts) return 'never';
  const d = (Date.now() / 1000) - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + 'm ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h ago';
  return Math.floor(d / 86400) + 'd ago';
};
const toast = (msg, isErr = false) => {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 4000);
};

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    ...opts,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
    body: opts.body ? (typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body)) : undefined,
  });
  if (res.status === 401) {
    location.href = '/login';
    return { ok: false, error: 'unauthenticated' };
  }
  let data;
  try { data = await res.json(); } catch { data = { ok: res.ok }; }
  if (!res.ok || data.ok === false) {
    toast(data.error || `HTTP ${res.status}`, true);
  }
  return data;
};

// ----- state -----
let STATE = null;
let FIRST_RUN_HANDLED = false;

// ----- field-level validation helpers -----
// Clear any previous inline error markers within `scope` (an element).
// Errors are rendered as <div class="field-error" data-for="<key>"> stubs in
// the HTML so we just look them up by data-for and toggle visibility.
const _clearFieldErrors = (scope) => {
  if (!scope) return;
  scope.querySelectorAll('.field-error').forEach(d => {
    d.classList.add('hidden');
    d.textContent = '';
  });
  scope.querySelectorAll('input.field-bad, select.field-bad, textarea.field-bad')
    .forEach(i => i.classList.remove('field-bad'));
};
// Show a field-specific error from a daemon response inside `scope`.
// Daemon convention: { ok:false, error, field?, fix? }. We highlight the
// matching input and stuff "<error> — <fix>" into the inline error div.
const _applyFieldError = (scope, resp) => {
  if (!scope || !resp || resp.ok || !resp.field) return false;
  const target = scope.querySelector(`.field-error[data-for="${resp.field}"]`);
  if (!target) return false;
  const txt = (resp.error || 'invalid value') + (resp.fix ? ' — ' + resp.fix : '');
  target.textContent = txt;
  target.classList.remove('hidden');
  // Highlight the matching input/select/textarea by id heuristic: same form
  // typically uses the field key as part of the id (dlRemotePath, upRemote,
  // rmBcHost, etc.). Match by data-field if present, otherwise by closest
  // <label> in the same scope.
  const inputs = scope.querySelectorAll(`[data-field="${resp.field}"]`);
  inputs.forEach(i => i.classList.add('field-bad'));
  return true;
};
// Render the inline help block for a given provider/kind into `target`.
// `kind` is "download", "upload", or "account". When kind="account" we
// concatenate the download+upload help so the user sees what the credential
// will be used for; when no help exists we hide the block.
const _renderProviderHelp = (target, provider, kind) => {
  if (!target) return;
  const help = ((STATE && STATE.provider_help) || {})[provider] || null;
  let text = '';
  if (help) {
    if (kind === 'download') text = help.download || '';
    else if (kind === 'upload') text = help.upload || '';
    else if (kind === 'account') {
      const parts = [];
      if (help.download) parts.push('Downloads: ' + help.download);
      if (help.upload) parts.push('Uploads: ' + help.upload);
      text = parts.join('\n\n');
    }
  }
  if (!text) {
    target.style.display = 'none';
    target.textContent = '';
    return;
  }
  target.style.display = '';
  // Use textContent + white-space:pre-line via inline style to keep
  // paragraph breaks readable without raw HTML injection.
  target.style.whiteSpace = 'pre-line';
  target.textContent = text;
};

const refreshState = async () => {
  STATE = await api('/api/state');
  if (STATE.mock) $('#mockBadge').classList.remove('hidden');
  renderAll();
  maybeShowFirstRun();
};

const maybeShowFirstRun = () => {
  if (FIRST_RUN_HANDLED) return;
  const s = STATE && STATE.settings;
  if (!s || s.setup_complete) { FIRST_RUN_HANDLED = true; return; }
  if (sessionStorage.getItem('bs_welcome_dismissed') === '1') { FIRST_RUN_HANDLED = true; return; }
  FIRST_RUN_HANDLED = true;
  const dlg = document.getElementById('welcomeDialog');
  if (!dlg) return;
  try { dlg.showModal(); } catch (_) { /* dialog unsupported */ }
  document.getElementById('welcomeStart').onclick = () => {
    sessionStorage.setItem('bs_welcome_dismissed', '1');
    dlg.close();
    const t = $$('.tab').find(x => x.dataset.tab === 'setup');
    if (t) t.click();
  };
  document.getElementById('welcomeDismiss').onclick = () => {
    sessionStorage.setItem('bs_welcome_dismissed', '1');
    dlg.close();
  };
  const docsBtn = document.getElementById('welcomeDocs');
  if (docsBtn) docsBtn.onclick = () => {
    sessionStorage.setItem('bs_welcome_dismissed', '1');
    dlg.close();
    const t = $$('.tab').find(x => x.dataset.tab === 'help');
    if (t) t.click();
  };
};

// ----- tab switching -----
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.toggle('active', x === t));
  $$('.tabpane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + t.dataset.tab));
}));

// ----- DRIVES -----
const renderDrives = () => {
  const root = $('#drivesList'); root.innerHTML = '';
  const drives = STATE.drives_live || [];
  if (!drives.length) {
    root.append(el('p', { class: 'hint' }, 'No USB drives detected. Plug a drive into the router USB port.'));
    return;
  }
  drives.forEach(d => {
    const used = (d.size_bytes || 0) - (d.free_bytes || 0);
    const pct = d.size_bytes ? (used / d.size_bytes * 100).toFixed(0) : 0;
    const card = el('div', { class: 'card' + (d.adopted ? '' : ' unadopted') });
    card.append(
      el('div', { class: 'title' },
        d.label || d.uuid,
        d.primary ? el('span', { class: 'pill ok' }, 'primary') : null,
        d.adopted ? null : el('span', { class: 'pill warn' }, 'new'),
        d.present ? null : el('span', { class: 'pill err' }, 'offline'),
      ),
      el('div', { class: 'meta' }, `${d.fs || '?'} · ${d.device || '?'} · UUID ${d.uuid}`),
      el('div', { class: 'meta' }, `${human(used)} used of ${human(d.size_bytes)} (${pct}%)`),
      el('div', { class: 'bar' }, el('span', { style: `width:${pct}%` })),
      el('div', { class: 'meta' }, d.mountpoint ? `Mounted at ${d.mountpoint}` : 'Not mounted'),
    );
    if (d.adopted && d.paused) {
      card.querySelector('.title').append(el('span', { class: 'pill warn' }, 'paused'));
    }
    const acts = el('div', { class: 'actions' });
    if (!d.adopted) {
      acts.append(el('button', {
        class: 'btn-primary',
        on: { click: () => adoptDrive(d) },
      }, 'Adopt'));
    } else {
      if (!d.primary) acts.append(el('button', {
        class: 'btn-secondary',
        on: { click: async () => { await api(`/api/drives/${d.uuid}/primary`, { method: 'POST' }); refreshState(); } },
      }, 'Set primary'));
      if (d.present) acts.append(el('button', {
        class: 'btn-secondary',
        title: d.paused ? 'Resume syncs against this drive' : 'Pause syncs without unmounting (e.g. before pulling the drive)',
        on: { click: async () => {
          const action = d.paused ? 'resume' : 'pause';
          const r = await api(`/api/drives/${d.uuid}/${action}`, { method: 'POST' });
          if (r.ok) toast(d.paused ? 'Drive resumed' : `Drive paused${r.cancelled ? ` (${r.cancelled} sync${r.cancelled === 1 ? '' : 's'} cancelled)` : ''}`);
          refreshState();
        } },
      }, d.paused ? '▶ Resume' : '⏸ Pause'));
      if (d.present && d.mountpoint) acts.append(el('button', {
        class: 'btn-secondary',
        on: { click: () => openEjectDialog(d) },
      }, 'Eject'));
      if (d.present && !d.mountpoint) acts.append(el('button', {
        class: 'btn-secondary',
        on: { click: async () => { const r = await api(`/api/drives/${d.uuid}/mount`, { method: 'POST' }); if (r.ok) toast('Drive remounted'); refreshState(); } },
      }, 'Mount'));
      acts.append(el('button', {
        class: 'btn-danger',
        on: { click: async () => { if (confirm('Forget this drive? Files on the drive are not touched.')) { await api(`/api/drives/${d.uuid}/forget`, { method: 'POST' }); refreshState(); } } },
      }, 'Forget'));
    }
    card.append(acts);
    root.append(card);
  });
};
$('#refreshDrives').addEventListener('click', refreshState);
const adoptDrive = async (d) => {
  const label = prompt('Label for this drive?', d.label || 'botsync');
  if (!label) return;
  const r = await api('/api/drives/adopt', {
    method: 'POST',
    body: { uuid: d.uuid, label, primary: !STATE.drives_live.some(x => x.adopted) },
  });
  if (r.ok) { toast('Drive adopted'); refreshState(); }
};

// ----- EJECT DIALOG -----
// Filesystem-aware reconnection guidance the user follows after physically
// pulling the drive. Keys are normalised to lowercase and matched against
// the drive's reported `fs` (from blkid). Each entry returns markup explaining
// how to mount the drive on Windows / macOS / Linux for that filesystem.
const _fsCompat = (fs) => {
  const f = (fs || '').toLowerCase();
  if (f === 'vfat' || f === 'fat' || f === 'fat32') return {
    name: 'FAT32', win: 'native', mac: 'native', lin: 'native',
    note: 'FAT32 has a 4 GB per-file limit. Files larger than 4 GB cannot exist on this drive.',
  };
  if (f === 'exfat') return {
    name: 'exFAT', win: 'native', mac: 'native', lin: 'install exfat-utils / exfatprogs',
    note: 'exFAT is the most portable format for files > 4 GB.',
  };
  if (f === 'ntfs') return {
    name: 'NTFS', win: 'native', mac: 'read-only (or install macFUSE + ntfs-3g for write)', lin: 'install ntfs-3g',
    note: 'macOS only mounts NTFS read-only out of the box. Install macFUSE + ntfs-3g if you need to write.',
  };
  if (f.startsWith('ext')) return {
    name: f.toUpperCase(), win: 'install Linux File Systems for Windows by Paragon, or use WSL2 (`wsl --mount`)',
    mac: 'install macFUSE + ext4fuse (read-only) or extFS for Mac by Paragon',
    lin: 'native',
    note: 'ext4 is the recommended filesystem for the primary drive on the router but is not natively readable on Windows / macOS without third-party tools.',
  };
  if (f === 'btrfs') return {
    name: 'Btrfs', win: 'install WinBtrfs',
    mac: 'install macFUSE + btrfs-fuse', lin: 'native',
    note: 'Btrfs is fully supported on Linux only.',
  };
  if (f === 'xfs') return {
    name: 'XFS', win: 'use WSL2 (`wsl --mount`)',
    mac: 'install macFUSE-based xfs driver', lin: 'native',
    note: 'XFS is read/write on Linux only.',
  };
  if (f === 'hfsplus' || f === 'hfs+') return {
    name: 'HFS+', win: 'install HFSExplorer (read-only) or Paragon HFS+ for Windows',
    mac: 'native', lin: 'install hfsplus-tools (mount with -t hfsplus)',
    note: 'HFS+ is the legacy macOS format.',
  };
  if (f === 'apfs') return {
    name: 'APFS', win: 'install APFS for Windows by Paragon (read-only without licence)',
    mac: 'native', lin: 'install apfs-fuse (read-only)',
    note: 'APFS is macOS-only and effectively read-only on Windows / Linux.',
  };
  return {
    name: fs || 'unknown', win: 'unknown', mac: 'unknown', lin: 'unknown',
    note: 'Unrecognised filesystem — mount instructions cannot be generated automatically.',
  };
};
const _fsHelpMarkup = (fs) => {
  const c = _fsCompat(fs);
  const row = (os, val) => `<div style="display:flex;gap:8px"><strong style="min-width:80px">${os}</strong><span>${val}</span></div>`;
  return `<div style="display:grid;gap:4px">
    <div><strong>Filesystem:</strong> ${c.name}</div>
    ${row('Windows', c.win)}
    ${row('macOS', c.mac)}
    ${row('Linux', c.lin)}
    <div class="hint" style="margin-top:6px">${c.note}</div>
  </div>`;
};
let _ejectTarget = null;
const openEjectDialog = (drive) => {
  _ejectTarget = drive;
  const dlg = $('#ejectDialog');
  $('#ejectTitle').textContent = `Eject ${drive.label || drive.uuid}?`;
  $('#ejectSummary').textContent =
    `${drive.fs || '?'} · ${drive.device || '?'} · UUID ${drive.uuid}` +
    (drive.mountpoint ? ` · mounted at ${drive.mountpoint}` : '');
  $('#ejectPrimaryWarning').classList.toggle('hidden', !drive.primary);
  $('#ejectFsHelp').innerHTML = _fsHelpMarkup(drive.fs);
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
};
const _ejectClose = () => {
  const dlg = $('#ejectDialog');
  if (typeof dlg.close === 'function') dlg.close();
  else dlg.removeAttribute('open');
  _ejectTarget = null;
};
$('#ejectCancel') && $('#ejectCancel').addEventListener('click', _ejectClose);
$('#ejectConfirm') && $('#ejectConfirm').addEventListener('click', async () => {
  const d = _ejectTarget;
  if (!d) return _ejectClose();
  _ejectClose();
  const r = await api(`/api/drives/${d.uuid}/eject`, { method: 'POST' });
  if (r && r.ok) toast(d.primary
    ? 'Primary drive ejected — power router off before unplugging'
    : 'Drive ejected — safe to unplug');
  else toast((r && (r.error || r.stderr)) || 'Eject failed', true);
  refreshState();
});

// ----- FILES (file explorer) -----
const FX = { drive: null, path: '', listing: null, loading: false, wired: false };

const fxFmtTime = (t) => {
  if (!t) return '';
  const d = new Date(t * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

const fxJoin = (base, name) => (base ? base + '/' + name : name);
const fxParent = (p) => {
  if (!p) return '';
  const i = p.lastIndexOf('/');
  return i < 0 ? '' : p.slice(0, i);
};

const fxPickDefaultDrive = () => {
  const drives = (STATE.drives_live || []).filter(d => d.adopted && d.present && d.mountpoint && !d.paused);
  if (!drives.length) return null;
  if (FX.drive && drives.some(d => d.uuid === FX.drive)) return FX.drive;
  const primary = drives.find(d => d.primary);
  return (primary || drives[0]).uuid;
};

const fxLoad = async () => {
  if (!FX.drive) { FX.listing = null; renderFiles(); return; }
  FX.loading = true;
  const qs = new URLSearchParams({ drive: FX.drive, path: FX.path });
  const r = await api('/api/files?' + qs.toString());
  FX.loading = false;
  if (!r || r.ok === false) {
    toast(r && r.error ? r.error : 'failed to list', 'err');
    FX.listing = { ok: false, entries: [], path: FX.path, error: r && r.error };
  } else {
    FX.listing = r;
    FX.path = r.path || '';
  }
  renderFiles();
};

const fxNavigate = (newPath) => { FX.path = newPath || ''; fxLoad(); };

const fxDeleteEntry = async (entry) => {
  const full = fxJoin(FX.path, entry.name);
  const msg = entry.is_dir
    ? `Delete folder "${entry.name}" and EVERYTHING inside it?\n\nThis cannot be undone.`
    : `Delete "${entry.name}"?`;
  if (!confirm(msg)) return;
  const r = await api('/api/files', {
    method: 'DELETE',
    body: { drive: FX.drive, path: full, recursive: !!entry.is_dir },
  });
  if (r && r.ok) { toast('Deleted'); fxLoad(); }
  else toast(r && r.error ? r.error : 'delete failed', 'err');
};

const fxRenameEntry = async (entry) => {
  const next = prompt(entry.is_dir ? 'New folder name (or "subdir/name" to move):' : 'New name (or "subdir/name" to move):', entry.name);
  if (!next || next === entry.name) return;
  if (next.includes('..')) { toast('invalid name', 'err'); return; }
  const newPath = next.startsWith('/') ? next.slice(1) : fxJoin(FX.path, next);
  const r = await api('/api/files/rename', {
    method: 'POST',
    body: { drive: FX.drive, path: fxJoin(FX.path, entry.name), new_path: newPath },
  });
  if (r && r.ok) { toast('Renamed'); fxLoad(); }
  else toast(r && r.error ? r.error : 'rename failed', 'err');
};

const fxMkdir = async () => {
  const name = prompt('New folder name:');
  if (!name) return;
  if (name.includes('/') || name.includes('..')) { toast('plain name only', 'err'); return; }
  const r = await api('/api/files/mkdir', {
    method: 'POST',
    body: { drive: FX.drive, path: fxJoin(FX.path, name) },
  });
  if (r && r.ok) { toast('Folder created'); fxLoad(); }
  else toast(r && r.error ? r.error : 'mkdir failed', 'err');
};

const fxDownloadUrl = (relPath) => {
  const qs = new URLSearchParams({ drive: FX.drive, path: relPath });
  return '/api/files/raw?' + qs.toString();
};

const fxUploadOne = (file, overwrite) => new Promise((resolve) => {
  const xhr = new XMLHttpRequest();
  const target = fxJoin(FX.path, file.name);
  xhr.open('POST', '/api/files/upload', true);
  xhr.setRequestHeader('X-Drive', FX.drive);
  xhr.setRequestHeader('X-Path', target);
  xhr.setRequestHeader('X-Overwrite', overwrite ? '1' : '0');
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    const hint = $('#fxUploadHint');
    if (hint) hint.textContent = `Uploading ${file.name}: ${pct}%`;
  };
  xhr.onload = () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    resolve({ ok: xhr.status >= 200 && xhr.status < 300 && body.ok !== false, status: xhr.status, body });
  };
  xhr.onerror = () => resolve({ ok: false, status: 0, body: { error: 'network error' } });
  xhr.send(file);
});

const fxUploadFiles = async (fileList) => {
  if (!FX.drive) { toast('pick a drive first', 'err'); return; }
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const hint = $('#fxUploadHint');
  let overwriteAll = false;
  for (const f of files) {
    if (hint) hint.textContent = `Uploading ${f.name}…`;
    let r = await fxUploadOne(f, overwriteAll);
    if (!r.ok && r.status === 409) {
      if (overwriteAll || confirm(`"${f.name}" already exists. Overwrite?`)) {
        overwriteAll = files.length > 1 && confirm('Overwrite for ALL remaining files too?') ? true : overwriteAll;
        r = await fxUploadOne(f, true);
      } else {
        continue;
      }
    }
    if (!r.ok) {
      toast(`upload failed: ${f.name} — ${(r.body && r.body.error) || r.status}`, 'err');
    }
  }
  if (hint) hint.textContent = '';
  toast('Upload complete');
  fxLoad();
};

const fxBindOnce = () => {
  if (FX.wired) return;
  FX.wired = true;
  const drv = $('#fxDrive');
  if (drv) drv.addEventListener('change', () => { FX.drive = drv.value || null; FX.path = ''; fxLoad(); });
  const ref = $('#fxRefresh'); if (ref) ref.addEventListener('click', fxLoad);
  const ej  = $('#fxEject');
  if (ej) ej.addEventListener('click', () => {
    // Look up the *full* drive record (with primary flag, fs, etc.) from
    // STATE so the eject dialog can show the right warning. The select
    // only carries uuid + label.
    const drives = STATE.drives_live || [];
    const d = drives.find(x => x.uuid === FX.drive);
    if (!d) return toast('No drive selected', true);
    openEjectDialog(d);
  });
  const up  = $('#fxUp');      if (up)  up.addEventListener('click', () => fxNavigate(fxParent(FX.path)));
  const mk  = $('#fxMkdir');   if (mk)  mk.addEventListener('click', fxMkdir);
  const inp = $('#fxUploadInput');
  if (inp) inp.addEventListener('change', () => { fxUploadFiles(inp.files); inp.value = ''; });
};

const renderFiles = () => {
  fxBindOnce();
  const drv = $('#fxDrive');
  if (!drv) return;
  const drives = (STATE.drives_live || []).filter(d => d.adopted && d.present && d.mountpoint && !d.paused);
  // Re-populate select if list changed.
  const sig = drives.map(d => d.uuid + ':' + (d.label || '')).join('|');
  if (drv.dataset.sig !== sig) {
    drv.innerHTML = '';
    drives.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.uuid;
      opt.textContent = (d.label || d.uuid) + (d.primary ? ' (primary)' : '');
      drv.append(opt);
    });
    drv.dataset.sig = sig;
  }
  if (!drives.length) {
    drv.innerHTML = '<option>(no usable drive)</option>';
    $('#fxBreadcrumbs').textContent = '';
    $('#fxTable tbody').innerHTML = '<tr><td colspan="5" class="hint">Adopt and mount a USB drive to use the file explorer.</td></tr>';
    $('#fxFreeHint').textContent = '';
    return;
  }
  if (!FX.drive || !drives.some(d => d.uuid === FX.drive)) {
    FX.drive = fxPickDefaultDrive();
    drv.value = FX.drive;
    if (FX.listing === null && !FX.loading) { fxLoad(); return; }
  } else if (drv.value !== FX.drive) {
    drv.value = FX.drive;
  }

  // Breadcrumbs.
  const crumbs = $('#fxBreadcrumbs');
  crumbs.innerHTML = '';
  const root = el('a', { href: '#', on: { click: (e) => { e.preventDefault(); fxNavigate(''); } } }, '/');
  crumbs.append(root);
  if (FX.path) {
    const parts = FX.path.split('/');
    let acc = '';
    parts.forEach((p, i) => {
      acc = acc ? acc + '/' + p : p;
      const cur = acc;
      crumbs.append(document.createTextNode(' / '));
      crumbs.append(el('a', { href: '#', on: { click: (e) => { e.preventDefault(); fxNavigate(cur); } } }, p));
    });
  }

  const tbody = $('#fxTable tbody');
  tbody.innerHTML = '';
  if (FX.loading && !FX.listing) {
    tbody.append(el('tr', {}, el('td', { colspan: 5, class: 'hint' }, 'Loading…')));
    return;
  }
  if (!FX.listing || FX.listing.ok === false) {
    tbody.append(el('tr', {}, el('td', { colspan: 5, class: 'hint' }, (FX.listing && FX.listing.error) || 'Unable to read folder.')));
    return;
  }
  const entries = FX.listing.entries || [];
  if (!entries.length) {
    tbody.append(el('tr', {}, el('td', { colspan: 5, class: 'hint' }, 'Empty folder.')));
  }
  entries.forEach(entry => {
    const icon = entry.is_dir ? '📁' : (entry.is_symlink ? '🔗' : '📄');
    const nameCell = entry.is_dir
      ? el('a', { href: '#', on: { click: (e) => { e.preventDefault(); fxNavigate(fxJoin(FX.path, entry.name)); } } }, entry.name)
      : document.createTextNode(entry.name);
    const acts = el('div', { class: 'row-end', style: 'gap:4px;justify-content:flex-end' });
    if (!entry.is_dir) {
      const a = el('a', {
        class: 'btn-secondary',
        href: fxDownloadUrl(fxJoin(FX.path, entry.name)),
        title: 'Download',
      }, '⬇');
      a.setAttribute('download', entry.name);
      acts.append(a);
    }
    acts.append(el('button', {
      class: 'btn-secondary',
      title: 'Rename / move within drive',
      on: { click: () => fxRenameEntry(entry) },
    }, '✎'));
    acts.append(el('button', {
      class: 'btn-danger',
      title: 'Delete',
      on: { click: () => fxDeleteEntry(entry) },
    }, '🗑'));
    tbody.append(el('tr', {},
      el('td', {}, icon),
      el('td', {}, nameCell),
      el('td', {}, entry.is_dir ? '' : human(entry.size)),
      el('td', {}, fxFmtTime(entry.mtime)),
      el('td', {}, acts),
    ));
  });
  const hint = $('#fxFreeHint');
  if (FX.listing.free_bytes != null) {
    hint.innerHTML = '';
    hint.append(document.createTextNode(`${human(FX.listing.free_bytes)} free of ${human(FX.listing.total_bytes)}`));
    const u = el('span', { id: 'fxUploadHint', style: 'margin-left:12px' });
    hint.append(u);
  }
};

// ----- DOWNLOADS -----
const renderDownloads = () => {
  const tbody = $('#dlTable tbody'); tbody.innerHTML = '';
  const items = Object.entries(STATE.downloads || {});
  const drivesById = Object.fromEntries((STATE.drives_live || []).map(d => [d.uuid, d]));
  const projects = STATE.projects || {};
  const jobs = (STATE.jobs || []).filter(j => j.type === 'download' && (j.state === 'running' || j.state === 'queued'));
  const jobByTarget = Object.fromEntries(jobs.map(j => [j.target_id, j]));
  if (!items.length) {
    tbody.append(el('tr', {}, el('td', { colspan: 12, class: 'hint' }, 'No download folders yet.')));
  }
  items.forEach(([id, d]) => {
    const drive = drivesById[d.drive_uuid];
    const job = jobByTarget[id];
    const stateCell = job
      ? el('td', { 'data-job-target': id },
          el('div', { class: 'bar', 'data-role': 'bar' },
            el('span', { 'data-role': 'fill', style: `width:${job.progress}%` })),
          el('div', { class: 'meta', 'data-role': 'meta' }, _fmtJobMeta(job)))
      : el('td', {}, el('span', { class: 'pill ' + (d.state === 'active' ? 'ok' : d.state === 'paused' ? 'warn' : 'muted') }, d.state));
    const tr = el('tr', {},
      el('td', {}, el('input', { type: 'checkbox', value: id, class: 'dl-sel' })),
      el('td', {}, d.label, d.warning ? el('div', { class: 'hint', style: 'color:var(--warn)' }, '⚠ ' + d.warning) : null),
      _renderProjectTagsCell(d),
      el('td', {}, d.provider || '—'),
      el('td', {}, d.remote || '—'),
      el('td', {}, drive ? drive.label : (d.drive_uuid || '—')),
      el('td', {}, human(d.remote_size)),
      el('td', {}, human(d.local_size)),
      stateCell,
      el('td', {}, since(d.last_sync)),
      _renderScheduleCell(d),
      el('td', { class: 'actions-cell' },
        el('button', { class: 'btn-link', on: { click: () => syncDownload(id, false) } }, 'Sync'),
        el('button', { class: 'btn-link', on: { click: () => toggleDownload(id, d) } }, d.state === 'paused' ? 'Resume' : 'Pause'),
        el('button', { class: 'btn-link', on: { click: () => openEditEntry('download', id, d) } }, 'Edit'),
        el('button', { class: 'btn-link', on: { click: () => resyncDownload(id) } }, 'Re-sync'),
        el('button', { class: 'btn-link', on: { click: () => deleteDownload(id, d) } }, 'Delete'),
      ),
    );
    tbody.append(tr);
  });
};
const syncDownload = async (id, fresh) => {
  const r = await api(`/api/downloads/${id}/${fresh ? 'resync' : 'sync'}`, { method: 'POST' });
  if (r.ok) toast('Sync started');
  refreshState();
};
const toggleDownload = async (id, d) => {
  await api(`/api/downloads/${id}`, { method: 'PATCH', body: { state: d.state === 'paused' ? 'active' : 'paused' } });
  refreshState();
};
const resyncDownload = async (id) => {
  if (!confirm('Wipe local files and re-sync from scratch?')) return;
  syncDownload(id, true);
};
const deleteDownload = async (id, d) => {
  const wipe = confirm('Delete this folder from the list?\n\nOK = also delete local files\nCancel = keep local files');
  // confirm returns true for OK; we want a 3-way but keep it simple:
  // Let's simplify: ask twice.
  await api(`/api/downloads/${id}?delete_files=${wipe ? 1 : 0}`, { method: 'DELETE' });
  refreshState();
};
$('#dlAddOpen').addEventListener('click', () => {
  $('#downloadAddPanel').classList.remove('hidden');
  populateDownloadAddForm();
  _clearFieldErrors($('#downloadAddPanel'));
  // Initial help: prefer the help for the currently-picked account's
  // provider; otherwise fall back to a generic blurb.
  _refreshDlProviderHelp();
});
$('#dlAddCancel').addEventListener('click', () => $('#downloadAddPanel').classList.add('hidden'));
// Best-effort URL → provider sniff. Mirrors the server-side parse_link()
// well enough for picking a default account; the server will validate.
const _detectProvider = (url) => {
  url = (url || '').trim();
  if (!url) return '';
  if (/drive\.google\.com/.test(url)) return 'drive';
  if (/dropbox\.com/.test(url)) return 'dropbox';
  if (/box\.com/.test(url)) return 'box';
  if (/1drv\.ms|onedrive|sharepoint/.test(url)) return 'onedrive';
  if (/^ftps?:\/\//i.test(url)) return 'ftp';
  if (/^sftp:\/\//i.test(url)) return 'sftp';
  if (/^https?:\/\//i.test(url)) return 'http';
  return '';
};
// Find the user's default account name for a given provider.
const _defaultRemoteFor = (provider) => {
  if (!provider) return '';
  const rs = STATE.remotes || {};
  for (const [n, r] of Object.entries(rs)) {
    if (r.provider === provider && r.default) return n;
  }
  return '';
};

// ----- per-entry sync interval helpers -----
// The Add Download / Add Upload / Edit Entry forms all share the same
// preset-dropdown + custom-seconds pattern. These helpers wire the show /
// hide of the custom input, populate the form from a stored value, and
// read the form back out as a string of seconds (or "" for manual).
const SCHED_PRESETS = ['0', '60', '300', '900', '1800', '3600', '21600', '86400'];
const _setScheduleForm = (presetEl, customEl, customWrap, value) => {
  // Accept numeric seconds, "" / "manual" / "off" -> 0, or "<n><s|m|h|d>".
  let secs = 0;
  if (typeof value === 'number') secs = Math.max(0, value | 0);
  else if (typeof value === 'string') {
    const v = value.trim().toLowerCase();
    const m = /^(\d+)\s*([smhd]?)$/.exec(v);
    if (m) {
      const mult = { '': 1, s: 1, m: 60, h: 3600, d: 86400 }[m[2]];
      secs = parseInt(m[1], 10) * mult;
    }
  }
  const asStr = String(secs);
  if (SCHED_PRESETS.includes(asStr)) {
    presetEl.value = asStr;
    customEl.value = '';
    customWrap.classList.add('hidden');
  } else {
    presetEl.value = 'custom';
    customEl.value = secs > 0 ? asStr : '';
    customWrap.classList.toggle('hidden', false);
  }
};
const _readScheduleForm = (presetEl, customEl) => {
  if (!presetEl) return '';
  if (presetEl.value === 'custom') {
    const n = parseInt((customEl && customEl.value) || '', 10);
    return Number.isFinite(n) && n > 0 ? String(n) : '';
  }
  const n = parseInt(presetEl.value, 10);
  return Number.isFinite(n) && n > 0 ? String(n) : '';
};
// Parse the stored ``schedule`` string back into seconds. Mirrors
// botsyncd._schedule_seconds(). Returns 0 for blank / "manual" / unparseable.
const _scheduleSeconds = (raw) => {
  if (raw == null) return 0;
  if (typeof raw === 'number') return raw > 0 ? Math.floor(raw) : 0;
  const s = String(raw).trim().toLowerCase();
  if (!s || s === 'manual' || s === 'off' || s === 'none' || s === '0') return 0;
  const m = /^(\d+)\s*([smhd]?)$/.exec(s);
  if (!m) return 0;
  const mult = { '': 1, s: 1, m: 60, h: 3600, d: 86400 }[m[2]];
  const n = parseInt(m[1], 10) * mult;
  return n > 0 ? n : 0;
};
// Human-friendly interval label, e.g. 60 -> "every 1 min", 3600 -> "every 1 h".
const _fmtInterval = (secs) => {
  if (!secs || secs <= 0) return 'Manual';
  if (secs < 60) return `every ${secs}s`;
  if (secs < 3600) {
    const m = Math.round(secs / 60);
    return `every ${m} min`;
  }
  if (secs < 86400) {
    const h = secs / 3600;
    return `every ${h % 1 === 0 ? h : h.toFixed(1)} h`;
  }
  const d = secs / 86400;
  return `every ${d % 1 === 0 ? d : d.toFixed(1)} d`;
};
// Short countdown string, e.g. "in 2m 15s" / "due now" / "12h 5m".
const _fmtCountdown = (secs) => {
  if (secs <= 0) return 'due now';
  if (secs < 60) return `in ${secs}s`;
  if (secs < 3600) return `in ${Math.floor(secs / 60)}m ${secs % 60}s`;
  if (secs < 86400) return `in ${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return `in ${Math.floor(secs / 86400)}d ${Math.floor((secs % 86400) / 3600)}h`;
};
// Build the Schedule / Next-sync cell shown beside Last sync. Shows the
// interval label on the first line and a relative countdown on the second
// (or "pending first run" / "paused" when there's nothing to count down to).
const _renderScheduleCell = (item) => {
  const interval = _scheduleSeconds(item && item.schedule);
  const top = el('div', {}, _fmtInterval(interval));
  let bottom;
  if (item && item.state === 'paused') {
    bottom = el('div', { class: 'hint', style: 'font-size:11px' }, 'paused');
  } else if (interval <= 0) {
    bottom = el('div', { class: 'hint', style: 'font-size:11px' },
      item && item.last_sync ? '—' : 'never run');
  } else if (!item || !item.last_sync) {
    bottom = el('div', { class: 'hint', style: 'font-size:11px' }, 'pending first run');
  } else {
    const due = item.last_sync + interval;
    const remaining = Math.max(0, Math.round(due - (Date.now() / 1000)));
    bottom = el('div', { class: 'hint', style: 'font-size:11px' }, _fmtCountdown(remaining));
  }
  return el('td', {}, top, bottom);
};
// Toggle the custom-seconds input as soon as the user picks "Custom…".
document.addEventListener('change', (e) => {
  if (e.target && e.target.matches('select[data-sched-preset]')) {
    const wrap = e.target.closest('.sched-row, dialog, .panel')
      ? (e.target.closest('.sched-row, dialog, .panel').querySelector('[data-sched-custom-wrap]'))
      : null;
    if (wrap) wrap.classList.toggle('hidden', e.target.value !== 'custom');
  }
});
// Pick provider help based on the selected account, or by sniffing the URL.
const _refreshDlProviderHelp = () => {
  const url = ($('#dlUrl') && $('#dlUrl').value || '').trim();
  const sel = $('#dlRemote');
  const remote = (sel && sel.value || '').trim();
  let provider = '';
  if (remote && STATE && STATE.remotes && STATE.remotes[remote]) {
    provider = STATE.remotes[remote].provider || '';
  }
  if (!provider) provider = _detectProvider(url);
  // If the user hasn't picked an account yet but we detected a provider,
  // pre-select the default account for that provider so they don't have
  // to open the dropdown when there's an obvious choice.
  if (sel && !remote && provider) {
    const def = _defaultRemoteFor(provider);
    if (def) {
      const has = Array.from(sel.options).some(o => o.value === def);
      if (has) sel.value = def;
    }
  }
  _renderProviderHelp($('#dlProviderHelp'), provider, 'download');
};
document.addEventListener('input', (e) => {
  if (e.target && e.target.id === 'dlUrl') _refreshDlProviderHelp();
});
document.addEventListener('change', (e) => {
  if (e.target && e.target.id === 'dlRemote') _refreshDlProviderHelp();
});
$('#dlAddSubmit').addEventListener('click', async () => {
  const panel = $('#downloadAddPanel');
  _clearFieldErrors(panel);
  const body = {
    url: $('#dlUrl').value.trim(),
    label: $('#dlLabel').value.trim(),
    remote: $('#dlRemote').value,
    drive_uuid: $('#dlDrive').value,
    remote_path: $('#dlRemotePath').value.trim(),
    project_id: $('#dlProject').value,
    project_ids: _projectIdsFromForm('#dlProject', '#dlProjectExtraChips'),
    auto_delete_at: _datetimeLocalToEpoch($('#dlAutoDelete').value),
    schedule: _readScheduleForm($('#dlSchedulePreset'), $('#dlScheduleCustom')),
  };
  if (!body.url) {
    _applyFieldError(panel, { ok: false, field: 'url', error: 'URL required',
      fix: 'Paste a Drive / Dropbox / Box / OneDrive folder URL, or ftp://host/path / sftp://host/path.' });
    return toast('URL required', true);
  }
  const r = await api('/api/downloads', { method: 'POST', body });
  if (r.ok) {
    $('#dlUrl').value = ''; $('#dlLabel').value = ''; $('#dlRemotePath').value = '';
    $('#dlAutoDelete').value = '';
    _setExtraChipIds($('#dlProjectExtraChips'), []);
    $('#downloadAddPanel').classList.add('hidden');
    if (r.warning) toast('Folder added — ' + r.warning, true);
    else if (r.queued) toast('Folder added — sync will start automatically (' + (r.queued_reason || 'queued') + ')');
    else toast('Folder added — sync started');
    refreshState();
  } else {
    _applyFieldError(panel, r);
  }
});
const populateDownloadAddForm = () => {
  const remoteSel = $('#dlRemote'); remoteSel.innerHTML = '';
  remoteSel.append(el('option', { value: '' }, '— pick an account —'));
  Object.entries(STATE.remotes || {}).forEach(([n, r]) => {
    const label = r && r.default ? `${n} ★ default` : n;
    remoteSel.append(el('option', { value: n }, label));
  });
  const driveSel = $('#dlDrive'); driveSel.innerHTML = '';
  (STATE.drives_live || []).filter(d => d.adopted).forEach(d => driveSel.append(el('option', { value: d.uuid }, d.label + (d.primary ? ' (primary)' : ''))));
  populateProjectSelect($('#dlProject'));
  _populateExtraSelect($('#dlProjectExtra'));
  _setExtraChipIds($('#dlProjectExtraChips'), []);
};
$('#dlBulkSync').addEventListener('click', async () => {
  for (const [id, d] of Object.entries(STATE.downloads || {})) {
    if (d.state === 'active') await api(`/api/downloads/${id}/sync`, { method: 'POST' });
  }
  toast('Queued active downloads');
  refreshState();
});
$('#dlSyncSelected').addEventListener('click', async () => {
  const ids = $$('.dl-sel').filter(cb => cb.checked).map(cb => cb.value);
  if (!ids.length) return toast('Select at least one folder', true);
  let ok = 0;
  for (const id of ids) {
    const r = await api(`/api/downloads/${id}/sync`, { method: 'POST' });
    if (r && r.ok !== false) ok++;
  }
  toast(`Queued ${ok}/${ids.length} download${ids.length !== 1 ? 's' : ''}`);
  refreshState();
});
$('#dlSelAll').addEventListener('change', (e) => {
  $$('.dl-sel').forEach(cb => cb.checked = e.target.checked);
});

// ----- UPLOADS -----
const renderUploads = () => {
  const tbody = $('#upTable tbody'); tbody.innerHTML = '';
  const items = Object.entries(STATE.uploads || {});
  const drivesById = Object.fromEntries((STATE.drives_live || []).map(d => [d.uuid, d]));
  const projects = STATE.projects || {};
  if (!items.length) {
    tbody.append(el('tr', {}, el('td', { colspan: 12, class: 'hint' }, 'No upload folders yet.')));
  }
  items.forEach(([id, u]) => {
    const drive = drivesById[u.drive_uuid];
    const tr = el('tr', {},
      el('td', {}, el('input', { type: 'checkbox', value: id, class: 'up-sel' })),
      el('td', {}, u.label),
      _renderProjectTagsCell(u),
      el('td', {}, u.provider),
      el('td', {}, u.remote || '—'),
      el('td', {}, (drive ? drive.label + ':' : '') + u.local_subpath),
      el('td', {}, u.remote_path || '—'),
      el('td', {}, u.mode),
      el('td', {}, el('span', { class: 'pill ' + (u.state === 'active' ? 'ok' : 'muted') }, u.state)),
      el('td', {}, since(u.last_sync)),
      _renderScheduleCell(u),
      el('td', { class: 'actions-cell' },
        el('button', { class: 'btn-link', on: { click: async () => { const r = await api(`/api/uploads/${id}/sync`, { method: 'POST' }); if (r.ok) toast('Upload started'); refreshState(); } } }, 'Sync'),
        el('button', { class: 'btn-link', on: { click: async () => { await api(`/api/uploads/${id}`, { method: 'PATCH', body: { state: u.state === 'paused' ? 'active' : 'paused' } }); refreshState(); } } }, u.state === 'paused' ? 'Resume' : 'Pause'),
        el('button', { class: 'btn-link', on: { click: () => openEditEntry('upload', id, u) } }, 'Edit'),
        el('button', { class: 'btn-link', on: { click: async () => { if (confirm('Remove from list? Local files are kept.')) { await api(`/api/uploads/${id}`, { method: 'DELETE' }); refreshState(); } } } }, 'Delete'),
      ),
    );
    tbody.append(tr);
  });
};
$('#upAddOpen').addEventListener('click', () => {
  const ps = $('#upProvider'); ps.innerHTML = '';
  Object.entries(STATE.providers || {}).forEach(([k, v]) => ps.append(el('option', { value: k }, v.label)));
  const rs = $('#upRemote'); rs.innerHTML = '';
  rs.append(el('option', { value: '' }, '— pick an account —'));
  Object.entries(STATE.remotes || {}).forEach(([n, r]) => {
    const label = r && r.default ? `${n} ★ default` : n;
    rs.append(el('option', { value: n }, label));
  });
  const ds = $('#upDrive'); ds.innerHTML = '';
  (STATE.drives_live || []).filter(d => d.adopted).forEach(d => ds.append(el('option', { value: d.uuid }, d.label)));
  populateProjectSelect($('#upProject'));
  _populateExtraSelect($('#upProjectExtra'));
  _setExtraChipIds($('#upProjectExtraChips'), []);
  $('#uploadAddPanel').classList.remove('hidden');
  _clearFieldErrors($('#uploadAddPanel'));
  _renderProviderHelp($('#upProviderHelp'), $('#upProvider').value, 'upload');
  // Pre-select the default account for the initially-selected provider so
  // single-account users don't need to touch the dropdown.
  const def = _defaultRemoteFor($('#upProvider').value);
  if (def) rs.value = def;
});
$('#upAddCancel').addEventListener('click', () => $('#uploadAddPanel').classList.add('hidden'));
document.addEventListener('change', (e) => {
  if (e.target && e.target.id === 'upProvider') {
    _renderProviderHelp($('#upProviderHelp'), $('#upProvider').value, 'upload');
    // When the provider changes, snap the account dropdown to the new
    // provider's default — but only if the user hasn't already picked
    // an account that already matches the new provider.
    const rs = $('#upRemote');
    const cur = rs && rs.value;
    const curProv = cur && (STATE.remotes || {})[cur] && STATE.remotes[cur].provider;
    if (rs && curProv !== $('#upProvider').value) {
      const def = _defaultRemoteFor($('#upProvider').value);
      rs.value = def || '';
    }
  }
});
$('#upAddSubmit').addEventListener('click', async () => {
  const panel = $('#uploadAddPanel');
  _clearFieldErrors(panel);
  const body = {
    label: $('#upLabel').value.trim(),
    provider: $('#upProvider').value,
    remote: $('#upRemote').value,
    local_name: $('#upLocalName').value.trim(),
    remote_path: $('#upRemotePath').value.trim(),
    mode: $('#upMode').value,
    drive_uuid: $('#upDrive').value,
    project_id: $('#upProject').value,
    project_ids: _projectIdsFromForm('#upProject', '#upProjectExtraChips'),
    auto_delete_at: _datetimeLocalToEpoch($('#upAutoDelete').value),
    schedule: _readScheduleForm($('#upSchedulePreset'), $('#upScheduleCustom')),
  };
  const r = await api('/api/uploads', { method: 'POST', body });
  if (r.ok) {
    $('#upLabel').value = ''; $('#upLocalName').value = ''; $('#upRemotePath').value = '';
    $('#upAutoDelete').value = '';
    _setExtraChipIds($('#upProjectExtraChips'), []);
    $('#uploadAddPanel').classList.add('hidden');
    if (r.queued) toast('Upload folder added — sync will start automatically (' + (r.queued_reason || 'queued') + ')');
    else toast('Upload folder added — sync started');
    refreshState();
  } else {
    _applyFieldError(panel, r);
  }
});
$('#upSyncSelected').addEventListener('click', async () => {
  const ids = $$('.up-sel').filter(cb => cb.checked).map(cb => cb.value);
  if (!ids.length) return toast('Select at least one folder', true);
  let ok = 0;
  for (const id of ids) {
    const r = await api(`/api/uploads/${id}/sync`, { method: 'POST' });
    if (r && r.ok !== false) ok++;
  }
  toast(`Queued ${ok}/${ids.length} upload${ids.length !== 1 ? 's' : ''}`);
  refreshState();
});
$('#upBulkSync').addEventListener('click', async () => {
  for (const [id, u] of Object.entries(STATE.uploads || {})) {
    if (u.state === 'active') await api(`/api/uploads/${id}/sync`, { method: 'POST' });
  }
  toast('Queued active uploads');
  refreshState();
});
$('#upSelAll').addEventListener('change', (e) => {
  $$('.up-sel').forEach(cb => cb.checked = e.target.checked);
});

// ----- EDIT DOWNLOAD/UPLOAD ENTRY -----
// Single shared modal (entryEditDialog) reused for both kinds. State is
// edited via the row's Pause/Resume button so it's intentionally not in the
// form — keeping that out of the modal avoids the double-source-of-truth
// problem where two separate widgets both claim ownership of `state`.
const openEditEntry = (kind, id, item) => {
  const dlg = $('#entryEditDialog');
  $('#entryEditTitle').textContent = (kind === 'upload' ? 'Edit upload — ' : 'Edit download — ') + (item.label || id);
  $('#entryEditKind').value = kind;
  $('#entryEditId').value = id;
  $('#entryEditLabel').value = item.label || '';
  $('#entryEditRemotePath').value = item.remote_path || '';
  _setScheduleForm(
    $('#entryEditSchedulePreset'),
    $('#entryEditScheduleCustom'),
    $('#entryEditDialog').querySelector('[data-sched-custom-wrap]'),
    item.schedule || '',
  );
  // Mode is upload-only (push/mirror/bisync). Hide the row entirely for
  // downloads rather than disabling, so it doesn't look like a setting the
  // user just hasn't configured yet.
  $('#entryEditModeWrap').classList.toggle('hidden', kind !== 'upload');
  if (kind === 'upload') $('#entryEditMode').value = item.mode || 'push';
  // Project select reflects the entry's current project_id (blank = none).
  // Saving a different value triggers a backend mv() of the local folder
  // so the user can re-shuffle entries between projects from the edit
  // dialog without losing already-synced data.
  populateProjectSelect($('#entryEditProject'), item.project_id || '');
  _populateExtraSelect($('#entryEditProjectExtra'));
  // Chips = project_ids minus the primary (which is already shown in the
  // primary <select>). Fall back to [] when the entry only has the legacy
  // single project_id.
  {
    const ids = (item.project_ids && item.project_ids.length) ? item.project_ids.slice() : (item.project_id ? [item.project_id] : []);
    const primary = item.project_id || (ids[0] || '');
    const extras = ids.filter(x => x && x !== primary);
    _setExtraChipIds($('#entryEditProjectExtraChips'), extras);
  }
  $('#entryEditAutoDelete').value = _epochToDatetimeLocal(item.auto_delete_at);
  // Build remote dropdown from the live remotes list, preselecting the
  // item's current remote (which may be blank for legacy/mock entries).
  const sel = $('#entryEditRemote'); sel.innerHTML = '';
  sel.append(el('option', { value: '' }, '— pick an account —'));
  Object.entries(STATE.remotes || {}).forEach(([n, r]) => {
    const label = r && r.default ? `${n} ★ default` : n;
    const opt = el('option', { value: n }, label);
    if (n === item.remote) opt.selected = true;
    sel.append(opt);
  });
  // Hint copy differs slightly between kinds because the field semantics do.
  $('#entryEditHint').textContent = kind === 'upload'
    ? 'Changing remote / remote path will redirect future uploads. Local files already on the drive are not moved.'
    : 'Changing remote / remote path will pull from the new location on the next sync. Local files already downloaded are not removed.';
  // Inline provider help for the entry's provider, plus reset stale errors.
  _clearFieldErrors(dlg);
  _renderProviderHelp($('#entryEditProviderHelp'), item.provider, kind);
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
};
$('#entryEditCancel').addEventListener('click', () => {
  const dlg = $('#entryEditDialog');
  if (typeof dlg.close === 'function') dlg.close();
  else dlg.removeAttribute('open');
});
$('#entryEditSubmit').addEventListener('click', async () => {
  const dlg = $('#entryEditDialog');
  _clearFieldErrors(dlg);
  const kind = $('#entryEditKind').value;
  const id = $('#entryEditId').value;
  if (!id) return;
  const body = {
    label: $('#entryEditLabel').value.trim(),
    remote: $('#entryEditRemote').value,
    remote_path: $('#entryEditRemotePath').value.trim(),
    schedule: _readScheduleForm($('#entryEditSchedulePreset'), $('#entryEditScheduleCustom')),
    project_id: $('#entryEditProject').value || '',
    project_ids: _projectIdsFromForm('#entryEditProject', '#entryEditProjectExtraChips'),
    auto_delete_at: _datetimeLocalToEpoch($('#entryEditAutoDelete').value),
  };
  if (kind === 'upload') body.mode = $('#entryEditMode').value;
  const path = kind === 'upload' ? `/api/uploads/${id}` : `/api/downloads/${id}`;
  const r = await api(path, { method: 'PATCH', body });
  if (r && r.ok !== false) {
    toast(kind === 'upload' ? 'Upload entry updated' : 'Download entry updated');
    if (typeof dlg.close === 'function') dlg.close();
    else dlg.removeAttribute('open');
    refreshState();
  } else {
    _applyFieldError(dlg, r);
    toast((r && r.error) || 'Save failed', true);
  }
});

// ----- SYNC LOG (per-type ring buffer rendered under each tab) -----
const _fmtDuration = (s) => {
  s = +s || 0;
  if (s < 1) return '<1s';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
};
const _fmtAbs = (ts) => {
  if (!ts) return '—';
  try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return String(ts); }
};
const _stateBadge = (st) => {
  const cls = st === 'done' ? 'ok' : st === 'error' ? 'err' : st === 'cancelled' ? 'warn' : 'muted';
  return el('span', { class: 'pill ' + cls }, st || '?');
};
const renderSyncLog = (type) => {
  const prefix = type === 'download' ? 'dl' : 'up';
  const tbody = $(`#${prefix}SyncLogTable tbody`);
  if (!tbody) return;
  tbody.innerHTML = '';
  const entries = ((STATE.sync_log || {})[type] || []).slice().reverse();
  if (!entries.length) {
    tbody.append(el('tr', {}, el('td', { colspan: 6, class: 'hint' }, 'No sync runs yet.')));
    return;
  }
  entries.forEach(e => {
    tbody.append(el('tr', {},
      el('td', { title: _fmtAbs(e.finished_at || e.started_at) }, since(e.finished_at || e.started_at)),
      el('td', {}, e.label || '—'),
      el('td', {}, _stateBadge(e.state)),
      el('td', {}, _fmtDuration(e.duration_s)),
      el('td', {}, human(e.bytes)),
      el('td', { class: 'sync-log-detail', title: e.error || '' }, e.error ? e.error : (e.state === 'done' ? 'ok' : '—')),
    ));
  });
};
const _clearSyncLog = async (type) => {
  if (!confirm(`Clear all ${type} sync history?`)) return;
  const r = await api('/api/sync-log/clear', { method: 'POST', body: { type } });
  if (r && r.ok) { toast(`${type} history cleared`); refreshState(); }
};
const _dlClr = $('#dlSyncLogClear'); if (_dlClr) _dlClr.addEventListener('click', () => _clearSyncLog('download'));
const _upClr = $('#upSyncLogClear'); if (_upClr) _upClr.addEventListener('click', () => _clearSyncLog('upload'));

// ----- REMOTES -----
const renderRemotes = () => {
  const tbody = $('#rmTable tbody'); tbody.innerHTML = '';
  const items = Object.entries(STATE.remotes || {});
  if (!items.length) tbody.append(el('tr', {}, el('td', { colspan: 7, class: 'hint' }, 'No accounts connected.')));
  // Count how many accounts exist per provider so we only offer the
  // "Make default" button when there's actually a choice to make. Single
  // accounts are implicitly the default and showing the button there
  // would just be noise.
  const providerCount = {};
  items.forEach(([, r]) => { providerCount[r.provider] = (providerCount[r.provider] || 0) + 1; });
  items.forEach(([name, r]) => {
    const reauth = !!r.needs_reauth;
    const healthLabel = reauth ? 'reauth needed' : (r.health || 'unknown');
    const healthCls = reauth ? 'reauth' : (r.health === 'ok' ? 'ok' : r.health === 'error' ? 'err' : 'muted');
    const dotCls = reauth ? 'err' : (r.health === 'ok' ? 'ok' : r.health === 'error' ? 'err' : 'warn');
    const isDefault = !!r.default;
    const showMakeDefault = !isDefault && providerCount[r.provider] > 1;
    tbody.append(el('tr', {},
      el('td', {}, el('span', { class: 'dot ' + dotCls })),
      el('td', {},
        name,
        isDefault ? el('span', { class: 'pill ok', style: 'margin-left:6px', title: 'Default account for ' + r.provider }, '★ default') : null,
      ),
      el('td', {}, (STATE.providers[r.provider] || {}).label || r.provider),
      el('td', {}, el('span', { class: 'pill ' + healthCls }, healthLabel)),
      el('td', {}, since(r.last_check)),
      el('td', { style: 'color:var(--muted);font-size:11px' }, r.error || ''),
      el('td', { class: 'actions-cell' },
        reauth ? el('button', { class: 'btn-primary', on: { click: () => openReauth(name, r.provider) } }, '🔐 Reauthenticate') : null,
        showMakeDefault ? el('button', { class: 'btn-link', title: 'Use this account by default for ' + r.provider + ' downloads/uploads', on: { click: async () => {
          const res = await api(`/api/remotes/${encodeURIComponent(name)}/default`, { method: 'POST' });
          if (res && res.ok) toast(`${name} is now the default ${r.provider} account`);
          else toast((res && res.error) || 'could not set default', true);
          refreshState();
        } } }, 'Make default') : null,
        el('button', { class: 'btn-link', on: { click: async () => {
          toast(`Checking ${name}\u2026`);
          const r = await api(`/api/remotes/${encodeURIComponent(name)}/check`, { method: 'POST' });
          if (r && r.ok) toast(`${name}: OK`);
          else if (r && r.needs_reauth) toast(`${name}: needs reauthentication`, true);
          else toast(`${name}: ${(r && r.error) || 'check failed'}`, true);
          refreshState();
        } } }, 'Check'),
        el('button', { class: 'btn-link', on: { click: async () => { if (confirm(`Remove account ${name}?`)) { await api(`/api/remotes/${encodeURIComponent(name)}`, { method: 'DELETE' }); refreshState(); } } } }, 'Remove'),
      ),
    ));
  });
};

// Open the OAuth panel pre-filled to reconnect an existing remote (same name).
function openReauth(name, provider) {
  document.querySelector('button[data-tab="remotes"]').click();
  $('#remoteAddPanel').classList.remove('hidden');
  const ps = $('#rmProvider'); ps.innerHTML = '';
  Object.entries(STATE.providers || {}).forEach(([k, v]) => ps.append(el('option', { value: k }, v.label)));
  if (provider) ps.value = provider;
  $('#rmName').value = name;
  $('#rmName').readOnly = true;
  $('#rmOAuth').classList.add('hidden');
  $('#rmDevice').classList.add('hidden');
  // For reauth we already have the credentials in rclone.conf, so hide the
  // Google-creds inputs even on the Drive provider.
  $('#rmGoogleCreds').classList.add('hidden');
  _updateAuthCmdPreview();
  // Pick the right primary action based on provider. Drive => in-browser
  // device flow (best UX). Everything else => headless rclone-token paste.
  if ((provider || ps.value) === 'drive') {
    toast('Click "Sign in with browser" to reauthenticate Google Drive — no rclone needed.');
    setTimeout(() => $('#rmAddDevice').focus(), 50);
  } else {
    // Auto-trigger the legacy headless flow so the user actually sees the
    // command and token paste box (was hidden before — that's why the old
    // Reauthenticate button "did nothing").
    $('#rmAddStart').click();
  }
}
// Update the rclone-authorize command preview based on the selected
// provider, so users adding Dropbox/Box/OneDrive don't see the
// Google-Drive example baked into the HTML.
function _updateAuthCmdPreview() {
  const provider = $('#rmProvider').value;
  const meta = (STATE.providers || {})[provider] || {};
  const t = meta.rclone_type || provider || 'drive';
  $('#rmAuthCmd').textContent = `rclone authorize "${t}"`;
  $('#rmHint').textContent = `Windows (PowerShell): .\\rclone authorize "${t}"   •   Windows (CMD) / macOS / Linux: rclone authorize "${t}"`;
  const isDrive = (provider === 'drive');
  const isBasic = (meta.auth === 'basic');
  const isNew = !$('#rmName').readOnly;
  // OAuth buttons hide when this provider doesn't use OAuth.
  $('#rmAddDevice').style.display = isDrive && !isBasic ? '' : 'none';
  $('#rmAddStart').style.display = isBasic ? 'none' : '';
  $('#rmAddBasic').style.display = isBasic ? '' : 'none';
  $('#rmGoogleCreds').classList.toggle('hidden', !(isDrive && isNew && !isBasic));
  // FTP/SFTP credentials block.
  $('#rmBasicCreds').classList.toggle('hidden', !isBasic);
  $('#rmFtpExtra').classList.toggle('hidden', provider !== 'ftp');
  $('#rmSftpExtra').classList.toggle('hidden', provider !== 'sftp');
  // Default port hint by provider.
  const portInput = $('#rmBcPort');
  if (portInput && !portInput.value) {
    portInput.placeholder = (provider === 'sftp') ? '22' : (provider === 'ftp' ? '21 (or 990 for implicit FTPS)' : '');
  }
  // Per-provider help block at the top of the connect panel.
  _renderProviderHelp($('#rmProviderHelp'), provider, 'account');
}
$('#rmProvider').addEventListener('change', _updateAuthCmdPreview);

// In-browser Google Drive device flow.
let devicePollTimer = null;
let deviceSession = null;
function _stopDevicePoll() {
  if (devicePollTimer) { clearTimeout(devicePollTimer); devicePollTimer = null; }
}
async function _pollDeviceOnce() {
  if (!deviceSession) return;
  const r = await api('/api/remotes/oauth/device/poll', { method: 'POST', body: { session_id: deviceSession.id } });
  if (!r) return;
  if (r.status === 'done') {
    _stopDevicePoll();
    deviceSession = null;
    $('#rmDeviceStatus').textContent = '✅ Connected. Refreshing…';
    toast('Google Drive reconnected');
    setTimeout(() => {
      $('#remoteAddPanel').classList.add('hidden');
      refreshState();
    }, 600);
    return;
  }
  if (r.status === 'pending' || r.ok) {
    devicePollTimer = setTimeout(_pollDeviceOnce, (deviceSession.interval || 5) * 1000);
    return;
  }
  _stopDevicePoll();
  deviceSession = null;
  $('#rmDeviceStatus').textContent = '❌ ' + (r.error || 'sign-in failed');
  toast(r.error || 'Google sign-in failed', true);
}
$('#rmAddDevice').addEventListener('click', async () => {
  if ($('#rmProvider').value !== 'drive') {
    return toast('In-browser sign-in is currently only supported for Google Drive', true);
  }
  const name = ($('#rmName').value || '').trim();
  if (!name) return toast('Enter an account name first', true);
  _stopDevicePoll();
  $('#rmDeviceStatus').textContent = 'Contacting Google…';
  $('#rmDevice').classList.remove('hidden');
  $('#rmOAuth').classList.add('hidden');
  const body = { name };
  // For first-time setup the user supplies client_id/secret. For reauth the
  // backend reads them from rclone.conf, so we just send empty fields.
  if (!$('#rmName').readOnly) {
    body.client_id = $('#rmGCid').value.trim();
    body.client_secret = $('#rmGCSec').value.trim();
  }
  const r = await api('/api/remotes/oauth/device/start', { method: 'POST', body });
  if (!r || !r.ok) {
    $('#rmDeviceStatus').textContent = '❌ ' + ((r && (r.error + (r.hint ? ' — ' + r.hint : ''))) || 'failed to start');
    return;
  }
  deviceSession = { id: r.session_id, interval: r.interval || 5 };
  const link = r.verification_url_complete || r.verification_url;
  $('#rmDeviceLink').href = link;
  $('#rmDeviceLink').textContent = 'Open Google sign-in →';
  $('#rmDeviceCode').textContent = r.user_code || '';
  $('#rmDeviceStatus').textContent = 'Waiting for you to finish on Google…';
  // Try to open the link automatically. Browsers may block this if it
  // wasn't a direct user gesture; that's fine — the link is also clickable.
  try { window.open(link, '_blank', 'noopener'); } catch (_) {}
  devicePollTimer = setTimeout(_pollDeviceOnce, (deviceSession.interval || 5) * 1000);
});

$('#rmAddOpen').addEventListener('click', () => {
  const ps = $('#rmProvider'); ps.innerHTML = '';
  Object.entries(STATE.providers || {}).forEach(([k, v]) => ps.append(el('option', { value: k }, v.label)));
  $('#rmName').value = '';
  $('#rmName').readOnly = false;
  $('#rmGCid').value = '';
  $('#rmGCSec').value = '';
  // Reset basic-creds inputs in case the panel was previously used for FTP.
  ['#rmBcHost', '#rmBcPort', '#rmBcUser', '#rmBcPass', '#rmBcKeyPem', '#rmBcKeyPass']
    .forEach(s => { const e = $(s); if (e) e.value = ''; });
  const tls = $('#rmBcTls'); if (tls) tls.value = '';
  const noEpsv = $('#rmBcNoEpsv'); if (noEpsv) noEpsv.checked = false;
  $('#remoteAddPanel').classList.remove('hidden');
  $('#rmOAuth').classList.add('hidden');
  $('#rmDevice').classList.add('hidden');
  _clearFieldErrors($('#remoteAddPanel'));
  _stopDevicePoll();
  _updateAuthCmdPreview();
});
$('#rmAddCancel').addEventListener('click', () => {
  _stopDevicePoll();
  deviceSession = null;
  $('#remoteAddPanel').classList.add('hidden');
});
let oauthSession = null;
$('#rmAddStart').addEventListener('click', async () => {
  _stopDevicePoll();
  $('#rmDevice').classList.add('hidden');
  const provider = $('#rmProvider').value;
  const name = $('#rmName').value.trim() || provider;
  const r = await api('/api/remotes/oauth/start', { method: 'POST', body: { provider, name } });
  if (!r.ok) return;
  oauthSession = r.session_id;
  const url = r.auth_url || 'https://rclone.org/downloads/';
  $('#rmAuthUrl').textContent = url.replace(/^https?:\/\//, '');
  $('#rmAuthUrl').href = url;
  if (r.command) $('#rmAuthCmd').textContent = r.command;
  // The provider-specific hint comes from the daemon when available;
  // otherwise fall back to a sensible client-side hint.
  if (r.instructions) {
    $('#rmHint').textContent = r.instructions;
  } else if (r.headless) {
    const t = (r.command || '').match(/"([^"]+)"/);
    const tt = t ? t[1] : 'drive';
    $('#rmHint').textContent = `Windows (PowerShell): .\\rclone authorize "${tt}"   •   Windows (CMD) / macOS / Linux: rclone authorize "${tt}". rclone prints the token JSON once you finish the sign-in.`;
  } else {
    $('#rmHint').textContent = '';
  }
  $('#rmOAuth').classList.remove('hidden');
});
$('#rmCopyCmd').addEventListener('click', async () => {
  const txt = $('#rmAuthCmd').textContent;
  try { await navigator.clipboard.writeText(txt); toast('Command copied'); }
  catch (e) { toast('Copy failed — select the text manually', true); }
});
$('#rmAddFinish').addEventListener('click', async () => {
  const tok = $('#rmToken').value.trim();
  if (!tok) return toast('Paste the token JSON first', true);
  const r = await api('/api/remotes/oauth/finish', { method: 'POST', body: { session_id: oauthSession, token: tok } });
  if (r.ok) { toast('Account connected'); $('#remoteAddPanel').classList.add('hidden'); $('#rmName').value = ''; $('#rmToken').value = ''; refreshState(); }
});

// FTP / SFTP basic-credentials account creation. Posts to /api/remotes/basic
// where the daemon validates fields, runs `rclone obscure` on the password,
// persists any SFTP key to a chmod-600 file, and writes the rclone.conf
// section. Field-level errors come back as { ok:false, field, error, fix }.
$('#rmAddBasic').addEventListener('click', async () => {
  const panel = $('#remoteAddPanel');
  _clearFieldErrors(panel);
  const provider = $('#rmProvider').value;
  const body = {
    provider,
    name: $('#rmName').value.trim(),
    host: $('#rmBcHost').value.trim(),
    port: $('#rmBcPort').value.trim(),
    user: $('#rmBcUser').value.trim(),
    pass: $('#rmBcPass').value,  // not trimmed — passwords may have spaces
  };
  if (provider === 'ftp') {
    body.tls = $('#rmBcTls').value;
    body.no_epsv = $('#rmBcNoEpsv').checked;
  } else if (provider === 'sftp') {
    body.key_pem = $('#rmBcKeyPem').value;
    body.key_pass = $('#rmBcKeyPass').value;
  }
  const r = await api('/api/remotes/basic', { method: 'POST', body });
  if (r && r.ok) {
    toast('Account saved — testing connection in background');
    $('#remoteAddPanel').classList.add('hidden');
    refreshState();
    // Trigger a Check so the user sees green/red right away. Best-effort.
    try { await api(`/api/remotes/${encodeURIComponent(body.name)}/check`, { method: 'POST' }); refreshState(); } catch (_) {}
  } else {
    _applyFieldError(panel, r);
  }
});

// ----- SHARING -----
const renderSharing = () => {
  const s = STATE.sharing || {};
  $('#shSmb').checked = !!s.smb;
  $('#shBonjour').checked = !!s.bonjour;
  $('#shNfs').checked = !!s.nfs;
  $('#shGuestRO').checked = !!s.guest_ro;
  $('#shUser').value = s.share_user || '';
  $('#shPass').value = s.share_pass || '';
};
$('#shSave').addEventListener('click', async () => {
  const r = await api('/api/sharing', { method: 'PATCH', body: {
    smb: $('#shSmb').checked, bonjour: $('#shBonjour').checked, nfs: $('#shNfs').checked,
    guest_ro: $('#shGuestRO').checked, share_user: $('#shUser').value, share_pass: $('#shPass').value,
  }});
  if (r.ok) toast('Sharing settings applied');
  refreshState();
});

// ----- NOTIFICATIONS -----
const renderNotifications = () => {
  const tbody = $('#ntTable tbody'); tbody.innerHTML = '';
  const channels = (STATE.notifications && STATE.notifications.channels) || {};
  const items = Object.entries(channels);
  if (!items.length) {
    tbody.append(el('tr', {}, el('td', { colspan: 8, class: 'hint' }, 'No channels configured.')));
  }
  items.forEach(([cid, ch]) => {
    const eventsLabel = (!ch.events || ch.events.includes('*')) ? 'all' : `${ch.events.length} types`;
    tbody.append(el('tr', {},
      el('td', {}, el('span', { class: 'dot ' + (ch.enabled ? (ch.last_error ? 'err' : 'ok') : 'muted') })),
      el('td', {}, ch.label || '(unnamed)'),
      el('td', {}, (STATE.notify_kinds && STATE.notify_kinds[ch.kind] || {}).label || ch.kind),
      el('td', {}, ch.min_severity || 'info'),
      el('td', {}, eventsLabel),
      el('td', {}, since(ch.last_send)),
      el('td', { style: 'color:var(--err); font-size:11px' }, ch.last_error || ''),
      el('td', { class: 'actions-cell' },
        el('button', { class: 'btn-link', on: { click: () => testChannel(cid) } }, 'Test'),
        el('button', { class: 'btn-link', on: { click: () => editChannel(cid, ch) } }, 'Edit'),
        el('button', { class: 'btn-link', on: { click: async () => { if (confirm(`Delete ${ch.label}?`)) { await api(`/api/notifications/channels/${cid}`, { method: 'DELETE' }); refreshState(); } } } }, 'Delete'),
      ),
    ));
  });
  // Render event log
  const log = $('#eventLog'); log.innerHTML = '';
  const events = (STATE.notifications && STATE.notifications.events) || [];
  if (!events.length) log.append(el('p', { class: 'hint' }, 'No events yet.'));
  events.slice().reverse().forEach(e => {
    const t = new Date((e.ts || 0) * 1000);
    const fields = e.fields ? Object.entries(e.fields).map(([k, v]) => `${k}=${v}`).join(' ') : '';
    log.append(el('div', { class: 'ev ' + e.severity },
      el('span', { class: 't' }, t.toLocaleString()),
      el('span', { class: 's' }, e.severity),
      el('span', {}, e.type),
      el('span', {}, e.message + (fields ? ` · ${fields}` : '')),
    ));
  });
};

const ntKindFields = {
  discord: [{ k: 'url', label: 'Webhook URL', placeholder: 'https://discord.com/api/webhooks/...' }],
  slack:   [{ k: 'url', label: 'Webhook URL', placeholder: 'https://hooks.slack.com/services/...' }],
  webhook: [
    { k: 'url', label: 'POST URL', placeholder: 'https://example.com/hook' },
    { k: 'auth_header', label: 'Authorization header (optional)', placeholder: 'Bearer xxx', secret: true },
  ],
  ntfy: [
    { k: 'url', label: 'Server URL', placeholder: 'https://ntfy.sh' },
    { k: 'topic', label: 'Topic', placeholder: 'botsync-mybox' },
    { k: 'auth_header', label: 'Authorization header (optional)', secret: true },
  ],
  email: [
    { k: 'host', label: 'SMTP host', placeholder: 'smtp.gmail.com' },
    { k: 'port', label: 'SMTP port', placeholder: '587 (STARTTLS) or 465 (SSL)' },
    { k: 'tls_mode', label: 'TLS mode', type: 'select',
      options: [
        { v: 'starttls', label: 'STARTTLS (port 587, recommended)' },
        { v: 'ssl',      label: 'SSL / Implicit TLS (port 465)' },
        { v: 'none',     label: 'None (plaintext, port 25 \u2014 not recommended)' },
      ] },
    { k: 'username', label: 'SMTP username', placeholder: 'me@example.com' },
    { k: 'password', label: 'SMTP password / app password', type: 'password', secret: true },
    { k: 'from', label: 'From address', placeholder: 'botsync@example.com' },
    { k: 'to', label: 'To address', placeholder: 'me@example.com' },
    { k: 'subject_prefix', label: 'Subject prefix (optional)', placeholder: 'BOT-SYNC' },
  ],
};

const renderNtFields = (kind, cfg) => {
  const root = $('#ntFields'); root.innerHTML = '';
  const defs = ntKindFields[kind] || [];
  defs.forEach(def => {
    const id = 'ntf_' + def.k;
    if (def.type === 'checkbox') {
      const wrap = el('label', { class: 'checkbox' });
      const cb = el('input', { type: 'checkbox', id });
      if (cfg && cfg[def.k]) cb.checked = true;
      wrap.append(cb, ' ' + def.label);
      root.append(wrap);
    } else if (def.type === 'select') {
      const sel = el('select', { id });
      const cur = (cfg && cfg[def.k] != null) ? String(cfg[def.k]) : '';
      (def.options || []).forEach(o => {
        const opt = el('option', { value: o.v }, o.label);
        if (o.v === cur) opt.selected = true;
        sel.append(opt);
      });
      root.append(el('label', {}, def.label, sel));
    } else {
      const inp = el('input', {
        type: def.type || 'text', id,
        placeholder: def.placeholder || '',
        value: (cfg && cfg[def.k] != null) ? cfg[def.k] : '',
      });
      root.append(el('label', {}, def.label, inp));
    }
  });
};

const renderNtEventList = (selected) => {
  const all = STATE.event_types || [];
  const list = $('#ntEventList'); list.innerHTML = '';
  const allSel = !selected || selected.includes('*');
  $('#ntEventsAll').checked = allSel;
  all.forEach(et => {
    const cb = el('input', { type: 'checkbox', value: et, class: 'nt-evt' });
    if (allSel || selected.includes(et)) cb.checked = true;
    list.append(el('label', { class: 'checkbox' }, cb, ' ' + et));
  });
};

const openNtForm = () => {
  $('#notifyAddPanel').classList.remove('hidden');
  const ks = $('#ntKind'); ks.innerHTML = '';
  Object.entries(STATE.notify_kinds || {}).forEach(([k, v]) => ks.append(el('option', { value: k }, v.label)));
  $('#ntHint').textContent = '';
};

const editChannel = (cid, ch) => {
  openNtForm();
  $('#ntFormTitle').textContent = 'Edit channel';
  $('#ntId').value = cid;
  $('#ntKind').value = ch.kind;
  $('#ntKind').disabled = true;
  $('#ntLabel').value = ch.label || '';
  $('#ntMinSeverity').value = ch.min_severity || 'info';
  $('#ntEnabled').checked = !!ch.enabled;
  renderNtFields(ch.kind, ch.config || {});
  renderNtEventList(ch.events || ['*']);
};

$('#ntAddOpen').addEventListener('click', () => {
  openNtForm();
  $('#ntFormTitle').textContent = 'Add channel';
  $('#ntId').value = '';
  $('#ntKind').disabled = false;
  $('#ntLabel').value = '';
  $('#ntMinSeverity').value = 'info';
  $('#ntEnabled').checked = true;
  const firstKind = Object.keys(STATE.notify_kinds || { discord: 1 })[0];
  $('#ntKind').value = firstKind;
  renderNtFields(firstKind, {});
  renderNtEventList(['*']);
});
$('#ntCancel').addEventListener('click', () => $('#notifyAddPanel').classList.add('hidden'));
$('#ntKind').addEventListener('change', () => renderNtFields($('#ntKind').value, {}));
$('#ntEventsAll').addEventListener('change', () => {
  const checked = $('#ntEventsAll').checked;
  $$('.nt-evt').forEach(cb => cb.checked = checked);
});

const collectNtForm = () => {
  const kind = $('#ntKind').value;
  const defs = ntKindFields[kind] || [];
  const config = {};
  defs.forEach(def => {
    const node = document.getElementById('ntf_' + def.k);
    if (!node) return;
    if (def.type === 'checkbox') config[def.k] = node.checked;
    else config[def.k] = node.value.trim();
  });
  let events = ['*'];
  if (!$('#ntEventsAll').checked) {
    events = $$('.nt-evt').filter(cb => cb.checked).map(cb => cb.value);
  }
  return {
    kind,
    label: $('#ntLabel').value.trim() || (STATE.notify_kinds[kind] || {}).label,
    config,
    events,
    min_severity: $('#ntMinSeverity').value,
    enabled: $('#ntEnabled').checked,
  };
};

$('#ntSave').addEventListener('click', async () => {
  const cid = $('#ntId').value;
  const body = collectNtForm();
  const url = cid ? `/api/notifications/channels/${cid}` : '/api/notifications/channels';
  const method = cid ? 'PATCH' : 'POST';
  const r = await api(url, { method, body });
  if (r.ok) {
    toast('Channel saved');
    $('#notifyAddPanel').classList.add('hidden');
    refreshState();
  }
});

$('#ntTest').addEventListener('click', async () => {
  let cid = $('#ntId').value;
  if (!cid) {
    // save first, then test
    const body = collectNtForm();
    const r = await api('/api/notifications/channels', { method: 'POST', body });
    if (!r.ok) return;
    cid = r.id;
    $('#ntId').value = cid;
    $('#ntKind').disabled = true;
    $('#ntFormTitle').textContent = 'Edit channel';
  }
  const r = await api(`/api/notifications/channels/${cid}/test`, { method: 'POST' });
  $('#ntHint').textContent = r.ok ? '✔ Test sent.' : '✘ ' + (r.error || 'failed');
  refreshState();
});

const testChannel = async (cid) => {
  const r = await api(`/api/notifications/channels/${cid}/test`, { method: 'POST' });
  toast(r.ok ? 'Test sent' : ('Test failed: ' + (r.error || '')), !r.ok);
  refreshState();
};

// ----- SYSTEM -----
const renderSystem = () => {
  const s = STATE.system || {};
  $('#systemPanel').innerHTML = '';
  const grid = el('div', { class: 'grid' });
  const rows = [
    ['Version', s.version],
    ['Mock mode', s.mock ? 'YES' : 'no'],
    ['rclone', s.rclone ? 'present' : 'NOT FOUND'],
    ['Hostname', s.hostname],
    ['Platform', s.platform],
    ['Root', s.root],
    ['Root free', human(s.root_free) + ' / ' + human(s.root_total)],
    ['Memory', human(s.mem_free) + ' free / ' + human(s.mem_total)],
    ['Swap', s.swap_active
      ? (human(s.swap_used) + ' used / ' + human(s.swap_total)
         + ((s.swap_devices && s.swap_devices.length)
            ? ' (' + s.swap_devices.map(d => d.device).join(', ') + ')'
            : ''))
      : 'disabled'],
    ['Loadavg', (s.loadavg || []).join(' ')],
    ['Uptime', s.uptime ? Math.floor(s.uptime / 3600) + ' h' : '?'],
  ];
  rows.forEach(([k, v]) => grid.append(el('div', { class: 'k' }, k), el('div', {}, String(v ?? ''))));
  $('#systemPanel').append(grid);

  const jobsRoot = $('#jobsList'); jobsRoot.innerHTML = '';
  const jobs = STATE.jobs || [];
  if (!jobs.length) jobsRoot.append(el('p', { class: 'hint' }, 'No jobs.'));
  jobs.slice().reverse().forEach(j => {
    jobsRoot.append(el('div', { class: 'job', 'data-job-id': j.id },
      el('div', { class: 'head' },
        el('strong', {}, `${j.type}: ${j.label}`),
        el('span', { class: 'pill ' + (j.state === 'running' ? 'ok' : j.state === 'error' ? 'err' : 'muted') }, j.state),
      ),
      el('div', { class: 'bar', 'data-role': 'bar' },
        el('span', { 'data-role': 'fill', style: `width:${j.progress}%` })),
      el('div', { class: 'meta', 'data-role': 'meta' }, _fmtJobMeta(j)),
      j.error ? el('div', { class: 'meta', style: 'color:var(--err)' }, j.error) : null,
      j.log_tail && j.log_tail.length ? el('pre', { class: 'log' }, j.log_tail.join('\n')) : null,
      j.state === 'running' ? el('button', { class: 'btn-link', on: { click: () => api(`/api/jobs/${j.id}/cancel`, { method: 'POST' }).then(refreshState) } }, 'Cancel') : null,
    ));
  });
};
const refreshLogs = async () => {
  const r = await api('/api/logs?tail=120');
  $('#logsPane').textContent = (r.lines || []).join('');
};

// ----- topbar status -----
const renderStatus = () => {
  const s = STATE.system || {};
  const adoptedCount = (STATE.drives_live || []).filter(d => d.adopted && d.present).length;
  const offline = (STATE.drives_live || []).filter(d => d.adopted && !d.present).length;
  const remotes = Object.values(STATE.remotes || {});
  const okR = remotes.filter(r => r.health === 'ok').length;
  const badR = remotes.filter(r => r.health === 'error').length;
  $('#systemStatus').innerHTML = `
    <span class="dot ${adoptedCount ? 'ok' : 'warn'}"></span> ${adoptedCount} drive${adoptedCount !== 1 ? 's' : ''}${offline ? ` (${offline} offline)` : ''}
    &nbsp;·&nbsp;
    <span class="dot ${badR ? 'err' : okR ? 'ok' : 'warn'}"></span> ${remotes.length} account${remotes.length !== 1 ? 's' : ''}
  `;
};

// ----- master render -----
// ----- alert banners (rclone update available, accounts needing reauth) -----
const renderAlerts = () => {
  const root = $('#alertBanners'); if (!root) return;
  root.innerHTML = '';
  // Primary drive disappeared while the daemon is running. Highest-severity
  // banner: it sits above everything else because nothing syncs while the
  // primary is gone, and the daemon's state file lives on it.
  const pd = STATE.primary_drive;
  if (pd && pd.uuid && !pd.present) {
    const dur = pd.missing_since ? since(pd.missing_since) : '';
    root.append(el('div', { class: 'alert-banner err' },
      el('div', { class: 'ab-body' },
        el('div', { class: 'ab-title' }, '⚠ Primary drive disconnected'),
        el('div', { class: 'ab-desc' },
          `${pd.label || pd.uuid} (${pd.fs || '?'}) is no longer present` +
          (dur ? ` — missing for ${dur}.` : '.') +
          ' All syncs are paused until it is reconnected. If you pulled it intentionally, ' +
          'shut the router down before unplugging it next time — the daemon stores its state ' +
          'file on this drive and a hot-pull can corrupt it.'),
      ),
    ));
  }
  const rs = STATE.rclone_status || {};
  if (rs.update_available) {
    const installed = rs.installed_version || 'unknown';
    const latest = rs.latest_version || '?';
    const failed = rs.last_update_error;
    root.append(el('div', { class: failed ? 'alert-banner err' : 'alert-banner' },
      el('div', { class: 'ab-body' },
        el('div', { class: 'ab-title' },
          failed ? `⚠️ rclone update failed — still on ${installed}`
                 : `📦 rclone update available — ${latest}`),
        el('div', { class: 'ab-desc' },
          failed ? `Last attempt: ${failed} (latest available: ${latest})`
                 : `Installed: ${installed}. Click Update now to run rclone selfupdate.`),
      ),
      el('div', { class: 'ab-actions' },
        rs.release_url ? el('a', { class: 'btn-link', href: rs.release_url, target: '_blank', rel: 'noreferrer noopener' }, 'Release notes') : null,
        el('button', { class: 'btn-primary', disabled: !!rs.updating, on: { click: doRcloneUpdate } }, rs.updating ? 'Updating…' : (failed ? 'Try again' : 'Update now')),
      ),
    ));
  }
  const reauths = Object.entries(STATE.remotes || {}).filter(([, r]) => r.needs_reauth);
  if (reauths.length) {
    const names = reauths.map(([n]) => n).join(', ');
    const [first, prov] = [reauths[0][0], reauths[0][1].provider];
    root.append(el('div', { class: 'alert-banner err' },
      el('div', { class: 'ab-body' },
        el('div', { class: 'ab-title' }, `🔐 Account${reauths.length > 1 ? 's' : ''} need${reauths.length > 1 ? '' : 's'} reauthentication`),
        el('div', { class: 'ab-desc' }, `${names} — token expired or was revoked. Reconnect to resume syncing.`),
      ),
      el('div', { class: 'ab-actions' },
        el('button', { class: 'btn-primary', on: { click: () => openReauth(first, prov) } }, 'Reconnect'),
      ),
    ));
  }
};

async function doRcloneUpdate() {
  if (!confirm('Run rclone selfupdate now? This downloads a new rclone binary from rclone.org and replaces the running binary.')) return;
  toast('Updating rclone… this can take 30–60 seconds');
  const r = await api('/api/system/rclone/update', { method: 'POST', body: {} });
  if (r && r.ok) {
    toast(`rclone updated: ${r.version_before || '?'} → ${r.version_after || '?'}`);
  } else {
    toast('Update failed: ' + (r && (r.error || r.stderr) || 'unknown error'), true);
  }
  refreshState();
}

const renderRcloneStatus = () => {
  const root = $('#rclonePanel'); if (!root) return;
  const rs = STATE.rclone_status || {};
  root.innerHTML = '';
  const grid = el('div', { class: 'grid' });
  const rows = [
    ['Installed', rs.installed_version || '—'],
    ['Latest available', rs.latest_version || '—'],
    ['Update available', rs.update_available ? 'YES' : 'no'],
    ['Last checked', since(rs.checked_at) || 'never'],
    ['Last update attempt', since(rs.last_update_attempt) || 'never'],
    ['Last update', rs.last_update_to ? `${rs.last_update_from || '?'} → ${rs.last_update_to}` : '—'],
    ['Last update error', rs.last_update_error || '—'],
    ['Check error', rs.check_error || '—'],
  ];
  rows.forEach(([k, v]) => grid.append(el('div', { class: 'k' }, k), el('div', {}, String(v ?? ''))));
  root.append(grid);
  const actions = el('div', { class: 'row-end', style: 'margin-top:10px;gap:8px' },
    el('button', { class: 'btn-secondary', on: { click: async () => { toast('Checking…'); await api('/api/system/rclone/check', { method: 'POST', body: {} }); refreshState(); } } }, '🔄 Check for updates'),
    el('button', { class: 'btn-primary', disabled: !rs.update_available || !!rs.updating, on: { click: doRcloneUpdate } }, rs.updating ? 'Updating…' : '⬇️ Update now'),
  );
  root.append(actions);
  if (rs.release_notes) {
    root.append(el('details', { style: 'margin-top:10px' },
      el('summary', { style: 'cursor:pointer' }, `Release notes ${rs.latest_version || ''}`),
      el('pre', { style: 'white-space:pre-wrap;font-size:12px;color:var(--muted)' }, rs.release_notes),
    ));
  }
};

// ----- DASHBOARD -----
const _switchTab = (name) => {
  const btn = $$('.tab').find(t => t.dataset.tab === name);
  if (btn) btn.click();
};
const _humanUptime = (s) => {
  if (!s) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};
const _bar = (used, total, opts = {}) => {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const cls = pct >= 90 ? 'err' : pct >= 75 ? 'warn' : '';
  return el('div', { class: 'dash-bar-wrap' },
    el('div', { class: 'lbls' },
      el('span', {}, opts.left || `${human(used)} / ${human(total)}`),
      el('span', {}, `${pct}%`)),
    el('div', { class: 'bar' }, el('span', { class: cls, style: `width:${pct}%` })),
  );
};
const _panel = (title, kids, attrs = {}) => {
  const klass = 'panel' + (attrs.wide ? ' wide' : '');
  const p = el('div', { class: klass });
  p.append(el('h3', {}, title));
  for (const k of [].concat(kids).filter(Boolean)) p.append(k);
  if (attrs.click) {
    p.style.cursor = 'pointer';
    p.title = 'Click to open';
    p.addEventListener('click', attrs.click);
  }
  return p;
};

const renderDashboard = () => {
  const root = $('#dashGrid'); if (!root) return;
  root.innerHTML = '';
  const sys = STATE.system || {};
  const settings = STATE.settings || {};
  const enabled = settings.enabled !== false;

  // -- Sync status --
  const drives = STATE.drives_live || [];
  const adopted = drives.filter(d => d.adopted);
  const online = adopted.filter(d => d.present);
  const offline = adopted.filter(d => !d.present);
  const downloads = Object.values(STATE.downloads || {});
  const uploads = Object.values(STATE.uploads || {});
  const remotes = Object.values(STATE.remotes || {});
  const remotesOk = remotes.filter(r => r.health === 'ok');
  const remotesBad = remotes.filter(r => r.health === 'error' || r.needs_reauth);
  const jobs = STATE.jobs || [];
  const running = jobs.filter(j => j.state === 'running');
  const queued = jobs.filter(j => j.state === 'queued');
  const errored = jobs.filter(j => j.state === 'error').slice(-3);

  // -- Status panel --
  const statusBits = [];
  statusBits.push(el('div', { class: 'dash-stat' },
    el('span', { class: 'num', style: enabled ? '' : 'color:var(--err)' }, enabled ? '✓ Active' : '✗ Disabled'),
    el('span', { class: 'lbl' }, enabled ? 'BOT-SYNC is running' : 'Master switch is off'),
  ));
  statusBits.push(el('div', { class: 'dash-row' },
    el('span', { class: 'lbl' }, 'Version'),
    el('span', { class: 'val' }, (sys.version || '—') + (sys.mock ? ' (mock)' : ''))));
  statusBits.push(el('div', { class: 'dash-row' },
    el('span', { class: 'lbl' }, 'Uptime'),
    el('span', { class: 'val' }, _humanUptime(sys.uptime))));
  statusBits.push(el('div', { class: 'dash-row' },
    el('span', { class: 'lbl' }, 'rclone'),
    el('span', { class: 'val' }, sys.rclone ? 'present' : 'NOT FOUND')));
  root.append(_panel('🤖 Status', statusBits));

  // -- Drives panel --
  const drivesBits = [];
  drivesBits.push(el('div', { class: 'dash-stat' },
    el('span', { class: 'num' }, String(online.length)),
    el('span', { class: 'lbl' }, `of ${adopted.length} adopted online${offline.length ? ` · ${offline.length} offline` : ''}`),
  ));
  if (!adopted.length) {
    drivesBits.push(el('div', { class: 'dash-empty' }, 'No drives adopted yet. Open the Drives tab to add one.'));
  } else {
    online.slice(0, 4).forEach(d => {
      const used = (d.size_bytes || 0) - (d.free_bytes || 0);
      drivesBits.push(_bar(used, d.size_bytes || 0,
        { left: (d.label || d.uuid) + (d.primary ? ' ★' : '') }));
    });
  }
  root.append(_panel('💾 Drives', drivesBits, { click: () => _switchTab('drives') }));

  // -- Memory & swap panel --
  const memBits = [];
  const memUsed = Math.max(0, (sys.mem_total || 0) - (sys.mem_free || 0));
  memBits.push(_bar(memUsed, sys.mem_total || 0, { left: 'RAM' }));
  if (sys.swap_active) {
    memBits.push(_bar(sys.swap_used || 0, sys.swap_total || 0, { left: 'Swap' }));
    const devs = (sys.swap_devices || []).map(d => d.device).join(', ');
    if (devs) memBits.push(el('div', { class: 'dash-row' },
      el('span', { class: 'lbl' }, 'Backing'), el('span', { class: 'val' }, devs)));
  } else {
    memBits.push(el('div', { class: 'dash-row' },
      el('span', { class: 'lbl' }, 'Swap'), el('span', { class: 'val' }, 'disabled')));
  }
  memBits.push(el('div', { class: 'dash-row' },
    el('span', { class: 'lbl' }, 'Load avg'),
    el('span', { class: 'val' }, (sys.loadavg || []).join(' ') || '—')));
  root.append(_panel('🧠 Memory', memBits, { click: () => _switchTab('system') }));

  // -- Accounts panel --
  const acctBits = [];
  acctBits.push(el('div', { class: 'dash-stat' },
    el('span', { class: 'num', style: remotesBad.length ? 'color:var(--err)' : '' }, String(remotes.length)),
    el('span', { class: 'lbl' }, `account${remotes.length === 1 ? '' : 's'}${remotesBad.length ? ` · ${remotesBad.length} need attention` : ''}`),
  ));
  if (!remotes.length) {
    acctBits.push(el('div', { class: 'dash-empty' }, 'No cloud accounts connected.'));
  } else {
    remotes.slice(0, 6).forEach(r => {
      const cls = r.needs_reauth ? 'err' : r.health === 'ok' ? 'ok' : r.health === 'error' ? 'err' : 'warn';
      acctBits.push(el('div', { class: 'dash-row' },
        el('span', { class: 'lbl' }, r.name || r.provider),
        el('span', { class: 'pill ' + cls }, r.needs_reauth ? 'reauth' : (r.health || 'unknown')),
      ));
    });
  }
  root.append(_panel('☁️ Accounts', acctBits, { click: () => _switchTab('remotes') }));

  // -- Sync activity panel (running + queued jobs) --
  const syncBits = [];
  syncBits.push(el('div', { class: 'dash-stat' },
    el('span', { class: 'num' }, String(running.length)),
    el('span', { class: 'lbl' }, `running${queued.length ? ` · ${queued.length} queued` : ''}`),
  ));
  if (!running.length && !queued.length) {
    syncBits.push(el('div', { class: 'dash-empty' }, 'Idle. No syncs running right now.'));
  } else {
    const list = el('div', { class: 'dash-jobs' });
    running.concat(queued).slice(0, 6).forEach(j => {
      list.append(el('div', { class: 'job-row' },
        el('span', { class: 'name' }, `${j.type === 'download' ? '⬇️' : '⬆️'} ${j.label || j.id}`),
        el('span', { class: 'progress' }, j.state === 'running'
          ? `${j.progress || 0}%${j.eta ? ` · ${j.eta}` : ''}`
          : j.state),
      ));
    });
    syncBits.push(list);
  }
  syncBits.push(el('div', { class: 'dash-row', style: 'margin-top:6px' },
    el('span', { class: 'lbl' }, `${downloads.filter(d => d.state !== 'paused').length}/${downloads.length} downloads active`),
    el('span', { class: 'val' }, `${uploads.filter(u => u.state !== 'paused').length}/${uploads.length} uploads active`),
  ));
  root.append(_panel('🔄 Sync activity', syncBits, { click: () => _switchTab('downloads') }));

  // -- Recent activity feed (sync_log) --
  const sl = STATE.sync_log || {};
  const merged = [].concat(
    (sl.download || []).map(e => ({ ...e, type: 'download' })),
    (sl.upload || []).map(e => ({ ...e, type: 'upload' })),
  ).sort((a, b) => (b.finished_at || b.started_at || 0) - (a.finished_at || a.started_at || 0))
   .slice(0, 8);
  const feedBits = [];
  if (!merged.length) {
    feedBits.push(el('div', { class: 'dash-empty' }, 'No sync runs yet. Add a download or upload to get started.'));
  } else {
    const feed = el('div', { class: 'dash-feed' });
    merged.forEach(e => {
      const cls = e.state === 'done' ? 'ok' : e.state === 'error' ? 'err' : e.state === 'cancelled' ? 'warn' : 'muted';
      feed.append(el('div', { class: 'row' },
        el('span', { class: 'when', title: _fmtAbs(e.finished_at || e.started_at) }, since(e.finished_at || e.started_at)),
        el('span', { class: 'what' }, `${e.type === 'download' ? '⬇️' : '⬆️'} ${e.label || '—'}${e.bytes ? ` · ${human(e.bytes)}` : ''}${e.error ? ` · ${e.error}` : ''}`),
        el('span', { class: 'pill ' + cls }, e.state || '?'),
      ));
    });
    feedBits.push(feed);
  }
  root.append(_panel('📜 Recent activity', feedBits, { wide: true }));
};

const _dashRefresh = $('#dashRefresh');
if (_dashRefresh) _dashRefresh.addEventListener('click', refreshState);
const _dashSyncAll = $('#dashSyncAll');
if (_dashSyncAll) _dashSyncAll.addEventListener('click', async () => {
  if (!confirm('Run every active download and upload now?')) return;
  toast('Queuing all active syncs…');
  let n = 0;
  for (const [id, d] of Object.entries(STATE.downloads || {})) {
    if (d.state === 'active') { await api(`/api/downloads/${id}/sync`, { method: 'POST' }); n++; }
  }
  for (const [id, u] of Object.entries(STATE.uploads || {})) {
    if (u.state !== 'paused') { await api(`/api/uploads/${id}/sync`, { method: 'POST' }); n++; }
  }
  toast(n ? `Queued ${n} sync${n === 1 ? '' : 's'}` : 'Nothing to sync');
  refreshState();
});

// ----- PROJECTS -----
// Lightweight grouping. The slug shown next to the name is what the daemon
// will use as the on-disk folder under downloads/ and uploads/<provider>/.
// We compute the slug client-side too just for the live preview; the
// authoritative slug is whatever the daemon writes back in STATE.projects.
const _slugifyClient = (s) => (s || '')
  .trim()
  .replace(/\s+/g, '-')
  .replace(/[^A-Za-z0-9_.\-]/g, '_')
  .replace(/^[._-]+|[._-]+$/g, '')
  .slice(0, 80);

const populateProjectSelect = (sel, currentId) => {
  if (!sel) return;
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, '— none —'));
  Object.entries(STATE.projects || {}).forEach(([pid, p]) => {
    const opt = el('option', { value: pid }, `${p.name} (${p.slug})`);
    if (pid === currentId) opt.selected = true;
    sel.append(opt);
  });
};

// ----- Multi-project tagging widget -----
// Each form (download add / upload add / entry edit) has a primary <select>
// plus a secondary <select> + "Tag" button + chip area. The chips area data
// store is the source of truth for the *additional* project ids (the primary
// is read separately at submit). At submit we POST/PATCH project_ids =
// [primary, ...chips] (deduped, primary head); the daemon mirrors the synced
// folder into each additional project's folder after each successful sync.
const _populateExtraSelect = (sel) => {
  if (!sel) return;
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, '— pick a project to tag —'));
  Object.entries(STATE.projects || {}).forEach(([pid, p]) => {
    sel.append(el('option', { value: pid }, `${p.name} (${p.slug})`));
  });
};
const _renderTagChips = (chipsEl, ids) => {
  if (!chipsEl) return;
  chipsEl.innerHTML = '';
  const projects = STATE.projects || {};
  (ids || []).forEach((pid) => {
    const p = projects[pid];
    const chip = el('span', { class: 'pill', style: 'margin-right:6px;cursor:default;display:inline-flex;align-items:center;gap:4px' },
      p ? `${p.name}` : '(missing)',
      el('button', {
        type: 'button',
        class: 'btn-link',
        title: 'Remove tag',
        style: 'padding:0 4px;line-height:1',
        on: { click: () => {
          const cur = (chipsEl.dataset.ids || '').split(',').filter(Boolean);
          const next = cur.filter(x => x !== pid);
          chipsEl.dataset.ids = next.join(',');
          _renderTagChips(chipsEl, next);
        } },
      }, '✕'),
    );
    chipsEl.append(chip);
  });
};
const _setExtraChipIds = (chipsEl, ids) => {
  if (!chipsEl) return;
  const dedup = Array.from(new Set((ids || []).filter(Boolean)));
  chipsEl.dataset.ids = dedup.join(',');
  _renderTagChips(chipsEl, dedup);
};
const _getExtraChipIds = (chipsEl) => {
  if (!chipsEl) return [];
  return (chipsEl.dataset.ids || '').split(',').filter(Boolean);
};
const _wireExtraTagAdd = (btnId, extraSelId, primarySelId, chipsId) => {
  const btn = $(btnId);
  if (!btn) return;
  btn.addEventListener('click', () => {
    const sel = $(extraSelId);
    const primary = $(primarySelId);
    const chipsEl = $(chipsId);
    if (!sel || !chipsEl) return;
    const pid = sel.value;
    if (!pid) return toast('Pick a project to tag first', true);
    if (primary && primary.value === pid) return toast('That project is already the primary', true);
    const cur = _getExtraChipIds(chipsEl);
    if (cur.includes(pid)) return toast('Already tagged with that project', true);
    _setExtraChipIds(chipsEl, [...cur, pid]);
    sel.value = '';
  });
};
// Combine primary + chips into the project_ids array we send to the daemon.
const _projectIdsFromForm = (primarySelId, chipsId) => {
  const primary = $(primarySelId);
  const chipsEl = $(chipsId);
  const extras = _getExtraChipIds(chipsEl);
  if (!primary || !primary.value) return extras;
  return Array.from(new Set([primary.value, ...extras]));
};
// datetime-local <-> epoch seconds. Empty input => null (no auto-delete).
const _datetimeLocalToEpoch = (val) => {
  if (!val) return null;
  // <input type="datetime-local"> emits "YYYY-MM-DDTHH:MM" in *local* time.
  const t = Date.parse(val);
  if (isNaN(t)) return null;
  return Math.floor(t / 1000);
};
const _epochToDatetimeLocal = (epoch) => {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  if (isNaN(d.getTime())) return '';
  // Build YYYY-MM-DDTHH:MM in local time without tz suffix.
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const _renderProjectTagsCell = (item) => {
  const projects = STATE.projects || {};
  const ids = (item && item.project_ids && item.project_ids.length)
    ? item.project_ids
    : (item && item.project_id ? [item.project_id] : []);
  if (!ids.length) return el('td', {}, '—');
  const td = el('td', {});
  ids.forEach((pid, i) => {
    const p = projects[pid];
    const chip = el('span', { class: 'pill ' + (i === 0 ? 'ok' : 'muted'), style: 'margin-right:4px', title: i === 0 ? 'Primary project' : 'Mirror tag' }, p ? p.name : '(missing)');
    td.append(chip);
  });
  return td;
};

// Projects no longer have their own tab — creation happens inline from the
// Downloads / Uploads add panels and from the entry edit dialog. The select
// next to each "+ New project" button is repopulated by populateProjectSelect
// each time renderDownloads/renderUploads runs, so we just need a tiny
// helper that prompts for a name, POSTs it, refreshes state, and selects
// the newly-created id in the originating dropdown.
const _inlineNewProject = async (sel) => {
  const name = (prompt('Project name:') || '').trim();
  if (!name) return;
  const r = await api('/api/projects', { method: 'POST', body: { name } });
  if (!r || r.ok === false) {
    toast((r && r.error) || 'Create failed', true);
    return;
  }
  toast(`Project created (slug: ${r.slug})`);
  await refreshState();
  if (sel && r.id) sel.value = r.id;
};
const _wireInlineNewProject = (btnId, selId) => {
  const btn = $(btnId);
  if (btn) btn.addEventListener('click', () => _inlineNewProject($(selId)));
};
_wireInlineNewProject('#dlProjectNew', '#dlProject');
_wireInlineNewProject('#upProjectNew', '#upProject');
_wireInlineNewProject('#entryEditProjectNew', '#entryEditProject');
_wireExtraTagAdd('#dlProjectExtraAdd', '#dlProjectExtra', '#dlProject', '#dlProjectExtraChips');
_wireExtraTagAdd('#upProjectExtraAdd', '#upProjectExtra', '#upProject', '#upProjectExtraChips');
_wireExtraTagAdd('#entryEditProjectExtraAdd', '#entryEditProjectExtra', '#entryEditProject', '#entryEditProjectExtraChips');

const renderAll = () => {
  if (!STATE) return;
  renderStatus();
  renderAlerts();
  renderMasterBanner();
  renderDashboard();
  renderSetup();
  renderDrives();
  renderFiles();
  renderDownloads();
  renderUploads();
  renderSyncLog('download');
  renderSyncLog('upload');
  renderRemotes();
  renderSharing();
  renderNotifications();
  renderSettings();
  renderSystem();
  renderRcloneStatus();
  renderConcurrencySelectors();
  renderFooterVersion();
};

// ----- CONCURRENCY (per-type job caps) -----
const renderConcurrencySelectors = () => {
  const lim = (STATE.limits || {});
  const dl = parseInt(lim.download_concurrency || lim.max_concurrent_jobs || 1, 10);
  const ul = parseInt(lim.upload_concurrency   || lim.max_concurrent_jobs || 1, 10);
  const dlSel = $('#dlConcurrency'); if (dlSel && document.activeElement !== dlSel) dlSel.value = String(Math.max(1, Math.min(8, dl)));
  const upSel = $('#upConcurrency'); if (upSel && document.activeElement !== upSel) upSel.value = String(Math.max(1, Math.min(8, ul)));
};
async function _setConcurrency(field, value) {
  const r = await api('/api/limits', { method: 'PATCH', body: { [field]: parseInt(value, 10) } });
  if (r && r.ok) {
    toast(`${field === 'download_concurrency' ? 'Downloads' : 'Uploads'}: ${value} at a time`);
    refreshState();
  } else {
    toast('Could not update concurrency', true);
  }
}
$('#dlConcurrency') && $('#dlConcurrency').addEventListener('change', e => _setConcurrency('download_concurrency', e.target.value));
$('#upConcurrency') && $('#upConcurrency').addEventListener('change', e => _setConcurrency('upload_concurrency',   e.target.value));

const renderFooterVersion = () => {
  const v = (STATE.system && STATE.system.version) || '';
  const fv = $('#footerVersion');
  if (fv) fv.textContent = v ? `BOT-SYNC v${v}` : '';
  const hv = $('#helpVersion');
  if (hv) hv.textContent = v ? `v${v}` : '';
  renderFooterStats();
  renderDocsOnce();
};

const renderFooterStats = () => {
  const fs = $('#footerStats'); if (!fs) return;
  const s = STATE.system || {};
  const jobs = STATE.jobs || [];
  const active = jobs.filter(j => j.state === 'running').length;
  const queued = jobs.filter(j => j.state === 'queued').length;
  const load = (s.loadavg && s.loadavg[0]) || '?';
  const ramFree = human(s.mem_free);
  const ramTot = human(s.mem_total);
  const dskFree = human(s.root_free);
  const dskTot = human(s.root_total);
  // Warn when free RAM dips below ~30 MB (router has 256 MB total).
  const lowRam = (s.mem_free || 0) > 0 && s.mem_free < 30 * 1024 * 1024;
  fs.classList.toggle('low-ram', lowRam);
  fs.textContent =
    `RAM ${ramFree} / ${ramTot}` +
    `  ·  Disk ${dskFree} / ${dskTot}` +
    `  ·  Load ${load}` +
    `  ·  Jobs ${active} running` + (queued ? ` (${queued} queued)` : '');
};

let DOCS_RENDERED = false;
const renderDocsOnce = () => {
  if (DOCS_RENDERED) return;
  const root = $('#docsPanel');
  if (!root) return;
  root.innerHTML = DOCS_HTML;
  DOCS_RENDERED = true;
};

const DOCS_HTML = `
<h3>What is BOT-SYNC?</h3>
<p>BOT-SYNC is a tiny self-hosted appliance that lives on your OpenWrt router
(primary target: GL-iNet GL-A1300) and keeps a USB drive in sync with remote
folders. Supported sources include
<strong>Google Drive, Dropbox, Box, OneDrive, FTP / FTPS, SFTP (SSH), and
plain HTTP</strong> folder listings. The drive is then re-shared back to
your LAN over SMB and Bonjour so any device on the network can read or
write the files transparently.</p>
<p>FTP, FTPS and SFTP behave like the cloud backends \u2014 you connect once on
the <strong>\u2601\ufe0f Accounts</strong> tab with host / user / password (or SSH
key), then point a Download or Upload at a remote path and pick a sync
interval. No OAuth required.</p>
<p>Almost everything (the daemon, the rclone binary, the state file, your
synced folders) lives on the USB drive. The router's flash only carries an
init script, a UCI config, a hotplug hook, and an optional firewall include
for the friendly <code>http://bot.sync/</code> hostname.</p>

<h3>How a sync works</h3>
<ol>
  <li>You plug in a USB drive. BOT-SYNC <em>adopts</em> it (writes a marker file).</li>
  <li>You connect an account on the <strong>\u2601\ufe0f Accounts</strong> tab \u2014 OAuth for Drive / Dropbox / Box / OneDrive, or plain credentials for FTP / FTPS / SFTP / HTTP. Each account becomes an rclone remote.</li>
  <li>You add a <strong>Download</strong> (paste a folder URL or remote path) or an <strong>Upload</strong> (pick a local sub-folder).</li>
  <li>You pick a <em>Sync interval</em> (Manual / 1 min \u2026 24 h / Custom seconds). The autosync loop re-fires the entry whenever the interval elapses.</li>
  <li>You hit <em>Sync</em> (or wait for the schedule). rclone runs in the background; progress and logs are visible under <strong>\ud83d\udee0\ufe0f System \u2192 Active jobs</strong>.</li>
  <li>The synced folders are exposed on the LAN as SMB shares (\\\\&lt;router-ip&gt;\\BOT-SYNC).</li>
</ol>

<h3>Supported providers</h3>
<table class="data">
  <thead><tr><th>Provider</th><th>Auth</th><th>Watch</th><th>Push</th></tr></thead>
  <tbody>
    <tr><td>Google Drive</td><td>OAuth (your client_id/secret)</td><td>\u2705</td><td>\u2705</td></tr>
    <tr><td>Dropbox</td><td>OAuth</td><td>\u2705 (see Dropbox shared links note)</td><td>\u2705</td></tr>
    <tr><td>Box</td><td>OAuth (rclone built-in)</td><td>\u2705</td><td>\u2705</td></tr>
    <tr><td>OneDrive</td><td>OAuth (rclone built-in)</td><td>\u2705</td><td>\u2705</td></tr>
    <tr><td>FTP / FTPS</td><td>host + user + password (+ TLS mode)</td><td>\u2705</td><td>\u2705</td></tr>
    <tr><td>SFTP (SSH)</td><td>host + user + password <em>or</em> private key</td><td>\u2705</td><td>\u2705</td></tr>
    <tr><td>HTTP</td><td>none / Basic</td><td>\u2705</td><td>read-only</td></tr>
  </tbody>
</table>

<h3>Master switches (Settings)</h3>
<ul>
  <li><strong>BOT-SYNC enabled</strong> \u2014 the master kill switch. Off = nothing runs.</li>
  <li><strong>\u2b07\ufe0f Downloads enabled</strong> \u2014 pause cloud-to-drive transfers without disabling uploads or notifications.</li>
  <li><strong>\u2b06\ufe0f Uploads enabled</strong> \u2014 pause drive-to-cloud transfers (useful if you don't want to push partial recordings, etc.).</li>
</ul>
<p class="hint">When a switch is off, the corresponding <em>Sync</em> button returns an error rather than queuing a job. Existing files on the drive and remote are never deleted by toggling these.</p>

<h3>Accounts</h3>
<p>BOT-SYNC speaks rclone under the hood. <strong>Cloud accounts</strong>
(Drive / Dropbox / Box / OneDrive) use a device OAuth flow that you complete
in your normal browser \u2014 no need to expose the router to the internet.
<strong>FTP, FTPS and SFTP</strong> are added inline: enter host, port, user,
password (or upload an SSH private key for SFTP), pick a TLS mode for FTPS,
and save \u2014 the credentials are written into <code>rclone.conf</code> on the
USB drive. <strong>HTTP</strong> only needs the URL (and optional Basic auth).
Per-account health is checked periodically (look for the green/red dot on
the topbar).</p>

<h3>Downloads vs. Uploads</h3>
<table class="data">
  <thead><tr><th></th><th>Download</th><th>Upload</th></tr></thead>
  <tbody>
    <tr><td>Direction</td><td>cloud \u2192 USB drive</td><td>USB drive \u2192 cloud</td></tr>
    <tr><td>Trigger</td><td>folder URL</td><td>local sub-folder name</td></tr>
    <tr><td>Modes</td><td>copy (additive)</td><td>push, mirror, bisync</td></tr>
    <tr><td>Resync</td><td>wipes local + re-pulls</td><td>n/a</td></tr>
  </tbody>
</table>

<h3>USB drive lifecycle</h3>
<ul>
  <li><strong>Adopt</strong> writes <code>.botsync_marker</code> to the drive root and registers it in state. Multiple drives are supported; one is flagged primary.</li>
  <li><strong>Pause</strong> cancels active syncs against this drive and refuses new ones, without unmounting. Useful if you want to access the drive over SMB at full speed.</li>
  <li><strong>Eject</strong> auto-pauses, flushes pending writes, then unmounts. Always eject before pulling the drive. Re-mounting it (via Mount or hotplug) auto-resumes.</li>
  <li><strong>Forget</strong> removes the drive from the pool but leaves files in place.</li>
  <li>Hotplug auto-mounts adopted drives on next plug-in (init script runs the daemon if a marker is present).</li>
</ul>

<h3>Sharing</h3>
<p>Toggleable on the <strong>\ud83d\udce1 Sharing</strong> tab \u2014 SMB (samba), Bonjour (avahi),
optional NFS, optional guest read-only access. The default is SMB+Bonjour
with a guest user that can read everything; change the credentials before
exposing the share to untrusted clients.</p>

<h3>Notifications</h3>
<p>Configure one or more channels (Discord, Slack, generic webhook, ntfy, email)
on the <strong>\ud83d\udd14 Notifications</strong> tab. Each channel can subscribe to a subset
of event types (job started/completed/failed, drive online/offline, etc.) and
a minimum severity. The 12 currently emitted events are listed in the channel
editor.</p>
<h4>SMTP email</h4>
<p>The <em>Email (SMTP)</em> channel speaks to any standards-compliant SMTP
server — Gmail, Outlook, iCloud, Fastmail, Zoho, your own Postfix, etc.
Fields:</p>
<ul>
  <li><strong>SMTP host</strong> — e.g. <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>, <code>smtp.fastmail.com</code>.</li>
  <li><strong>SMTP port</strong> — <code>587</code> for STARTTLS, <code>465</code> for SSL/implicit TLS, <code>25</code> for plaintext.</li>
  <li><strong>TLS mode</strong> — STARTTLS (default, port 587), SSL (port 465), or None.</li>
  <li><strong>Username / password</strong> — your SMTP creds. For Gmail / iCloud / Outlook, generate an <em>app password</em> rather than using your account password.</li>
  <li><strong>From / To</strong> — envelope addresses.</li>
  <li><strong>Subject prefix</strong> — optional, defaults to <code>BOT-SYNC</code>. Subject becomes <code>[<prefix> <severity>] <event.type></code>.</li>
</ul>
<p>Hit <em>Send test</em> on the channel row to fire a synthetic event. Errors
(bad creds, blocked port, wrong TLS mode) are surfaced inline and stored
as the channel's <code>last_error</code>.</p>
<h3>Friendly hostname (http://bot.sync/)</h3>
<p>The installer wires up <code>bot.sync</code> as a port-free LAN hostname. Three pieces:</p>
<ol>
  <li>An alias IP (default <code>&lt;lan&gt;.244</code>) on <code>br-lan</code>.</li>
  <li>A dnsmasq <code>/bot.sync/&lt;alias-ip&gt;</code> entry.</li>
  <li>An iptables PREROUTING REDIRECT from port 80 on the alias IP to the daemon's port (8585).</li>
</ol>
<p class="hint">Pass <code>--no-hostname</code> to <code>setup.sh</code> if you'd rather stick with <code>http://&lt;router-ip&gt;:8585/</code>.</p>

<h3>Security notes</h3>
<ul>
  <li>HTTP only \u2014 there is no HTTPS terminator. LAN-only by design.</li>
  <li>Change the bootstrap admin password on first run (Setup wizard step 1).</li>
  <li>OAuth tokens are stored in <code>rclone.conf</code> on the USB drive. Treat the drive as a secret.</li>
  <li>The master <em>BOT-SYNC enabled</em> switch is the safest panic button.</li>
</ul>

<h3>Where files live</h3>
<table class="data">
  <thead><tr><th>Path</th><th>Purpose</th></tr></thead>
  <tbody>
    <tr><td><code>/etc/init.d/botsync</code></td><td>procd init script</td></tr>
    <tr><td><code>/etc/config/botsync</code></td><td>UCI config (master switch, port, creds, hostname)</td></tr>
    <tr><td><code>/etc/firewall.botsync</code></td><td>fw3 include for the bot.sync redirect</td></tr>
    <tr><td><code>/etc/hotplug.d/block/90-botsync</code></td><td>USB hotplug hook</td></tr>
    <tr><td><code>&lt;usb&gt;/bin/botsyncd.py</code> + <code>ui/</code></td><td>daemon + web UI</td></tr>
    <tr><td><code>&lt;usb&gt;/etc/botsync.json</code></td><td>persistent state (atomically written)</td></tr>
    <tr><td><code>&lt;usb&gt;/etc/rclone.conf</code></td><td>cloud account credentials</td></tr>
    <tr><td><code>&lt;usb&gt;/var/log/botsync.log</code></td><td>rolling log</td></tr>
    <tr><td><code>&lt;usb&gt;/downloads/&lt;provider&gt;/...</code></td><td>downloaded folder content</td></tr>
    <tr><td><code>&lt;usb&gt;/uploads/&lt;provider&gt;/...</code></td><td>folders that get pushed up</td></tr>
  </tbody>
</table>

<h3>Master kill switch from the shell</h3>
<pre>uci set botsync.main.enabled=0; /etc/init.d/botsync stop
uci set botsync.main.enabled=1; /etc/init.d/botsync start</pre>

<h3>Uninstall</h3>
<pre>sh /tmp/botsync-install/setup.sh --uninstall</pre>
<p>Removes the init script, UCI config, hotplug hook, dnsmasq entry, firewall
rule + include, and alias IP. The USB drive is left untouched.</p>

<h3>Changelog</h3>
<dl>
  <dt><strong>v0.7.10</strong> (current)</dt>
  <dd>
    <strong>Schedule column on Downloads & Uploads.</strong> Each row now
    has a Schedule cell next to <em>Last sync</em> showing the configured
    interval (e.g. <em>every 5 min</em> / <em>Manual</em>) plus a live
    countdown to the next scheduled run (<em>in 2m 15s</em> / <em>due
    now</em> / <em>pending first run</em> / <em>paused</em>). Updates on
    the existing 3-second state poll. Cross-platform installer
    <code>install/install.py</code> covers Pi / Linux / macOS / Windows
    in addition to the OpenWrt router \u2014 use <code>--print-only</code>
    to preview the install plan from any host.
  </dd>
  <dt><strong>v0.7.9</strong></dt>
  <dd>
    <strong>Per-entry sync interval.</strong> Every download and upload
    now has a <em>Sync interval</em> dropdown (Manual / 1 min / 5 / 15 /
    30 / 1 h / 6 h / 24 h / Custom seconds). The autosync loop re-fires
    each entry whenever <code>now - last_sync &gt;= interval</code>.
  </dd>
  <dt><strong>v0.7.8</strong></dt>
  <dd>
    <strong>Default account per provider.</strong> The first account you
    connect for each provider becomes the default; pasting a share link
    in Add Download auto-picks the matching default account. Use the
    <em>\u2605 default</em> pill / <em>Make default</em> button on the
    Accounts tab to change.
  </dd>
  <dt><strong>v0.7.0\u20130.7.7</strong></dt>
  <dd>
    Eject UX with OS+filesystem-aware warnings, USB hotplug watchdog
    (auto-pause on primary missing), Files browser improvements, lockup
    mitigation, sharing/notification hardening. See the project README
    for the full release notes.
  </dd>
  <dt><strong>v0.5.0</strong></dt>
  <dd>
    Hardening &amp; reliability pass. <code>procd</code> respawn tightened to
    unlimited retries with a 60s/5s window. New <strong>cron watchdog</strong>
    (<code>/usr/sbin/botsync-watchdog</code>) pings <code>/api/watchdog/ping</code>
    every minute and force-restarts the daemon after 3 consecutive failures
    so a hung process is recovered. Internal <strong>30s heartbeat</strong>
    written to <code>&lt;usb&gt;/var/run/botsyncd.heartbeat</code>. New
    <strong>stuck-job watchdog</strong> auto-cancels jobs running longer
    than the configurable timeout (default 6h) and fires
    <code>job.stuck</code>. <strong>Crash recovery</strong>: a
    <code>botsyncd.running</code> marker detects unclean shutdowns at the
    next start and emits <code>system.crash_recovered</code> /
    <code>system.watchdog_restart</code>; jobs left in <em>running</em>
    state are reset to <em>error</em> with <code>job.interrupted</code>.
    Top-level <code>sys.excepthook</code> writes a numbered crash log to
    <code>&lt;usb&gt;/var/log/crash/</code> and emits <code>system.error</code>
    before procd respawns. Settings tab gains a <strong>Reliability &amp;
    watchdog</strong> panel (PID, uptime, last heartbeat, watchdog status)
    and a stuck-job timeout input.
  </dd>
  <dt><strong>v0.4.0</strong></dt>
  <dd>
    SMTP email notification channel hardened: explicit <strong>TLS mode</strong>
    selector (STARTTLS / SSL / None), implicit TLS support on port 465
    (Gmail / iCloud / Outlook app-password friendly), customisable subject
    prefix, automatic mode detection when port 465 is set, and proper
    <code>ssl.create_default_context()</code> usage. The existing <em>Send test</em>
    button now exercises the same code path real notifications take.
  </dd>
  <dt><strong>v0.3.0</strong></dt>
  <dd>
    Added independent Uploads / Downloads master switches in Settings.
    Added the in-app <strong>\ud83d\udcd6 Help</strong> tab and a "Read the docs" button on the
    welcome modal. Version is now displayed in the footer and on the Help
    page header. Daemon now deep-merges defaults so new settings are
    picked up on existing state files.
  </dd>
  <dt><strong>v0.2.0</strong></dt>
  <dd>
    Friendly hostname <code>http://bot.sync/</code> via alias IP +
    dnsmasq + iptables PREROUTING REDIRECT (replaces the earlier and broken
    string-match approach). Model-aware installer with
    <code>--model {gl-a1300|generic|auto}</code>, <code>--hostname</code>,
    <code>--no-hostname</code>, <code>--port</code>, <code>--uninstall</code>.
    Minimum OpenWrt 19.07. Comprehensive
    <a href="https://github.com/" target="_blank">INSTRUCTIONS.md</a> and refreshed README.
  </dd>
  <dt><strong>v0.1.0</strong></dt>
  <dd>
    Initial rebrand from cloudsync. USB-drive-backed daemon, OAuth-based
    cloud accounts, downloads + uploads, SMB / Bonjour / NFS sharing,
    notifications (Discord / Slack / webhook / ntfy / email), setup wizard,
    welcome modal, mock mode for desktop development.
  </dd>
</dl>

<h3>License &amp; acknowledgements</h3>
<p>BOT-SYNC is released under the
<a href="https://opensource.org/licenses/MIT" target="_blank" rel="noopener">MIT License</a>.
The daemon itself is pure Python 3 standard library plus vanilla
HTML/CSS/JS &mdash; no bundled third-party packages.</p>
<p><strong>Massive thanks to the
<a href="https://rclone.org" target="_blank" rel="noopener">rclone</a>
project</strong> (MIT, &copy; Nick Craig-Wood and contributors). Every
cloud / FTP / FTPS / SFTP / HTTP transfer in BOT-SYNC is implemented as
an <code>rclone</code> subprocess call. The installer downloads an
unmodified upstream rclone binary from
<code>downloads.rclone.org</code> onto your USB drive at install time;
nothing is forked or redistributed.</p>
<p>BOT-SYNC also rides on top of the
<a href="https://openwrt.org" target="_blank" rel="noopener">OpenWrt</a>
platform tools (UCI, dnsmasq, Samba, mDNSResponder, iptables, nfsd) and
calls the Google Drive / Dropbox / Box / OneDrive APIs through rclone &mdash;
use of those APIs is governed by each vendor's Terms of Service.
Trademarks belong to their respective owners. Full attribution lives in
<code>THIRD_PARTY_LICENSES.md</code> in the repository.</p>
`;

// ----- master switch banner -----
const renderMasterBanner = () => {
  const enabled = (STATE.settings && STATE.settings.enabled !== false);
  const banner = $('#masterBanner');
  if (enabled) {
    banner.classList.add('hidden');
  } else {
    banner.classList.remove('hidden');
    banner.classList.add('off');
    $('#masterStateLabel').textContent = 'disabled';
    $('#masterSwitch').checked = false;
  }
};
$('#masterSwitch').addEventListener('change', async (e) => {
  const r = await api('/api/settings', { method: 'PATCH', body: { enabled: e.target.checked } });
  if (r.ok) toast('BOT-SYNC ' + (e.target.checked ? 'enabled' : 'disabled'));
  refreshState();
});

// ----- SETUP wizard -----
const renderSetup = () => {
  const root = $('#setupSteps'); if (!root) return;
  const s = STATE.settings || {};
  const drives = (STATE.drives_live || []).filter(d => d.adopted);
  const remotes = Object.keys(STATE.remotes || {});
  const channels = Object.keys((STATE.notifications || {}).channels || {});
  const passwordChanged = !!(s.setup_complete);
  const steps = [
    { num: 1, icon: '🔐', title: 'Change admin password',
      done: passwordChanged,
      body: passwordChanged
        ? 'Done. Visit Settings to change again.'
        : 'Open the Settings tab and replace the bootstrap password.',
      action: passwordChanged ? null : { label: 'Open Settings', tab: 'settings' } },
    { num: 2, icon: '💾', title: 'Adopt a USB drive',
      done: drives.length > 0,
      body: drives.length
        ? `${drives.length} drive(s) adopted: ${drives.map(d => d.label || d.uuid).join(', ')}.`
        : 'Plug a USB drive into the router and adopt it on the Drives tab.',
      action: drives.length ? null : { label: 'Open Drives', tab: 'drives' } },
    { num: 3, icon: '☁️', title: 'Connect a cloud account',
      done: remotes.length > 0,
      body: remotes.length
        ? `Connected: ${remotes.join(', ')}.`
        : 'Sign in to Google Drive or Dropbox on the Accounts tab.',
      action: remotes.length ? null : { label: 'Open Accounts', tab: 'remotes' } },
    { num: 4, icon: '📁', title: 'Add a folder to sync',
      done: Object.keys(STATE.downloads || {}).length > 0 || Object.keys(STATE.uploads || {}).length > 0,
      body: 'Add a Google Drive / Dropbox folder URL on Downloads, or pick a local folder to push on Uploads.',
      action: { label: 'Open Downloads', tab: 'downloads' } },
    { num: 5, icon: '🔔', title: 'Set up notifications',
      done: channels.length > 0,
      body: channels.length
        ? `${channels.length} channel(s) configured.`
        : 'Wire up Discord, email, ntfy or a webhook to get alerts.',
      action: channels.length ? null : { label: 'Open Notifications', tab: 'notifications' } },
    { num: 6, icon: '🎉', title: 'Mark setup complete',
      done: !!s.setup_complete,
      body: s.setup_complete ? 'Setup is complete. 🚀' : 'Click below when you are happy with the configuration.',
      action: s.setup_complete ? null : { label: 'Mark complete', fn: async () => {
        await api('/api/settings', { method: 'PATCH', body: { setup_complete: true } });
        refreshState();
      } } },
  ];
  root.innerHTML = '';
  let firstUndone = steps.findIndex(x => !x.done);
  steps.forEach((st, i) => {
    const cls = 'step ' + (st.done ? 'done' : (i === firstUndone ? 'current' : ''));
    const div = el('div', { class: cls },
      el('h3', {},
        el('span', { class: 'num' }, String(st.num)),
        el('span', { class: 'icon' }, st.icon || ''),
        st.title),
      el('p', { class: 'hint' }, st.body),
    );
    if (st.action) {
      const btn = el('button', { class: 'btn-secondary' }, st.action.label);
      btn.addEventListener('click', () => {
        if (st.action.tab) {
          const t = $$('.tab').find(x => x.dataset.tab === st.action.tab);
          if (t) t.click();
        } else if (st.action.fn) {
          st.action.fn();
        }
      });
      div.append(btn);
    }
    root.append(div);
  });
};

// ----- SETTINGS tab -----
const renderSettings = () => {
  const s = STATE.settings || {};
  $('#settingsEnabled').checked = s.enabled !== false;
  $('#settingsDownloadsEnabled').checked = s.downloads_enabled !== false;
  $('#settingsUploadsEnabled').checked = s.uploads_enabled !== false;
  $('#settingsSessionTtl').value = s.session_ttl_hours || 12;
  $('#settingsStuckJob').value = (s.stuck_job_hours === 0 ? 0 : (s.stuck_job_hours || 6));
  renderReliability();

  const all = STATE.providers_all || STATE.providers || {};
  const enabled = s.providers_enabled || {};
  const wrap = $('#providerToggles'); wrap.innerHTML = '';
  Object.entries(all).forEach(([key, info]) => {
    const row = el('div', { class: 'provider-row' + (enabled[key] ? '' : ' disabled') },
      el('span', { class: 'pname' }, info.label),
    );
    const sw = el('label', { class: 'switch' });
    const cb = el('input', { type: 'checkbox' });
    cb.checked = !!enabled[key];
    cb.addEventListener('change', async () => {
      const body = { providers_enabled: { [key]: cb.checked } };
      await api('/api/settings', { method: 'PATCH', body });
      refreshState();
    });
    sw.append(cb, el('span', { class: 'slider' }));
    row.append(sw);
    wrap.append(row);
  });

  $('#pwUser').value = STATE.auth_user || '';
  // Hardware preset section is fed by /api/performance, not /api/settings.
  refreshPerformance().catch(() => {});
};
$('#settingsEnabled').addEventListener('change', async (e) => {
  await api('/api/settings', { method: 'PATCH', body: { enabled: e.target.checked } });
  refreshState();
});
$('#settingsDownloadsEnabled').addEventListener('change', async (e) => {
  await api('/api/settings', { method: 'PATCH', body: { downloads_enabled: e.target.checked } });
  toast('Downloads ' + (e.target.checked ? 'enabled' : 'paused'));
  refreshState();
});
$('#settingsUploadsEnabled').addEventListener('change', async (e) => {
  await api('/api/settings', { method: 'PATCH', body: { uploads_enabled: e.target.checked } });
  toast('Uploads ' + (e.target.checked ? 'enabled' : 'paused'));
  refreshState();
});
$('#settingsSessionTtl').addEventListener('change', async (e) => {
  let v = parseInt(e.target.value, 10);
  if (!Number.isFinite(v)) v = 12;
  if (v < 1) v = 1;
  if (v > 720) v = 720;
  e.target.value = v;
  await api('/api/settings', { method: 'PATCH', body: { session_ttl_hours: v } });
  toast('Session timeout: ' + v + 'h (applies to next sign-in)');
  refreshState();
});
$('#settingsStuckJob').addEventListener('change', async (e) => {
  let v = parseInt(e.target.value, 10);
  if (!Number.isFinite(v)) v = 6;
  if (v < 0) v = 0;
  if (v > 168) v = 168;
  e.target.value = v;
  await api('/api/settings', { method: 'PATCH', body: { stuck_job_hours: v } });
  toast(v === 0 ? 'Stuck-job watchdog disabled' : ('Stuck-job timeout: ' + v + 'h'));
  refreshState();
});

// ----- Performance / hardware preset (Settings → ⚡ panel) -----
//
// /api/performance returns the full preset catalogue, the user's choice,
// the merged active values, the auto-detected RAM, and the live job
// concurrency the JobManager is enforcing. Re-rendered every time the
// Settings tab is opened. PATCH applies immediately — no daemon restart.
const refreshPerformance = async () => {
  let info;
  try {
    info = await api('/api/performance');
  } catch (e) {
    return;
  }
  const presets = info.presets || {};
  const active = info.preset || 'router';
  const detected = info.detected || {};
  const vals = info.active_values || {};
  const concur = info.concurrency || {};

  const det = $('#perfDetected');
  if (det) {
    const ram = detected.ram_mb ? (detected.ram_mb + ' MB') : 'unknown';
    const auto = detected.auto_choice || 'router';
    det.textContent = 'Detected RAM: ' + ram + ' — auto-recommended preset: ' + auto;
  }

  const wrap = $('#perfPresets');
  if (wrap) {
    wrap.innerHTML = '';
    const order = ['router', 'pi', 'desktop', 'custom'];
    order.forEach((key) => {
      const p = presets[key] || (key === 'custom'
        ? { label: 'Custom', description: 'Tune individual flags below.' }
        : null);
      if (!p) return;
      const card = el('label', { class: 'perf-card' + (active === key ? ' active' : '') });
      const radio = el('input', { type: 'radio', name: 'perfPreset', value: key });
      radio.checked = active === key;
      radio.addEventListener('change', async () => {
        try {
          await api('/api/performance', { method: 'PATCH', body: { preset: key } });
          toast('Preset applied: ' + key);
          refreshPerformance();
        } catch (e) {
          $('#perfMsg').textContent = 'Failed to apply preset: ' + (e.message || e);
        }
      });
      card.append(radio);
      card.append(el('span', { class: 'perf-label' }, p.label || key));
      card.append(el('div', { class: 'perf-desc' }, p.description || ''));
      if (key !== 'custom') {
        const flags = [
          'global=' + (p.max_global_jobs || '?'),
          'transfers=' + (p.transfers || '?'),
          'checkers=' + (p.checkers || '?'),
          'buffer=' + (p.buffer_size_mb || 0) + 'M',
          'mem=' + (p.rclone_mem_mb || '?') + 'M',
          (p.bwlimit_kbps ? ('bw=' + p.bwlimit_kbps + 'k') : 'bw=∞'),
        ].join('  ');
        card.append(el('div', { class: 'perf-flags' }, flags));
      }
      wrap.append(card);
    });
  }

  // Populate custom-overrides form with the *currently active* values so
  // the user can switch to "custom" and tweak from a sane starting point.
  const setIf = (id, v) => { const el = $(id); if (el && v !== undefined) el.value = v; };
  setIf('#perfMaxGlobal', vals.max_global_jobs);
  setIf('#perfTransfers', vals.transfers);
  setIf('#perfCheckers', vals.checkers);
  setIf('#perfBuffer', vals.buffer_size_mb);
  setIf('#perfMts', vals.multi_thread_streams);
  setIf('#perfBacklog', vals.max_backlog);
  setIf('#perfRetries', vals.low_level_retries);
  setIf('#perfMem', vals.rclone_mem_mb);
  setIf('#perfBw', vals.bwlimit_kbps);
  setIf('#perfNice', vals.nice);

  const activeMsg = $('#perfActive');
  if (activeMsg) {
    activeMsg.textContent = 'Live: ' + (concur.download || '?') + ' download lane(s), '
      + (concur.upload || '?') + ' upload lane(s), per-rclone child capped at '
      + (vals.rclone_mem_mb || '?') + ' MB.';
  }
};

const _saveCustomPerf = async () => {
  const intOr = (id, fb) => {
    const v = parseInt(($(id) || {}).value, 10);
    return Number.isFinite(v) && v >= 0 ? v : fb;
  };
  const body = {
    preset: 'custom',
    custom: {
      max_global_jobs:      intOr('#perfMaxGlobal', 1),
      transfers:            intOr('#perfTransfers', 2),
      checkers:             intOr('#perfCheckers', 2),
      buffer_size_mb:       intOr('#perfBuffer', 0),
      multi_thread_streams: intOr('#perfMts', 0),
      max_backlog:          intOr('#perfBacklog', 1000),
      low_level_retries:    intOr('#perfRetries', 3),
      rclone_mem_mb:        intOr('#perfMem', 128),
      bwlimit_kbps:         intOr('#perfBw', 0),
      nice:                 intOr('#perfNice', 5),
    },
  };
  try {
    const r = await api('/api/performance', { method: 'PATCH', body });
    if (r && r.ok === false) {
      $('#perfMsg').textContent = 'Error: ' + (r.error || 'unknown') + (r.fix ? ' — ' + r.fix : '');
      return;
    }
    toast('Custom performance overrides saved');
    refreshPerformance();
  } catch (e) {
    $('#perfMsg').textContent = 'Failed: ' + (e.message || e);
  }
};
const _perfSaveBtn = $('#perfSaveCustom');
if (_perfSaveBtn) _perfSaveBtn.addEventListener('click', _saveCustomPerf);

const _agoText = (ts) => {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
};
const _fmtUptime = (s) => {
  if (!s) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
};
const _fmtEta = (s) => {
  if (!s || s < 0 || !Number.isFinite(s)) return '—';
  s = Math.floor(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return m + 'm ' + sec + 's';
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 24) return h + 'h ' + mm + 'm';
  const d = Math.floor(h / 24), hh = h % 24;
  return d + 'd ' + hh + 'h';
};
const _fmtJobMeta = (job) => {
  const pct = (job.progress || 0).toFixed(0);
  const bytes = `${human(job.bytes_done)} / ${human(job.bytes_total)}`;
  const rate = job.transfer_rate ? `${human(job.transfer_rate)}/s` : '—';
  const eta = job.state === 'running' ? _fmtEta(job.eta_seconds) : '—';
  return `${pct}% · ${bytes} · ${rate} · ETA ${eta}`;
};
async function pollJobsFast() {
  // Quick refresh of running download/upload progress without rebuilding the table.
  const cells = document.querySelectorAll('[data-job-target]');
  const tiles = document.querySelectorAll('[data-job-id]');
  if (!cells.length && !tiles.length) return;
  let r;
  try { r = await api('/api/jobs'); } catch (e) { return; }
  const jobs = (r && r.jobs) || [];
  const byTarget = {};
  const byId = {};
  jobs.forEach(j => {
    byId[j.id] = j;
    if (j.state === 'running' || j.state === 'queued') byTarget[j.target_id] = j;
  });
  cells.forEach(cell => {
    const id = cell.getAttribute('data-job-target');
    const j = byTarget[id];
    if (!j) return;
    const fill = cell.querySelector('[data-role="fill"]');
    const meta = cell.querySelector('[data-role="meta"]');
    if (fill) fill.style.width = (j.progress || 0) + '%';
    if (meta) meta.textContent = _fmtJobMeta(j);
  });
  tiles.forEach(tile => {
    const j = byId[tile.getAttribute('data-job-id')];
    if (!j) return;
    const fill = tile.querySelector('[data-role="fill"]');
    const meta = tile.querySelector('[data-role="meta"]');
    if (fill) fill.style.width = (j.progress || 0) + '%';
    if (meta) meta.textContent = _fmtJobMeta(j);
  });
}
async function renderReliability() {
  const root = $('#reliabilityStatus');
  if (!root) return;
  let info = {};
  try {
    const r = await api('/api/system');
    info = (r && r.reliability) || {};
  } catch (e) {
    root.innerHTML = '<p class="hint">Could not read reliability info: ' + (e.message || e) + '</p>';
    return;
  }
  const wdActive = info.watchdog_active;
  const wdBadge = wdActive
    ? '<span class="badge ok">active</span>'
    : '<span class="badge warn">not seen yet</span>';
  root.innerHTML = '' +
    '<div><span class="k">Daemon PID</span><span class="v">' + (info.pid || '—') + '</span></div>' +
    '<div><span class="k">Uptime</span><span class="v">' + _fmtUptime(info.uptime_s) + '</span></div>' +
    '<div><span class="k">Last heartbeat</span><span class="v">' + _agoText(info.last_heartbeat) + '</span></div>' +
    '<div><span class="k">Cron watchdog</span><span class="v">' + wdBadge + ' &nbsp;(last ping ' + _agoText(info.last_watchdog_ping) + ')</span></div>';
}
$('#pwSave').addEventListener('click', async () => {
  const old = $('#pwOld').value;
  const n1 = $('#pwNew').value;
  const n2 = $('#pwNew2').value;
  const user = $('#pwUser').value.trim();
  const msg = $('#pwMsg');
  msg.textContent = '';
  if (n1.length < 6) { msg.textContent = 'New password must be at least 6 characters.'; return; }
  if (n1 !== n2) { msg.textContent = 'New passwords do not match.'; return; }
  const r = await api('/api/auth/password', { method: 'POST', body: { user, old_password: old, new_password: n1 } });
  if (r.ok) {
    msg.textContent = 'Updated. You will need to sign in again.';
    $('#pwOld').value = $('#pwNew').value = $('#pwNew2').value = '';
    setTimeout(() => location.href = '/login', 1500);
  } else {
    msg.textContent = r.error || 'failed';
  }
});

// ----- logout -----
$('#logoutBtn').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/login';
});

// ----- boot -----
refreshState();
setInterval(refreshState, 3000);
setInterval(refreshLogs, 5000);
setInterval(pollJobsFast, 1000);
refreshLogs();

})();
