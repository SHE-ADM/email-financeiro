# progress.md — estado atual do `pagamentos`

> **Verdade com prazo de validade.** Este é o único arquivo do projeto cujo conteúdo se espera
> que esteja errado no mês seguinte. Invariante (o que não pode quebrar) vive no `CLAUDE.md`;
> o porquê e as medições vivem em `docs/`; procedimento vive nas skills de `.claude/skills/`.
>
> 🔴 **Contador que o comando responde melhor NÃO se escreve aqui.** Antes de anotar um número,
> pergunte se existe comando que o produz — se existir, anote o comando, não o número.
>
> **Atualizado em:** 2026-08-20

---

## Como derivar o estado (preferir sempre ao número escrito)

| Pergunta | Comando |
|---|---|
| Qual a última migration aplicada? | `ls supabase/migrations \| tail -1` |
| Qual o número da próxima? | a última + 1 — **nunca reserve número com antecedência** (lição da Onda 5: 109/110/111 foram reservados e consumidos por outro trabalho) |
| Quantos arquivos no manifesto de deploy? | `node -e "console.log(require('./scheduler/deploy-manifest.json').length)"` |
| Produção está em paridade? | `py -3 scheduler\check_deploy_parity.py` **na máquina de produção** (exit 1 em divergência) |
| Qual o total da suíte? | `npm test` (Node) · `py -3 -m pytest tests/ -q` (Python) — medir com `--maxWorkers=1` no `frontend-vite` |

---

## Roadmap de enriquecimento de dados — 9 ondas

Plano e invariantes de cada onda: [docs/roadmap-enriquecimento-dados.md](docs/roadmap-enriquecimento-dados.md).
**Execução é uma onda por vez**, cumprindo o protocolo de 5 passos da §3 do plano.

| # | Onda | Status |
|---|---|---|
| 1 | 9 colunas na `vw_payables` · `demonstrativo_despesas` · rate limit | ✅ concluída |
| 2 | `body_full` + `body_search` + `buscar_emails` | ✅ concluída · deploy aplicado |
| 3 | `fiscal_document` pela chave de acesso · `documentos_fiscais` | ✅ concluída · deploy aplicado |
| 4 | varredura histórica da caixa postal | ✅ concluída (sem migration, sem deploy) |
| 5 | conteúdo do CT-e | ⚠️ **PARCIAL — só o item 5.3.** 5.1/5.2 (itens de NF-e) **suspensos** por falta de população |
| 6 | `dim_date` · colunas derivadas · recorrência/parcelamento | ✅ concluída |
| 7 | trilha de auditoria (`audit_log`) · 2 tools | ✅ concluída · validada em produção |
| 8 | gate de acesso ao chat por grupo · prova do recorte de RLS | ✅ concluída |
| 9 | onda **condicional** — 7 gatilhos medidos, 1 ocorreu | ⚠️ **1 de 7 itens** (pontualidade) |

**Próxima:** retomar a Onda 5 depende de o acervo de DANFEs crescer — ver "Pendências".

---

## Produção (pipeline Python)

Procedimento completo na skill **`deploy-producao`**. Histórico de cada deploy:
[docs/deploy/historico-deploys.md](docs/deploy/historico-deploys.md).

| Item | Estado |
|---|---|
| Último deploy | **2026-09-11** — empresa pagadora LE BLANC (`sk_company = 4`) na regra de precedência + vínculo do anexo à conta existente no dedup. Arquivos copiados: `read_emails.py`, `deploy-manifest.json`. Migration 135 (backfill) **já aplicada** (base compartilhada) |
| Paridade verificada | ✅ **em produção** (2026-09-11) — `check_deploy_parity.py`: **32/32 conferem, 0 faltando, 0 divergentes, 0 extras**. Inclui `read_emails.py` e os 4 arquivos da cobrança sincronizados em 2026-09-01 (`run.py`, `run_cobranca.ps1`, `setup-cobranca-task.ps1`, `setup-gatilhos-task.ps1`) |
| Tarefas agendadas | 5 ativas — Email Reader (5 min) · Cobrança (10:00) · Backup (02:00) · Baixa (08:00) · Gatilhos Roadmap (dia 1, 07:00) |

