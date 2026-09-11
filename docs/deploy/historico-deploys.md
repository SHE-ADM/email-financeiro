# Histórico de deploys

## 2026-09-11 — Empresa pagadora LE BLANC (`sk_company = 4`)

**O que foi ao ar.** A 4ª empresa pagadora entrou na regra de precedência de `resolve_sk_company`:
menção a LE BLANC em qualquer grafia ("le blanc", "leblanc", "le_blanc", "le-blanc") e em qualquer
fonte — assunto, corpo, remetente, texto e nome do anexo, descrição, pagador e **fornecedor** — ou
`payer_cnpj` com a raiz `20584679` ⇒ `sk_company = 4`, **vencendo a ester**. As regras FARDOS,
LEBIANCO e o default OTIMOTEX TECIDOS seguem intactas abaixo dela. De carona foi o vínculo do anexo
à conta existente no caminho de dedup (contas 1238/1239/1240, ALKO), que já estava no mesmo
`read_emails.py`.

**Arquivos:** `read_emails.py`, `deploy-manifest.json`. Sem módulo novo, sem dependência nova,
sem re-registro de tarefa. **Migration 135** (backfill: 348, 759, 1350, 1351 → 4) já aplicada
antes do deploy — base compartilhada dev+prod.

**Verificação em produção:** `check_deploy_parity.py` → **32/32 conferem, 0 faltando, 0
divergentes, 0 extras**. O resultado também encerra a pendência da entrada de 2026-09-01: os 4
arquivos da cobrança que tinham mudado de hash (`run.py`, `run_cobranca.ps1`,
`setup-cobranca-task.ps1`, `setup-gatilhos-task.ps1`) conferem. Primeiro sinal no dado: o próximo e-mail que cite a LE BLANC tem de nascer com `sk_company = 4`
(`SELECT id, sk_company FROM financial_account_control WHERE created_at >= '2026-09-11' AND sk_company = 4`).

**Pendente fora do pipeline:** a mudança do chat de IA (`tools.ts`/`gateway.ts`, filtro pela
empresa 4) sai pelo Vercel via PR para `main`.

## 2026-09-01 — Cobrança de vencidos: horário movido para 10:00 + Return Path/DKIM da Locaweb

**O que mudou.** O horário da tarefa `Pagamentos - Cobrança Vencidos` foi alterado **direto no
Windows Task Scheduler da máquina de produção** (08:00 → 10:00), fora do fluxo de deploy normal.
Documentação e scripts do repositório foram sincronizados na sequência para não ficarem
divergentes da realidade: `CLAUDE.md`, `progress.md`, a skill `deploy-producao` (`pipelines.md`,
inclusive a nota de coincidência de horário com a Baixa Automática, que deixou de valer), a skill
`cobranca-vencidos` (`SKILL.md`, `task_scheduler_setup.md`), e os `.ps1`/`.py` com o horário
hardcoded (`run.py`, `run_cobranca.ps1`, `setup-cobranca-task.ps1` — inclusive `$TRIGGER_H`, para
que um reprovisionamento futuro da tarefa gere 10:00 e não volte a 08:00 — e o comentário em
`setup-gatilhos-task.ps1`).

Também foi resolvida a configuração de **Return Path + DKIM** no painel SMTP Locaweb, via o
subdomínio dedicado `envio.otimotex.com.br` (CNAME + CNAME DMARC + TXT DKIM, todos autenticados) —
ver [env_reference.md](../../skills/cobranca-vencidos/references/env_reference.md). Um domínio
`smtp.otimotex.com.br` cadastrado por engano em "Endereços de remetente" (tela distinta, não
obrigatória) foi excluído.

**Arquivos que MUDARAM HASH e ainda precisam ser copiados para produção** (o manifesto já foi
regravado com `--update`, mas a cópia física é manual, como sempre): `run.py`, `run_cobranca.ps1`,
`setup-cobranca-task.ps1`, `setup-gatilhos-task.ps1` (só comentário). Até a cópia, um
`check_deploy_parity.py` rodado em produção vai acusar esses 4 como **divergentes** — é esperado,
não é regressão.

