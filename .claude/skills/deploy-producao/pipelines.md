# Os pipelines agendados — o que fazem, o que copiar e como validar

Referência da skill `deploy-producao`. O **procedimento** está no `SKILL.md`; aqui ficam as
particularidades de cada pipeline. Destino em todos: `C:\Sheild\API\Pagamentos\`.

> São **5 tarefas** no Agendador (pasta `\Sheild\`), mas só **4 pipelines de deploy** clássicos —
> a 5ª (Gatilhos Roadmap) entrou depois. A parte de baixo deste arquivo descreve **o que cada um
> faz** (regra de negócio); a de cima, **o que copiar**.

## Regra geral — o que a mudança exige

| Mudou… | Copiar | Re-registrar tarefa? |
|---|---|---|
| Lógica do pipeline (`.py` em `skills\…\scripts\`) | os `.py` alterados (conjunto interdependente) | Não |
| Wrapper/agendador **funcional** (`.ps1` — horário, timeout, runner) | o `.ps1` alterado | **Sim**, se mudou `setup-*-task.ps1` |
| Só comentário/doc (sem efeito funcional) | nada | Não |

A tarefa agendada inicia um **processo novo** a cada disparo e lê os arquivos do disco — **nunca é
preciso reiniciar nada**.

---

## 1. Email Reader (leitura, a cada 5 min)

**Copiar** — `skills\email-reader\scripts\read_emails.py`,
`skills\pdf-contas-pagar\scripts\extract_pdf.py`, `skills\pdf-contas-pagar\scripts\febraban.py`,
`skills\pdf-contas-pagar\scripts\fiscal_key.py`, `skills\pdf-contas-pagar\scripts\cte_content.py`
(os que tiverem mudado) **+ `scheduler\deploy-manifest.json`**.

🔴 **Interdependentes.** `read_emails.py` chama `extract_pdf.extract_to_csv()` **in-process**; e
`extract_pdf.py` importa `febraban` e `fiscal_key` **no topo** — módulo ausente ⇒ `ImportError` ⇒
**nenhum PDF extraído**. Copie o módulo novo primeiro, ou todos juntos.

⚠️ **`cte_content.py` é ARQUIVO NOVO (Onda 5, 2026-08-12)** — conteúdo do CT-e (rota, peso, NF
transportada, frete) a partir da fatura agregada. Ao contrário de `febraban`/`fiscal_key`, ele é
importado **lazy** pelo `read_emails.py`, então a ausência **degrada com aviso**
(`modulo 'cte_content' indisponivel … Deploy parcial?`) em vez de parar a extração. É mais
brando, e por isso mais fácil de esquecer: o pipeline segue verde gravando os documentos **sem**
peso/rota/frete, e o único sinal fica no log. Confira o aviso depois do primeiro ciclo.

**Dependência:** `pypdf` (desde 2026-06-29, boleto com senha + split de carnê) —
`py -3 -m pip install "pypdf~=6.13"`. Sem ele, `import extract_pdf` falha e a extração para.

**Validar** (o comando exato depende da mudança; ver Passo 4 do `SKILL.md`):

```powershell
py -3 -c "import sys; sys.path.insert(0,'skills/email-reader/scripts'); import read_emails as R; print(hasattr(R,'<simbolo novo>'))"
```

---

## 2. Cobrança de vencidos (envios, 10:00)

**Copiar** — a pasta `skills\cobranca-vencidos\scripts\` inteira (8 arquivos).

🔴 **8 scripts INTERDEPENDENTES.** `run.py` (batch) e `resend.py` (reenvio) dependem de
`send_core.py`; este de `email_sender`/`supabase_log`/`template`; e `run.py` também de
`failure_notify`. Copiar só um dá `ImportError`. **Na dúvida, copie o conjunto:** `db_firebird.py`,
`email_sender.py`, `failure_notify.py`, `resend.py`, `run.py`, `send_core.py`, `supabase_log.py`,
`template.py`.

**Pré-requisitos (diferentes do reader):** driver Firebird **`fdb`** instalado; `.env` com
`FB_HOST`/`FB_PORT`/`FB_DATABASE`/`FB_USER`/`FB_PASSWORD`/`FB_CHARSET` **e** o bloco SMTP
transacional (`SMTP_HOST=smtplw.com.br`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD`) —
sem ele cai no fallback IMAP, que tem o gargalo `451`. Mais `COBRANCA_SEND_DELAY_SECONDS` (10s).

