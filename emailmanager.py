from __future__ import annotations

from ezmail import EzReader, EzMail  # type: ignore
from subprocess import run, STARTUPINFO, STARTF_USESHOWWINDOW, CREATE_NO_WINDOW
from typing import Any
import threading, sys, os, webbrowser, json, logging, imaplib, ssl
from logging.handlers import RotatingFileHandler
from datetime import datetime as _dt
import email as _email_mod
from email.header import decode_header as _hdr_decode
from pystray import Icon, MenuItem, Menu # type: ignore
from PIL import Image
from re import findall
from flask import Flask, request, jsonify, render_template_string

# ── Paths ──────────────────────────────────────────────────────────────────────
CONFIG_DIR  = os.path.join(os.environ.get('PROGRAMDATA', r'C:\ProgramData'), 'EmailManager')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'data.json')
LOG_PATH    = os.path.join(CONFIG_DIR, 'emailmanager.log')

IS_FROZEN = bool(getattr(sys, 'frozen', False))
BASE_DIR  = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

ICON_ACTIVE  = os.path.join(BASE_DIR, 'active.ico')
ICON_STOPPED = os.path.join(BASE_DIR, 'stopped.ico')

FLASK_PORT = 5050

# ── Logger ─────────────────────────────────────────────────────────────────────
os.makedirs(CONFIG_DIR, exist_ok=True)
_logger = logging.getLogger('emailmanager')
_logger.setLevel(logging.DEBUG)
_fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
_fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
_logger.addHandler(_fh)

# ── Global state ───────────────────────────────────────────────────────────────
stop_event = threading.Event()
bg_thread: threading.Thread | None = None
tray_icon: Icon | None = None  # type: ignore

# ── Flask app ──────────────────────────────────────────────────────────────────
logging.getLogger('werkzeug').setLevel(logging.ERROR)
web_app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG: dict[str, Any] = {'interval': 30, 'accounts': []}

def ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)

def read_config() -> dict[str, Any]:
    ensure_config_dir()
    if not os.path.exists(CONFIG_PATH):
        _logger.warning('Config not found — creating default at %s', CONFIG_PATH)
        write_config(_DEFAULT_CONFIG)
        return dict(_DEFAULT_CONFIG)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        data: Any = json.load(f)
    if isinstance(data, list):
        return {'interval': 30, 'accounts': data}
    return data

def write_config(config: dict[str, Any]) -> None:
    ensure_config_dir()
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def build_readers(config: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    accounts = config.get('accounts', [])
    interval = int(config.get('interval', 30))
    _logger.info('Config loaded — interval: %ds, accounts: %d', interval, len(accounts))
    readers = []
    for d in accounts:
        _logger.info('  Account: %s (%s)', d['nome'], d['account'].get('email', '?'))
        readers.append({
            'name': d['nome'],
            'reader': EzReader(d['imap'], d['account']),
            'url': d.get('url', ''),
            'black_list': d.get('black_list', []),
            'white_list': d.get('white_list', []),
            'important_list': d.get('important_list', []),
        })
    return readers, interval

# ── IMAP cleanup helpers ───────────────────────────────────────────────────────
def _imap_connect(acct_cfg: dict) -> imaplib.IMAP4_SSL:
    imap = acct_cfg['imap']
    acc  = acct_cfg['account']
    ctx  = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(imap['server'], int(imap['port']), ssl_context=ctx)
    conn.login(acc['email'], acc['auth_value'])
    return conn

def _decode_hdr(raw: Any) -> str:
    if raw is None:
        return ''
    parts = _hdr_decode(raw) if isinstance(raw, str) else _hdr_decode(raw.decode('utf-8', 'replace'))
    out = ''
    for part, enc in parts:
        out += part.decode(enc or 'utf-8', 'replace') if isinstance(part, bytes) else str(part)
    return out.strip()

def _imap_date(iso: str) -> str:
    return _dt.strptime(iso, '%Y-%m-%d').strftime('%d-%b-%Y')

def _build_criteria(sender: str, date_from: str, date_to: str) -> str:
    parts = []
    if sender:
        parts.append(f'FROM "{sender.replace(chr(34), "")}"')
    if date_from:
        parts.append(f'SINCE {_imap_date(date_from)}')
    if date_to:
        parts.append(f'BEFORE {_imap_date(date_to)}')
    return '(' + (' '.join(parts) if parts else 'ALL') + ')'

def _fetch_preview(conn: imaplib.IMAP4_SSL, uids: list[bytes], limit: int = 200) -> list[dict]:
    preview = []
    for uid in uids[:limit]:
        try:
            _, raw = conn.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
            if not raw or not raw[0]:
                continue
            msg = _email_mod.message_from_bytes(raw[0][1])
            preview.append({
                'uid':     uid.decode(),
                'from':    _decode_hdr(msg.get('From', '')),
                'subject': _decode_hdr(msg.get('Subject', '(no subject)')),
                'date':    msg.get('Date', ''),
            })
        except Exception:
            pass
    return preview

def _imap_folder(folder: str) -> str:
    """Quote folder names that contain spaces or start with '[' as required by IMAP."""
    if ' ' in folder or folder.startswith('['):
        return f'"{folder}"'
    return folder

def _batch_store(conn: imaplib.IMAP4_SSL, uids: list[bytes], flag: str) -> None:
    for i in range(0, len(uids), 500):
        uid_str = b','.join(uids[i:i + 500])
        conn.uid('store', uid_str, '+FLAGS', flag)

# ── Notifications ──────────────────────────────────────────────────────────────
def _ps_escape(value: str) -> str:
    return value.replace("'", "''")

def notify(title: str, message: str, url: str = '') -> None:
    t = _ps_escape(title)
    m = _ps_escape(message)
    u = _ps_escape(url)

    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode('{t}')) > $null
