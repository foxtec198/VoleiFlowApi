# VoleiFlow API

API Flask para cadastro de jogadores, eventos, inscrições, presença, Lista Negra e formação equilibrada de times.

## Instalação

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\venv\Scripts\python.exe -m flask --app app init-db
.\venv\Scripts\python.exe -m flask --app app create-admin
.\venv\Scripts\python.exe app.py
```

A API fica em `http://localhost:7000`. O comando idempotente `init-db` cria tabelas ausentes e cadastra as posições/configurações padrão sem apagar ou sobrescrever registros existentes. Alterações estruturais no banco de produção são administradas diretamente e não usam arquivos de migration versionados.

O comando `create-admin` solicita nome, e-mail e senha de forma interativa. A senha deve conter maiúscula, minúscula, número e símbolo.

## Prioridade nas inscrições

A prioridade vai de `1` (maior) a `3` (menor). Membros começam no nível 2 e convidados no nível 3. Ao completar três presenças, o jogador ganha um nível; ao registrar qualquer falta, perde um nível. A inscrição guarda o vínculo e a prioridade calculada naquele momento, e vagas ainda pendentes são reorganizadas por essa prioridade.

## Locais

Cada local usa uma rota própria no frontend, como `/nilo`, e o cliente envia o slug em `X-Place-Slug`. Jogadores, vínculo membro/convidado, prioridade, turnos, eventos, times, Lista Negra e configurações são isolados por local. Nome, rota, endereço e link do mapa podem ser administrados na tela de posições e turnos.

## Segurança e e-mail

- Defina um `PASSWORD_PEPPER` aleatório com pelo menos 32 caracteres antes de criar o Admin.
- As senhas usam HMAC-SHA256 com pepper e Argon2id. O login emite JWT HS256 válido por 8 horas.
- As rotas administrativas recebem o JWT no header `Access-Token`.
- Configure SMTP para o envio de confirmação. Sem SMTP, a inscrição continua registrada e a falha não desfaz a transação.
- Para produção, use PostgreSQL em `DB_URI` e um segredo forte em `SECRET`.

## Testes

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Os testes cobrem duplicidade, prioridade por confirmação, formação/balanceamento, presença, Lista Negra e sincronização offline idempotente.
API para o APP VoleiFlow