⏳ **Não exercitado em produção ainda:** a captura **automática** de conteúdo de CT-e a partir
de um e-mail novo (Onda 5). O backfill cobriu o acervo e o fluxo tem teste; falta chegar a
próxima fatura agregada (são semanais). Conferir com
`SELECT count(*) FROM fiscal_document WHERE content_extracted_at >= '<data>'`.

---

## Banco

- **Última migration:** derivar (ver tabela acima). Changelog: [docs/db/historico-migrations.md](docs/db/historico-migrations.md).
- **Regras para migration nova:** skill `migrations-supabase`.
- **A base é COMPARTILHADA dev+prod** — migration aplicada vale para os dois; não há passo
  separado de banco no deploy.

---

## Pendências

| Item | Estado | Gatilho de reabertura |
|---|---|---|
| Onda 5 — itens de NF-e (5.1/5.2) | **SUSPENSO** | acervo de DANFEs crescer (eram 15, com 6 detectáveis) |
| CT-e via DACTE individual (5.3-b, com LLM) | **não implementado** | layout por transportadora inviabiliza regex |
| Handler SIEG (boleto por link) | **ADIADO** | entrar uma fatura SIEG **em aberto** para validar o download |
| Lmed/mdnet (boleto por link) | **ADIADO** | tem CAPTCHA com imagem — sem solução automática |
| DKIM no DNS (cobrança) | ✅ **RESOLVIDO em 2026-09-01** | Return Path + DKIM configurados e autenticados via subdomínio `envio.otimotex.com.br` (CNAME `envio`→`smtplw.com`, CNAME `_dmarc.envio`→`_dmarc.smtpdlv.com.br`, TXT `smtp._domainkey.envio`) — confirmado por consulta pública ao DNS. Ver [env_reference.md](skills/cobranca-vencidos/references/env_reference.md) |
| RBAC completo (`permission`/`group_*`) | **desenhado, não implementado** | [docs/design/permissoes-por-grupo.md](docs/design/permissoes-por-grupo.md) |
| Upload no `/contas` pré-preencher campos | **ideia, não implementar ainda** | decisão registrada na memória |
| Guia de arrecadação lida por **Vision** não recebe as duas correções de guia | ✅ **RESOLVIDO em 2026-08-20** | As duas regras passaram a valer nas **3** fontes visuais. O ramo Vision virou `_build_records_vision` (a assimetria com `_build_records_text` era o defeito); a data-limite chega pelo campo novo **`payment_deadline`** do prompt e quem decide adotá-la é `apply_arrecadacao_deadline`, gated pelo barcode e **compartilhada com o caminho de texto**; texto disponível (tier 2, página espelhada) vence o campo do modelo. O valor ganhou 2ª barreira contra OCR (`arrecadacao_value_refuted`, ≥10× ⇒ não sobrescreve e anota). Fechada de carona a lacuna do `docx_vision` no gate `barcode_self_refuted` (era tupla literal). Suíte **1593** (+26), **7 mutantes** vermelhos. **Nenhum dado histórico precisou de correção** — a medição de 2026-08-19 achou 0 divergências nas 9 guias auditáveis. Detalhe em [docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md) |
| Risco residual do Vision: a data-limite **transcrita pelo modelo** entrava sem contraprova | ✅ **RESOLVIDO em 2026-08-20** (2ª rodada) | Sobrava a assimetria: o **valor** cruzava com o documento e a **data** era validada só na FORMA. Reproduzido antes de corrigir — `payment_deadline` de **2126** gravava conta que **nunca vence** (invisível em KPI, aging e cobrança); **2016**, nascida vencida há dez anos. Agora `arrecadacao_deadline_refuted` cruza com o vencimento que o modelo leu do **mesmo documento** (teto **180 dias** × folga real medida de **0–3**), em **duas direções**, **opt-in pela procedência** (a data do TEXTO é determinística e entra sem cruzamento) e com referência **por item** (carnê). `_iso_date` deixou de aceitar dígito a mais depois da data. **Medição do acervo:** 988 contas, **0** com `due_date` a mais de 180 dias da extração; guias por Vision entre **−11 e +16 dias** — classe nunca ocorrida, guarda preventiva. Suíte **1608** (+14), **8 mutantes** vermelhos |
| Fallback 3 de fornecedor (por NOME do bloco encaminhado) morto no caminho de PDF | **achado, não corrigido** | lê `payload['email_body_excerpt']`, que o caminho de anexo nunca povoa. Não revivido de propósito: desemboca em `resolve_supplier`, que **cria** fornecedor |
| 8 guias JUCE antigas sem texto extraível | **não reclassificadas** | são escaneadas/`.docx` e já estão pagas; provar o acrônimo exigiria leitura por Vision (custo de API). As legíveis foram conferidas e **nenhuma era DAR/DARE** |
| Empresa pagadora LE BLANC (`sk_company = 4`) | ✅ **pipeline em produção (2026-09-11)** · chat de IA aguardando PR | migration 135 aplicada; falta o `tools.ts`/`gateway.ts` (filtro pela empresa 4) ir ao Vercel via PR para `main`. Detalhe em [docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md) |
| TanStack Query em `Consulta`/`Emails` | **rollout pendente** | padrão já aplicado em `SuppliersPage` |
| CABERNET 0107-1507 (`email_control` 888) | **irrecuperável, sem perda** | fora da INBOX e sem anexo no Storage — não há o que reprocessar. A quinzena está coberta pela conta **574** (venc. 22/07, paga), do e-mail 893 que trouxe o mesmo boleto 1h23 depois. O erro 257 fica como histórico |