**Verificação em produção (antes desta sincronização):** `check_deploy_parity.py` → **32/32
conferem, 0 faltando, 0 divergentes, 0 extras**, confirmando que a query nova (`CD_GP_NO`, ver
entrada de 2026-08-31 abaixo) já estava no ar.

## 2026-08-31 — Cobrança de vencidos: exclusão por grupo econômico (`CD_GP_NO`) substitui exclusão por `CD_ID`

**O que foi ao ar.** A query Firebird de títulos vencidos (`db_firebird.py`) trocou a exclusão por
`CD_ID <> 8949` / `<> 21775` por exclusão de 6 grupos econômicos via `CD_GP_NO`
(`INBRANDS`, `RESTOQUE`, `SHOULDER`, `SKAI`, `SOMA`, `LOJAS MEL`), replicada nos dois blocos do
`UNION ALL` (`VW_PSQ_FIN_REC_BAN` e `VW_PSQ_FIN_REC_BAN_004`). A exclusão antiga por `CD_ID` foi
**removida, não somada** — se algum dos dois clientes específicos ainda precisava ficar de fora por
outro motivo não coberto pelos grupos novos, ele volta a entrar na cobrança.

**Arquivos:** `db_firebird.py`, `deploy-manifest.json`. Sem `.env` novo, sem dependência nova, sem
re-registro de tarefa (mudança é só lógica, não `.ps1`).

**Verificação em produção:** `run.py --dry-run` caiu de 115–185 títulos/dia (patamar medido nos 5
dias anteriores à mudança) para **18**, assinatura do novo filtro ativo — a query antiga manteria a
contagem na casa das centenas. `check_deploy_parity.py` → **32/32 conferem, 0 faltando, 0
divergentes, 0 extras**.

## 2026-08-20 — Tipo `dar / dare`, fallback de fornecedor por e-mail encaminhado e contraprova da guia de arrecadação no Vision

