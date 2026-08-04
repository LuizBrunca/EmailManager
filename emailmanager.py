from __future__ import annotations

from ezmail import EzReader, EzMail  # type: ignore
from subprocess import run, STARTUPINFO, STARTF_USESHOWWINDOW, CREATE_NO_WINDOW
from typing import Any
import threading, sys, os, webbrowser, json, logging, imaplib, ssl, winreg
from logging.handlers import RotatingFileHandler
from datetime import datetime as _dt, timedelta as _timedelta
import email as _email_mod
from email.header import decode_header as _hdr_decode
from pystray import Icon, MenuItem, Menu # type: ignore
from PIL import Image
from re import findall, compile as _re_compile
from flask import Flask, request, jsonify, render_template_string, send_file

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
_notifications_enabled: bool = True

# ── Flask app ──────────────────────────────────────────────────────────────────
logging.getLogger('werkzeug').setLevel(logging.ERROR)
web_app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG: dict[str, Any] = {
    'interval': 30,
    'notifications_enabled': True,
    'start_with_windows': False,
    'notify_max_age_days': 5,
    'accounts': [],
}

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
        return {'interval': 30, 'notifications_enabled': True, 'accounts': data}
    return data

def write_config(config: dict[str, Any]) -> None:
    ensure_config_dir()
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

# ── Start with Windows ────────────────────────────────────────────────────────
_RUN_KEY_PATH   = r'Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_VALUE_NAME = 'EmailManager'

def set_start_with_windows(enabled: bool) -> None:
    """Add/remove the per-user autostart entry (HKCU\\...\\Run) for this app."""
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH)
    except OSError as exc:
        _logger.exception('Could not open Run registry key: %s', exc)
        return
    try:
        if enabled:
            exe_path = sys.executable if IS_FROZEN else os.path.abspath(__file__)
            winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
            _logger.info('Start-with-Windows enabled (%s)', exe_path)
        else:
            try:
                winreg.DeleteValue(key, _RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
            _logger.info('Start-with-Windows disabled')
    finally:
        winreg.CloseKey(key)

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
            'notify': d.get('notify', True),
            'notify_non_important': d.get('notify_non_important', True),
        })
    return readers, interval

