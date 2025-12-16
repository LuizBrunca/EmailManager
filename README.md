EmailManager

O EmailManager é uma aplicação em Python que monitora contas de e-mail via IMAP e exibe notificações no Windows para novos e-mails, com suporte a lista branca, lista negra e múltiplas contas. Ele roda em segundo plano com ícone na bandeja do sistema.

📁 Estrutura básica

emailmanager.py → arquivo principal da aplicação

data.json → arquivo de configuração das contas (não incluído por padrão)

data_example.json → exemplo de configuração

active.ico → ícone quando o serviço está ativo

stopped.ico → ícone quando o serviço está parado

app.ico → ícone do executável

⚙️ Configuração inicial
1️⃣ Ajustar o caminho do data.json

No arquivo emailmanager.py, edite a variável abaixo para apontar para o diretório desejado:

DATA_DIR = r'C:\Brunca\EmailManager'


📌 Importante:
O arquivo final data.json deve ficar dentro desse diretório definido.

2️⃣ Configurar o arquivo de dados

Abra o arquivo data_example.json

Ajuste os campos conforme sua necessidade:

nome → nome identificador da conta

imap.server e imap.port

account.email

account.auth_value (senha ou token)

black_list → remetentes a serem descartados

white_list → remetentes permitidos

url → link para abrir o webmail

Exemplo:

{
    "nome": "Conta Principal",
    "imap": { "server": "imap.exemplo.com", "port": 993 },
    "account": {
        "email": "email@exemplo.com",
        "auth_value": "senha",
        "auth_type": "password"
    },
    "url": "https://mail.google.com/",
    "black_list": [],
    "white_list": []
}


Após configurar, renomeie o arquivo:

data_example.json → data.json

🛠️ Gerando o executável (.exe)
3️⃣ Instalar o PyInstaller

Certifique-se de que o Python esteja instalado e execute:

pip install pyinstaller

4️⃣ Gerar o executável

No diretório onde está o arquivo emailmanager.py, execute:

pyinstaller --noconsole --onefile --icon=app.ico --add-data "active.ico;." --add-data "stopped.ico;." emailmanager.py


📌 Esse comando:

Gera um único .exe

Oculta o console

Inclui os ícones necessários

Define o ícone do aplicativo

▶️ Executando o EmailManager

O executável final estará em:

dist/emailmanager.exe


Você pode mover o .exe para qualquer pasta de sua preferência

Basta executar o arquivo para iniciar o monitoramento

🔄 Inicialização com o Windows (opcional)

Se desejar que o EmailManager inicie automaticamente com o sistema:

Pressione Win + R

Digite:

shell:startup


Crie um atalho do emailmanager.exe nessa pasta

Pronto 🎉 — o aplicativo iniciará junto com o Windows.

🧠 Observações finais

O controle do serviço (Iniciar, Parar, Reiniciar, Sair) é feito pelo ícone na bandeja

As notificações funcionam sem necessidade de registro de AppID

Compatível com múltiplas contas IMAP