---

## Gatilhos da Onda 9 (medição mensal)

Medidos em **2026-08-13**: dos 7 gatilhos, **só um ocorreu** (pontualidade de pagamento →
migration 121, 12ª tool). Seguem sem evidência: CF-e/NFC-e (0 documentos), NFS-e (1 conta),
text-to-SQL (0 pergunta descoberta no log), tabelas agregadas (tools em 2–7 ms contra teto de
500), receitas/DRE (0 entradas) e conciliação (sem integração).

A série vive em **`analytics.roadmap_trigger_snapshot`** (migration 122), alimentada pela tarefa
agendada *Pagamentos - Gatilhos Roadmap* (skill `roadmap-gatilhos`, dia 1 às 07:00).
**Não copie a série para cá** — consulte a tabela.

```sql
SELECT trigger_key, measured_on, fired, metrics
FROM analytics.roadmap_trigger_snapshot ORDER BY measured_on DESC, trigger_key;
```

---

## Triagem de backlog — SonarCloud (não reinvestigar do zero)

Análise **CI-based** desde 2026-07-18 (`sonar-project.properties` + `.github/workflows/sonarcloud.yml`).
O gate julga só código **novo** (`new_*`); o backlog do `main` é dívida que **não bloqueia PR**.

| Achado | Decisão |
|---|---|
| 106 issues Python | 100% code smells; as 9 "vulnerabilidades" eram falsos positivos |
| 4× `S8707` (path de CLI em `extract_pdf.py`) | **Won't Fix** — CLI de operador confiável; suprimido por `sonar.issue.ignore.multicriteria` |
| `S1192` de vocabulário de domínio (mime types, "nota fiscal"…) | **não corrigir** — a constante piora a legibilidade |
| `S3776`/`S8786`/`S7632` no núcleo de `read_emails.py` | **não corrigir em sweep** — função a função, com A/B sobre dados reais (precedente: `extract_from_email_body`, 61→17) |
| 37 smells mecânicos + 1 blocker (077) + S6418/S6819/S6845/S125 | ✅ corrigidos |

⚠️ Resolver issue na UI ("Won't Fix") **não é permanente** — o engine reabre ao re-basear.
A supressão durável é o `sonar-project.properties` versionado.

---

## Snapshot da suíte (informativo — derive antes de citar)

Medido em **2026-08-17**: Node **1.583** (frontend-vite 908 em 146 arquivos · api-backend 620 ·
packages/shared 53 · portal-next 2) e Python **1.486**.

🔴 **Ao fechar uma onda, cite o INCREMENTO, não o total** — o incremento é propriedade da onda e
não envelhece; o total muda a cada PR. Meça contra o commit anterior num `git worktree` isolado.
