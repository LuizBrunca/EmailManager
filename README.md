# EmailManager

A lightweight Windows desktop app that monitors multiple IMAP email accounts and fires native Windows toast notifications when new emails arrive. Runs silently in the system tray — no browser, no console window.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Multi-account** — monitor as many IMAP accounts as you want simultaneously
- **Native Windows notifications** — uses `Windows.UI.Notifications` with an "Open email" action button that opens your webmail
- **System tray control** — Start / Restart / Stop / Toggle Notifications / Exit, no console window
- **Blacklist** — senders on the blacklist are automatically moved to trash and skipped
- **Whitelist** — when set, only senders on the whitelist trigger notifications; all others are silently marked as read
- **Importants** — senders on this list always trigger notifications and the email is **left unread** so alerts repeat every cycle until you read it manually
- **Per-account notification toggles** — mute all notifications for an account, or just regular (non-important) mail, independently per account
- **Global notifications toggle** — mute all toast notifications from the tray icon with one click, no need to open Settings
- **Code detection** — emails whose body contains the word "code" or "código" show the full body inline in the notification (useful for OTP / auth codes)
- **Configurable interval** — set polling frequency in the Settings UI
- **Web UI** — configure everything through a built-in settings page; no manual file editing needed
- **Portable** — compiles to a single `.exe` with no installer needed

---

## Requirements

- Windows 10 or later
- Python 3.9+ (only needed to run from source or to build the `.exe`)

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/EmailManager.git
cd EmailManager
```

### 2. Install dependencies

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run

```bash
python emailmanager.py
```

On first launch the Settings page opens automatically in your browser. Right-click the tray icon at any time to reopen it.

---

## Configuration

All settings are managed through the built-in web UI at `http://127.0.0.1:5050`.

**Per-account fields:**

| Field | Description |
|---|---|
| Account name | Display name shown in notifications |
| IMAP server / port | e.g. `imap.gmail.com` / `993` |
| Email / Password | Your credentials or app token |
| URL | Webmail URL opened when you click a toast notification |
| Blacklist | Senders auto-moved to trash (one per line) |
| Whitelist | If non-empty, only these senders trigger notifications (one per line) |
| Importants | These senders always notify and the email is left unread until you read it manually (one per line) |
| Notify | Master switch for this account — off silences everything for it, including error notices (default: on) |
| Notify non-important | Off = regular emails still marked read but not notified; Importants unaffected (default: on) |

**Sender matching** is by substring — `@spam.com`, `newsletter`, and `noreply` all work. Full addresses are not required.

**Priority order** (evaluated top to bottom per email):

| Priority | List | Action |
|---|---|---|
| 1 | Blacklist | Move to trash, skip |
| 2 | Importants | Notify, **leave unread** (repeats every cycle) |
| 3 | Whitelist | If set and sender not matched, mark as read silently |
| 4 | Default | Notify and mark as read |

The **Notify** and **Notify non-important** toggles apply on top of this priority order, not instead of it: turning **Notify** off silences everything for that account (steps 2 and 4 above still run their read/unread bookkeeping, just without a toast, and step-4's error notices are silenced too); turning off **Notify non-important** only silences step 4, leaving Importants (step 2) unaffected.

> **Gmail note:** Generate an [App Password](https://myaccount.google.com/apppasswords) — IMAP with 2FA requires it.

Saving in the UI automatically restarts monitoring with the new settings.

### Global notifications toggle

Right-click the tray icon → **Notifications: On/Off** mutes all toast notifications globally — including error/system messages like "Exiting…" or "Could not load config" — regardless of per-account settings. The label reflects the current state and toggling takes effect immediately, no restart needed. The setting persists to `data.json` and is honored on next launch.

---

## Cleanup

Right-click the tray icon → **Cleanup** to bulk-delete emails by sender, date range, or both — with a preview before deletion.

---

## Building a standalone `.exe`

Double-click `build.bat`, or run manually:

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --icon=app.ico --add-data "active.ico;." --add-data "stopped.ico;." emailmanager.py
```

Output: `dist\emailmanager.exe`

The config is stored in `C:\ProgramData\EmailManager\data.json` — separate from the exe so you never need to rebuild after changing settings.

---

## Auto-start with Windows

1. Press `Win + R`, type `shell:startup`, press Enter
2. Create a shortcut to `emailmanager.exe` in that folder

The app will launch automatically on every login.

---

## Project structure

```
EmailManager/
├── emailmanager.py       # Application source
├── active.ico            # Tray icon — monitoring active
├── stopped.ico           # Tray icon — monitoring stopped
├── app.ico               # Executable icon
├── build.bat             # One-click exe builder
└── requirements.txt      # Python dependencies
```

---

## License

MIT
