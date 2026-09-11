# SKILL: cobranca-vencidos

## O que faz

Consulta títulos vencidos no Firebird 5 (views `VW_PSQ_FIN_REC_BAN` e
`VW_PSQ_FIN_REC_BAN_004`), envia email de cobrança HTML via SMTP Locaweb para cada
cliente e registra o log de envio na tabela `cobranca_envios` do Supabase.

**Regra de negócio central:** cada `DOCUMENT_ID` (campo `TITULO` no Firebird) é
enviado **uma única vez para sempre**. Antes de enviar, o script consulta o Supabase;
se o título já constar como `sent`, pula. Isso garante idempotência mesmo que o
agendador rode múltiplas vezes.

---

## Arquitetura

```
Windows Task Scheduler (10:00 diário)
        │
        ▼
skills/cobranca-vencidos/scripts/run.py
        │
        ├── Firebird 5 (fdb / firebirdsql)
        │     └── VW_PSQ_FIN_REC_BAN  UNION ALL  VW_PSQ_FIN_REC_BAN_004
        │         WHERE STFI = 'VENCIDO' AND DTVC >= CURRENT_DATE - 7
        │
        ├── Supabase REST (httpx)
        │     ├── GET  cobranca_envios?document_id=eq.X  → dedup check
        │     └── POST cobranca_envios                   → insert log
        │
        └── SMTP Locaweb (smtplib + ssl)
              └── HTML template com assinatura embutida (base64 inline)
```

---

## Estrutura de arquivos

```
skills/cobranca-vencidos/
├── SKILL.md                          ← este arquivo
├── scripts/
│   ├── run.py                        ← entry-point (Task Scheduler aponta aqui)
│   ├── db_firebird.py                ← conexão e query Firebird
│   ├── email_sender.py               ← composição e envio SMTP
│   ├── supabase_log.py               ← dedup check + insert log Supabase
│   └── template.py                   ← HTML do email com placeholders
├── references/
│   ├── migration.sql                 ← migration Supabase (tabela cobranca_envios)
│   ├── task_scheduler_setup.md       ← passo a passo Windows Task Scheduler
│   └── env_reference.md              ← documentação de todas as variáveis .env
└── assets/
    └── signature.png                 ← assinatura Otimotex/Le Bianco (colocar aqui)
```

---

## Dependências Python

Todas já presentes no `.venv` do projeto, **exceto `fdb`**:

| Pacote | Uso | Status |
|---|---|---|
| `fdb` | Driver Firebird 5 | ✅ instalado (`fdb~=2.0.4`) em `py -3` — 2026-06-19 |
| `httpx` | Supabase REST | ✅ |
| `python-dotenv` | Carregar .env | ✅ |
| `Pillow` | Encode base64 assinatura | ✅ (PIL) |
| `smtplib` | Envio SMTP | ✅ (stdlib) |

> **Driver instalado: `fdb` 2.0.4** (escolhido por haver Firebird Client nativo no
> servidor). Instalado no ambiente `py -3` — o mesmo padrão do pipeline
> (`dev:flask = py -3 server/app.py`, scheduler usa `py -3.x`) — e fixado em
> `server/requirements.txt` (`fdb~=2.0.4`, fonte de verdade das deps Python).
> `db_firebird.py` cai para `firebirdsql` (puro Python) caso o client nativo falte.

**Reinstalar o driver Firebird (se necessário):**
```bash
# fdb requer o Firebird Client nativo instalado no servidor
py -3 -m pip install fdb

# Alternativa pura Python (sem client nativo) — db_firebird detecta automaticamente
py -3 -m pip install firebirdsql
```

---

## Configuração .env

Adicionar no `.env` da raiz do projeto:

```env
# === Firebird ===
FB_HOST=localhost
FB_PORT=3050
FB_DATABASE=C:\Dados\empresa.fdb
FB_USER=SYSDBA
FB_PASSWORD=masterkey
FB_CHARSET=WIN1252

# === Supabase (já existente no projeto) ===
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# === SMTP Locaweb (já existente no projeto) ===
# Lido da tabela `company` no Supabase — não duplicar aqui
# O script busca: SELECT smtp_host, smtp_port, smtp_user, smtp_password, email FROM company LIMIT 1
```

---

## Execução manual (teste)

```bash
# Na raiz do projeto, com .venv ativo
py -3 skills/cobranca-vencidos/scripts/run.py

# Modo dry-run (não envia, não grava — só imprime o que enviaria)
py -3 skills/cobranca-vencidos/scripts/run.py --dry-run
```

---

## Migration Supabase

Rodar `references/migration.sql` no SQL Editor do Supabase antes da primeira execução.

---

## Agendamento

Ver `references/task_scheduler_setup.md` para configurar o Windows Task Scheduler
apontando para este script às 10:00 diariamente.

---

## Modo de desenvolvimento (e-mail de teste)

Enquanto `DEV_MODE=true` no `.env`, todos os envios são redirecionados para
`DEV_OVERRIDE_EMAIL` (independente do `PRIMARY_EMAIL` / `CC_EMAIL` do Firebird).
Em produção, remover ou setar `DEV_MODE=false`.

```env
DEV_MODE=true
DEV_OVERRIDE_EMAIL=ricardo@otimotex.com.br
```
