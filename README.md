# EmailManager

A lightweight Windows desktop app that monitors multiple IMAP email accounts and fires native Windows toast notifications when new emails arrive. Runs silently in the system tray — no browser, no console window.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Multi-account** — monitor as many IMAP accounts as you want simultaneously
- **Native Windows notifications** — uses `Windows.UI.Notifications` with an "Open email" action button that opens your webmail
- **System tray control** — Start / Restart / Stop / Exit, no console window
- **Blacklist** — senders on the blacklist are automatically moved to trash and skipped
- **Whitelist** — when set, only senders on the whitelist trigger notifications; all others are silently marked as read
- **Code detection** — emails whose body contains the word "code" or "código" show the full body inline in the notification (useful for OTP / auth codes)
- **Configurable interval** — set polling frequency in `data.json`
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

### 3. Configure your accounts

```bash
copy data_example.json data.json
```

Edit `data.json`:

```json
{
    "interval": 30,
    "accounts": [
        {
            "nome": "My Gmail",
            "imap": { "server": "imap.gmail.com", "port": 993 },
            "account": {
                "email": "you@gmail.com",
                "auth_value": "your-app-password",
                "auth_type": "password"
            },
            "url": "https://mail.google.com",
            "black_list": ["noreply@spam.com"],
            "white_list": []
        }
    ]
}
```

**Fields:**

| Field | Description |
|---|---|
| `interval` | Polling interval in seconds (default: `30`) |
| `nome` | Display name shown in notifications |
| `imap.server` | IMAP hostname (e.g. `imap.gmail.com`) |
| `imap.port` | IMAP port — almost always `993` (SSL) |
| `account.email` | Your email address |
| `account.auth_value` | Password or app token |
| `account.auth_type` | Authentication type (`"password"`) |
| `url` | Webmail URL opened when you click the notification |
| `black_list` | Senders auto-moved to trash (no notification) |
| `white_list` | If non-empty, only these senders trigger notifications |

> **Gmail note:** Generate an [App Password](https://myaccount.google.com/apppasswords) — IMAP with 2FA requires it.

> **Security:** `data.json` is in `.gitignore` and will never be committed. Never add it manually.

### 4. Run

```bash
python emailmanager.py
```

The app starts minimized to the system tray. Right-click the tray icon to control it.

---

## Building a standalone `.exe`

Double-click `build.bat`, or run manually:

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --icon=app.ico --add-data "active.ico;." --add-data "stopped.ico;." emailmanager.py
```

Output: `dist\emailmanager.exe`

**Deployment layout — `data.json` lives next to the exe, not inside it:**

```
anywhere\
├── emailmanager.exe   ← compiled app (never changes)
└── data.json          ← your config (edit freely, no recompile needed)
```

You can update accounts, passwords, or the polling interval by editing `data.json` and restarting the app from the tray — no rebuild required. If `data.json` is missing, the app will show a notification with the exact path where it expects the file.

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
├── data_example.json     # Config template (safe to commit)
├── data.json             # Your config with credentials (gitignored)
├── active.ico            # Tray icon — monitoring active
├── stopped.ico           # Tray icon — monitoring stopped
├── app.ico               # Executable icon
├── build.bat             # One-click exe builder
└── requirements.txt      # Python dependencies
```

---

## License

MIT