$texts.Item(1).AppendChild($xml.CreateTextNode('{m}')) > $null
"""
    if url:
        ps += f"""
$actions = $xml.CreateElement('actions')
$action  = $xml.CreateElement('action')
$action.SetAttribute('content', 'Open email')
$action.SetAttribute('arguments', '{u}')
$action.SetAttribute('activationType', 'protocol')
$actions.AppendChild($action) > $null
$xml.DocumentElement.AppendChild($actions) > $null
"""
    ps += """
$toast    = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('EmailManager')
$notifier.Show($toast)
"""
    si = STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
        startupinfo=si,
        creationflags=CREATE_NO_WINDOW,
    )

# ── Email processing ───────────────────────────────────────────────────────────
def process_account(
    name: str, reader: EzReader, url: str,
    black_list: list[str], white_list: list[str], important_list: list[str],
) -> None:
    _logger.debug('Checking account: %s', name)
    try:
        with reader as r:
            emails: list[EzMail] = r.fetch_unread()
            _logger.info('[%s] %d unread email(s) found', name, len(emails))
            for email in emails:
                sender = findall(r"<(.*?)>", email.sender)[0] if '<' in email.sender else email.sender
                _logger.debug('[%s] Processing — from: %s | subject: %s', name, sender, email.subject)

                if any(b in sender for b in black_list):
                    _logger.info('[%s] BLACKLISTED — trashed: %s', name, sender)
                    r.move_to_trash(email)
                    continue

                if any(i in sender for i in important_list):
                    message = f'From: {sender}\nSubject: {email.subject}'
                    body = email.body.lower()
                    if ' code ' in body or ' código ' in body:
                        message += f'\nBody: {email.body.replace(chr(10), " ")}'
                    _logger.info('[%s] IMPORTANT — notifying (left unread): %s', name, sender)
                    notify(f'New email — {name}', message, url)
                    continue  # intentionally NOT marking as read

                if white_list and not any(w in sender for w in white_list):
                    _logger.info('[%s] NOT IN WHITELIST — silenced: %s', name, sender)
                    r.mark_as_read(email)
                    continue

                message = f'From: {sender}\nSubject: {email.subject}'
                body = email.body.lower()
                if ' code ' in body or ' código ' in body:
                    message += f'\nBody: {email.body.replace(chr(10), " ")}'

                _logger.info('[%s] NOTIFYING — from: %s | subject: %s', name, sender, email.subject)
                notify(f'New email — {name}', message, url)
                r.mark_as_read(email)
    except Exception as exc:
        _logger.exception('[%s] Error: %s — %s', name, type(exc).__name__, exc)
        try:
            notify('EmailManager', f'Error reading {name}: {type(exc).__name__}: {exc}')
        except Exception:
            pass

def _background_loop(event: threading.Event) -> None:
    _logger.info('Background loop starting')
    try:
        config = read_config()
        readers, interval = build_readers(config)
    except Exception as exc:
        _logger.exception('Could not load config: %s', exc)
        notify('EmailManager', f'Could not load config: {exc}')
        return

    if not readers:
        _logger.warning('No accounts configured')
        notify('EmailManager', 'No accounts configured.\nOpen Settings to add one.')
        return

    cycle = 0
    while not event.is_set():
        cycle += 1
        _logger.debug('── Cycle %d ──', cycle)
        for account in readers:
            if event.is_set():
                break
            process_account(
                account['name'], account['reader'],
                account['url'], account['black_list'], account['white_list'], account['important_list'],
            )
        event.wait(interval)
    _logger.info('Background loop stopped')

# ── Monitoring control ─────────────────────────────────────────────────────────
def _update_icon() -> None:
    if tray_icon:
        tray_icon.icon = Image.open(ICON_ACTIVE if not stop_event.is_set() else ICON_STOPPED)

def _start() -> None:
    global stop_event, bg_thread
    if bg_thread and bg_thread.is_alive():
        _logger.debug('_start called but thread already running')
        return
    _logger.info('Starting monitoring')
    stop_event = threading.Event()
    bg_thread = threading.Thread(target=_background_loop, args=(stop_event,), daemon=True)
    bg_thread.start()
    _update_icon()
    notify('EmailManager', f'Monitoring started.\nLog: {LOG_PATH}')

def _stop() -> None:
    _logger.info('Stopping monitoring')
    stop_event.set()
    _update_icon()
    notify('EmailManager', 'Monitoring stopped.')

def _restart() -> None:
    _logger.info('Restarting monitoring')
    global bg_thread
    stop_event.set()
    bg_thread = None
    _start()

# ── System tray handlers ───────────────────────────────────────────────────────
def on_settings(_icon: Icon, _item: MenuItem) -> None:
    webbrowser.open(f'http://127.0.0.1:{FLASK_PORT}')

def on_cleanup(_icon: Icon, _item: MenuItem) -> None:
    webbrowser.open(f'http://127.0.0.1:{FLASK_PORT}/cleanup')

def on_view_log(_icon: Icon, _item: MenuItem) -> None:
    if not os.path.exists(LOG_PATH):
        notify('EmailManager', f'Log file not found:\n{LOG_PATH}')
        return
    os.startfile(LOG_PATH)

def on_start(_icon: Icon, _item: MenuItem) -> None:
    _start()

def on_restart(_icon: Icon, _item: MenuItem) -> None:
    _restart()

def on_stop(_icon: Icon, _item: MenuItem) -> None:
    _stop()

def on_exit(tray: Icon, _item: MenuItem) -> None:
    stop_event.set()
    notify('EmailManager', 'Exiting...')
    tray.stop()
    os._exit(0)

# ── Settings page ──────────────────────────────────────────────────────────────
_SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EmailManager — Settings</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    min-height: 100vh;
    padding-bottom: 80px;
  }

  .topbar {
    background: #1e3a8a;
    color: #fff;
    padding: 14px 28px;
    display: flex;
    align-items: baseline;
    gap: 10px;
    box-shadow: 0 2px 12px rgba(0,0,0,.5);
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .topbar h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: .03em; }
  .topnav { display: flex; gap: 4px; margin-left: 20px; }
  .topnav-link {
    padding: 4px 14px; border-radius: 6px; font-size: .82rem; font-weight: 500;
    color: rgba(255,255,255,.65); text-decoration: none; transition: background .15s, color .15s;
  }
  .topnav-link:hover  { background: rgba(255,255,255,.1); color: #fff; }
  .topnav-link.active { background: rgba(255,255,255,.18); color: #fff; }

  .container { max-width: 800px; margin: 28px auto; padding: 0 16px; }

  .card {
    background: #1e293b;
    border-radius: 12px;
    padding: 22px 24px;
    margin-bottom: 20px;
    border: 1px solid #334155;
    box-shadow: 0 4px 16px rgba(0,0,0,.3);
  }
  .card-title {
    font-size: .9rem;
    font-weight: 700;
    color: #60a5fa;
    text-transform: uppercase;
    letter-spacing: .06em;
    margin-bottom: 18px;
    padding-bottom: 10px;
    border-bottom: 1px solid #334155;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
  .form-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
  .form-row label { font-size: .8rem; font-weight: 600; color: #94a3b8; }
  .form-row input, .form-row textarea {
    padding: 8px 11px;
    border: 1.5px solid #334155;
    border-radius: 8px;
    font-size: .88rem;
    color: #e2e8f0;
    font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
    background: #0f172a;
  }
  .form-row input:focus, .form-row textarea:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59,130,246,.18);
  }
  .form-row textarea { resize: vertical; min-height: 68px; line-height: 1.5; }
  .hint { font-size: .72rem; color: #475569; margin-top: 2px; }
  .span-2 { grid-column: span 2; }

  /* Account cards */
  .acct-card {
    border: 1.5px solid #334155;
    border-radius: 10px;
    margin-bottom: 12px;
    overflow: hidden;
    transition: border-color .15s;
  }
  .acct-card:hover { border-color: #475569; }

  .acct-header {
    display: flex;
    align-items: center;
    padding: 11px 14px;
    background: #0f172a;
    cursor: pointer;
    user-select: none;
    gap: 10px;
  }
  .acct-header:hover { background: #1e293b; }
  .acct-name  { font-weight: 600; font-size: .9rem; flex: 1; color: #f1f5f9; }
  .acct-email { font-size: .78rem; color: #64748b; }
  .chevron { transition: transform .2s; color: #475569; flex-shrink: 0; }
  .chevron.open { transform: rotate(180deg); }

  .acct-body { padding: 16px; display: none; background: #1e293b; }
  .acct-body.open { display: block; }
  .acct-footer { display: flex; justify-content: flex-end; padding-top: 6px; }

  .add-row { display: flex; justify-content: flex-end; margin-top: 6px; }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: .85rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: background .15s, opacity .15s;
    font-family: inherit;
  }
  .btn:active { opacity: .8; }
  .btn-primary  { background: #2563eb; color: #fff; }
  .btn-primary:hover  { background: #1d4ed8; }
  .btn-danger   { background: #b91c1c; color: #fff; }
  .btn-danger:hover   { background: #991b1b; }
  .btn-outline  { background: transparent; color: #60a5fa; border: 1.5px solid #1d4ed8; }
  .btn-outline:hover  { background: #1e3a8a33; }
  .btn-sm { padding: 5px 12px; font-size: .78rem; }

  /* Save bar */
  .save-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: #1e293b;
    border-top: 1px solid #334155;
    padding: 12px 28px;
    display: flex;
    align-items: center;
    gap: 14px;
    box-shadow: 0 -4px 16px rgba(0,0,0,.4);
    z-index: 100;
  }
  .save-bar .note { font-size: .78rem; color: #475569; margin-left: auto; }

  #status {
    font-size: .82rem;
    font-weight: 600;
    padding: 6px 13px;
    border-radius: 6px;
    display: none;
  }
  #status.ok  { background: #14532d; color: #86efac; display: inline-block; }
  #status.err { background: #7f1d1d; color: #fca5a5; display: inline-block; }

  @media (max-width: 560px) {
    .form-grid { grid-template-columns: 1fr; }
    .span-2 { grid-column: span 1; }
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>EmailManager</h1>
  <nav class="topnav">
    <a href="/" class="topnav-link active">Settings</a>
    <a href="/cleanup" class="topnav-link">Cleanup</a>
  </nav>
</div>

<div class="container">

  <div class="card">
    <div class="card-title">General</div>
    <div style="max-width:220px">
      <div class="form-row">
        <label for="interval">Check interval (seconds)</label>
        <input type="number" id="interval" min="10" step="5" value="30">
        <span class="hint">Minimum: 10 seconds</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      Email Accounts
      <button class="btn btn-outline btn-sm" onclick="addAccount()">+ Add Account</button>
    </div>
    <div id="accounts-list"></div>
    <div id="no-accounts" style="text-align:center;padding:24px 0;color:#94a3b8;font-size:.88rem;display:none">
      No accounts configured. Click "+ Add Account" to get started.
    </div>
  </div>

</div>

<div class="save-bar">
  <button class="btn btn-primary" onclick="saveSettings()">Save &amp; Apply</button>
  <span id="status"></span>
  <span class="note">Log: <code style="font-size:.75rem;color:#60a5fa">{{ log_path }}</code> &nbsp;·&nbsp; Saving restarts monitoring.</span>
</div>

<template id="acct-tpl">
  <div class="acct-card">
    <div class="acct-header" onclick="toggleAcct(this)">
      <span class="acct-name">New Account</span>
      <span class="acct-email"></span>
      <svg class="chevron open" width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2.5">
        <path d="m6 9 6 6 6-6"/>
      </svg>
    </div>
    <div class="acct-body open">
      <div class="form-grid">
        <div class="form-row">
          <label>Account name</label>
          <input type="text" name="nome" placeholder="e.g. Work Gmail" autocomplete="off">
        </div>
        <div class="form-row">
          <label>URL to open on notification</label>
          <input type="url" name="url" placeholder="https://mail.google.com">
          <span class="hint">Opened when clicking a toast notification.</span>
        </div>
        <div class="form-row">
          <label>Email address</label>
          <input type="email" name="email" placeholder="you@example.com" autocomplete="off">
        </div>
        <div class="form-row">
          <label>Password / App Password</label>
          <input type="password" name="auth_value" placeholder="••••••••" autocomplete="new-password">
        </div>
        <div class="form-row">
          <label>IMAP server</label>
          <input type="text" name="imap_server" placeholder="imap.gmail.com">
        </div>
        <div class="form-row">
          <label>IMAP port</label>
          <input type="number" name="imap_port" value="993">
        </div>
        <div class="form-row span-2">
          <label>Blacklist</label>
          <textarea name="black_list" placeholder="noreply@spam.com&#10;ads@newsletter.com"></textarea>
          <span class="hint">One sender per line. Matching emails are moved to trash.</span>
        </div>
        <div class="form-row span-2">
          <label>Whitelist</label>
          <textarea name="white_list" placeholder="boss@company.com&#10;client@partner.com"></textarea>
          <span class="hint">One sender per line. If set, only these senders trigger notifications.</span>
        </div>
        <div class="form-row span-2">
          <label style="color:#f59e0b">Importants</label>
          <textarea name="important_list" placeholder="ceo@company.com&#10;@vip-domain.com" style="border-color:#92400e"></textarea>
          <span class="hint">One sender per line. These always trigger notifications and the email is <strong>left unread</strong> until you read it manually.</span>
        </div>
      </div>
      <div class="acct-footer">
        <button class="btn btn-danger btn-sm" onclick="removeAcct(this)">Remove Account</button>
      </div>
    </div>
  </div>
</template>

<script>
function toggleAcct(header) {
  const body    = header.nextElementSibling;
  const chevron = header.querySelector('.chevron');
  const isOpen  = body.classList.toggle('open');
  chevron.classList.toggle('open', isOpen);
}

function updateHeader(card) {
  const nome  = card.querySelector('[name=nome]').value  || 'New Account';
  const email = card.querySelector('[name=email]').value || '';
  card.querySelector('.acct-name').textContent  = nome;
  card.querySelector('.acct-email').textContent = email;
}

document.addEventListener('input', e => {
  const card = e.target.closest('.acct-card');
  if (card) updateHeader(card);
});

function syncEmpty() {
  const empty = document.getElementById('no-accounts');
  const has   = document.querySelectorAll('.acct-card').length > 0;
  empty.style.display = has ? 'none' : 'block';
}

function appendCard(acct) {
  const frag = document.getElementById('acct-tpl').content.cloneNode(true);
  const card = frag.querySelector('.acct-card');

  if (acct) {
    card.querySelector('[name=nome]').value        = acct.nome                  || '';
    card.querySelector('[name=url]').value         = acct.url                   || '';
    card.querySelector('[name=email]').value       = acct.account?.email        || '';
    card.querySelector('[name=auth_value]').value  = acct.account?.auth_value   || '';
    card.querySelector('[name=imap_server]').value = acct.imap?.server          || '';
    card.querySelector('[name=imap_port]').value   = acct.imap?.port            || 993;
    card.querySelector('[name=black_list]').value     = (acct.black_list     || []).join('\n');
    card.querySelector('[name=white_list]').value     = (acct.white_list     || []).join('\n');
    card.querySelector('[name=important_list]').value = (acct.important_list || []).join('\n');
  }

  document.getElementById('accounts-list').appendChild(card);
  updateHeader(document.querySelector('.acct-card:last-child'));
  syncEmpty();
}

function addAccount() {
  appendCard(null);
  const card = document.querySelector('.acct-card:last-child');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.querySelector('[name=nome]').focus();
}

function removeAcct(btn) {
  btn.closest('.acct-card').remove();
  syncEmpty();
}

function readCard(card) {
  const v     = name => card.querySelector(`[name=${name}]`).value.trim();
  const lines = val  => val.split('\n').map(s => s.trim()).filter(Boolean);
  return {
    nome: v('nome'),
    url:  v('url'),
    account: { email: v('email'), auth_value: v('auth_value'), auth_type: 'password' },
    imap:    { server: v('imap_server'), port: parseInt(v('imap_port')) || 993 },
    black_list:     lines(v('black_list')),
    white_list:     lines(v('white_list')),
    important_list: lines(v('important_list')),
  };
}

async function loadConfig() {
  const res    = await fetch('/api/config');
  const config = await res.json();
  document.getElementById('interval').value = config.interval || 30;
  (config.accounts || []).forEach(appendCard);
  syncEmpty();
}

async function saveSettings() {
  const payload = {
    interval: parseInt(document.getElementById('interval').value) || 30,
    accounts: Array.from(document.querySelectorAll('.acct-card')).map(readCard),
  };

  const el = document.getElementById('status');
  el.className = '';
  el.textContent = '';

  try {
    const res  = await fetch('/api/config', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      el.className   = 'ok';
      el.textContent = '✓ Saved — monitoring restarted.';
    } else {
      el.className   = 'err';
      el.textContent = '✗ ' + (data.error || 'Unknown error');
    }
  } catch {
    el.className   = 'err';
    el.textContent = '✗ Could not reach the app.';
  }

  setTimeout(() => { el.className = ''; el.textContent = ''; }, 4000);
}

loadConfig();
</script>
</body>
</html>"""