**O que foi ao ar.** Três frentes do mesmo caso de origem (conta 1101), mescladas em
[PR #249](https://github.com/SHE-ADM/Pagamentos/pull/249): tipo `dar / dare` consolidado
(migrations 132/133), fallback 1b de fornecedor pelo e-mail do remetente original encaminhado
(migration 134, com supressão de write-back sobre curadoria já existente) e as duas regras da
guia de arrecadação (valor = total a recolher, vencimento = data-limite) estendidas às 3 fontes
visuais (`pdf_vision`/`image_vision`/`docx_vision`) — a data-limite transcrita pelo modelo ganhou
contraprova de coerência contra o vencimento lido do mesmo documento (teto de 180 dias).

**Arquivos:** `febraban.py`, `extract_pdf.py`, `read_emails.py`, `deploy-manifest.json` — nessa
ordem (`extract_pdf.py` importa `febraban.py` no topo; módulo ausente estoura e nenhum PDF é
extraído). **Migrations 132/133/134** já aplicadas (base compartilhada dev+prod).

**Verificação em produção:** `check_deploy_parity.py` → **32/32 conferem, 0 faltando, 0
divergentes, 0 extras**. Hash do `scheduler\deploy-manifest.json`
(`A1228BEB...A40678`) bateu byte a byte com o do repositório antes mesmo do verificador rodar —
o SHA-256 por arquivo do manifesto garante que o código publicado é idêntico ao do commit
`6b355a8` (merge do PR #249 em `main`).

## 2026-08-18 — Senha de boleto: o CNPJ completo (e a filial que não estava na conta)

**Sintoma.** O e-mail "PAGAMENTO BOLETO CABERNET 0108-1408" (`email_control` 1563, erro **314**)
em `extracao_falhou`: o anexo é um PDF cifrado e nenhuma senha candidata abria. A senha era o
**CNPJ inteiro do pagador**, e a regra só cobria os prefixos `[:4]`, `[:5]` e `[:6]`.

**O que o sintoma escondia.** O mesmo remetente já havia perdido o "0107-1507" em julho
(`email_control` 888, erro **257**) exatamente assim. Um e-mail em `falha` não chama atenção
sozinho — quem procurou foi o usuário, um mês depois, por outro e-mail do mesmo fornecedor.

**Causa:** a regra de senha era uma lista literal de prefixos, e a fonte era o CNPJ de **uma**
empresa (`sk_company=1`).

**A descoberta que ampliou o escopo.** As três pagadoras compartilham a raiz `47273917` — logo os
**prefixos coincidem** e a origem única nunca havia incomodado. Mas o **CNPJ completo difere por
filial** (`...0001-23` / `...0002-23` / `...0003-23`), e a empresa pagadora do e-mail só é
resolvida no **Passo 2**, depois da extração: no momento de abrir o PDF não se sabe de quem ele é.
Corrigir só o comprimento teria deixado LEBIANCO e FARDOS falhando pelo mesmo motivo, com o mesmo
sintoma mudo. Tentar uma senha errada custa uma chamada local ao `pypdf`; faltar a certa perde o
boleto em silêncio — daí tentar as de todas.

**Lição de teste.** Dos quatro mutantes instalados, o que restringia a query de volta a
`sk_company=eq.1` **passou**: o teste devolvia o mesmo payload mockado qualquer que fosse a URL, e
por isso não travava a garantia que o nome prometia. Foi corrigido para asserir a própria consulta.

**Arquivos:** `skills/email-reader/scripts/read_emails.py` (`PDF_PASSWORD_CNPJ_LENGTHS`,
`PDF_PASSWORD_MIN_DIGITS` derivado, `company_cnpjs`, `_payer_cnpjs`) ·
`skills/pdf-contas-pagar/scripts/extract_pdf.py` (só comentários da regra) · `deploy-manifest.json`
(hashes; segue em **32** arquivos). **Sem** migration, **sem** `.env` novo, **sem** dependência
nova (`pypdf` já era pré-requisito).

**Resultado do reprocessamento.** O 1563 abriu com **senha de 14 dígitos** e caiu em
`duplicidade` — correto: a conta **1094** (CABERNET, R$ 6.922,78, venc. 24/08) já havia sido
lançada à mão e foi **enriquecida com o código de barras** em vez de duplicada. O 888 não está
mais na INBOX nem no Storage: irrecuperável, mas sem perda — a quinzena está na conta **574**
(venc. 22/07, paga), que veio de um segundo e-mail com o mesmo boleto.

## 2026-08-17 — Anexo .docx com boleto (o descarte silencioso)

**Sintoma enganoso.** O e-mail `email_control` 1516 ("BOLETO: 0003150-04.2023.8.26.0577") estava em
`falha` com a nota *"Corpo sem sinal financeiro (sem valor nem documento)"* e `has_attachment =
false` — ou seja, o registro **acusava o corpo do e-mail** por um documento que o próprio pipeline
havia jogado fora. O filtro de `save_attachments` só conhecia PDF e imagem, e o `continue` do ramo
não logava nada: sem anexo salvo, sem linha em lugar nenhum, sem rastro.

**Causa:** allowlist de anexo fechada em PDF/imagem, com descarte mudo.

**Como foi encontrado:** pelo usuário, que sabia que o anexo existia. **Nenhum sinal automático
apontava para lá** — e essa é a lição que sobrevive a este deploy: o banco não registra anexo
rejeitado (`attachment_names` fica NULL), então não havia como medir o que se perdia. Por isso o
descarte passou a emitir `Anexo ignorado (tipo não suportado): <nome> | <content-type>`, hoje a
única fonte possível de "que formatos estamos perdendo" (`.doc`, `.odt`, `.xlsx`, `.msg` seguem
fora do escopo).

**Arquivos:** `skills/pdf-contas-pagar/scripts/docx_content.py` (**ARQUIVO NOVO** — leitura de
.docx, só stdlib) · `skills/pdf-contas-pagar/scripts/extract_pdf.py` (roteamento `_extract_docx`,
`VISION_SOURCES`, `_vision_source_block` e `_try_barcode_vision` estritos) ·
`skills/email-reader/scripts/read_emails.py` (`attachment_kind`/`attachment_ext` como fonte única,
`_attachment_text`, `_UPLOAD_CONTENT_TYPES`) · `deploy-manifest.json` (31 → **32**).

**Migrations 130 e 131**, aplicadas antes via psql — a 131 acrescenta `docx_text`/`docx_vision` ao
domínio de `extraction_source`. Base compartilhada dev+prod, portanto **sem passo de banco** na
máquina de produção.

**Sem** `.env` novo, **sem** dependência nova (`zipfile` é stdlib), **sem** `setup-*-task.ps1`.

**Validação:** `check_deploy_parity` **32/32** + smoke de import na própria máquina
(`py -3 -c "import extract_pdf, docx_content; …"` → `ok True ('pdf_vision', 'image_vision',
'docx_vision')`). Antes do deploy, os 3 e-mails afetados (1516, 1515, 1322) foram reprocessados do
dev, gerando as contas **1076/1077/1078** — guias do TJSP/FEDTJ, com o código de barras conferido
**dígito a dígito** contra a imagem do documento.

⚠️ **`docx_content.py` estoura se faltar — o OPOSTO do `cte_content.py`.** Ele é importado no
**topo** do `extract_pdf.py`, junto de `febraban`/`fiscal_key`: sem o arquivo, o import falha e
**nenhum PDF é extraído**, não só os .docx. É mais barulhento e por isso mais seguro que a
degradação silenciosa do `cte_content` — mas obriga a ORDEM de cópia: o módulo novo **primeiro**,
o `extract_pdf.py` depois.

📌 **O que a paridade não responde.** Ela compara bytes dos 32 arquivos (módulo esquecido aparece
como `faltando`), mas não diz se o ambiente consegue **importar** — versão de Python, `__pycache__`
antigo, dependência que só existe no dev. Com import no topo o modo de falha é total, e um
`py -3 -c "import extract_pdf"` separa as duas perguntas em segundos.

---

## 2026-08-12 — Barra na chave de acesso (61 CT-e perdidos) + conteúdo do CT-e (Onda 5)

**Sintoma: nenhum.** É o que torna este deploy diferente dos outros desta página — não houve
e-mail em `falha`, linha em `/erros` nem teste vermelho. O extrator errava **para menos**: a chave
de acesso do CT-e não era vista, e o acervo ficava menor do que devia parecendo completo. O sinal
mais próximo de um sintoma eram **39 dos 88 PDFs de transporte** registrados com `models=[55]` —
só a NF-e citada dentro do DACTE —, o que se lê como "documento sem CT-e", não como falha de
leitura.

**Causa:** `fiscal_key._DIGIT_RUN_RE` não tolerava **barra** no separador. Rodonaves, TRB e SSW
imprimem a chave com o CNPJ do emitente FORMATADO dentro dela
(`35.2608.44.914.992/0001-38-57-001-…`): o run de dígitos partia em 14 + 30 e nenhum pedaço
alcançava 44. **61 CT-e recuperados** no backfill (acervo 232 → 293).

**Como foi encontrado:** baixando os 144 PDFs fiscais do bucket e comparando o TEXTO com o que
estava gravado. Nenhuma métrica, log ou suíte apontava para lá.

**Arquivos:** `skills/pdf-contas-pagar/scripts/fiscal_key.py` (a barra) ·
`skills/pdf-contas-pagar/scripts/cte_content.py` (**ARQUIVO NOVO** — conteúdo do CT-e) ·
`skills/email-reader/scripts/read_emails.py` (`update_fiscal_content` + gancho) ·
`deploy-manifest.json`.

**Junto foi a Onda 5, item 5.3** (migration **119**, já aplicada no banco): rota, peso, NF
transportada, destinatário e frete, extraídos da **fatura agregada** da transportadora por regex —
sem LLM. O parser é fail-closed pelo SUB-TOTAL impresso: fatura que não fecha não grava nada.
Backfill de **57 CT-e**; e, cruzando pelo PDF de origem, **9 de 9** faturas com conta a pagar
vinculada batem exatamente com o `amount` da conta — o que prova que o frete é decomposição da
despesa já lançada, não uma despesa nova.

**Sem** `.env` novo, **sem** dependência nova, **sem** `setup-*-task.ps1`. A migration 119 foi
aplicada antes, via psql.

⚠️ **`cte_content.py` degrada em SILÊNCIO se faltar.** Ao contrário de `febraban`/`fiscal_key`
(importados no topo do `extract_pdf.py`, cuja ausência estoura), ele é importado **lazy** pelo
reader: sem ele o pipeline segue verde gravando documentos **sem** peso/rota/frete, e o único
sinal é a linha `modulo 'cte_content' indisponivel … Deploy parcial?` no log. É mais brando e por
isso mais fácil de esquecer.

**Implantado e validado em produção em 2026-08-12.** Paridade **28/28** (0 faltando · 0
divergentes · 0 extras) e, porque paridade prova o arquivo mas não a execução, dois smoke tests
na própria máquina:

```powershell
py -3 -c "import sys; sys.path.insert(0,'skills/email-reader/scripts'); sys.path.insert(0,'skills/pdf-contas-pagar/scripts'); import read_emails as R; print(hasattr(R,'_register_cte_content'), R._cte_content() is not None)"
py -3 -c "import sys; sys.path.insert(0,'skills/pdf-contas-pagar/scripts'); import fiscal_key as F; print(F.extract_access_keys('35.2608.44.914.992/0001-38-57-001-062.409.157-162.409.157-0'))"
```

Resultado: `gancho: True | modulo carregou: True` e a chave
`35260844914992000138570010624091571624091570` — que é justamente um dos 61 CT-e que antes se
perdiam. O primeiro comando é o que importa aqui: ele prova que o import lazy **encontra** o
módulo naquele Python, que é exatamente o que degradaria calado.

SHA-256 nesta versão: `fiscal_key.py` `2a6e34f3…90b9994` · `cte_content.py` `1877e67b…77aec29` ·
`read_emails.py` `ac298ec3…e7c0b99`.

**Lições não-óbvias:**

- **Extrator que erra PARA MENOS não gera sintoma.** Não há erro, não há linha em `/erros`, não há
  teste vermelho — só um acervo menor que parece completo. A única verificação que encontra isso é
  comparar o **conteúdo da fonte** com o que foi gravado; auditoria por métrica agregada não vê.
- **Classificar documento por assunto ou nome de arquivo erra.** Foi a classificação por metadados
  que sustentou a premissa errada do levantamento anterior (41 DACTEs, quando são 83).
- **Ao auditar um campo, varra TODAS as origens.** A auditoria de 17/07 cobriu só `pdf_text` (205
  contas) e concluiu "1 divergência"; o defeito morava em `pdf_vision`. Restringir a varredura à
  origem que se suspeita confirma a suspeita e deixa o resto invisível.

## 2026-08-07 — Vision multi-boleto: carnê escaneado deixava de virar conta

**Sintoma:** e-mail com boletos escaneados ficava `extraído` com **0 contas** e uma linha
`sem_valor` em `/erros`. Medido: 3 e-mails do dia, **21 boletos, R$ 315.556,57**, recuperados por
`scripts/reprocess_message.py` depois da correção.

**Arquivos:** `skills/pdf-contas-pagar/scripts/extract_pdf.py` (núcleo) ·
`skills/pdf-contas-pagar/scripts/febraban.py` (`barcode_self_refuted`) ·
`skills/email-reader/scripts/read_emails.py` (assinatura de e-mail) · `deploy-manifest.json`.

**Segunda entrega do mesmo dia — barcode corrompido pelo OCR:** auditadas as 442 contas com
código de barras, **18 estavam corrompidas** (valor 10×, fator impossível), todas `pdf_vision`
— fecha em 442 com 349 consistentes + 68 não-boleto + 7 em que só o valor diverge (preservados).
Foram limpas (`barcode = NULL`) com prova por hash — 784 contas antes e depois, conteúdo idêntico.
O gate `barcode_self_refuted` impede novas. **Contraintuitivo, mas é o ponto:** apagar o código
REDUZ duplicata — corrompido, ele não casa o boleto real na 2ª via e a dedup cria conta nova;
ausente, ela recai nas impressões 2/3 (documento+valor, valor+vencimento), que funcionam.

**Sem** `.env` obrigatório (`VISION_MAX_TOKENS` tem default 8000), **sem** dependência nova,
**sem** migration, **sem** `setup-*-task.ps1`.

**Implantado em produção em 2026-08-10** (informado pelo operador), a partir do merge do PR #219
em `main` (`3f9e87c`). O que foi para a máquina inclui uma **terceira** correção da mesma onda, que
não estava na entrega original de 07/08 e veio do code review: o `EXTRACTION_PROMPT` é
**compartilhado** pelos dois caminhos, então o modelo passou a devolver ARRAY também no `pdf_text`
— onde ele caía num `except` genérico e virava **1 conta de regex marcada como sucesso**, perdendo
os demais pagáveis. Ou seja, a correção do multi-boleto reintroduziu, pela outra porta, exatamente
a perda silenciosa que existia para matar. Hoje `_build_records_text` detecta ≥2 itens e devolve
`_failure_record`, que cai no fallback tier-2 (Vision). **Lição:** ao ensinar UM caminho a consumir
um formato novo de resposta, verifique quem mais recebe o mesmo prompt.

SHA-256 dos três arquivos nesta versão (conferíveis na máquina de produção):
`read_emails.py` `bc7c89a1…76aa0a7` · `extract_pdf.py` `0b3afd8f…602654586` ·
`febraban.py` `1ccb90f8…4666bcb1ce`. A fonte da verdade do estado continua sendo
`py -3 scheduler\check_deploy_parity.py` **executado em produção** (exit 0 = paridade).

**Lições não-óbvias:**

- **`stop_reason` ignorado torna o truncamento invisível.** A resposta cortada no teto de tokens
  não gera erro nenhum: o JSON pela metade só "não parseia", e o registro vazio resultante era
  logado contra o DOCUMENTO (`sem_valor`) em vez de contra o EXTRATOR. Todo teto de tokens precisa
  de uma checagem do motivo de parada — o número sozinho não avisa quando é atingido.
- **O split por página não cobre o que mais precisa dele.** `_payable_pages` lê TEXTO; o carnê
  escaneado não tem nenhum. Justamente o arquivo com N boletos era o que ia inteiro numa leitura só.
  A saída foi aceitar **N registros por leitura**, não melhorar a detecção de páginas.
- **Ao conferir o resultado, `gmail_message_id` casa com `LIKE '<id>#%'`.** A igualdade simples
  mostrou "1 conta" logo após o pipeline gravar 6 — quase virou um diagnóstico de perda inexistente.

---

## Deploys anteriores do pipeline Python (produção)

Registro condensado dos deploys manuais para `C:\Sheild\API\Pagamentos`. Extraído do `CLAUDE.md`
em 2026-08-04, quando os blocos somavam ~490 linhas de passo-a-passo **já cumprido**.

**Isto é histórico, não procedimento.** O procedimento vivo está no `CLAUDE.md`:

- **"COMO SABER SE PRODUÇÃO ESTÁ ATUALIZADA"** — `check_deploy_parity.py` é a fonte da verdade do
  estado; as lições transversais (manifesto viaja junto, `EXTRA` = régua obsoleta, contar pelo que
  MUDOU, nunca `--update` em produção) vivem lá.
- **"Deploy manual do Email Reader em produção"** — quais arquivos copiar e como validar.

Cada entrada guarda **o que mudou** e **a lição não-óbvia**, quando houver. O passo-a-passo
operacional foi descartado por já ter sido executado; o texto integral continua recuperável em
`git show <commit>:CLAUDE.md` (o corte é do commit de 2026-08-04).

> A regra de negócio de cada item **não** está aqui — ela vive na seção correspondente do
> `CLAUDE.md` (ex.: "Normalização de `document_type`", "Auto-resolução de fornecedor"). Aqui fica
> só o que diz respeito a **levar aquilo para produção**.

---

## 2026-08-04

| # | Mudança | Arquivos |
|---|---|---|
| 5º | **HOTFIX `pdf_links`** — o 4º deploy do dia quebrou o caminho principal (todo e-mail com anexo → `falha`) | `read_emails.py` + manifesto |
| 4º | E-mail não-pagável deixa de virar `falha` (`email_sem_conteudo_extraivel`, `is_disposable_sender`) | `read_emails.py` + manifesto |
| 3º | Guarda de TÍTULO na dedup por nosso número + status `duplicidade` para dedup só-PDF | `read_emails.py` + manifesto |
| 2º | Guia de arrecadação: valor = total a recolher (do barcode) e vencimento = data-limite | `febraban.py`, `extract_pdf.py` + manifesto |
| 1º | Fornecedor rotulado no corpo vence o nome do anexo sem identificador forte | `read_emails.py` + manifesto |

**Lições que ficaram:**

- 🔴 **Ordem de cópia importa quando o módulo é importado no topo.** O `extract_pdf.py` do 2º
  deploy importa `amount_from_arrecadacao`/`arrecadacao_dv_refuted` de `febraban.py` **no topo** —
  com o `febraban.py` antigo o import falha e **nenhum PDF é extraído**, não só a guia. Copie o
  módulo novo primeiro, ou os dois juntos. Assimetria deliberada: o `read_emails.py` **degrada**
  (avisa no log e segue), o `extract_pdf.py` **estoura** — é o que faz um rename futuro aparecer
  alto e cedo em vez de virar silêncio.
- ⚠️ **Comando de uma linha para produção é PowerShell — a quebra de linha vai como `\r\n`, nunca
  como `` `r`n ``.** O PowerShell escapa com backtick, então dentro de aspas duplas o `` `r`n ``
  vira CR/LF **reais**, que partem a string Python no meio (`SyntaxError: unterminated string
  literal`). A barra invertida não é escape do PowerShell, então `\r\n` chega literal ao Python,
  que o interpreta. Forma que funciona nos dois shells: **aspas duplas externas + aspas simples
  internas + `\r\n`**. Trocar as aspas de lugar também quebra (com aspas simples externas o
  PowerShell consome as duplas ao repassar ao executável nativo).
- O 5º deploy é a origem da **lição 6 da §2** do `CLAUDE.md` (guarda de wiring por texto não cobre
  o call site executado). A lição está lá, não aqui.

---

## 2026-08-03 — corpo PLACEHOLDER ("conteúdo em HTML")

`read_emails.py` passou a cair no HTML quando o texto plano é só o aviso de que a mensagem está em
HTML. Copiado com o manifesto.

**Lição:** o manifesto esquecido produz um **veredito enganoso** — o `check_deploy_parity.py` lê o
manifesto **do diretório de produção**, então com a régua velha o arquivo recém-copiado aparece
como `DIVERGENTE` e o script manda recopiar justamente o que está certo. Sinal que distingue os
dois lados: **a validação funcional responde `True` e o verificador acusa divergência ⇒ o problema
é o manifesto**. O branch/merge não influencia — produção não é clone git.

---

## 2026-08-01 — Onda 3: documento fiscal pela chave de acesso

`fiscal_key.py` (**arquivo NOVO**), `extract_pdf.py` (reexport) e `read_emails.py` (gancho), mais o
manifesto — que foi de 26 para 27 arquivos.

**Lições:** o `extract_pdf.py` importa `fiscal_key` no topo → sem ele, `ModuleNotFoundError` e
**nenhum PDF extraído**; copiar o novo primeiro. E foi aqui que se descobriu que **`EXTRA` casando
`DEPLOY_GLOBS` significa manifesto obsoleto**, não arquivo sobrando (produção não cria arquivo) —
a lição está no bloco de paridade do `CLAUDE.md`.

---

## 2026-07-31 — Onda 2: corpo completo do e-mail

`read_emails.py` passou a gravar `email_control.body_full`. Migrations 105/106 já aplicadas na
Supabase compartilhada.

**Lição de verificação:** a presença de uma constante prova só que o arquivo mudou; para provar que
a **alteração** está lá, use `inspect.getsource` da função em produção —
`print('grava:', 'body_full' in inspect.getsource(R.process_message))`. Vale para qualquer deploy
do reader em que o horário da cópia seja incerto.

**Nota:** e-mail processado logo ANTES da cópia fica sem `body_full` e a dedup **não** o
reprocessa. Esperado; esses corpos eram alvo da Onda 4.

---

## 2026-07-28 — quatro deploys

| Mudança | Arquivos |
|---|---|
| Endurecimento do caminho do corpo + **`febraban.py` (arquivo NOVO)** | `read_emails.py`, `extract_pdf.py`, `febraban.py` |
| Regra de SEGURADORA + porta livre no guard SSRF + PDF espelhado | `read_emails.py`, `extract_pdf.py` |
| Tabela de faturas achatada no corpo | `read_emails.py`, `extract_pdf.py` |
| Ignorar "Recebemos o seu pagamento" · notificação de cobrança de plataforma | `read_emails.py` |

**Lição:** o `febraban.py` foi o **primeiro** arquivo novo a revelar que `DEPLOY_GLOBS` precisa ser
estendido — sem ele o corpo degrada para validação só por comprimento **e avisa no log**
(`[BARCODE] … Deploy parcial?`). É assim que se detecta uma cópia incompleta.

---

## 2026-07-23 · 2026-07-20 · 2026-07-16 · 2026-07-15 · 2026-07-10 · 2026-07-06

Todos em `read_emails.py` (salvo onde indicado), todos com migrations já aplicadas na Supabase
compartilhada — portanto **sem passo de banco** em produção.

| Data | Mudança |
|---|---|
| 07-23 | NFS-e/NF-e combinada com boleto vira conta · remetente encaminhado no corpo + "Fatura No:" |
| 07-20 | Descartar extrato/relatório que acompanha o boleto |
| 07-17 | `sk_company` como chave de relacionamento · empresa pagadora por precedência (3 empresas) |
| 07-16 | Contato do fornecedor · dedup por nosso número · **Beneficiário Final** (`extract_pdf.py`) |
| 07-15 | Vínculo do anexo do e-mail (migration 079) |
| 07-10 | `pix` deixa de ser tipo de documento (`extract_pdf.py` junto) · autoria `created_by` |
| 07-06 | Cartório + classificação contábil forçada — **único que exigiu `.env`** (`EMAIL_KEYWORDS`) |

**Lições:**

- **Os deltas de `read_emails.py` são cumulativos.** Cada cópia carrega as pendências anteriores —
  por isso a lista acima não precisa ser aplicada em ordem, basta a versão mais recente.
- **`sk_company` degradou com segurança** porque `company_id` foi preservado (NOT NULL UNIQUE): o
  código antigo seguiu funcionando entre a migration e a cópia. Migration que **substitui** coluna
  deve manter a antiga até o deploy do consumidor.
- **07-06 é o único caso de `.env`** nesta série. O `.env` não é versionado, então mudança em
  `EMAIL_KEYWORDS` exige edição manual no arquivo de produção — não vem junto com o `.py`.

---

## 2026-06-29 — dependência nova: `pypdf`

A descriptografia de boletos com senha e o split de carnê exigem `pypdf` na máquina do scheduler:
`py -3 -m pip install "pypdf~=6.13"`. Sem ele, `import extract_pdf` falha e a extração para.
**Dependência nova é o único caso em que copiar arquivo não basta** — está no `CLAUDE.md`, junto do
procedimento.
