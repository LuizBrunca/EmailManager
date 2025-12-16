from ezmail import EzReader, EzMail
from time import sleep
from subprocess import run, STARTUPINFO, STARTF_USESHOWWINDOW, CREATE_NO_WINDOW
import threading, sys, os
from pystray import Icon, MenuItem, Menu
from PIL import Image
from re import findall
from json import load

DATA_DIR = r'C:\Brunca\EmailManager'
INTERVAL = 30

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ICON_ATIVO = os.path.join(BASE_DIR, "active.ico")
ICON_PARADO = os.path.join(BASE_DIR, "stopped.ico")

running = True


def read_json():
    with open(f'{DATA_DIR}\data.json', 'r') as f:
        data = load(f)
    
    readers = []
    
    for d in data:
        readers.append({
            'nome': d['nome'],
            'reader': EzReader(d["imap"], d["account"]),
            'url': d['url'],
            'black_list': d['black_list'],
            'white_list': d['white_list']
        })
    
    return readers

def notifier(title: str, message: str, url: str = None):
    """Exibe notificação do Windows com botão 'Abrir email' (sem registrar AppID)."""
    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
    $texts = $xml.GetElementsByTagName("text")
    $texts.Item(0).AppendChild($xml.CreateTextNode('{title}')) > $null
    $texts.Item(1).AppendChild($xml.CreateTextNode('{message}')) > $null
    """

    if url:
        ps_script += f"""
        if ('{url}' -ne '') {{
            $actions = $xml.CreateElement("actions")
            $action = $xml.CreateElement("action")
            $action.SetAttribute("content", "Abrir email")
            $action.SetAttribute("arguments", "{url}")
            $action.SetAttribute("activationType", "protocol")
            $actions.AppendChild($action) > $null
            $xml.DocumentElement.AppendChild($actions) > $null
        }}
        """

    ps_script += """
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('EmailNotifier')
    $notifier.Show($toast)
    """

    si = STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        startupinfo=si,
        creationflags=CREATE_NO_WINDOW,
    )

def read_emails(reader_name: str, reader: EzReader, url: str = None, black_list: list[str] = [], white_list: list[str] = []):
    with reader as r:
        emails: list[EzMail] = r.fetch_unread()
        for email in emails:
            sender = findall(r"<(.*?)>", email.sender)[0] if "<" in email.sender else email.sender
                
            if not any(w in sender for w in white_list):
                r.mark_as_read(email)
                
            if any(b in sender for b in black_list):
                r.move_to_trash(email)
                continue
            
            title = f"Novo email para {reader_name}"
            message = f"De: {sender}\nAssunto: {email.subject}"
            
            if ' código ' in email.body.lower() or ' code ' in email.body.lower():
                message += f"\nCorpo: {email.body.replace('\n', ' ')}"
            
            notifier(title, message, url)

def background_task():
    readers = read_json()
    
    while running:
        for r in readers:
            read_emails(r['nome'], r['reader'], r['url'], r['black_list'], r['white_list'])
            
        for _ in range(INTERVAL):
            if not running:
                break
            sleep(1)

def carregar_icone(p): return Image.open(p)
def atualizar_icone(): icon.icon = carregar_icone(ICON_ATIVO if running else ICON_PARADO)

def on_start(i, _):
    global running
    if not running:
        running = True
        atualizar_icone()
        threading.Thread(target=background_task, daemon=True).start()
        notifier("Sistema", "Iniciando o EmailManager...")

def on_restart(i, _):
    on_stop(i, _)
    on_start(i, _)

def on_stop(i, _):
    global running
    running = False
    atualizar_icone()
    notifier("Sistema", "Parando o EmailManager...")

def on_exit(i, _):
    global running
    running = False
    notifier("Sistema", "Encerrando o EmailManager...")
    i.stop()
    os._exit(0)


if __name__ == "__main__":
    icon = Icon("EmailManager", carregar_icone(ICON_ATIVO),
                menu=Menu(
                    MenuItem("Iniciar", on_start),
                    MenuItem("Reiniciar", on_restart),
                    MenuItem("Parar", on_stop),
                    MenuItem("Sair", on_exit)
                ))

    threading.Thread(target=background_task, daemon=True).start()
    notifier("Sistema", "Iniciando o EmailManager...")
    icon.run()