# ── Cleanup page ───────────────────────────────────────────────────────────────
_CLEANUP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EmailManager — Cleanup</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0; min-height: 100vh; padding-bottom: 80px;
  }
  .topbar {
    background: #1e3a8a; color: #fff; padding: 14px 28px;
    display: flex; align-items: center; gap: 10px;
    box-shadow: 0 2px 12px rgba(0,0,0,.5); position: sticky; top: 0; z-index: 50;
  }
  .topbar h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: .03em; }
  .topnav { display: flex; gap: 4px; margin-left: 20px; }
  .topnav-link {
    padding: 4px 14px; border-radius: 6px; font-size: .82rem; font-weight: 500;
    color: rgba(255,255,255,.65); text-decoration: none; transition: background .15s, color .15s;
  }
  .topnav-link:hover  { background: rgba(255,255,255,.1); color: #fff; }
  .topnav-link.active { background: rgba(255,255,255,.18); color: #fff; }
  .container { max-width: 900px; margin: 28px auto; padding: 0 16px; }
  .card {
    background: #1e293b; border-radius: 12px; padding: 22px 24px;
    margin-bottom: 20px; border: 1px solid #334155;
    box-shadow: 0 4px 16px rgba(0,0,0,.3);
  }
  .card-title {
    font-size: .9rem; font-weight: 700; color: #60a5fa;
    text-transform: uppercase; letter-spacing: .06em;
    margin-bottom: 18px; padding-bottom: 10px; border-bottom: 1px solid #334155;
    display: flex; align-items: center; justify-content: space-between;
  }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
  .form-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
  .form-row label { font-size: .8rem; font-weight: 600; color: #94a3b8; }
  .form-row input, .form-row select {
    padding: 8px 11px; border: 1.5px solid #334155; border-radius: 8px;
    font-size: .88rem; color: #e2e8f0; font-family: inherit;
    background: #0f172a; transition: border-color .15s, box-shadow .15s;
  }
  .form-row input:focus, .form-row select:focus {
    outline: none; border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59,130,246,.18);
  }
  .form-row select option { background: #1e293b; }
  .hint { font-size: .72rem; color: #475569; margin-top: 2px; }
  .span-2 { grid-column: span 2; }
  /* Results table */
  .results-wrap { overflow-x: auto; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: .82rem; }
  thead th {
    text-align: left; padding: 8px 10px; font-weight: 600;
    color: #64748b; border-bottom: 1px solid #334155;
    text-transform: uppercase; font-size: .72rem; letter-spacing: .05em;
  }
  tbody tr { border-bottom: 1px solid #1e293b; }
  tbody tr:hover { background: #0f172a; }
  tbody td { padding: 7px 10px; color: #cbd5e1; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: .72rem; font-weight: 700; background: #172554; color: #93c5fd;
  }
  /* Buttons */
  .btn {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 8px 18px; border-radius: 8px; font-size: .85rem;
    font-weight: 500; cursor: pointer; border: none;
    transition: background .15s, opacity .15s; font-family: inherit;
  }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn-outline { background: transparent; color: #60a5fa; border: 1.5px solid #1d4ed8; }
  .btn-outline:hover:not(:disabled) { background: #1e3a8a33; }
  .btn-danger  { background: #b91c1c; color: #fff; }
  .btn-danger:hover:not(:disabled)  { background: #991b1b; }
  /* Action bar */
  .action-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: #1e293b; border-top: 1px solid #334155;
    padding: 12px 28px; display: flex; align-items: center; gap: 14px;
    box-shadow: 0 -4px 16px rgba(0,0,0,.4); z-index: 100;
  }
  #status {
    font-size: .82rem; font-weight: 600; padding: 6px 13px;
    border-radius: 6px; display: none;
  }
  #status.ok  { background: #14532d; color: #86efac; display: inline-block; }
  #status.err { background: #7f1d1d; color: #fca5a5; display: inline-block; }
  #status.info { background: #1e3a8a; color: #93c5fd; display: inline-block; }
  .warn-box {
    background: #431407; border: 1px solid #7c2d12;
    color: #fb923c; border-radius: 8px; padding: 10px 14px;
    font-size: .82rem; margin-top: 4px; display: none;
  }
  @media (max-width: 560px) {
    .form-grid { grid-template-columns: 1fr; }
    .span-2 { grid-column: span 1; }
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>EmailManager</h1>
  <nav class="topnav">
    <a href="/" class="topnav-link">Settings</a>
    <a href="/cleanup" class="topnav-link active">Cleanup</a>
  </nav>
</div>

<div class="container">

  <div class="card">
    <div class="card-title">Target</div>
    <div class="form-grid">
      <div class="form-row">
        <label for="account">Account</label>
        <select id="account"><option value="">— select account —</option></select>
      </div>
      <div class="form-row">
        <label for="folder">Folder</label>
        <input type="text" id="folder" value="INBOX" placeholder="INBOX">
        <span class="hint">Examples: INBOX &nbsp;·&nbsp; [Gmail]/All Mail &nbsp;·&nbsp; [Gmail]/Spam</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Filters <span style="font-size:.75rem;font-weight:400;color:#475569;text-transform:none;letter-spacing:0">— at least one required, combined with AND</span></div>
    <div class="form-grid">
      <div class="form-row span-2">
        <label for="sender">Sender (partial or full)</label>
        <input type="text" id="sender" placeholder="e.g.  spam@example.com  or  @newsletter.com  or  marketing">
      </div>
      <div class="form-row">
        <label for="date_from">Received from</label>
        <input type="date" id="date_from">
      </div>
      <div class="form-row">
        <label for="date_to">Received up to</label>
        <input type="date" id="date_to">
      </div>
    </div>
  </div>

  <div id="results-card" class="card" style="display:none">
    <div class="card-title">
      Results &nbsp;<span class="badge" id="results-count">0</span>
      <span style="font-size:.75rem;font-weight:400;color:#475569;text-transform:none;letter-spacing:0" id="preview-note"></span>
    </div>
    <div id="warn-box" class="warn-box">
      ⚠ Deletion is permanent and cannot be undone.
    </div>
    <div class="results-wrap">
      <table>
        <thead><tr><th>From</th><th>Subject</th><th>Date</th></tr></thead>
        <tbody id="results-body"></tbody>
      </table>
    </div>
  </div>

</div>

<div class="action-bar">
  <button class="btn btn-outline" id="preview-btn" onclick="doPreview()">Preview</button>
  <button class="btn btn-danger" id="delete-btn" style="display:none" onclick="doDelete()">
    Delete <span id="delete-count">0</span> emails permanently
  </button>
  <span id="status"></span>
</div>

<script>
let totalFound = 0;

async function loadAccounts() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  const sel = document.getElementById('account');
  (cfg.accounts || []).forEach(a => {
    const o = document.createElement('option');
    o.value = a.nome;
    o.textContent = `${a.nome}  (${a.account?.email || ''})`;
    sel.appendChild(o);
  });
}

function payload() {
  return {
    account:   document.getElementById('account').value,
    folder:    document.getElementById('folder').value.trim() || 'INBOX',
    sender:    document.getElementById('sender').value.trim(),
    date_from: document.getElementById('date_from').value,
    date_to:   document.getElementById('date_to').value,
  };
}

function setStatus(msg, cls) {
  const el = document.getElementById('status');
  el.textContent = msg; el.className = cls;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function doPreview() {
  const p = payload();
  if (!p.account) { setStatus('Select an account first.', 'err'); return; }
  if (!p.sender && !p.date_from && !p.date_to) { setStatus('Set at least one filter.', 'err'); return; }

  setStatus('Searching…', 'info');
  document.getElementById('preview-btn').disabled = true;
  document.getElementById('delete-btn').style.display = 'none';
  document.getElementById('results-card').style.display = 'none';

  try {
    const res  = await fetch('/api/cleanup/preview', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(p),
    });
    const data = await res.json();
    if (!res.ok) { setStatus(data.error || 'Error', 'err'); return; }

    totalFound = data.total;
    setStatus('', '');

    document.getElementById('results-count').textContent = data.total;
    document.getElementById('preview-note').textContent =
      data.total > 200 ? `(showing first 200 of ${data.total})` : '';

    const tbody = document.getElementById('results-body');
    tbody.innerHTML = '';
    data.preview.forEach(e => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td title="${esc(e.from)}">${esc(e.from)}</td><td title="${esc(e.subject)}">${esc(e.subject)}</td><td>${esc(e.date)}</td>`;
      tbody.appendChild(tr);
    });

    document.getElementById('results-card').style.display = 'block';
    document.getElementById('warn-box').style.display = data.total > 0 ? 'block' : 'none';

    const delBtn = document.getElementById('delete-btn');
    document.getElementById('delete-count').textContent = data.total;
    delBtn.style.display = data.total > 0 ? 'inline-flex' : 'none';
  } catch (e) {
    setStatus('Could not reach the app.', 'err');
  } finally {
    document.getElementById('preview-btn').disabled = false;
  }
}

async function doDelete() {
  if (!confirm(`Permanently delete ${totalFound} email(s)?\n\nThis cannot be undone.`)) return;

  setStatus('Deleting…', 'info');
  document.getElementById('delete-btn').disabled = true;

  try {
    const res  = await fetch('/api/cleanup/delete', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload()),
    });
    const data = await res.json();
    if (!res.ok) { setStatus(data.error || 'Error', 'err'); return; }

    setStatus(`✓ ${data.deleted} email(s) deleted.`, 'ok');
    document.getElementById('results-card').style.display = 'none';
    document.getElementById('delete-btn').style.display = 'none';
    totalFound = 0;
  } catch (e) {
    setStatus('Could not reach the app.', 'err');
  } finally {
    document.getElementById('delete-btn').disabled = false;
  }
}

loadAccounts();
</script>
</body>
</html>"""

# ── Flask routes ───────────────────────────────────────────────────────────────
@web_app.route('/')
def index():
    return render_template_string(_SETTINGS_HTML, log_path=LOG_PATH)

@web_app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(read_config())

@web_app.route('/api/config', methods=['POST'])
def api_save_config():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    try:
        write_config(data)
        threading.Thread(target=_restart, daemon=True).start()
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@web_app.route('/cleanup')
def cleanup_page():
    return render_template_string(_CLEANUP_HTML)

@web_app.route('/api/cleanup/preview', methods=['POST'])
def api_cleanup_preview():
    body      = request.get_json(silent=True) or {}
    acct_name = body.get('account', '')
    folder    = body.get('folder', 'INBOX')
    sender    = body.get('sender', '')
    date_from = body.get('date_from', '')
    date_to   = body.get('date_to', '')

    if not sender and not date_from and not date_to:
        return jsonify({'error': 'Set at least one filter.'}), 400

    config   = read_config()
    acct_cfg = next((a for a in config.get('accounts', []) if a['nome'] == acct_name), None)
    if not acct_cfg:
        return jsonify({'error': 'Account not found.'}), 400

    try:
        criteria = _build_criteria(sender, date_from, date_to)
        _logger.info('Cleanup preview — account: %s folder: %s criteria: %s', acct_name, folder, criteria)
        conn = _imap_connect(acct_cfg)
        conn.select(_imap_folder(folder), readonly=True)
        _, data = conn.uid('search', None, criteria)
        uids    = data[0].split() if data[0] else []
        preview = _fetch_preview(conn, uids)
        conn.logout()
        _logger.info('Cleanup preview — %d email(s) found', len(uids))
        return jsonify({'total': len(uids), 'preview': preview})
    except Exception as exc:
        _logger.exception('Cleanup preview error: %s', exc)
        return jsonify({'error': str(exc)}), 500

@web_app.route('/api/cleanup/delete', methods=['POST'])
def api_cleanup_delete():
    body      = request.get_json(silent=True) or {}
    acct_name = body.get('account', '')
    folder    = body.get('folder', 'INBOX')
    sender    = body.get('sender', '')
    date_from = body.get('date_from', '')
    date_to   = body.get('date_to', '')

    if not sender and not date_from and not date_to:
        return jsonify({'error': 'Set at least one filter.'}), 400

    config   = read_config()
    acct_cfg = next((a for a in config.get('accounts', []) if a['nome'] == acct_name), None)
    if not acct_cfg:
        return jsonify({'error': 'Account not found.'}), 400

    try:
        criteria = _build_criteria(sender, date_from, date_to)
        conn = _imap_connect(acct_cfg)
        conn.select(_imap_folder(folder), readonly=False)
        _, data = conn.uid('search', None, criteria)
        uids    = data[0].split() if data[0] else []

        if uids:
            if folder.upper().startswith('[GMAIL]/'):
                # Gmail virtual folders require moving to Trash before expunging
                trash = _imap_folder('[Gmail]/Trash')
                for i in range(0, len(uids), 500):
                    uid_str = b','.join(uids[i:i + 500])
                    conn.uid('COPY', uid_str, trash)
                    conn.uid('STORE', uid_str, '+FLAGS', '\\Deleted')
                conn.expunge()
            else:
                _batch_store(conn, uids, '(\\Deleted)')
                conn.expunge()

        conn.logout()
        _logger.info('Cleanup delete — %d email(s) deleted from %s/%s', len(uids), acct_name, folder)
        return jsonify({'deleted': len(uids)})
    except Exception as exc:
        _logger.exception('Cleanup delete error: %s', exc)
        return jsonify({'error': str(exc)}), 500

def _run_flask() -> None:
    web_app.run(host='127.0.0.1', port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ensure_config_dir()
    _logger.info('═' * 50)
    _logger.info('EmailManager starting — log: %s', LOG_PATH)
    first_run = not os.path.exists(CONFIG_PATH)

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    tray_icon = Icon(
        'EmailManager',
        Image.open(ICON_ACTIVE),
        menu=Menu(
            MenuItem('Settings', on_settings),
            MenuItem('Cleanup',  on_cleanup),
            MenuItem('View Log', on_view_log),
            Menu.SEPARATOR,
            MenuItem('Start',    on_start),
            MenuItem('Restart',  on_restart),
            MenuItem('Stop',     on_stop),
            Menu.SEPARATOR,
            MenuItem('Exit',     on_exit),
        ),
    )

    if first_run:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{FLASK_PORT}')).start()
    else:
        _start()

    tray_icon.run()