**Validar** — o `--dry-run` cobre imports **e** a conexão Firebird, sem enviar e-mail:

```powershell
py -3 skills\cobranca-vencidos\scripts\run.py --dry-run
```

⚠️ Está em produção com `DEV_MODE=false` (envia para clientes reais). Rode o `--dry-run` depois de
qualquer alteração, antes de confiar na próxima execução agendada.

---

## 3. Backup do Supabase (02:00)

**Copiar** — a pasta `skills\backup-supabase\` inteira (5 módulos irmãos: `config.py`,
`backup_db.py`, `backup_storage.py`, `retention.py`, `run.py`) + `scheduler\run_backup.ps1` +
`scheduler\setup-backup-task.ps1`.

🔴 **Pré-requisito NÃO ÓBVIO — `pg_dump` ≥ 17 na máquina.** O servidor é PG 17; um `pg_dump` 16
aborta com "server version mismatch". Produção pode não ter pgAdmin: instale o PostgreSQL 17+
client ou aponte `PG_DUMP_PATH` no `.env`.

**`.env` precisa de `SUPABASE_DB_URL`** — a mesma connection string do pgAdmin (**Session pooler**,
IPv4, porta **5432**; a 6543/Transaction não funciona com `pg_dump`), senha URL-encoded.

**Validar** — o `--dry-run` detecta o `pg_dump` e confere a variável, mas **não testa a senha**:

```powershell
py -3 skills\backup-supabase\scripts\run.py --dry-run
py -3 skills\backup-supabase\scripts\run.py --skip-storage   # dump real, valida a senha
```

**Disco:** backup é completo todo dia × retenção 30 dias. Se apertar, baixe `BACKUP_RETENTION_DAYS`.

---

## 4. Baixa automática (reconciliação, 08:00)

**Copiar** — a pasta `skills\baixa-automatica\` inteira + `scheduler\run_baixa.ps1` +
`scheduler\setup-baixa-task.ps1`.

**Sem dependência nova** (`urllib` + `python-dotenv`) e **sem `.env` novo** (reusa
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`). `run.py` é módulo isolado — basta a pasta.

**Validar** — o `--dry-run` reporta a contagem das DUAS regras (baixa + marcação de vencidos) sem
gravar:

```powershell
py -3 skills\baixa-automatica\scripts\run.py --dry-run
```

🔴 **Regra 1 (baixa para `pago`) é espelhada no frontend** (`qualifiesForAutoPago` em
`Consulta.tsx`, que sai pelo Vercel). Ao mudar essa regra, ajuste **os dois lados**. A Regra 2
(vencidos) vive só neste batch.

⚠️ **Deixou de coincidir com a Cobrança de vencidos em 2026-09-01** — a Cobrança foi reagendada
para 10:00; a Baixa segue às 08:00. Enquanto coincidiam, eram independentes (bancos e sistemas
distintos) e rodavam em paralelo sem conflito; hoje simplesmente não se sobrepõem mais.

---

## 5. Gatilhos do roadmap (mensal, dia 1 às 07:00)