# ── IMAP cleanup helpers ───────────────────────────────────────────────────────
def _imap_connect(acct_cfg: dict) -> imaplib.IMAP4_SSL:
    imap = acct_cfg['imap']
    acc  = acct_cfg['account']
    ctx  = ssl.create_default_context()
    if not imap.get('verify_ssl', True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
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

_LIST_RE = _re_compile(r'^\(([^)]*)\)\s+"([^"]*)"\s+(.+)$')

def _list_folders(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Return real mailbox names from the server, avoiding locale-guessing (e.g.
    Gmail's special folders are named in the account's own language)."""
    status, data = conn.list()
    if status != 'OK':
        return []
    names = []
    for raw in data:
        if not raw:
            continue
        line = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
        m = _LIST_RE.match(line)
        if not m:
            continue
        flags, _, name = m.groups()
        if '\\Noselect' in flags:
            continue
        names.append(name.strip('"'))
    return names

def _batch_store(conn: imaplib.IMAP4_SSL, uids: list[bytes], flag: str) -> None:
    for i in range(0, len(uids), 500):
        uid_str = b','.join(uids[i:i + 500])
        conn.uid('store', uid_str, '+FLAGS', flag)

# ── Notifications ──────────────────────────────────────────────────────────────
def _ps_escape(value: str) -> str:
    return value.replace("'", "''")

def notify(title: str, message: str, url: str = '') -> None:
    if not _notifications_enabled:
        return
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

def _within_notify_window(date: _dt | None, max_age_days: int) -> bool:
    """True if the email is recent enough to warrant a notification.

    Mailboxes can carry days/weeks of unread backlog (first run, a stretch
    offline, etc.) — without this, EmailManager would fire a notification
    for every one of them at once. 0 disables the limit."""
    if not date or max_age_days <= 0:
        return True
    now = _dt.now(date.tzinfo) if date.tzinfo else _dt.now()
    return (now - date) <= _timedelta(days=max_age_days)

# ── Email processing ───────────────────────────────────────────────────────────
def process_account(
    name: str, reader: EzReader, url: str,
    black_list: list[str], white_list: list[str], important_list: list[str],
    notify_enabled: bool = True, notify_non_important: bool = True,
    notify_max_age_days: int = 5,
) -> None:
    _logger.debug('Checking account: %s', name)
    try:
        with reader as r:
            emails: list[EzMail] = r.fetch_unread()
            _logger.info('[%s] %d unread email(s) found', name, len(emails))
            for email in emails:
                sender = findall(r"<(.*?)>", email.sender)[0] if '<' in email.sender else email.sender
                _logger.debug('[%s] Processing — from: %s | subject: %s', name, sender, email.subject)
                recent = _within_notify_window(email.date, notify_max_age_days)

                if any(b in sender for b in black_list):
                    _logger.info('[%s] BLACKLISTED — trashed: %s', name, sender)
                    r.move_to_trash(email)
                    continue

                if any(i in sender for i in important_list):
                    message = f'From: {sender}\nSubject: {email.subject}'
                    body = email.body.lower()
                    if ' code ' in body or ' código ' in body:
                        message += f'\nBody: {email.body.replace(chr(10), " ")}'
                    if notify_enabled and recent:
                        _logger.info('[%s] IMPORTANT — notifying (left unread): %s', name, sender)
                        notify(f'New email — {name}', message, url)
                    else:
                        _logger.info('[%s] IMPORTANT — too old to notify, left unread: %s', name, sender)
                    continue  # intentionally NOT marking as read

                if white_list and not any(w in sender for w in white_list):
                    _logger.info('[%s] NOT IN WHITELIST — silenced: %s', name, sender)
                    r.mark_as_read(email)
                    continue

                message = f'From: {sender}\nSubject: {email.subject}'
                body = email.body.lower()
                if ' code ' in body or ' código ' in body:
                    message += f'\nBody: {email.body.replace(chr(10), " ")}'

                if notify_enabled and notify_non_important and recent:
                    _logger.info('[%s] NOTIFYING — from: %s | subject: %s', name, sender, email.subject)
                    notify(f'New email — {name}', message, url)
                else:
                    _logger.info('[%s] READ (no notify) — from: %s | subject: %s', name, sender, email.subject)
                r.mark_as_read(email)
    except Exception as exc:
        _logger.exception('[%s] Error: %s — %s', name, type(exc).__name__, exc)
        if notify_enabled:
            try:
                notify('EmailManager', f'Error reading {name}: {type(exc).__name__}: {exc}')
            except Exception:
                pass

def _background_loop(event: threading.Event) -> None:
    _logger.info('Background loop starting')
    try:
        config = read_config()
        readers, interval = build_readers(config)
        notify_max_age_days = int(config.get('notify_max_age_days', 5))
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
                account['notify'], account['notify_non_important'],
                notify_max_age_days,
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
def on_toggle_notifications(_icon: Icon, _item: MenuItem) -> None:
    global _notifications_enabled
    _notifications_enabled = not _notifications_enabled
    config = read_config()
    config['notifications_enabled'] = _notifications_enabled
    write_config(config)
    _logger.info('Notifications toggled %s', 'ON' if _notifications_enabled else 'OFF')

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

# ── Shared design system ─────────────────────────────────────────────────────
_BASE_CSS = r"""
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  :root {
    color-scheme: dark;
    --bg:            #05060a;
    --card:           #0b0c12;
    --card-border:    rgba(70,130,200,.22);
    --card-border-hi: rgba(70,130,200,.42);
    --elevated:       #060709;
    --text:           #eef1f6;
    --text-muted:     #a7b2c2;
    --text-faint:     #5c6577;
    --accent:         #3f8ef0;
    --accent-soft:    #8fc4ff;
    --accent-dim:     rgba(63,142,240,.16);
    --accent-line:    rgba(63,142,240,.45);
    --danger:         #e0554a;
    --danger-strong:  #b23a30;
    --success:        #45c17a;
    --info:           #4fa8e8;
    --shadow-lg:      0 20px 50px -12px rgba(0,0,0,.75);
    --shadow-sm:      0 6px 18px rgba(0,0,0,.45);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html { color-scheme: dark; }

  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding-bottom: 96px;
    -webkit-font-smoothing: antialiased;
  }

  ::selection { background: var(--accent-dim); color: var(--accent-soft); }

  ::-webkit-scrollbar { width: 12px; height: 12px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: #1c2230; border-radius: 8px; border: 3px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background: #29344a; }

  /* Topbar */
  .topbar {
    background: rgba(10,11,15,.72);
    backdrop-filter: blur(14px) saturate(140%);
    -webkit-backdrop-filter: blur(14px) saturate(140%);
    border-bottom: 1px solid var(--card-border);
    color: var(--text);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .topbar .brand { display: flex; align-items: center; gap: 10px; }
  .topbar .brand svg { color: var(--accent); flex-shrink: 0; }
  .topbar h1 {
    font-size: 1.15rem;
    font-weight: 700;
    letter-spacing: .01em;
    color: var(--text);
  }
  .topnav { display: flex; gap: 22px; margin-left: 30px; }
  .topnav-link {
    position: relative;
    padding: 4px 2px 14px;
    font-size: .78rem;
    font-weight: 600;
    letter-spacing: .09em;
    text-transform: uppercase;
    color: var(--text-faint);
    text-decoration: none;
    transition: color .18s;
  }
  .topnav-link::after {
    content: '';
    position: absolute; left: 0; right: 0; bottom: 0;
    height: 2px; border-radius: 2px;
    background: var(--accent);
    transform: scaleX(0);
    transition: transform .2s ease;
  }
  .topnav-link:hover  { color: var(--text-muted); }
  .topnav-link.active { color: var(--accent-soft); }
  .topnav-link.active::after { transform: scaleX(1); }

  .container { max-width: 860px; margin: 40px auto; padding: 0 20px; }

  /* Cards */
  .card {
    background: var(--card);
    border-radius: 14px;
    padding: 26px 28px;
    margin-bottom: 22px;
    border: 1px solid var(--card-border);
    box-shadow: var(--shadow-lg);
    position: relative;
  }
  .card-title {
    font-size: .74rem;
    font-weight: 700;
    color: var(--accent-soft);
    text-transform: uppercase;
    letter-spacing: .14em;
    margin-bottom: 20px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--card-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .title-lead { display: flex; align-items: center; gap: 11px; }
  .title-bar { width: 3px; height: 22px; min-width: 3px; border-radius: 3px; background: var(--accent); }
  .card-title .title-note {
    font-size: .74rem; font-weight: 400; color: var(--text-faint);
    text-transform: none; letter-spacing: 0;
  }

  /* Form */
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 22px; }
  .form-row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
  .form-row label { font-size: .76rem; font-weight: 600; color: var(--accent-soft); letter-spacing: .02em; }
  .form-row input, .form-row textarea, .form-row select {
    padding: 9px 12px;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    font-size: .87rem;
    color: var(--text);
    font-family: inherit;
    transition: border-color .15s, box-shadow .15s, background .15s;
    background: var(--elevated);
  }
  .form-row input:focus, .form-row textarea:focus, .form-row select:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-dim);
  }
  .form-row select option { background: #14161f; }
  .form-row textarea { resize: vertical; min-height: 68px; line-height: 1.55; }
  .form-row-checkbox { display: flex; align-items: center; gap: 9px; margin-bottom: 16px; cursor: pointer; }
  .form-row-checkbox input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; flex-shrink: 0; }
  .form-row-checkbox span { font-size: .8rem; font-weight: 500; color: var(--text); }
  .hint { font-size: .72rem; color: var(--text-faint); margin-top: 1px; }
  .span-2 { grid-column: span 2; }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 9px 18px;
    border-radius: 8px;
    font-size: .82rem;
    font-weight: 600;
    letter-spacing: .01em;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background .18s, border-color .18s, opacity .15s, transform .1s;
    font-family: inherit;
    white-space: nowrap;
  }
  .btn svg { flex-shrink: 0; }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }

  .btn-primary {
    background: var(--accent);
    color: #04101f;
  }
  .btn-primary:hover:not(:disabled) { background: #2f7de0; }

  .btn-danger {
    background: var(--danger);
    color: #200604;
  }
  .btn-danger:hover:not(:disabled) { background: #c8483d; }

  .btn-outline {
    background: rgba(63,142,240,.06);
    color: var(--accent-soft);
    border-color: var(--accent-line);
  }
  .btn-outline:hover:not(:disabled) { background: var(--accent-dim); border-color: var(--accent); }

  .btn-ghost-danger {
    background: transparent;
    color: #d59892;
    border-color: transparent;
    padding: 6px 10px;
  }
  .btn-ghost-danger:hover:not(:disabled) { background: rgba(224,85,74,.12); color: #eab3ae; }

  .btn-sm { padding: 6px 13px; font-size: .76rem; }

  /* Status pill */
  #status {
    font-size: .8rem;
    font-weight: 600;
    padding: 7px 14px;
    border-radius: 999px;
    display: none;
    border: 1px solid transparent;
  }
  #status.ok   { background: rgba(69,193,122,.12); color: var(--success); border-color: rgba(69,193,122,.3); display: inline-flex; align-items: center; }
  #status.err  { background: rgba(224,85,74,.12); color: #e6928c; border-color: rgba(224,85,74,.3); display: inline-flex; align-items: center; }
  #status.info { background: rgba(127,168,201,.12); color: var(--info); border-color: rgba(127,168,201,.3); display: inline-flex; align-items: center; }

  /* Sticky bottom action bar (shared shape for save-bar / action-bar) */
  /* All bottom-bar controls are always centered as a group — a lone or a
     paired button set should sit in the visual middle, never hug an edge.
     Meta text (if any) is anchored to the right independently so it never
     throws the centering off. */
  .bottom-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: rgba(13,14,20,.82);
    backdrop-filter: blur(14px) saturate(140%);
    -webkit-backdrop-filter: blur(14px) saturate(140%);
    border-top: 1px solid var(--card-border);
    padding: 14px 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 18px;
    box-shadow: 0 -10px 30px rgba(0,0,0,.35);
    z-index: 100;
  }
  .bottom-bar .bar-actions { display: flex; align-items: center; justify-content: center; gap: 12px; }
  .bottom-bar .bar-meta {
    position: absolute; right: 32px; top: 50%; transform: translateY(-50%);
    font-size: .74rem; color: var(--text-faint); text-align: right;
  }
  .bottom-bar .bar-meta code { font-size: .73rem; color: var(--accent-soft); }

  @media (max-width: 560px) {
    .form-grid { grid-template-columns: 1fr; }
    .span-2 { grid-column: span 1; }
    .bottom-bar { flex-direction: column; align-items: stretch; }
    .bottom-bar .bar-meta { position: static; transform: none; text-align: center; }
  }
"""

_BRAND_SVG = r"""<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6.5 12 13l9-6.5"/><rect x="3" y="5" width="18" height="14" rx="2.5"/></svg>"""

# ── Settings page ──────────────────────────────────────────────────────────────
_SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EmailManager — Settings</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
""" + _BASE_CSS + r"""

  /* Account cards */
  .acct-card {
    border: 1px solid var(--card-border);
    border-radius: 11px;
    margin-bottom: 12px;
    overflow: hidden;
    transition: border-color .15s;
    background: var(--elevated);
  }
  .acct-card:hover { border-color: var(--card-border-hi); }

  .acct-header {
    display: flex;
    align-items: center;
    padding: 13px 16px;
    cursor: pointer;
    user-select: none;
    gap: 10px;
  }
  .acct-header:hover { background: rgba(91,147,209,.06); }
  .acct-name  { font-weight: 600; font-size: .89rem; flex: 1; color: var(--text); }
  .acct-email { font-size: .78rem; color: var(--text-faint); }
  .chevron { transition: transform .2s; color: var(--accent); flex-shrink: 0; }
  .chevron.open { transform: rotate(180deg); }

  .acct-body { padding: 4px 18px 18px; display: none; }
  .acct-body.open { display: block; }
  .acct-footer { display: flex; justify-content: flex-end; padding-top: 4px; border-top: 1px dashed var(--card-border); margin-top: 4px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">""" + _BRAND_SVG + r"""<h1>EmailManager</h1></div>
  <nav class="topnav">
    <a href="/" class="topnav-link active">Settings</a>
    <a href="/cleanup" class="topnav-link">Cleanup</a>
  </nav>
</div>

<div class="container">

  <div class="card">
    <div class="card-title"><span class="title-lead"><span class="title-bar"></span>General</span></div>
    <div class="form-grid">
      <div class="form-row">
        <label for="interval">Check interval (seconds)</label>
        <input type="number" id="interval" min="10" step="5" value="30">
        <span class="hint">Minimum: 10 seconds</span>
      </div>
      <div class="form-row">
        <label for="notify_max_age_days">Only notify unread mail received in the last (days)</label>
        <input type="number" id="notify_max_age_days" min="0" step="1" value="5">
        <span class="hint">Prevents a flood of notifications for old unread mail. 0 = no limit.</span>
      </div>
    </div>
    <label class="form-row-checkbox" style="margin-top:2px">
      <input type="checkbox" id="start_with_windows">
      <span>Start EmailManager automatically when Windows starts</span>
    </label>
  </div>

  <div class="card">
    <div class="card-title">
      <span class="title-lead"><span class="title-bar"></span>Email Accounts</span>
      <button class="btn btn-outline btn-sm" onclick="addAccount()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        Add Account
      </button>
    </div>
    <div id="accounts-list"></div>
    <div id="no-accounts" style="text-align:center;padding:30px 0;color:var(--text-faint);font-size:.86rem;display:none">
      No accounts configured. Click "Add Account" to get started.
    </div>
  </div>

</div>

<div class="bottom-bar">
  <div class="bar-actions">
    <button class="btn btn-primary" onclick="saveSettings()">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>
      Save &amp; Apply
    </button>
    <span id="status"></span>
  </div>
  <span class="bar-meta">Log: <code>{{ log_path }}</code><br>Saving restarts monitoring.</span>
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
        <label class="form-row-checkbox span-2">
          <input type="checkbox" name="notify" checked>
          <span>Enable notifications for this account</span>
        </label>
        <label class="form-row-checkbox span-2">
          <input type="checkbox" name="notify_non_important" checked>
          <span>Notify non-important emails</span>
        </label>
        <div class="form-row">
          <label>URL to open on notification</label>
          <input type="url" name="url" placeholder="https://mail.google.com" value="https://mail.google.com">
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
          <input type="text" name="imap_server" placeholder="imap.gmail.com" value="imap.gmail.com">
        </div>
        <div class="form-row">
          <label>IMAP port</label>
          <input type="number" name="imap_port" value="993">
        </div>
        <label class="form-row-checkbox span-2">
          <input type="checkbox" name="verify_ssl" checked>
          <span>Verify SSL certificate (uncheck only for a trusted internal server with a self-signed certificate)</span>
        </label>
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
          <label style="color:var(--accent-soft)">Importants</label>
          <textarea name="important_list" placeholder="ceo@company.com&#10;@vip-domain.com" style="border-color:var(--accent-line)"></textarea>
          <span class="hint">One sender per line. These always trigger notifications and the email is <strong>left unread</strong> until you read it manually.</span>
        </div>
      </div>
      <div class="acct-footer">
        <button class="btn btn-ghost-danger btn-sm" onclick="removeAcct(this)">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0-1 14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2L4 6"/></svg>
          Remove Account
        </button>
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
    card.querySelector('[name=verify_ssl]').checked = acct.imap?.verify_ssl     !== false;
    card.querySelector('[name=black_list]').value     = (acct.black_list     || []).join('\n');
    card.querySelector('[name=white_list]').value     = (acct.white_list     || []).join('\n');
    card.querySelector('[name=important_list]').value = (acct.important_list || []).join('\n');
    card.querySelector('[name=notify]').checked                 = acct.notify                 !== false;
    card.querySelector('[name=notify_non_important]').checked   = acct.notify_non_important    !== false;
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
  const c     = name => card.querySelector(`[name=${name}]`).checked;
  return {
    nome: v('nome'),
    url:  v('url'),
    account: { email: v('email'), auth_value: v('auth_value'), auth_type: 'password' },
    imap:    { server: v('imap_server'), port: parseInt(v('imap_port')) || 993, verify_ssl: c('verify_ssl') },
    black_list:     lines(v('black_list')),
    white_list:     lines(v('white_list')),
    important_list: lines(v('important_list')),
    notify:               c('notify'),
    notify_non_important: c('notify_non_important'),
  };
}

async function loadConfig() {
  const res    = await fetch('/api/config');
  const config = await res.json();
  document.getElementById('interval').value = config.interval || 30;
  document.getElementById('notify_max_age_days').value = config.notify_max_age_days ?? 5;
  document.getElementById('start_with_windows').checked = !!config.start_with_windows;
  (config.accounts || []).forEach(appendCard);
  syncEmpty();
}

async function saveSettings() {
  const payload = {
    interval: parseInt(document.getElementById('interval').value) || 30,
    notify_max_age_days: parseInt(document.getElementById('notify_max_age_days').value) || 0,
    start_with_windows: document.getElementById('start_with_windows').checked,
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
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
""" + _BASE_CSS + r"""

  /* Results table */
  .results-wrap { overflow-x: auto; margin-top: 4px; border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: .82rem; }
  thead th {
    text-align: left; padding: 9px 12px; font-weight: 600;
    color: var(--text-faint); border-bottom: 1px solid var(--card-border);
    text-transform: uppercase; font-size: .7rem; letter-spacing: .08em;
  }
  tbody tr { border-bottom: 1px solid rgba(255,255,255,.04); }
  tbody tr:hover { background: rgba(91,147,209,.05); }
  tbody td { padding: 8px 12px; color: var(--text-muted); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: .72rem; font-weight: 700; background: var(--accent-dim); color: var(--accent-soft);
    border: 1px solid var(--accent-line);
  }
  .warn-box {
    background: rgba(224,85,74,.1); border: 1px solid rgba(224,85,74,.32);
    color: #e29d97; border-radius: 8px; padding: 11px 15px;
    font-size: .82rem; margin-top: 4px; margin-bottom: 14px; display: none;
  }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">""" + _BRAND_SVG + r"""<h1>EmailManager</h1></div>
  <nav class="topnav">
    <a href="/" class="topnav-link">Settings</a>
    <a href="/cleanup" class="topnav-link active">Cleanup</a>
  </nav>
</div>

<div class="container">

  <div class="card">
    <div class="card-title"><span class="title-lead"><span class="title-bar"></span>Target</span></div>
    <div class="form-grid">
      <div class="form-row">
        <label for="account">Account</label>
        <select id="account"><option value="">— select account —</option></select>
      </div>
      <div class="form-row">
        <label for="folder">Folder</label>
        <select id="folder"><option value="INBOX">INBOX</option></select>
        <span class="hint" id="folder-hint">Select an account to load its real folder list.</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title"><span class="title-lead"><span class="title-bar"></span>Filters</span> <span class="title-note">— at least one required, combined with AND</span></div>
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
      <span class="title-lead"><span class="title-bar"></span>Results&nbsp;&nbsp;<span class="badge" id="results-count">0</span></span>
      <span class="title-note" id="preview-note"></span>
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

<div class="bottom-bar">
  <div class="bar-actions">
    <button class="btn btn-outline" id="preview-btn" onclick="doPreview()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      Preview
    </button>
    <button class="btn btn-danger" id="delete-btn" style="display:none" onclick="doDelete()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0-1 14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2L4 6"/></svg>
      Delete <span id="delete-count">0</span> emails permanently
    </button>
    <span id="status"></span>
  </div>
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
  sel.addEventListener('change', loadFolders);
}

async function loadFolders() {
  const acct = document.getElementById('account').value;
  const sel  = document.getElementById('folder');
  const hint = document.getElementById('folder-hint');
  sel.innerHTML = '<option value="INBOX">INBOX</option>';
  if (!acct) { hint.textContent = 'Select an account to load its real folder list.'; return; }
  hint.textContent = 'Loading folders…';
  try {
    const res  = await fetch('/api/cleanup/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ account: acct }),
    });
    const data = await res.json();
    if (data.error) { hint.textContent = data.error; return; }
    sel.innerHTML = '';
    (data.folders || []).forEach(f => {
      const o = document.createElement('option');
      o.value = f; o.textContent = f;
      if (f.toUpperCase() === 'INBOX') o.selected = true;
      sel.appendChild(o);
    });
    hint.textContent = `${data.folders.length} folder(s) found.`;
  } catch (e) {
    hint.textContent = 'Could not load folders.';
  }
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
@web_app.route('/favicon.ico')
def favicon():
    return send_file(os.path.join(BASE_DIR, 'app.ico'), mimetype='image/vnd.microsoft.icon')

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
        if 'notifications_enabled' not in data:
            data['notifications_enabled'] = read_config().get('notifications_enabled', True)
        write_config(data)
        set_start_with_windows(bool(data.get('start_with_windows', False)))
        threading.Thread(target=_restart, daemon=True).start()
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@web_app.route('/cleanup')
def cleanup_page():
    return render_template_string(_CLEANUP_HTML)

@web_app.route('/api/cleanup/folders', methods=['POST'])
def api_cleanup_folders():
    body      = request.get_json(silent=True) or {}
    acct_name = body.get('account', '')

    config   = read_config()
    acct_cfg = next((a for a in config.get('accounts', []) if a['nome'] == acct_name), None)
    if not acct_cfg:
        return jsonify({'error': 'Account not found.'}), 400

    try:
        conn    = _imap_connect(acct_cfg)
        folders = _list_folders(conn)
        conn.logout()
        return jsonify({'folders': folders})
    except Exception as exc:
        _logger.exception('Cleanup folders error: %s', exc)
        return jsonify({'error': str(exc)}), 500

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
        status, _ = conn.select(_imap_folder(folder), readonly=True)
        if status != 'OK':
            conn.logout()
            return jsonify({'error': f'Folder not found: {folder}'}), 400
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
        status, _ = conn.select(_imap_folder(folder), readonly=False)
        if status != 'OK':
            conn.logout()
            return jsonify({'error': f'Folder not found: {folder}'}), 400
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
    _startup_config = read_config()
    _notifications_enabled = _startup_config.get('notifications_enabled', True)
    set_start_with_windows(bool(_startup_config.get('start_with_windows', False)))

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    tray_icon = Icon(
        'EmailManager',
        Image.open(ICON_ACTIVE),
        menu=Menu(
            MenuItem(
                lambda item: f'Notifications: {"On" if _notifications_enabled else "Off"}',
                on_toggle_notifications,
            ),
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
