from __future__ import annotations

from ezmail import EzReader, EzMail
from subprocess import run, STARTUPINFO, STARTF_USESHOWWINDOW, CREATE_NO_WINDOW
from typing import Any
import threading, sys, os
from pystray import Icon, MenuItem, Menu
from PIL import Image
from re import findall
from json import load

# Resolve paths so the app works both as a script and as a PyInstaller exe.
# BASE_DIR points to bundled resources (icons); DATA_DIR points to data.json.
IS_FROZEN: bool = bool(getattr(sys, 'frozen', False))
BASE_DIR: str = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
DATA_DIR: str = os.path.dirname(sys.executable) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))

ICON_ACTIVE = os.path.join(BASE_DIR, 'active.ico')
ICON_STOPPED = os.path.join(BASE_DIR, 'stopped.ico')

stop_event = threading.Event()
bg_thread: threading.Thread | None = None
icon: Icon | None = None


def load_config() -> tuple[list[dict[str, Any]], int]:
    path = os.path.join(DATA_DIR, 'data.json')
    with open(path, 'r', encoding='utf-8') as f:
        data: Any = load(f)

    if isinstance(data, list):
        accounts: list[dict[str, Any]] = data
        interval = 30
    else:
        accounts = data.get('accounts', [])
        interval = int(data.get('interval', 30))

    readers: list[dict[str, Any]] = []
    for d in accounts:
        readers.append({
            'name': d['nome'],
            'reader': EzReader(d['imap'], d['account']),
            'url': d.get('url', ''),
            'black_list': d.get('black_list', []),
            'white_list': d.get('white_list', []),
        })
    return readers, interval


def _ps_escape(value: str) -> str:
    # In a PowerShell single-quoted string, a literal ' is written as ''.
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


def process_account(
    name: str,
    reader: EzReader,
    url: str,
    black_list: list[str],
    white_list: list[str],
) -> None:
    try:
        with reader as r:
            emails: list[EzMail] = r.fetch_unread()
            for email in emails:
                sender: str = findall(r"<(.*?)>", email.sender)[0] if '<' in email.sender else email.sender

                if any(b in sender for b in black_list):
                    r.move_to_trash(email)
                    continue

                if white_list and not any(w in sender for w in white_list):
                    r.mark_as_read(email)
                    continue

                message = f'From: {sender}\nSubject: {email.subject}'
                body = email.body.lower()
                if ' code ' in body or ' código ' in body:
                    message += f'\nBody: {email.body.replace(chr(10), " ")}'

                notify(f'New email — {name}', message, url)
    except Exception as exc:
        notify('EmailManager', f'Error reading {name}: {type(exc).__name__}: {exc}')


def _background_loop(event: threading.Event) -> None:
    config_path = os.path.join(DATA_DIR, 'data.json')
    try:
        readers, interval = load_config()
    except FileNotFoundError:
        notify('EmailManager', f'data.json not found.\nCreate it at: {config_path}')
        return
    except Exception as exc:
        notify('EmailManager', f'Could not load data.json: {exc}')
        return

    while not event.is_set():
        for account in readers:
            if event.is_set():
                break
            process_account(
                account['name'], account['reader'],
                account['url'], account['black_list'], account['white_list'],
            )
        event.wait(interval)


def _update_icon() -> None:
    if icon:
        icon.icon = Image.open(ICON_ACTIVE if not stop_event.is_set() else ICON_STOPPED)


def _start() -> None:
    global stop_event, bg_thread
    if bg_thread and bg_thread.is_alive():
        return
    stop_event = threading.Event()
    bg_thread = threading.Thread(target=_background_loop, args=(stop_event,), daemon=True)
    bg_thread.start()
    _update_icon()
    notify('EmailManager', 'Monitoring started.')


def _stop() -> None:
    stop_event.set()
    _update_icon()
    notify('EmailManager', 'Monitoring stopped.')


def on_start(_icon: Icon, _item: MenuItem) -> None:
    _start()

def on_restart(_icon: Icon, _item: MenuItem) -> None:
    # Signal the old thread with its own event, then spin up a fresh one.
    old_event = stop_event
    old_event.set()
    global bg_thread
    bg_thread = None
    _start()

def on_stop(_icon: Icon, _item: MenuItem) -> None:
    _stop()

def on_exit(tray: Icon, _item: MenuItem) -> None:
    stop_event.set()
    notify('EmailManager', 'Exiting...')
    tray.stop()
    os._exit(0)


if __name__ == '__main__':
    icon = Icon(
        'EmailManager',
        Image.open(ICON_ACTIVE),
        menu=Menu(
            MenuItem('Start',   on_start),
            MenuItem('Restart', on_restart),
            MenuItem('Stop',    on_stop),
            MenuItem('Exit',    on_exit),
        ),
    )
    _start()
    icon.run()