**Copiar** — `skills\roadmap-gatilhos\scripts\run.py` + `scheduler\run_gatilhos.ps1` +
`scheduler\setup-gatilhos-task.ps1`. Sem dependência nova (`urllib` + `python-dotenv`, mesmas
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` da baixa automática).

⚠️ **É a única rotina agendada que NÃO faz parte do pipeline financeiro** — não lê e-mail, não
cobra ninguém, não move dinheiro. Falha reprova a tarefa e gera Event Log (`Pagamentos-Gatilhos`,
EventId **1005**) como as demais, mas **sem impacto no negócio**: o efeito é a série ficar sem o
ponto do mês. Comece a triagem por aí.

🔴 **É a única MENSAL, e é registrada por XML — não pelos cmdlets.**
`New-ScheduledTaskTrigger` não tem opção mensal, e `-Once -RepetitionInterval 30 dias` seria
sutilmente errado (30 dias não é um mês: a medição escorrega até cair fora do mês pretendido).
**Três defeitos foram pagos até chegar ao XML, e os três só apareceram AO REGISTRAR:**

1. **Trigger por CIM (`MSFT_TaskMonthlyTrigger`) não funciona** — monta sem erro e o
   `Register-ScheduledTask` recusa com *"Parâmetro incorreto"*, sem dizer qual.
2. **`<Principal>` vai DENTRO de `<Principals>`**, e a ORDEM dos nós é fixa:
   `RegistrationInfo → Principals → Settings → Triggers → Actions` — sequência tirada de um
   `Export-ScheduledTask` **real**, não do XSD publicado.
3. 🔴 **Verificar o TIPO do trigger é falso-positivo garantido** — via XML o Agendador expõe o
   gatilho como `MSFT_TaskTrigger` genérico, e a checagem reprovou uma tarefa **correta**. Hoje a
   prova é **`NextRunTime`**: comportamento, não implementação. *Verificação que acusa defeito
   onde não há custa mais que verificação nenhuma.*

🔴 **`BUDGET_SECONDS` (10 min) e `ExecutionTimeLimit` (15 min) são um PAR.** O pior caso de rede
passa de 24 min, e quem encerraria seria o **Agendador**: sem exit code próprio, sem log de resumo
e **sem gravar os gatilhos já medidos**. Com o teto no script, ele para sozinho, grava o que
apurou e sai 1. A folga de 5 min é para a gravação.

⚠️ Os temporários do runner são **por processo** (`_stdout_$PID.tmp`) — com nome fixo, uma
execução manual coincidindo com a agendada mata a segunda.

---

# O que cada pipeline FAZ (regra de negócio)

## Cobrança de vencidos — segundo pipeline, de SAÍDA

Lê títulos vencidos no **Firebird**, monta e-mail HTML e envia por **SMTP transacional Locaweb**
(`smtplw.com.br`), registrando sucesso/falha no Supabase.

```
Firebird (VW_PSQ_FIN_REC_BAN + _004)  →  run.py  →  SMTP Locaweb
   STFI='VENCIDO', DTVC >= hoje-7             │      To: cliente · Cc: representante
                                              ├─ dedup: already_sent() × cobranca_envios_log (document_id UNIQUE)
                                              ├─ sucesso → cobranca_envios_log (+ limpa erros antigos do título)
                                              ├─ falha   → cobranca_erros_log  → UI /cobranca/erros
                                              └─ ao fim: resumo por CC das falhas DEFINITIVAS
```

- 🔴 **`send_core.py` é o núcleo compartilhado** por `run.py` (batch) e `resend.py` (reenvio
  manual). Centraliza render→envia→loga; **nunca propaga exceção SMTP**. Não duplicar entre os
  fluxos.
- 🔴 **To primeiro; se o principal falhar, o Cc NÃO é enviado.** A `SmtpSession` reaproveita a
  conexão **no lote** (o gargalo é a fila de saída da Locaweb: `451 queue file write error`).
  ⚠️ `smtplib.SMTPException` herda de `OSError` — o catch de queda usa
  `(SMTPServerDisconnected, ConnectionError, TimeoutError)`, **nunca** `OSError`, senão uma recusa
  definitiva (451/5xx/auth) seria reenviada.
- 🔴 **Classificação de falha é regra de negócio:** `smtp_falha` = instabilidade (o próximo run
  **retenta**); `smtp_bloqueio` = negação (exige **ação humana**). Mensagens de `error_message`
  são **leigas** (a coluna "Motivo" da UI); o técnico vai em `error_detail`.
- 🔴 **Exit code separa DADO de OPERAÇÃO:** cliente sem e-mail no Firebird (`email_ausente`,
  `email_invalido`) **não reprova** a tarefa — o run cumpriu seu papel. Só erro operacional sai
  `!= 0`. Antes, um run 100% OK com clientes sem e-mail aparecia como `0x1`.
- **Segurança (não regredir):** `template.py` escapa `customer_name`/`document_id` com
  `html.escape`; `email_sender` normaliza o Subject (`_strip_crlf`) e **descarta** Cc com quebra
  de linha, no header **e** no envelope; o STARTTLS usa `_secure_tls_context()` com
  `minimum_version = TLSv1_2`.
- **Notificação ao CC:** só as falhas **definitivas** viram resumo por representante; transitórias
  re-tentam sozinhas. O reenvio manual **não** notifica (o usuário já vê os erros na tela).
- **Entregabilidade (fora do código):** SPF ✅ · DMARC `p=none` · **DKIM a configurar**.

## Backup do Supabase — terceiro pipeline, de INFRA

`pg_dump` do banco + download do bucket `attachments`, com manifesto e retenção.

- **Backup é COMPLETO todo dia** (re-baixa o bucket inteiro), não incremental. Disco ≈
  (dump + Storage) × retenção.
- **Restaurar:** `pg_restore --no-owner --no-privileges --clean --if-exists`; Storage = re-upload
  (o nome do arquivo **é** a chave do objeto).
- ⚠️ Os logs `INFO` do Python saem por **stderr**, então aparecem sob `--- STDERR ---` no log do
  wrapper **mesmo em sucesso** — não é erro; o sinal é o exit code.

## Baixa automática — quarto pipeline, de RECONCILIAÇÃO

**Duas regras independentes** no mesmo script, cada uma isolada (falha numa não impede a outra;
exit 1 se qualquer uma falhar, mas a que teve sucesso já gravou — não é transação).

| Regra | Condição | Efeito |
|---|---|---|
| **1 — Baixa** | `has_invoice` **e** `has_bank_slip` **e** `due_date <= hoje` **e** `status_id ∈ {1,2,3}` | → `pago` (8) |
| **2 — Vencidos** | `status_id ∈ {1,3}` **e** `due_date < hoje` (**estritamente**) | → `vencido` (2) |

Situações **fechadas** são preservadas; nenhuma das duas **reverte**.

🔴 **Por que o batch existe:** a trigger `fn_set_status_from_due_date` só recalcula em
INSERT/UPDATE da linha — sem nenhuma edição, uma conta não transiciona sozinha com o passar do dia.

🔴 **Setar `status_id=2` só é seguro DEPOIS da migration 095.** O `2` **está** em `{1,2,3}`, então
a trigger recalcula a cada UPDATE; antes da 095 ela usava `extracted_at` **congelado** como
referência e revertia o UPDATE para `3` **na mesma transação**, em silêncio — o `PATCH` respondia
200 e o valor final era decidido pela trigger. Medido: das 123 contas que deveriam estar
`vencido`, só **3** persistiam. Era bug de **fundação** (desde a 034), não da skill.

🔴 **A Regra 1 é espelhada no frontend** (`qualifiesForAutoPago`) — ver a nota do item 4 acima.

⚠️ **Isolamento do teste:** o `run.py` é carregado via `importlib` com nome **único**
(`baixa_automatica_run`), não `import run` — várias skills têm `run.py` e o nome colidiria em
`sys.modules`, quebrando os testes da cobrança.

---

# Windows Task Scheduler

Cinco tarefas na pasta `\Sheild\` (produção `C:\Sheild\API\Pagamentos`):

| Tarefa | Frequência | Wrapper | Event Log |
|---|---|---|---|
| Email Reader | 5 min | `run_reader.ps1` | — |
| Cobrança Vencidos | 10:00 | `run_cobranca.ps1` | `Pagamentos-Cobranca` |
| Backup Supabase | 02:00 | `run_backup.ps1` | `Pagamentos-Backup` (1003) |
| Baixa Automática | 08:00 | `run_baixa.ps1` | `Pagamentos-Baixa` |
| Gatilhos Roadmap | dia 1, 07:00 | `run_gatilhos.ps1` | `Pagamentos-Gatilhos` (1005) |

- Os wrappers detectam Python com `pdfplumber` (ordem `py -3.12`, `-3.13`, `-3.11`, `-3.10`, `-3`,
  PATH) e mantêm log diário com retenção de 30 dias.
- **Produção NÃO é clone git** — é um deploy mínimo (`scheduler\` + `skills\` + `.env` + `data\` +
  `logs\`). Os `.ps1` usam caminhos relativos a `$PSScriptRoot`, então funcionam sem ajuste.
- **Coexistem com outras tarefas em `\Sheild\`** (API B2B etc.) — os scripts de Agendador usam
  **allowlist**, nunca "pasta inteira" nem "tudo que estiver Running".
- Instalação em outra máquina: `scheduler/INSTALL.md`.
