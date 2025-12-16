# 📧 EmailManager

O **EmailManager** é uma aplicação desenvolvida em **Python** para monitoramento de contas de e-mail via **IMAP**, exibindo **notificações nativas do Windows** sempre que novos e-mails são recebidos.  
Suporta **múltiplas contas**, **lista branca**, **lista negra** e execução em **segundo plano** com ícone na bandeja do sistema.

---

## 📂 Estrutura do Projeto

```
EmailManager/
├── emailmanager.py      # Arquivo principal da aplicação
├── data.json            # Arquivo de configuração (criado pelo usuário)
├── data_example.json    # Exemplo de configuração
├── active.ico           # Ícone do sistema ativo
├── stopped.ico          # Ícone do sistema parado
└── app.ico              # Ícone do executável (.exe)
```

---

## ⚙️ Configuração Inicial

### 1️⃣ Definir o caminho do `data.json`

No arquivo **`emailmanager.py`**, edite a variável abaixo conforme o local onde o arquivo `data.json` ficará armazenado:

```python
DATA_DIR = r'C:\Brunca\EmailManager'
```

> ⚠️ **Atenção**  
> O arquivo `data.json` **deve obrigatoriamente** estar dentro do diretório definido acima.

---

### 2️⃣ Configurar as contas de e-mail

1. Abra o arquivo **`data_example.json`**
2. Edite os dados conforme sua necessidade:

**Campos disponíveis:**
- `nome` → Nome identificador da conta
- `imap.server` → Servidor IMAP
- `imap.port` → Porta do servidor
- `account.email` → Endereço de e-mail
- `account.auth_value` → Senha ou token
- `account.auth_type` → Tipo de autenticação
- `black_list` → Lista de remetentes a serem ignorados
- `white_list` → Lista de remetentes permitidos
- `url` → Link do webmail para abertura direta

3. Após a configuração, **renomeie o arquivo**:

```
data_example.json → data.json
```

---

## 🛠️ Gerando o Executável (.exe)

### 3️⃣ Instalar o PyInstaller

Com o Python já instalado, execute no terminal:

```bash
pip install pyinstaller
```

---

### 4️⃣ Compilar o aplicativo

No diretório onde está o arquivo `emailmanager.py`, execute:

```bash
pyinstaller --noconsole --onefile --icon=app.ico --add-data "active.ico;." --add-data "stopped.ico;." emailmanager.py
```

🔧 **O que esse comando faz:**
- Gera um único arquivo `.exe`
- Oculta o console do Windows
- Inclui os ícones de status
- Define o ícone do aplicativo

---

## ▶️ Executando o EmailManager

- O executável final será gerado em:

```
dist/emailmanager.exe
```

- Você pode mover o `.exe` para qualquer pasta desejada
- Execute normalmente para iniciar o monitoramento

---

## 🔄 Inicialização Automática com o Windows (Opcional)

Caso deseje que o EmailManager inicie junto com o sistema:

1. Pressione **`Win + R`**
2. Digite:
   ```
   shell:startup
   ```
3. Crie um **atalho** do arquivo `emailmanager.exe` dentro dessa pasta

✔️ Pronto! O aplicativo será iniciado automaticamente ao ligar o computador.

---

## 🧠 Observações Importantes

- O controle do serviço (**Iniciar / Parar / Reiniciar / Sair**) é feito pelo **ícone na bandeja do sistema**
- As notificações utilizam o sistema nativo do Windows
- Não é necessário registrar AppID
- Compatível com múltiplas contas IMAP
- Ideal para uso pessoal ou corporativo

---

✨ **EmailManager — monitore seus e-mails sem abrir o navegador.**
