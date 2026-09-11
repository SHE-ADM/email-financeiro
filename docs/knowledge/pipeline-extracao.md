# Regras de negócio do pipeline de extração

Extraído do `CLAUDE.md` em 2026-08-04. Este arquivo guarda o **porquê**: os casos reais, as
medições e o raciocínio por trás de cada regra do caminho e-mail → PDF/corpo → conta.

Os **invariantes** ("não regredir") continuam resumidos no `CLAUDE.md`, na seção
"Pontos-chave que exigem ler vários arquivos" — ele é carregado automaticamente em toda sessão,
este arquivo não. **Ao mexer no pipeline, leia os dois:** o `CLAUDE.md` diz *o que* não pode
quebrar; aqui está *por que* e *como se descobriu*.

Ordem preservada do documento original. Nada foi reescrito — o texto é verbatim.

---
### Dedup de conteúdo + reemissão (`financial_account_control`)

Além do dedup por `message_id`, `find_financial_duplicate(payload)` evita gravar o
**mesmo documento** chegado em e-mails diferentes. Casa por 4 impressões: (1) barcode;
(**1b**) **`sk_supplier`** + `nosso_numero` — ver abaixo; (2) **`sk_supplier`** + `invoice_number`
(≥6) + valor — pega **guia/DAS reemitida** com o mesmo número e vencimento novo; (3) **`sk_supplier`**
+ valor + vencimento (**+ tipo só quando o novo NÃO tem barcode** — ver abaixo). Quando encontra
duplicata, `extract_and_store_accounts` **não cria outra conta**: se a reemissão tem
vencimento **mais recente**, chama `update_financial` para atualizar `due_date` + boleto
(`barcode`, `amount_charged`, `fine_interest`, `other_additions`) na conta existente — uma
guia paga uma vez, sempre com o boleto válido. A trigger recalcula a situação em `status` no
UPDATE (só quando em aberto — migration 034).

🔴 **A impressão 1b tem uma GUARDA DE TÍTULO desde 2026-08-04 — não removê-la** (`_same_title`).
O campo que o LLM extrai como "nosso número" **nem sempre identifica o título**: em alguns
layouts ele copia o **código AGÊNCIA/CONTA do cedente** — no T.R.T Monitoramento,
`0001/0000515-6`, o **mesmo em todos os boletos** daquele fornecedor. A 1b então fundia a
mensalidade de **agosto** com a de **julho**: a conta nova não era criada, o `update_financial`
gravava na conta antiga o **barcode do boleto novo**, e o `email_control` ficava **`extraído`
sem conta** — pagável **perdido em silêncio**, sem linha em `/erros`. Foi assim que o boleto de
R$ 450,00 venc. 15/08 sumiu (relato do usuário: *"esse novo de ontem não to achando"*).
**Discriminador:** uma REEMISSÃO é o MESMO título e carrega o MESMO nº de documento; boletos
DISTINTOS têm números distintos. Medido: no caso que criou a 1b (SIEG 323/560)
`invoice_number == nosso_numero` nos dois; no TRT o nosso número é idêntico e os números de
documento diferem (`00561066674` × `00569007593`). `_same_title` compara por **continência de
dígitos** (`001/00561066674-1` contém `00561066674` — carteira e DV variam por campo), exige
≥6 dígitos e devolve **True** ("pode deduplicar") quando um dos lados não tem número próprio
ou tem número **sintético** — conservador de propósito, porque deduplicar a mais **perde um
pagável**, enquanto deduplicar a menos só cria uma conta a revisar. Testes:
`tests/test_dup_nosso_numero_titulo.py` (9 casos, validado por mutante). Correção de dados:
conta **316** teve o barcode e o `nosso_numero` restaurados (estavam com os do boleto de
agosto) e o e-mail 1250 foi reprocessado → conta **847** (R$ 450,00, venc. 15/08, "a vencer").
✅ **RESOLVIDO — `status='extraído'` voltou a significar "gerou conta"** (2026-08-04). Antes,
ele era emitido também quando o CSV foi gerado e a dedup descartou TUDO, e foi essa ambiguidade
que deixou a perda do T.R.T invisível: e-mail verde, sem conta, sem erro em `/erros`. Agora
`status_for_result` devolve **`duplicidade`** nesse caso (`if duplicate and not body_created`
dentro do ramo `csv_generated`), então o e-mail cai no card "Duplicidades" de `/emails` e a
auditoria fica possível. O `not body_created` preserva a precedência do corpo — conta nova
gravada pelo corpo continua `extraído` (teste pré-existente).
🔴 **A informação já existia: `attachment_account` é `accounts_saved > 0 or dup_matches > 0`**,
logo `attachment_account and accounts_saved == 0` É a dedup. A 1ª versão acrescentou um 5º
valor ao retorno de `extract_and_store_accounts` e **quebrou 38 testes** que o desempacotam com
4 — assinatura pública não se muda por um contador. O helper `_pdf_only_deduplicated` existe
para dar um NOME que a guarda de wiring possa procurar.
⚠️ **A guarda de WIRING é o que trava isso** (`WiringDuplicidadeDoPdfTest`): os testes de
`status_for_result` passam `duplicate=True` na mão e ficaram VERDES com o mutante que corta a
ligação (`or False`) — 1079/1079. A guarda lê o código de `process_message` e exige
`_pdf_only_deduplicated` **dentro do argumento `duplicate=`**, com sanidade do parser. Reincidência
da lição §2 item 5 — a função pura não prova que o call site a usa.

**Varredura de perdas (2026-08-04):** os **106** e-mails `extraído` sem conta foram auditados
baixando o PDF de cada um e conferindo se a linha digitável existe em alguma conta — **39** são
dedup correta, **65** não têm boleto legível (link/corpo/CT-e/imagem) e **2** eram candidatos,
ambos verificados como dedup CORRETA (SIEG = reemissão já paga na conta 323; Adolpho Loyola =
mesmo título da conta 483, `109/57990943-4`, cancelada pelo usuário). **O T.R.T foi a única
perda real**, e já está recuperada (conta 847).

**Impressão 1b — `sk_supplier` + `nosso_numero` (identificador ESTÁVEL do título — não regredir):**
o **nosso número** é o identificador do título no banco e a **2ª via / aviso de vencimento MANTÉM o
mesmo** — mesmo quando a reemissão muda **VALOR (juros) E VENCIMENTO** ao mesmo tempo, combinação que
faz as impressões 1/2/3 falharem (o barcode difere pelo fator+valor; a 2 exige valor igual; a 3 exige
valor E vencimento iguais). Falha real **ids 323/560** (fatura SIEG): o aviso de vencimento reemitiu
com **+juros** (435,18 → 444,01) e **venc +1 dia** (15/07 → 16/07), gerando conta duplicada porque
NENHUMA das 3 impressões casou — mas o `nosso_numero` `000000091070-8` é idêntico. A 1b roda **após o
barcode e antes das 2/3** (identificador forte), escopada por fornecedor (o nosso número é único por
beneficiário/título). Guarda `_is_real_nosso_numero` (**≥8 dígitos, não só zeros**) evita fundir
títulos distintos por nosso número vazio/curto/lixo. Casada, cai no mesmo caminho de reemissão (atualiza
o vencimento/boleto da conta existente). Varredura do banco: só **1** grupo duplicado por nosso número
(323/560) — o 560 foi **hard-deletado** (2026-07-16), 323 preservado. Testes: `tests/test_dup_nosso_numero.py`.
Este era o bug "aleatório" de duplicidade: só se manifestava quando a reemissão alterava valor **e**
vencimento juntos. **Deploy:** copiar só `read_emails.py` (sem `.env`/banco). **Limitação conhecida:** a
1b compara o `nosso_numero` como TEXTO (`=eq.`) — cobre reemissões do mesmo gerador/formato (o caso
real); variação de formatação do nosso número entre reemissões não é coberta (evolução futura:
normalizar por dígitos, se surgir).

**Impressão 3 casa por `sk_supplier`+valor+vencimento, INDEPENDENTE do `document_type`
(robustez cross-e-mail — não regredir):** regra de negócio — **se fornecedor + valor +
vencimento coincidem, é a MESMA conta a pagar**; o `document_type` varia entre os documentos
que descrevem a dívida (`boleto` no PDF, `fatura`/`outro`/`pix` no corpo). Antes a impressão 3
exigia `document_type` igual e deixava a duplicata passar em **qualquer ordem de chegada**,
criando 2 contas — casos reais **ids 7/176** (ESPRO), **217/218** e **511/512** (Smart Web).
O tipo **saiu** da impressão 3. Distinção que permanece — **código de barras**:
- **Novo doc COM barcode** (boleto autoritativo): casa `sk_supplier`+valor+vencimento com
  `barcode=is.null` — só candidatos SEM linha digitável (conta do corpo/notificação da mesma
  dívida). Boletos DISTINTOS (cada um com barcode próprio, ex.: parcelas HYOSUNG, guias GNRE de
  R$ 399,03) **NÃO** se fundem: candidato com barcode ≠ é documento distinto (a impressão 1 já
  teria casado se fosse o mesmo).
- **Novo doc SEM barcode** (corpo/notificação/reemissão): casa **qualquer** conta da mesma
  dívida (`sk_supplier`+valor+vencimento), inclusive um boleto já gravado com barcode — um
  documento sem linha digitável nunca é um 2º pagável legítimo, então é a mesma dívida.

**Enriquecimento:** quando um boleto casa uma conta do corpo **sem barcode** (vencimento igual),
`extract_and_store_accounts` grava a linha digitável do boleto na conta sobrevivente (o boleto
sempre vence o corpo), sem duplicar. Testes: `tests/test_dup_barcode_synthetic.py`
(`DedupCrossTypeBodyTest` + `test_corpo_casa_boleto_existente_qualquer_ordem`) e
`tests/test_boleto_dedup_suppresses_body.py` (enriquecimento). **Limpeza retroativa** (2026-07-13):
hard delete das duplicatas do corpo ids 7 (mantido 176), 218 (mantido 217) e 512 (mantido 511,
enriquecido com o barcode). **NÃO são duplicatas** (preservados): boletos distintos de mesmo
valor/vencimento com barcodes próprios (HYOSUNG 286/287, GNRE 297/300 e 329/330, DAMSP 267/402)
e lançamentos manuais com números distintos (Multa 411/412). **Limpeza retroativa** (2026-08-03):
hard delete do id **210** (CATAGUASES R$ 4.842,19 venc. 29/06, vinda do CORPO de um "Aviso de
Vencimento") — duplicava o **124**, que é o boleto real e foi **preservado** (`pdf_text`, com
barcode, nosso número e o PDF anexado). Critério do usuário: remover a **mais nova**; "sem status
pago" não desempatava (ambas estavam `pago`). O `email_control` de origem passou a **`duplicidade`**
— sem isso ficaria `extraído` apontando para conta inexistente. É duplicata ANTERIOR ao fix de
2026-07-13 (a regra atual já a bloquearia: o corpo sem barcode casa `sk_supplier`+valor+vencimento).

✅ **A impressão 3 está CONFIRMADA em produção (varredura de 2026-08-04 — não regredir).** Agrupando
a base inteira por `sk_supplier`+valor+vencimento, **a duplicata mais recente é a conta 718** (28/07:
o "Aviso de vencimento" da ITW/PPF duplicando o boleto **515** — mesmo título 211839-2, R$ 2.407,44,
venc. 28/07, `fatura` × `boleto`). **Nenhuma duplicata nasceu depois disso**, e a data não é
coincidência: o fix da impressão 3 é de **13/07**, mas só chegou à máquina de produção no **deploy
de 29/07** — um dia depois da 718. As 451 e 524 são da mesma janela e da mesma causa. Ou seja, o
intervalo entre "corrigido no repo" e "copiado para produção" é observável no dado, e é o argumento
concreto para o `check_deploy_parity.py` existir.

**Retry da consulta de dedup (robustez de rede — não regredir):** um hiccup de rede na
consulta de duplicidade (`_find`) faria `find_financial_duplicate` retornar `None` ("sem
duplicata") e o pipeline **gravaria conta duplicada**. Por isso `_find` **re-tenta** em falha
transitória (`DUP_QUERY_ATTEMPTS` default 3, backoff `DUP_QUERY_BACKOFF` default 1,5s × tentativa)
antes de desistir; um resultado **vazio** (`rows == []`) NÃO é erro — retorna `None` de imediato
(não re-tenta). Esgotadas as tentativas, retorna `None` (não bloqueia a inserção
indefinidamente) após logar. Testes: `tests/test_dup_barcode_synthetic.py`
(`DedupQueryRetryTest`).

**Boletos DISTINTOS não podem fundir por número SINTÉTICO nem por valor/vencimento
quando têm código de barras próprio (não regredir):** quando o PDF não traz Nº do
documento nem vencimento, o pipeline gera um `invoice_number` **sintético**
(`{tipo}_{ddmmaa}` ou `PIX_…`) e *defaulta* o vencimento p/ a data da extração. Dois
boletos diferentes do mesmo fornecedor com o **mesmo valor** colidiam nessas duas
impressões e a dedup **perdia** um deles (caso real: HYOSUNG 181063-1/2/3 e 5 guias GNRE,
duas de R$ 399,03). Correção em `find_financial_duplicate`: a **impressão 2 IGNORA número
sintético** (`_is_synthetic_invoice_number` — só Nº PRÓPRIO do documento é chave; a reemissão
de DAS/guia segue funcionando por número real) e a **impressão 3 só casa candidatos SEM
barcode quando o NOVO tem barcode** (`barcode=is.null`) — código de barras presente e
diferente = documento distinto (a impressão 1 já teria casado se fossem o mesmo). Sem barcode
no novo (corpo do e-mail), a impressão 3 casa por `sk_supplier`+valor+vencimento (ver a regra
detalhada abaixo — o `document_type` **não** entra na impressão 3). Testes:
`tests/test_dup_barcode_synthetic.py`.

**A dedup casa por `sk_supplier`, não por texto do fornecedor** (migrations 040/041/042): o
fornecedor é resolvido ANTES da dedup por `_finalize_supplier` (RPC
`resolve_supplier_for_account` → `SupabaseControl.resolve_supplier`), que grava
`payload['sk_supplier']` e **remove** as colunas brutas `supplier_name`/`supplier_cnpj`/
`supplier_cpf` do payload. Como a resolução já normaliza nome/CNPJ (via `resolve_supplier_id`:
CNPJ → CPF → e-mail → `normalize_search(legal_name/trade_name)` → auto-insert), a antiga dedup
por nome (RPC `financial_dup_by_name` / `_dup_by_name`) foi **removida** — "EFE Displays" e
"EFE DISPLAYS" deduplicam por já resolverem o mesmo `sk_supplier`. Teste:
`tests/test_dup_by_supplier_id.py`.

**Dedup casada não vinculava o PDF à conta existente — comprovante ausente em silêncio (achado
2026-09-04, fornecedor ALKO):** o usuário reportou dois e-mails (`bi@envios.alko.com.br`, ids
1781/1782, 28/08) marcados `sem_valor` em `/erros` e pediu para "corrigir, com robustez técnica".
Investigação: o `sem_valor` era do banner de marketing Alko/Dekorama (`.gif` de cabeçalho, sem dado
financeiro) — correto, não é bug. Mas cada e-mail também trazia o boleto **real** (`Boleto_1.pdf`,
`Boleto_1_1.pdf`, `Boleto_2.pdf`), e esses **casaram por barcode** (impressão 1) contra as contas
**1238/1239/1240** (NF 222961/222962/222963, R$ 14.965,50 / R$ 101.250,00 / R$ 9.054,38),
lançadas manualmente dois dias antes a partir do e-mail "Envio da NF-e" — confirmado batendo o
barcode extraído do PDF (baixado do bucket e parseado com `febraban.normalize_barcode`, o mesmo
parser canônico do pipeline) contra o gravado no banco: **idêntico nos três casos**. A dedup em si
estava correta. O bug real: `extract_and_store_accounts` atualiza `due_date`/`barcode`/juros na
conta existente (branches REEMISSÃO/DUP-DOC) mas **nunca chamava `register_attachment`** — só o
caminho de conta NOVA vinculava o anexo. O PDF já estava no Storage (upload do Passo 1), mas a
linha em `financial_account_attachment` nunca era criada: a conta ficava **sem nenhum comprovante
anexado**, sem erro, sem status distinto, sem sintoma — só um anexo ausente que ninguém nota até
precisar dele. Corrigido chamando `ctrl.register_attachment(dup["id"], src, ...)` no bloco de
dedup, com o mesmo `size_bytes`/`uploaded_by` do caminho de conta nova (idempotente, não-fatal).
Testes: `tests/test_boleto_dedup_suppresses_body.py` (4 asserções novas no call site real,
validadas por mutante — revertendo o fix os 4 caem no vermelho). Correção de dados: os 3 anexos
foram vinculados retroativamente às contas 1238/1239/1240 (mesma chamada `register_attachment`,
`origin='pipeline'`).

### Normalização de `document_type`

`extract_pdf.py` usa `_ns()` (strip de acentos + lowercase) para lookup em `_DOC_TYPE_NORM`.
CHECK constraint em `financial_account_control.document_type` usa `lower()` (migrations 014,
017, **024**, **026**, **043**, **062**, **066**, **075**, **086**, **087**, **132** e **133**). Tipos aceitos incluem: `boleto`, `cte`, `nfe`, `nfse`, `tributo`,
`das`, `seguro`, `fatura`, `recibo`, `contrato`, `honorários`, `container`, `multa`, `dar / dare`, `cartório`, `cheque`, `comprovante`, `outro`
(DAS de Simples Nacional → `das`; **`multa`** = multa/penalidade/juros avulsos, auto de
infração).

**`dar / dare`** = Documento de Arrecadação ESTADUAL, **entrada ÚNICA para os dois acrônimos**
(migration **133**, com backfill das 26 contas que diziam `dare`). Histórico: a 062 separou
DAE=eSocial de DARE=estadual; a **132** criou `dar` sozinho (caso da conta 1101, guia da JUCEMAT
cujo cabeçalho imprime `DOCUMENTO DE ARRECADAÇÃO - DAR MODELO 1 - AUT`); a **133** consolidou os
dois, porque nomeiam o MESMO instrumento e o que varia é o estado que o imprime — mesmo padrão
de `dam / duam`.

🔴 **`dar` é auto-classificado APENAS por rótulo explícito ou FRASE.** Ele é prefixo de `darf`
**e** verbo comum do português ("dar baixa", "padaria", "guardar"): a forma pura nunca entra nos
classificadores difusos — `KEYWORDS` casa por SUBSTRING e `_SUBJECT_TAX_DOC_KEYWORDS`/
`_BODY_DOC_KEYWORDS` casam por palavra inteira, o que **não basta** para um verbo. As frases
aceitas são `dar modelo 1`, `dar-1`, `dar/aut`, `dar avulso` e o próprio `dare` (inequívoco).

🔴 **`documento de arrecadacao estadual` NÃO é gatilho de `dar / dare`** — e isso tem prova no
acervo: essa frase é o nome por EXTENSO do **DAE** em Pernambuco (conta 1103, `DAE JUCEPE /
Documento de Arrecadação Estadual`) e no Ceará (conta 821, `DAE - Documento de Arrecadação
Estadual`). Como o bucket `DAR / DARE` vem ANTES do `DAE` em `KEYWORDS`, incluí-la faria **todo
DAE** ser gravado como DAR, sem erro nenhum. O nome oficial do DARE ("de **RECEITAS** estaduais")
é uma string disjunta e pode ficar. Travado por teste em `tests/test_doc_type_dar.py`.
**`cheque`** (migration 086) = o cheque como DOCUMENTO da conta, distinto do `payment_method`
`cheque` (forma de pagamento). Tipo **SELECIONÁVEL** no cadastro manual (`ContaForm`) e no filtro
de `/consulta` (ambos derivam do enum `DOCUMENT_TYPES`); a extração só o emite pelo rótulo
EXPLÍCITO em `_DOC_TYPE_NORM` — **NÃO** há auto-classificação pela palavra "cheque" no assunto/
corpo (evita falso positivo com a forma de pagamento). Teste: `tests/test_doc_type_cheque.py`.
**Guard de consistência do domínio** (`tests/test_doc_type_domain_consistency.py` — não regredir):
trava as invariantes cross-camada revisadas em 2026-07-18 — (A) o enum `DOCUMENT_TYPES` é **idêntico**
ao CHECK da migration de document_type mais recente (o teste acha a de MAIOR número que recria o
constraint); (B) **todo** valor emitido por `_DOC_TYPE_NORM` (extração), `_BODY_DOC_KEYWORDS`,
`_UTILITY_DOC_KEYWORDS` e `_SUBJECT_TAX_DOC_KEYWORDS` ∈ enum (senão o INSERT quebraria com 23514) e
`_normalize_doc_type` sempre retorna valor do enum; (C) `_is_tax_document` reconhece todo tributo
canônico (inclui `dam / duam`) e exclui `gps`/`multa`. Ao adicionar um tipo, rodar este teste — ele
falha se o enum e a migration divergirem, ou se um classificador emitir valor fora do domínio.
**`comprovante`** (migration 087) = comprovante/recibo como DOCUMENTO da conta. Mesmo padrão do
`cheque`: tipo **SELECIONÁVEL** no cadastro manual (`ContaForm`) e no filtro de `/consulta` (derivam
do enum `DOCUMENT_TYPES`); a extração só o emite pelo rótulo EXPLÍCITO em `_DOC_TYPE_NORM` — **NÃO**
há auto-classificação pela palavra "comprovante" no assunto/corpo, para não conflitar com
`subject_is_payment_confirmation` (que já IGNORA e-mail de "comprovante de pagamento" — recibo de
pagamento já feito, não conta a pagar). Teste: `tests/test_doc_type_comprovante.py`.
**`pix` NÃO é tipo de documento (removido na migration 075)** — é só forma de pagamento
(`PAYMENT_METHODS`). Um pagamento PIX sem outro indício de tipo fica `document_type='outro'` e
`payment_method='pix'`; quando não há Nº de documento próprio, o sintético é
**`{payment_method}_{valor}`** (`pix_R$ …`) para PIX+`outro`, senão `{tipo}_{ddmmaa}`. As antigas
fontes de `document_type='pix'` (`apply_pix_override` no PDF e o ramo `has_pix` do corpo) foram
removidas — o backfill da 075 converteu os `pix` existentes em `outro`.
`container` = frete/demurrage/movimentação de
contêineres (keyword de assunto + classificação no corpo e PDF; migration 026).
`SKIP_ACCOUNT_TYPES = ['nfe', 'nfse']` — não geram conta a pagar.

**EXCEÇÃO — NFS-e/NF-e COMBINADA com boleto no MESMO arquivo → o boleto vence (não regredir —
caso real Amil id 1045/1046, 2026-07-23):** o skip por `document_type` do **Passo 2** de
`extract_and_store_accounts` (`if dtype in SKIP_ACCOUNT_TYPES: continue`, o **primeiro** check do
loop) descartava a linha **incondicionalmente**, sem olhar se ela também carregava um boleto
pagável. Alguns prestadores (planos de saúde, telecom) emitem **NFS-e + boleto no MESMO PDF** —
ex.: a Amil "e-Faturamento": cabeçalho "NOTA FISCAL DE SERVIÇOS ELETRÔNICA - NFS-e" no topo +
ficha de compensação com linha digitável abaixo. O extrator vê o cabeçalho fiscal e rotula
`document_type='nfse'`, então a **conta a pagar real** (Amil R$ 7.217,91, Itaú, venc. 07/08/2026)
era jogada fora → `nonpayable_only=True` → o e-mail virava **`ignorado`** (silenciosamente, sem
erro em `/erros`). Correção: o skip só dispara quando a linha **NÃO** tem boleto real
(`_is_boleto_barcode(row['barcode'])`); tendo linha digitável válida, a linha é **re-rotulada
`document_type='boleto'`** (o pagável vence) e segue o fluxo normal. **Escopo mínimo/robusto:**
(a) NF-e/NFS-e **PURA** (sem barcode — chave de acesso não é boleto) segue pulada, sem regressão;
(b) o re-rótulo é coerente com as guardas seguintes — `_email_has_real_boleto`/`real_boleto_amounts`
já operam por **barcode**, não por `document_type`, então a regra fatura+boleto continua descartando
uma fatura separada de mesmo valor; (c) não afeta o caso multi-anexo com uma NFS-e separada sem
boleto próprio. **Por que não apareceu antes:** o boleto Amil é **PDF cifrado** (senha = prefixo do
CNPJ do pagador); antes do fix de descriptografia (~2026-06-29) a extração **falhava** (o e-mail de
junho id 354 ficou `pendente`), então a lacuna do skip só ficou exposta quando a extração passou a
suceder (julho). Testes: `tests/test_fatura_boleto.py` (classe `NfseComBoletoTest`: nfse+boleto e
nfe+boleto viram conta re-rotulada `boleto`; NFS-e pura sem barcode segue ignorada; combinado +
fatura de mesmo valor grava só o boleto). **Recuperação retroativa** (2026-07-23): e-mails 1045/1046
reprocessados (`reprocess_message.py`) → conta **674** criada (Amil, R$ 7.217,91, venc. 07/08/2026,
"a vencer"); o 1046 deduplicou contra o 1045 (uma conta só). **Consolidação de fornecedor**
(2026-07-23): o reprocessamento criou um cadastro Amil novo (sk 1311, **com** CNPJ 29309127000179),
duplicando o pré-existente **sk 141** (Amil sem CNPJ, da conta 417 já paga) — a dedup de fornecedor
não os fundiu por diferença de nome ("SA"/acento) + ausência de CNPJ no 141. A pedido do usuário,
consolidado em **um único cadastro sk 141**: gravado o CNPJ no 141, contas 417/674 re-apontadas para
141 e **sk 1311 removido** (hard delete de duplicata recém-criada — exceção pontual à regra de soft
delete de `supplier`, mesmo precedente dos fornecedores-lixo). **Deploy:** copiar só `read_emails.py`
(o `extract_pdf.py` NÃO muda; sem `.env`, sem passo de banco). Validação (esperado `boleto`):
`py -3 -c "import sys; sys.path.insert(0,'skills/email-reader/scripts'); import read_emails as R; r={'document_type':'nfse','barcode':'34192153100007217911092614333532938395767000'}; print('boleto' if R._is_boleto_barcode(r['barcode']) else r['document_type'])"`

**Cartório (`cartório`) — pagamento de/em cartório (não regredir):** custas de
tabelionato/registro/protesto. Classificado por **contexto no ASSUNTO ou no NOME DO
FORNECEDOR** — a palavra `cartorio`/`cartório` (ou `tabelionato`/`tabeliao`), por palavra
inteira sem acento (`_is_cartorio_context`, `read_emails.py`). `_apply_cartorio_doc_type`
**só re-rotula tipos genéricos** (`boleto`/`outro`/`pix`), preservando guias/utilities/`cte`/
`honorários` (um ITBI pago no cartório continua ITBI). Aplicado nos **dois caminhos**
(`build_financial_payload` do PDF e `extract_from_email_body` do corpo), **abaixo** de
utility/tax/transporte. `extract_pdf.py` também mapeia `cartorio`/`tabelionato` → `cartório`
em `_DOC_TYPE_NORM` (caso o Claude emita o tipo direto). Keyword de assunto (`cartório`/
`cartorio`/`tabelionato`) em `KEYWORDS_DEFAULT` **e** no `EMAIL_KEYWORDS` do `.env`. Caso de
origem: id 400 ("PAGAMENTO CARTORIO ...", boleto real → re-rotulado `cartório`). A migration
066 amplia o CHECK + faz backfill dos genéricos com contexto de cartório no assunto. Teste:
`tests/test_doc_type_cartorio.py`.

**Classificação contábil AUTOMÁTICA de GUIAS TRIBUTÁRIAS (extração — não regredir):** para
**e‑mails tributários** (`_is_tax_document`, `document_type` ∈ `darf, das, gru, dae, dar / dare,
gnre, ipva, iptu, dam, duam, dam / duam, iss, itbi, gare, tributo`), a guia é **relacionada
automaticamente ao plano de contas** (`financial_chart_of_account`) pelo TIPO/CONTEXTO do imposto,
determinando `cost_center_id`/`chart_account_id` — **NÃO** a partir do `supplier`. **Precedência
MÁXIMA:** essa regra determinística **VENCE** o default do fornecedor e as demais regras; e **grava
com write‑back** (o `supplier` é atualizado com o mesmo destino, exceto **OTIMOTEX** `sk=1` e
**funcionário** — trigger `trg_supplier_no_funcionario_classification`). Quando **não** dá para
determinar (sem sinal, `tributo` sem esfera, ou código ausente no cadastro), **não força** — cai no
comportamento atual (default do fornecedor).

O alvo é sempre um **`account_code`** do plano, resolvido para `(cost_center_id, chart_account_id)`
por `SupabaseControl.classification_for_account_code(code)` (cacheado, `:640`) — **não hardcodar ids**
(se o cadastro reclassificar, a regra acompanha). Resolvedor `_resolve_tax_chart_code(document_type,
blob, sender_email)` em **3 níveis** (blob = assunto+descrição+corpo, sem acento via `_ns_body`/
`_has_word`):

1. **Frase/combinação específica** (maior prioridade): `_ICMS_IMPORT_PHRASES`→`4.3.05` (ICMS
   Importação); `_ICMS_ST_PHRASES` **ou** GNRE de `@lebianco` (`_is_lebianco_sender`)→`4.1.02`
   (ICMS‑ST); `imposto de importacao`→`4.3.01` (II); `pis`+`cofins`+(`csll`/`retid`)→`4.2.05`.
2. **Por `document_type`** (a guia já determina o imposto — vem ANTES do scan p/ não rebaixar GNRE):
   `gnre`→`4.4.01` (GNRE a Recolher), `gare`→`4.1.01` (ICMS), `iss`→`4.1.06`, `ipva`→`6.4.02`,
   `iptu`→`6.4.01`, `das`→`4.4.04` (Taxas Federais — Simples sem conta dedicada).
3. **Palavra‑chave distintiva** no texto (refina DARF/DARE/GRU): `irrf`→`4.2.03`, `irpj`→`4.2.01`,
   `csll`→`4.2.02`, `inss`→`4.2.04`, `iss`→`4.1.06`, `ipi`→`4.1.03`, `cofins`→`4.1.05`, `pis`→`4.1.04`,
   `icms`→`4.1.01`, `ipva`→`6.4.02`, `iptu`→`6.4.01`.
4. **Fallback por ESFERA** do `document_type`: federal (`darf`/`gru`/`dae`)→`4.4.04`; estadual
   (`dar / dare`)→`4.4.02`; municipal (`dam`/`duam`/`dam / duam`/`itbi`)→`4.4.03`. `tributo` (sem esfera)
   **não força** (evita mis‑forçar boleto de fornecedor mal‑rotulado).

`resolve_forced_classification(ctrl, document_type, subject, *extra_texts, sender_email, sk_supplier)`
→ `(cc, ca, write_back)`: se `_is_tax_document`, o fornecedor **não** está excluído (ver EXCLUSÃO
abaixo) e o resolvedor devolve um code com cc/ca ≠ 0/0 → `(cc, ca, True)`. Aplicado por
`apply_forced_classification` (que passa `sk_supplier`) APÓS `_finalize_supplier` e ANTES da
gravação, nos dois caminhos (PDF em `extract_and_store_accounts`; corpo em `try_extract_from_body`,
onde os clones de parcela herdam a classificação). Write‑back via `update_supplier_classification(sk,
cc, ca)` (PATCH `supplier`, best‑effort; nunca `sk=1`). As regras antigas fixas (IRRF/ICMS‑Import) e
por‑código (DAM/DUAM→ISS, GNRE‑ST) foram **subsumidas** pelo resolvedor (removidas
`_detect_forced_classification`/`_chart_code_for_document` e as constantes `CC_FISCAL`/`CA_IRRF`/
`CA_ICMS_IMPORT`/`DAM_DUAM_*`/`GNRE_ICMS_ST_CHART_CODE`). **Mudanças de comportamento:** DAM/DUAM sai
de ISS (4.1.06) → Taxas Municipais (4.4.03); GNRE sem ST agora classifica (4.4.01, antes 0/0);
IRRF/ICMS‑Import/GNRE‑ST/DAM passam a fazer **write‑back**; um CT‑e com "IRRF" no texto segue como
transporte (não é tributário). **TRANSPORTE (não‑tributário) preservado à parte:** CT‑e/frete →
`CC_LOGISTICA`(4)/`CA_TRANSPORTADORAS`(339), com write‑back, só por assunto+document_type
(`_is_transport_context`). Backfill retroativo: `scripts/reprocess_classification_overrides.py`
(`--dry-run`, reusa `resolve_forced_classification`). Testes: `tests/test_classification_overrides.py`.
**Backfill APLICADO em 2026-07-10** (11 guias tributárias legítimas reclassificadas —
DAM/DUAM→Taxas Municipais, DARE→Taxas Estaduais, DAS→Taxas Federais, GNRE→GNRE a Recolher,
IPTU→conta dedicada) + **write-back** em 4 fornecedores (CONTABIL ESQUEMA→GNRE, Receita Federal
×2→Taxas Federais, PREFEITURA SP→Taxas Municipais). **Correção de dados do Dr. Ricardo (sk 1262):**
7 contas (ids 423‑429) normalizadas para **reembolso** (`recibo`/`pix`, Jurídico/Reembolsos 14/530);
a 425 estava órfã em Fiscal/ICMS-Import (3/11) e foi corrigida — depois disso a guarda de exclusão
foi criada para impedir reincidência.
**EXCLUSÃO de fornecedor (não regredir):** fornecedores em
`TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS` (hoje `{1262}` = **Dr. Ricardo**, despachante) **NÃO**
recebem a classificação tributária forçada — a regra é pulada (o `sk_supplier` é passado a
`resolve_forced_classification`) e a conta mantém o **default do fornecedor** (Dr. Ricardo =
Jurídico/Reembolsos 14/530). Motivo: as contas dele são **reembolso de tributos, honorários e
outros tipos jurídicos**, nunca conta fiscal pura, mesmo quando o documento é uma guia de
arrecadação (Junta Comercial). Ver memória `dr-ricardo-reembolso`. **Risco residual:** outro
fornecedor mal‑rotulado como tributário (ainda não na allowlist de exclusão) seria classificado
por esfera — acrescentar o `sk_supplier` ao set quando identificado.

**CT-e / transporte: só o BOLETO gera conta; o CT-e fiscal é ignorado (não regredir):** o
CT-e (Conhecimento de Transporte) é documento **fiscal**, não pagável — quem se paga é o
**boleto** de frete. Regra (espelha a NF-e, mas condicional ao boleto):
- **CT-e/transporte SEM boleto → não gera conta; e-mail vira `ignorado`** (não `falha`).
- **CT-e/transporte COM boleto → extrai só o boleto**, rotulado `document_type='cte'`.
- **Boleto de transporte → `document_type='cte'`** quando o **contexto é de transporte**:
  assunto com `cte`/`ct-e`/`dacte`/`conhecimento de transporte`/`transporte`/`transportadora`,
  **ou** fornecedor de transporte (nome com `transporte(s)`/`transportadora`/`logística`/
  `cargas`/`encomendas`/`frete(s)`), **ou** já classificado `cte`.

Implementação em `read_emails.py` (não em `extract_pdf.py`, que continua classificando CT-e por
chave de acesso): helpers `_is_transport_supplier`, `_is_transport_context` e
`_apply_transport_boleto_doc_type` (mesmo padrão de `_classify_utility_by_supplier`). O
**boleto** é distinguido da chave de acesso NF-e/CT-e por `_is_boleto_barcode` (44 FEBRABAN
moeda '9' ou 48 de arrecadação; a chave de acesso de 44 dígitos **não** casa). O re-rótulo
boleto→cte é aplicado nos **dois caminhos** (`build_financial_payload` do PDF e
`extract_from_email_body` do corpo), **abaixo** de utility/tax (uma transportadora que manda um
DARF continua DARF — só tipos genéricos `boleto`/`outro`/`pix`/`cte` são re-rotulados) e **acima**
de `boleto`. O **skip** de CT-e-sem-boleto ocorre em `extract_and_store_accounts` (que passa a
retornar também `nonpayable_only`) e em `try_extract_from_body` (novo retorno `BODY_IGNORED`); o
status `ignorado` vem de `status_for_result(nonpayable=…)`, posicionado **antes** de
`csv_generated` (o PDF do CT-e gera CSV mas nenhuma conta — senão viraria `extraído`, errado).
Caso misto (CT-e fiscal + boleto no mesmo e-mail): o boleto grava → `accounts_saved>0` →
`extraído`; o CT-e é pulado. Testes: `tests/test_doc_type_transporte.py` (+ casos `nonpayable` em
`tests/test_status_for_result.py`). **Limpeza retroativa** dos dados já gravados:
`scripts/reprocess_cte_accounts.py` (aplicado em 2026-07-02 — Fase A re-rotulou 28 boletos de
transporte para `cte`; Fase B **hard delete** de 100 CT-e fiscais + 87 e-mails → `ignorado`;
estado final: as únicas contas `cte` são boletos de transporte).

**CEDENTE do boleto vence o EMITENTE do CT-e agregado (fatura SSW — não regredir):** numa
fatura de transporte que **agrega um ou mais CT-e**, o credor é o **cedente/beneficiário do
boleto** (a transportadora que EMITE a fatura e recebe o pagamento), **não** o **emitente do
CT-e** (a transportadora física SUBCONTRATADA). O extrator, vendo o bloco "IDENTIFICAÇÃO DO
EMITENTE" do CT-e, gravava o subcontratado como fornecedor — falha real id 528 (fatura
CAMPINENSE Nº 0324348, R$ 502,40) gravada sob **TRANSPORTADORA J.D.F.** (o CT-e agregado).
Correção em **duas camadas** (defesa em profundidade):
- **Prompt (`extract_pdf.py`, soft):** a seção CT-e ganhou a "PRIORIDADE DO CEDENTE DO BOLETO"
  — se o documento tem boleto (linha digitável / 'Cedente' / 'Nosso Número'), o
  `supplier_name`/`supplier_cnpj` é o cedente; a regra de EMITENTE vale só para **DACTE/CT-e
  PURO** (sem boleto).
- **Guarda determinística (`read_emails.py`, robusta — imune à variação do LLM):**
  `_ssw_cedente_from_body(sender, body, own_cnpj)` extrai o cedente do CORPO da fatura SSW
  (`sswsistemas.com.br`) — nome via "…realizados por `<NOME>`" e CNPJ do rodapé (o CNPJ
  mascarado que **não** é o da própria OTIMOTEX). Em `extract_and_store_accounts`, para a linha
  que **é boleto real** (`_is_boleto_barcode`), o cedente do corpo **sobrepõe** o fornecedor
  extraído (`[CEDENTE-SSW]`) antes de `_finalize_supplier`; como a resolução prioriza CNPJ, o
  fornecedor correto é resolvido/criado de forma determinística. **Degrada com segurança**:
  remetente não-SSW ou corpo sem cedente → nenhum override (comportamento atual). O
  `process_message` passa `body_text` à função; os reprocessadores históricos
  (`reprocess_link_emails`) usam o default `body_text=""` (no-op). Testes:
  `tests/test_ssw_cedente.py`. Correção pontual do id 528 aplicada em 2026-07-14 (fornecedor →
  CAMPINENSE TRANSPORTE DE CARGAS LTDA, sk 1278; nº doc → `0324348`).

**Beneficiário Final vence Beneficiário/Cedente (boleto securitizado — não regredir):** em boleto
**securitizado/factoring**, o "Beneficiário"/"Cedente" é a securitizadora/empresa de COBRANÇA e o
**"Beneficiário Final"** é o credor REAL (o fornecedor que vendeu) — o fornecedor da conta é o
**BENEFICIÁRIO FINAL**. Falha real (ids 561/562, "BOLETOS INORGAN"): boleto gravado sob **MB COBRANCAS
LTDA** (CNPJ 45.175.261/0001-80, o Beneficiário) sendo o correto **INORGAN INDUSTRIA QUIMICA LTDA**
(56.879.838/0001-51, o Beneficiário Final). Correção em `extract_pdf.py`:
- **Prompt (soft):** já prefere `beneficiario final > beneficiario > cedente` (linha ~152), mas o LLM
  às vezes escolhe o Beneficiário mais proeminente.
- **Override DETERMINÍSTICO (robusto — imune ao LLM):** `extract_beneficiario_final(text)` acha o
  rótulo "Beneficiário Final" no TEXTO do PDF (nome + CNPJ, na mesma linha OU nas 1-2 seguintes) e
  `apply_beneficiario_final(rec, raw)` **sobrescreve** `supplier_name`/`supplier_cnpj` (e zera
  `supplier_cpf`), aplicado no fim de `build_record` do caminho **pdf_text** (após o barcode, antes do
  `return`). Vale para os dois sub-caminhos (LLM e regex fallback). **Vision (`pdf_vision`/
  `image_vision`) NÃO tem o texto do PDF** (o `raw` é a resposta JSON) → depende do prompt.
- **EXIGE o CNPJ do beneficiário final (não regredir — o cerne da robustez):** "Beneficiário Final"
  também aparece como **RÓTULO DE COLUNA** no cabeçalho de MUITOS boletos (ex.: "Ag./Cód. Beneficiário
  Final") — aí o texto ao lado é lixo/o próprio beneficiário, **sem CNPJ**. A varredura completa
  achou **2 casos REAIS com CNPJ** (561/562) vs **36 rótulos-de-coluna sem CNPJ** (BRASPRESS, STC,
  SEVEN EXPRESS…). Por isso o override **só atua quando há CNPJ** ao lado do rótulo (tanto no pipeline
  quanto no backfill) — o CNPJ é o discriminador entre securitização REAL e rótulo de coluna. Sem
  isso, o pipeline corromperia o fornecedor dos 36. Testes: `tests/test_beneficiario_final.py` (inclui
  o caso do rótulo-de-coluna → no-op).
- **Backfill:** `scripts/reprocess_beneficiario_final.py` (`--dry-run`/`--ids 561,562`) varre as contas
  `pdf_text` com `source_file`, baixa o PDF do bucket, extrai o beneficiário final (mesmo extractor) e
  re-aponta `sk_supplier` (resolve/cria via RPC) **só quando o CNPJ difere**; name-only vira revisão
  manual (logado, não aplicado). Idempotente. **Aplicado em 2026-07-16:** ids 561/562 → INORGAN
  (sk 944, CNPJ 56.879.838/0001-51); os 36 name-only foram corretamente ignorados. **Deploy:** copiar
  só `extract_pdf.py` (o `read_emails.py` NÃO muda; sem `.env`/passo de banco).

**Fornecedor ROTULADO no corpo vence o nome do ANEXO sem identificador forte
(`_body_supplier_identity` — conta 822, 2026-08-04; não regredir):** o nome que o Vision/LLM lê de
um **pedido/recibo** é apenas "alguma razão social impressa na página" — e num pedido isso costuma
ser a **TRANSPORTADORA**, não quem recebe o pagamento. Como a regra geral é "o anexo vence o corpo",
o corpo nem era consultado. Falha real (conta **822**, "Pagamento Bordados" de `bruna@lebianco.com.br`):
o anexo era a foto de um `Pedido.jpeg` e o Vision gravou **"TRANSFER EXPRESS"** (criando um cadastro
novo, sem CNPJ), enquanto o CORPO nomeava o fornecedor de forma explícita — `Razão Social: I S da
Silva Camisetas e Malharia` + `CNPJ: 44.427.588/0001-30` (sk 1193, que **já existia**, com 3 contas).

Regra: quando a linha extraída do anexo **NÃO traz `supplier_cnpj` nem `supplier_cpf`**, o par
**nome ROTULADO + identificador** do corpo o sobrepõe. Aplicado no **Passo 2** de
`extract_and_store_accounts`, logo **DEPOIS** do override SSW (quando aquele grava o cedente, a linha
já tem identificador forte e este não dispara). Mesma família de `_ssw_cedente_from_body`.

- **A condição é o coração da regra:** com CNPJ/CPF **próprios**, o **ANEXO manda**. Boleto e nota
  fiscal sempre os trazem, então **nada regride** — só a extração de imagem/pedido, que é justamente
  a que erra o nome.
- 🔴 **Exige nome ROTULADO *e* identificador — só o CNPJ NÃO basta.** Nos boletos que o despachante
  repassa (contas **423-428**, "Dr. Ricardo") o corpo traz o CNPJ do **CLIENTE solto**, sem rótulo:
  disparar ali trocaria o fornecedor correto pelo de um terceiro, reintroduzindo pela porta dos
  fundos o erro que a memória [[dr-ricardo-reembolso]] documenta. Essa guarda não é teórica — foi
  medida: das 8 contas históricas de anexo com fornecedor sem CNPJ **e** CNPJ no corpo, o **par
  rotulado só existe na 822**.
- **Janela `_BODY_SUPPLIER_ID_WINDOW` (200 chars)** entre o nome e o identificador — o corpo cita
  vários CNPJs (pagador, plataforma no rodapé, terceiro mencionado) e só vale o que está **junto** do
  rótulo. Mesmo padrão da janela da chave PIX em `parse_supplier_contacts`.
- **Descarta o CNPJ da própria empresa pagadora pela RAIZ de 8 dígitos** (bloco do destinatário;
  filiais do grupo compartilham a raiz) e **nome que seja tipo de documento** (`_is_non_supplier_term`).
- **CNPJ e CPF são exclusivos** no override: gravar os dois faria a RPC casar por CNPJ e deixar um
  CPF órfão no cadastro. Prioriza o CNPJ (PJ), como o override SSW.
- **Reusa `_resolve_body_supplier_identity`** (a extração canônica do corpo) em vez de criar uma 2ª
  fonte de verdade do que é "fornecedor rotulado".
- 🔴 **O CEDENTE do override SSW tem precedência — a flag `ssw_aplicado` NÃO é redundante**
  *(achado da autorrevisão)*. Quando o único CNPJ do corpo SSW é o da **própria empresa**,
  `_ssw_cedente_from_body` devolve o cedente **só com NOME** — a linha fica sem identificador forte
  e este override sobreporia o cedente recém-gravado. O cedente do boleto é o credor autoritativo
  daquela fatura; nada no corpo o supera. Checar só "tem CNPJ/CPF?" não basta para expressar isso.
- **Verificação — A/B contra a base REAL, não só fixtures:** rodado o helper contra os corpos das
  **423** contas de anexo; dispara em **1** (a 822) e com o valor correto. ⚠️ A medição equivalente
  feita por **regex no Postgres deu FALSO NEGATIVO** (não casou nem a 822) — a checagem tem de rodar
  no **Python**, com a mesma função do pipeline. Testes: `tests/test_body_supplier_override.py`
  (18 casos, validados contra **5 mutantes** — um por guarda, mais o da precedência SSW).
- **Correção de dados (2026-08-04):** conta 822 → sk 1193 + classificação default dele (6/585); o
  cadastro-lixo **1320 "TRANSFER EXPRESS"** recebeu **soft delete** (`deleted_at`) — 0 contas órfãs.
  O **valor** foi ajustado depois, por decisão do usuário, de R$ 4.874,40 (total do pedido, que o
  Vision leu da imagem) para **R$ 2.437,20** — o `1º pagamento` que o corpo especifica; a
  `processing_notes` registra a troca. ⚠️ **O 2º pagamento NÃO foi lançado:** o corpo diz "após o
  pagamento total farão a emissão da NF", mas não traz data nem cobrança da 2ª parcela, e criar
  conta sem documento seria inventar obrigação. Ele deve chegar por e-mail próprio.

**GUIA DE ARRECADAÇÃO: o valor é o TOTAL A RECOLHER e o vencimento é a DATA-LIMITE
(GNRE — 2026-08-04; não regredir):** uma guia tem **duas** de cada, e o extrator vinha
pegando a errada nas duas:

| Campo | O que o LLM pegava | O correto |
|---|---|---|
| Valor | `Valor Principal` (só o tributo) | **`Total a Recolher`** (principal + atualização + juros + multa) |
| Vencimento | `Data de Vencimento` (do TRIBUTO — já passou) | **`Documento Válido para pagamento`** (data-limite desta guia) |

Estrago medido antes da correção: **27 das 31** GNRE gravadas **a MENOR** (R$ 297,17 no
total — pagar a menor gera novos juros) e **31 das 32** com vencimento **anterior à própria
emissão**, isto é, nascendo `vencido` (a tela mostrava um bloco inteiro de guias vermelhas).

- **O VALOR vem do CÓDIGO DE BARRAS, não do texto** (`amount_from_arrecadacao` em
  `febraban.py`, aplicado por `apply_arrecadacao_amount`): o emissor codifica o total a
  recolher nas posições 5-15 do código de arrecadação. É determinístico e imune ao LLM —
  mesmo papel que o fator de vencimento tem para o vencimento do boleto. Diferente de
  `apply_barcode_amount` (que só PREENCHE quando falta valor), este **SOBRESCREVE**: o
  número errado também é um número, e aquele não o corrigiria.
- 🔴 **`id_valor` (posição 3) decide DUAS coisas — o que o campo significa e como o DV é
  calculado.** `6`/`8` = valor EFETIVO (dinheiro); `7`/`9` = valor de **REFERÊNCIA**
  (identificador: contrato, matrícula, competência). Tratar 7/9 como valor gravaria um
  número enorme e arbitrário como R$. O módulo do DV vem do mesmo dígito: 6/7 → módulo 10,
  8/9 → módulo 11.
- **`arrecadacao_dv_refuted` preenche a lacuna que `barcode_dv_refuted` declarava não
  cobrir** ("arrecadacao (48) tem outro esquema de DV"). Sem ela, um barcode corrompido por
  OCR sobrescreveria um valor que o LLM leu certo — a classe de falha do id 463. Validada
  contra os dados reais (**31/31 conferem**) *e* por discriminação (corrompendo 1 dígito por
  vez, **380/432 = 87%** são refutadas) — 100% de aprovação sozinho também seria o sintoma
  de uma função que nunca refuta nada.
- 🔴 **`amount_charged` recebe o total DIRETAMENTE, nunca via `resolve_amount_charged`**
  *(achado da autorrevisão)*. Aquela função aplica a aritmética de BOLETO (`amount −
  descontos + mora/multa`), e numa guia os juros **já estão dentro** do total: recalcular
  somaria `fine_interest` uma segunda vez (id 773: 47,51 + 0,47 = **47,98**, valor que não
  existe no documento; no id 817 o erro seria de **+R$ 103,80**). Os componentes são
  **preservados** como memória de cálculo. Consequência assumida: em guia de arrecadação a
  identidade `amount − desc + juros = amount_charged` **não vale**, porque a guia não tem
  "valor do documento" separado do total.
- **A precedência do vencimento vive num lugar só** (`apply_text_due_date`, extraída do
  `build_record`): (1) rótulo "Vencimento" impresso, quando plausível — regra pré-existente
  do boleto securitizado (id 473/474); (2) data-limite da guia, que vence até o item 1.
- **O item 2 é restrito ao documento de ARRECADAÇÃO, decidido pelo BARCODE** (determinístico),
  não pelo `document_type` do LLM: num boleto comum um "válido para pagamento até" significa
  outra coisa.
- 🔴 **O item 2 NÃO passa por `_due_date_plausible`.** Em guia de tributo o `issue_date` do
  documento é **anulado** (`TAX_DOC_TYPES` — guia não tem emissão confiável) e quem preenche
  a coluna é o fallback do reader: **a data do E-MAIL**. Um reenvio dias depois a põe depois
  do dia-limite, e a guarda `>= emissão` descartaria justamente a data correta.
- **Verificação — A/B contra os PDFs REAIS, não só fixtures:** os 31 PDFs foram baixados do
  bucket e reprocessados. Data-limite encontrada em **31/31**; o rótulo "Vencimento" do
  `_TEXT_DUE_RE` não casa em **nenhuma** GNRE (hoje a data vem do LLM). **Alcance medido em
  toda a base:** entre os **33** documentos de arrecadação NÃO-GNRE (dare, darf, iptu, conta
  de telefone, dae, iss, dam/duam), **0** teriam o valor alterado e **0** o vencimento — 30
  não têm o rótulo e 3 têm data-limite idêntica ao vencimento. As duas regras são cirúrgicas.
  Testes: `tests/test_arrecadacao_gnre.py` (**+33 casos** e **6 mutantes** nesta correção; o
  arquivo cresceu depois — ver a seção do caminho visual, abaixo).
- ⚠️ **Um mutante revelou teste que passava PELO MOTIVO ERRADO** *(lição a repetir)*: o caso
  da guarda de "valor de referência" trocava o `id_valor` para 9 — o que **também** quebra o
  DV —, então o `None` vinha do gate de DV, não da guarda testada, e o mutante que removia a
  guarda não era pego. A fixture passou a **recalcular o DV** (helper `_com_id_valor`), mais
  uma contraprova de que 6/8 **entregam** o valor.
- **Correção de dados (2026-08-04):** as **31** guias com barcode tiveram `amount`,
  `amount_charged` e `due_date` corrigidos a partir das duas fontes verificadas (barcode com
  DV conferido + texto do PDF). Resultado: vencimento anterior à emissão **31 → 0**; 4 guias
  saíram de `vencido` para `a vencer`. A **32ª (id 266)** não foi tocada — é lançamento
  manual, sem barcode e sem PDF, logo sem fonte para verificar.

### A regra chega ao caminho VISUAL (2026-08-20)

Por 16 dias as duas correções acima valeram **só no `pdf_text`**: elas eram chamadas em
`_build_records_text`, e o ramo Vision era **código inline** dentro do dispatcher
`build_records`. Guia **escaneada** dependia inteiramente do que o modelo leu.

**Exposição medida antes de corrigir** (2026-08-19): **38 guias tributárias** vieram por
Vision (35 `pdf_vision` + 3 `docx_vision`), R$ 912.285,26. Das **9 auditáveis** — as que têm
linha de arrecadação de 48 dígitos —, o valor gravado conferia com o barcode em **100%**
(0 divergências). O risco era estrutural e real, mas **não se materializou**: as correções
existem porque o parser de TEXTO errava, e o Vision, lendo a página renderizada, vinha
copiando o "Total a Recolher" certo.

⚠️ **A metade "vencimento" NÃO tem oráculo independente** — o código de arrecadação não
carrega fator de vencimento, ao contrário do boleto. É justamente por isso que ela precisa
de contraprova no CÓDIGO: onde nada de fora confere o número, o único julgamento possível é
o de coerência interna (ver `arrecadacao_deadline_refuted`, abaixo). **Medição de apoio**
(2026-08-20, acervo inteiro): em **988 contas** de todas as fontes, **ZERO** tem `due_date` a
mais de 180 dias da própria extração; nas 38 guias por Vision o intervalo real é de **−11 a
+16 dias**. A classe nunca ocorreu — a guarda é preventiva e sua fronteira é ~11× o extremo
já observado.

- 🔴 **`_build_records_vision` existe para tornar a assimetria VISÍVEL.** Enquanto um lado
  tinha função nomeada e o outro era inline, "o que o texto faz e o visual não" não aparecia
  em lugar nenhum. Trava de teste: um laço sobre **`VISION_SOURCES`** exige as duas correções
  nas **3** fontes, com sanidade da constante (laço vazio passaria calado).
- 🔴 **A DECISÃO do vencimento é `apply_arrecadacao_deadline`, para os dois caminhos.** O que
  muda é só a **procedência** da data: no texto, o regex determinístico
  (`extract_payment_deadline_from_text`); no visual, o campo **`payment_deadline`**, novo no
  `EXTRACTION_PROMPT`. O modelo **transcreve o rótulo**; quem decide adotá-lo é o código, com
  gate pelo **barcode**. Uma 2ª cópia da regra divergiria no primeiro ajuste e o sintoma seria
  a guia de um dos caminhos voltar a nascer `vencido`, sem erro nenhum.
- 🔴 **Texto disponível VENCE o campo do modelo.** Há dois casos em que o texto foi extraído e
  ainda assim se recorre ao Vision — o **tier 2** (texto sem valor) e a **página espelhada** —,
  e ali o resultado visual substituía a extração de texto inteira, levando a data-limite
  determinística junto. `build_records` passou a receber `doc_text`.
- 🔴 **A instrução do `due_date` no prompt PERMANECE**, e não é redundância: sem barcode não há
  gate, e é ela que ainda põe a data-limite na guia cujo código não foi lido.
- 🔴 **`payment_deadline` NÃO é coluna** — é insumo da decisão, consumido em
  `_build_records_vision` a partir do item JSON. Chave que não é coluna faz o PostgREST recusar
  o INSERT (**PGRST204**) e a conta deixa de ser gravada; teste trava `set(rec) ⊆ CSV_COLUMNS`.
- 🔴 **Data vinda do modelo é VALIDADA antes de virar `due_date`** (`_iso_date`): aceita ISO e
  formato BR, rejeita data impossível e prosa. Sem isso a falha migraria para o INSERT, longe
  da causa. 🔴 **O que vem DEPOIS dos 10 primeiros chars precisa ser separador de hora** — o
  parse era por PREFIXO, então `31/07/20260` e `2026-07-3199` (um dígito a mais, que é a
  assinatura de um deslocamento na transcrição) passavam como `2026-07-31` com o excedente
  descartado em silêncio: o valor **malformado** virava data plausível em vez de virar `None`.
  Sufixo de hora (`T00:00:00`, ` 12:00`) continua valendo.
- 🔴 **Sobrescrever valor no visual tem uma 2ª barreira** (`arrecadacao_value_refuted`, ligada
  por `ocr_barcode=True`). Ali o **próprio código é OCR**: o DV geral pega ~87% das corrupções
  de um dígito, e as ~13% que passam gravariam **10× a dívida**. Total ≥ **10×** o valor lido
  ⇒ **não sobrescreve**, preserva o número do documento e **anota** a divergência.
  🔴 **A guarda é de UMA direção só.** Refutar o lado oposto (barcode 10× *menor*) faria a
  guarda preservar o número do LLM e gravar **a menor** — exatamente o estrago que a regra da
  guia existe para matar. Casos reais ficam em **1,01×** (id 773) e **1,19×** (id 817); atingir
  10× por juros exigiria 900% sobre o principal.
- 🔴 **A DATA ganhou a contraprova que só o VALOR tinha** (`arrecadacao_deadline_refuted`).
  Era a assimetria que sobrou do conserto acima: o valor passou a cruzar com o documento e a
  data seguia entrando com validação de **forma** apenas. Um dígito de **ano** trocado
  atravessa o `_iso_date` inteiro, e o estrago **não é simétrico** ao do valor — um
  `payment_deadline` de **2126** grava uma conta que **nunca vence**, invisível em todo KPI,
  no aging e na cobrança, sem levantar erro; **2016** a faz nascer vencida há dez anos.
  Reproduzido antes de corrigir: os três valores entravam crus.
  - **A referência é a data que o MODELO leu do MESMO documento** (o vencimento do tributo).
    As duas convivem na guia e estão a **dias** uma da outra — folga medida de **0 a 3 dias**
    nas 31 guias. Teto de **180 dias**: ~60× a folga medida, ~11× o extremo do acervo.
  - **DUAS direções**, ao contrário da guarda de valor. Lá refutar o lado oposto preservaria o
    número errado do LLM (o estrago original); aqui a recusa preserva a data do **documento**
    nos dois sentidos, então nenhuma direção é perigosa.
  - **Opt-in pela PROCEDÊNCIA, não pelo call site.** No builder visual a data-limite tem duas
    origens: a do TEXTO é determinística e entra **sem** cruzamento; a do MODELO cruza. Um
    `text_deadline or item[...]` com contraprova fixa submeteria a data determinística a uma
    guarda que ela não precisa — travado por teste.
  - **A referência é do ITEM, nunca do documento.** Num carnê, parcelas a meses de distância
    julgadas contra a data da primeira seriam refutadas **em bloco**, e as legítimas nasceriam
    com o vencimento do tributo — o defeito que a regra existe para matar.
  - ⚠️ **O que ela NÃO cobre é parte do contrato:** erro de **mês ou dia** (até ~31 dias) não
    é separável de uma validade longa legítima. A guarda não tenta pegá-lo, e há teste
    travando isso para que ninguém aperte o teto acreditando o contrário.
  - ⚠️ **Ela prova COERÊNCIA ENTRE DUAS LEITURAS, não correção de nenhuma.** As duas datas
    vêm do mesmo modelo; se o que ele errou foi a **referência**, a recusa cai sobre a
    data-limite **boa** e o registro fica com o vencimento do tributo — o estado anterior à
    correção, visível e anotado. É a direção conservadora: incoerência não diz qual das duas
    leituras errou, e preservar o que já estava gravado é a única escolha que não inventa
    informação.
  - 🔴 **Sem `due_date` lido, não há referência a INVENTAR.** O `ensure_due_date` põe *hoje* no
    registro, e usar esse valor refutaria a data certa num **reprocessamento histórico** — a
    mesma armadilha já documentada em `apply_text_due_date`. Sem referência, a data-limite
    entra: é o "não há o que refutar" da família `*_refuted`.
- **Lacuna adjacente fechada na mesma passagem:** o descarte de barcode auto-refutado em
  `build_record_from_json` era regido por **tupla literal** `("pdf_vision","image_vision")` e
  deixava `docx_vision` de fora — Vision sobre a imagem embutida no Word é o mesmo OCR, e o
  print de boleto colado no Word corrompe dígito igual. Passou a usar `VISION_SOURCES`, que é
  a constante criada justamente para impedir essa classe de esquecimento.
- **Verificação — 1ª rodada** (simetria das regras): suíte Python **1593** (+26); **7
  mutantes**, todos vermelhos e revertidos com `diff -q` limpo. Um deles revelou um teste
  verde pelo motivo errado — o fator do `BOLETO_BANCARIO` da fixture decodifica em
  **2026-07-31**, a mesma data-limite da guia, e a anti-vacuidade do caso "boleto ignora a
  data-limite" seria verde por coincidência.
- **Verificação — 2ª rodada** (contraprova da data): suíte Python **1608** (+14); **8
  mutantes**, todos vermelhos, reversão confirmada byte a byte. Dois casos meus passaram
  verdes com o defeito instalado e foram **reescritos**: o de "sem vencimento lido" usava uma
  guia recente, que cabe no teto mesmo com a referência caindo para *hoje* — passou a usar uma
  guia de **300 dias** atrás, derivada de `today()` para não envelhecer; e o de N pagáveis
  usava parcelas a dias de distância, em que a referência compartilhada dá o **mesmo**
  veredito — passou a usar parcelas a **352 dias**.

**Override de GUIA TRIBUTÁRIA pelo ACRÔNIMO no ASSUNTO (não regredir):** guias estaduais
são visualmente quase idênticas (DARE × GARE × GNRE) e o Claude do `extract_pdf.py` troca
uma pela outra (caso real: id 326, assunto "PAGAMENTO DARE - REF. T05S1" extraído do
`pdf_text` como `gare`). Regra: o **acrônimo explícito no assunto é o sinal mais confiável**
do tipo de guia (quem encaminha o pagamento digita o tipo certo) e **sobrepõe** a
classificação do PDF/corpo. `_classify_tax_doc_type_from_subject(subject)`
(`_SUBJECT_TAX_DOC_KEYWORDS`) casa por **palavra inteira** (`_has_word`, sem acento) →
`darf/gps/das/gru/dare/dae/gnre/gare/ipva/iptu/iss/itbi/dam / duam/multa`. **Conservador:**
`das` (artigo do português) e `dam` **não** casam pela forma pura — só por frase inequívoca
(`simples nacional`/`simei`) para não gerar falso positivo em "pagamento DAS contas".
Aplicado nos **dois caminhos** com precedência **abaixo da concessionária** e **acima** de
honorários/PIX/keyword: `build_financial_payload` (PDF) e `extract_from_email_body` (corpo).
O prompt do `extract_pdf.py` também instrui o Claude a copiar EXATAMENTE o acrônimo impresso
no cabeçalho (não inferir pelo estado). Teste: `tests/test_doc_type_tax_subject.py`.

**Contas de concessionária** (migration 043): `conta de água`, `conta de luz` e
`conta de telefone / internet` (com barra, estilo `dam / duam`). Classificadas em `read_emails.py`
por **duas regras** (palavra inteira via `_has_word`, sem acento via `_ns_body`), ambas com
**precedência máxima** sobre boleto/fatura/PIX:
- **Frase do assunto/corpo** — `_UTILITY_DOC_KEYWORDS` + `_classify_utility_doc_type(*texts)`:
  água=`conta (de) água`; luz=`conta (de) luz`; telefone/internet=`conta (de) telefone|internet`,
  `(conta) vivo`, `vivo (conta)`, `vivo`, `fibra`.
- **Marca no NOME DO FORNECEDOR** — `_UTILITY_SUPPLIER_BRANDS` +
  `_classify_utility_by_supplier(supplier_name)`: `enel`/`eletropaulo`→luz;
  `vivo`/`claro`/`tim`→telefone-internet; `sabesp`→água. **Escopo restrito ao `supplier_name`** de
  propósito: `claro`/`tim`/`vivo` são palavras comuns no corpo ("está claro", "ao vivo") — casar no
  corpo livre geraria falso positivo.

Aplicadas no corpo (`extract_from_email_body`, recebe `subject`) e no PDF
(`build_financial_payload`, recebe `subject` — `extract_pdf.py` é cego ao assunto, então o override
é em `read_emails.py`); a frase tem precedência sobre a marca (`frase or marca`). `payment_method`
permanece o detectado (não é forçado). Geram conta a pagar (não entram em `SKIP_ACCOUNT_TYPES`).
Teste: `tests/test_doc_type_utilities.py`.

**Captura do nº de documento no corpo** (migration 043 / `_BODY_DOCNUM_RE`): além de
`_BODY_INVOICE_RE` (NF/fatura + dígitos), o rótulo **explícito** `Número do documento` captura
valores **alfanuméricos** (ex.: Sabesp `SOR202659903949`, CATAGUASES `014696-001`) como fallback,
antes do SIEG e antes do nº sintético `{tipo}_{ddmmyy}`. Conservador de propósito — rótulos
frouxos (`documento nº`) capturavam lixo ("Banco"). Backfill da migration 043 corrigiu os ids 5,
18 e 171.

**Fallback "Fatura No: NNNN"** (`_BODY_INVOICE_NO_RE`, não regredir): variante de "fatura Nº"
escrita por extenso ("No", sem o sinal º/°) que `_BODY_INVOICE_RE` não cobre — a letra "o" de "No"
não está na classe opcional `[º°.]?` do regex principal, então o número nunca casava e a conta
caía no nº sintético `{tipo}_{ddmmyy}`. Fallback de precedência mais baixa (só quando
`_BODY_INVOICE_RE` e `_BODY_DOCNUM_RE` já falharam); exige dígitos logo após o rótulo (só
espaço/`:`/`-` no meio) para não casar "fatura no valor de.../fatura no total de...". Caso real:
lembrete periódico da Contabil Esquema (contas 668/669, texto "Fatura No: 20880"). Teste:
`tests/test_forwarded_supplier_name.py`.

**Fallback "Cobrança Nº NNNN"** (`_BODY_CHARGE_NUM_RE`, não regredir): identificador da
**cobrança** nas plataformas de assinatura (Efí/Gerencianet e afins); o `\s*` aceita a quebra
de linha entre o rótulo e o "Nº" (HTML achatado). Precedência mais baixa que os três acima.
**Nunca capturar "Assinatura Nº"**: esse número é o MESMO em todas as cobranças do contrato,
então usá-lo como `invoice_number` faria a cobrança do mês seguinte **deduplicar** contra a
anterior (impressão 2: fornecedor+número+valor) e o título seria **perdido em silêncio**.
Caso real: conta 694 — ver "NOTIFICAÇÃO DE COBRANÇA DE PLATAFORMA". Teste:
`tests/test_body_platform_invoice.py`.

**Regra honorários** (migration 024): e-mail de honorários (keyword de assunto `honorário`;
termo `honorário(s)` no corpo ou recibo) é gravado com `document_type='honorários'` e
`payment_method='pix'` — honorários mantêm o tipo `honorários` mesmo com PIX detectado (o PIX só
define a forma de pagamento, nunca o tipo — ver a nota sobre `pix` removido dos tipos de documento),
e o pagamento é forçado a `pix` tanto no corpo (`extract_from_email_body`) quanto no PDF
(`build_financial_payload`).

**Forma de pagamento DECLARADA no corpo → `payment_method` (não regredir):** quando o pagador
escreve como pagou (ex.: "PAGAMENTO EM DINHEIRO", "pago depósito", "TED AGÊNCIA…", "Tipo de
pagamento: Débito Automático"), a forma é capturada em vez de cair em `outro`. Caso de origem:
id 442 (MANOS DOCES, R$ 182,49, "PAGAMENTO EM DINHEIRO") gravava `outro` → agora `dinheiro`.
`_classify_body_payment_method(*texts)` (`read_emails.py`) casa por **palavra inteira sem acento**
(`_has_word`/`_ns_body`) contra `_BODY_PAYMENT_METHOD_KEYWORDS` e devolve o valor do enum
`PAYMENT_METHODS` (`dinheiro`/`depósito`/`débito automático`/`crédito`/`débito`/`cartão`/`ted`/
`transferência`/`cheque`/`vale`/`duplicata`/`pix`/`boleto`) ou `None`. `débito automático` (débito
direto em conta — "Tipo de pagamento: Débito Automático" das contas Sabesp; migration 071) vem
**ANTES** do `débito` genérico (cartão) na lista, para casar o valor específico. **Precedência
POR TEXTO** (chamado com
`(body_text, subject)` → o **corpo vence o assunto**: id 325 corpo "TED AGÊNCIA…" vs assunto
"PAGAMENTO PIX" → `ted`); dentro de um texto, a ordem da lista desempata (`crédito`/`débito`
antes de `cartão`, p/ "cartão de crédito" → `crédito`) — mas essa ordem codifica
**especificidade, não confiança**, e por isso o **rodapé institucional da plataforma de
cobrança é descartado ANTES** de classificar (`_strip_platform_boilerplate`; ver
"NOTIFICAÇÃO DE COBRANÇA DE PLATAFORMA" abaixo). Aplicado em `extract_from_email_body`
**só como preenchimento de lacuna**: roda quando `payment_method == 'outro'`, **abaixo** do
`has_pix` (PIX) e do override de boleto por código de barras — que têm precedência e não são
sobrescritos. Só no caminho do **corpo** (o do PDF usa o `payment_method` do extrator). Falso
positivo de `crédito`/`débito` em texto não-financeiro (ex.: "cadastros de crédito" de alerta de
protesto) é contido a montante pela regra `subject_is_ignorable_notification` (protesto/cartório
viram `ignorado`, nunca chegam a virar conta). Teste: `tests/test_body_payment_method.py`.

**NOTIFICAÇÃO DE COBRANÇA DE PLATAFORMA (Efí/Gerencianet e afins) — conta 694, três
defeitos no mesmo e-mail (não regredir):** e-mail de boleto de assinatura emitido por
plataforma. Cada defeito tem causa e correção próprias:

- **`payment_method='crédito'` num título que o e-mail chama de BOLETO** do assunto à
  primeira linha. A única menção a crédito estava no **rodapé institucional da
  plataforma** — *"é possível emitir e enviar boletos, carnês, cobranças via **cartão de
  crédito** e links de pagamento"* —, uma lista dos **produtos dela**, não uma declaração
  sobre este título. Dentro de um mesmo texto o desempate é a ordem de
  `_BODY_PAYMENT_METHOD_KEYWORDS`, que codifica **especificidade** (`débito automático`
  antes de `débito`), **não confiança** — e `crédito` (5º) vencia `boleto` (12º).
  Correção: **`_strip_platform_boilerplate`** descarta o texto a partir do 1º marcador de
  rodapé (`_PLATFORM_FOOTER_MARKERS`) **antes** de classificar. O corte é na **POSIÇÃO
  exata** do marcador, não na linha inteira: quando o marcador divide a linha com
  conteúdo útil ("Pago em dinheiro. Esta cobrança foi gerada pela Efí."), descartar a
  linha toda jogaria fora a própria declaração que se quer classificar. Isso só é
  possível porque **`_ns_keep_len`** normaliza preservando o comprimento (`str.translate`
  1:1, acento→ASCII e maiúscula→minúscula num passo só) — o `_ns_body` usa NFD, que
  **decompõe** o acento em 2 code points e descarta 1, mudando os índices e invalidando
  qualquer mapeamento de offset de volta ao texto original.
  Escopo deliberado: só o classificador de forma de pagamento (o de tipo de documento
  não foi tocado, para não mover dado sem medição).
- **`invoice_number` sintético** — o número real vinha rotulado "Cobrança Nº 1040983896"
  (`_BODY_CHARGE_NUM_RE`; o `\s*` cobre a quebra de linha entre o rótulo e o "Nº", do
  HTML achatado). **Nunca capturar "Assinatura Nº"**: o nº da assinatura é o **mesmo em
  todas as cobranças** do contrato, então usá-lo faria a cobrança do mês seguinte
  **deduplicar** contra a anterior (impressão 2) e o título sumiria em silêncio.
- **Fornecedor LIXO** — o nome saiu do assunto ("Boleto **referente à assinatura
  1040983896 de Manutenção - ot**") e virou um cadastro em `supplier`, enquanto o real
  ("AGENCIA K1 DIGITAL WEBSITES E MARKETING") estava sob **"Dados do emissor"**, na
  **linha seguinte** ao rótulo — que `_BODY_NAME_RE` não alcança (exige valor na MESMA
  linha). Correção em duas camadas: **`_BODY_ISSUER_RE`** (bloco `Dados do
  emissor/beneficiário/cedente/sacador`, valor na mesma linha ou na seguinte; fallback de
  `_BODY_NAME_RE`, que mantém precedência) e uma **guarda em
  `_supplier_name_from_subject`** (`_SUBJECT_NON_NAME_START_RE`) que rejeita o que sobra
  começando por conectivo (`referente`, `ref.`, `relativo`, `sobre`, `conforme`,
  `acerca`). **"de/da/do" ficam DE FORA** da guarda de propósito — iniciam razão social
  real ("DE NADAI ALIMENTAÇÃO"). O rótulo **"emitido por" também ficou de fora** do
  `_BODY_ISSUER_RE`: no rodapé ele nomeia a **plataforma** ("emitido por
  www.sejaefi.com.br"), não o fornecedor.

**Resolvedores de campo do corpo — `extract_from_email_body` orquestra, não resolve
(refactor de 2026-07-28):** cada campo do corpo é uma **cadeia de precedência**
(`rotulado → tabela → padrão`). Mantidas inline, cada regra nova virava mais um ramo na
mesma função, que chegou a **complexidade ciclomática 61 (grau F)** — o ponto em que uma
alteração deixa de ser revisável. As cadeias viraram funções **puras**, testáveis
isoladamente: `_resolve_body_supplier_identity`, `_resolve_body_barcode`,
`_resolve_body_invoice_number` (tabela `_BODY_INVOICE_SOURCES`, em ordem de precedência,
no lugar de 5 `if`s encadeados), `_resolve_body_dates` e `_resolve_body_doc_and_payment`.
Resultado medido com `radon`: **61 (F) → 17 (C)** na função principal; cada resolver fica
em A/B/C. **Comportamento idêntico** — provado pelo A/B abaixo (0 diferenças). A ordem de
cada cadeia É a regra de negócio (ex.: a linha da tabela vem ANTES do fallback pela data
do e-mail); é isso que `tests/test_body_resolvers.py` trava.

**Barcode do corpo: a FORMA vence o RÓTULO (não regredir).** `_BODY_BARCODE_RE` aceita
quaisquer `[\d.\s]{47,60}` após o rótulo — e `\s` **inclui quebra de linha**, então um
número curto rotulado pode **colar** com os dígitos das linhas seguintes e formar 48
dígitos, comprimento que `normalize_barcode` aceita como arrecadação (que ela não valida
além do tamanho): um código de barras **inventado**, que envenenaria a dedup. Por isso
`_resolve_body_barcode` tenta **primeiro** `_extract_body_linha_digitavel` (valida a
estrutura dos 5 campos FEBRABAN) e só então o rótulo — que permanece como fallback porque
cobre o que a forma estruturada não cobre: a arrecadação de 48 dígitos. Defeito
**pré-existente**, achado pelo teste do resolver, não por falha em produção.

**As guardas do barcode vivem na função CANÔNICA, não no leitor de e-mail (não
regredir).** A primeira versão pôs a trava em `read_emails._reject_glued_arrecadacao` — e
o code review seguinte mostrou que **o caminho de PDF tinha o MESMO defeito, em forma pior**:
o fallback de `extract_barcode` casa `[\d\s\.]{47,60}` **sem nenhuma âncora de rótulo**
(no corpo, ao menos, exige-se o rótulo antes). A guarda duplicada foi removida e a regra
passou para `extract_pdf`, cobrindo os dois caminhos:

- **`normalize_barcode`** rejeita 48 dígitos que não comecem por **`8`** (identificador de
  produto da arrecadação FEBRABAN). Verificado: **35/35** dos barcodes de 48 dígitos
  gravados começam por `8`. O mesmo invariante está em **`is_boleto_barcode`**, porque ela
  também julga texto **CRU**, que não passou pelo normalizador (detecção de página pagável).
- **`barcode_dv_refuted`** — DV geral (módulo 11) do boleto bancário de 44 dígitos. O nome
  afirma o que a função consegue **provar**: `False` significa **não refutado** (o DV
  confere OU não há DV a conferir — chave NF-e/CT-e, arrecadação, tamanho inválido), nunca
  "validado". Um nome como `is_valid` seria lido como garantia e mentiria nos casos em que
  a regra nem se aplica.
- **O PADRÃO é o lado SEGURO, e a concessão está no NOME** (não num parâmetro escolhido por
  omissão): **`normalize_barcode`** valida e rejeita o que o DV refuta;
  **`normalize_barcode_allow_misread`** é o opt-in explícito para dígitos de procedência
  confiável (captura estruturada, ou Vision/LLM lendo o documento). Assim, quem escrever uma
  captura frouxa nova e chamar por hábito recebe a validação — e não o buraco. As duas falhas
  não se equivalem: código **inventado** causa dedup falsa e **perda silenciosa de pagável**;
  barcode **ausente** só custa a chave de dedup, com a conta gravada do mesmo jeito. O
  `read_emails` espelha o par (`_normalize_body_barcode` / `_normalize_body_barcode_allow_misread`)
  — sem flag booleana.
- **Os FALLBACKS DEFENSIVOS do `read_emails` espelham a canônica, invariante incluso.** Os
  helpers do corpo importam o `extract_pdf` de forma lazy e caem num fallback local se o
  import falhar; ao endurecer a canônica, o fallback de `_is_boleto_barcode` ficou para
  trás e passou a aceitar 48 dígitos sem o `8` — uma cópia que **diverge da regra real é
  pior que não existir**, porque mente em silêncio justamente quando a fonte única está
  indisponível. Coberto por teste que força a falha de import (`TestFallbackDefensivoAlinhado`).
  O fallback de `_normalize_body_barcode` degrada de propósito para **lenient** mesmo com
  na variante segura: sem a canônica não há como validar DV, e rejeitar por não-saber
  perderia barcode legítimo — mas o invariante do `8` (que não depende dela) continua valendo.
  **A degradação AVISA no log (uma vez por processo)**, por `_febraban_fn` — ponto ÚNICO
  que cobre os dois modos de indisponibilidade com o mesmo aviso: módulo ausente (**deploy
  PARCIAL**) e módulo presente **sem a função** (canônica renomeada). O segundo é o
  traiçoeiro: o despacho é por NOME (`getattr`), então sem o aviso a validação cairia calada
  devolvendo resultado plausível. Um teste-guarda confere que os nomes existem de fato, e o
  re-export do `extract_pdf` faz o rename estourar já no import.

**`febraban.py` — o cluster de código de barras é módulo próprio, SEM dependências.** Ele
vivia dentro do `extract_pdf`, então o caminho do CORPO importava pandas + pdfplumber + PIL
+ pypdf (**~580 ms**) só para rodar alguns regex sobre dígitos — e era esse peso que exigia
o import lazy com fallback em 4 helpers do `read_emails`. Movidas para
`skills/pdf-contas-pagar/scripts/febraban.py` (só stdlib, **~8 ms**): `normalize_barcode`,
`normalize_barcode_allow_misread`, `barcode_dv_refuted`, `is_boleto_barcode`,
`extract_barcode`, `extract_linha_digitavel`, `amount_from_barcode`, `due_date_from_barcode`,
`authoritative_barcode_due_date` (+ helpers de data e as constantes de fator). O
**`extract_pdf` reexporta tudo**, então todo call site e teste que usava `extract_pdf.<nome>`
segue valendo — e o re-export ainda serve de guarda: renomear algo lá quebra o import,
alto e cedo.

**O `try` cobre só a OBTENÇÃO da função, não a CHAMADA (não regredir).** Antes, um erro
DENTRO da canônica virava "barcode não validado" — um bug se disfarçando de degradação.
Agora a chamada fica fora do `try` e sobe como erro. A ÚNICA exceção é
`_apply_barcode_due_date`, best-effort deliberado por rodar no choke point de TODA gravação
(uma correção opcional de vencimento não pode derrubar a conta) — mas ali o `except` faz
`log.exception`, com traceback, em vez de engolir calado.

**A assimetria é deliberada e medida — não "esqueceram de validar".** Por origem de
extração, o DV fecha em **`pdf_text` 228/228** e **`email_body` 6/6**, e em **`pdf_vision`
36/56**: dígito vindo do TEXTO sempre fecha (o que valida a implementação), e o DV
discrimina com precisão a leitura corrompida por **OCR** — o id 463, já documentado como
barcode corrompido, está entre as falhas. Por isso a extração **ESTRUTURADA** permanece
lenient: ali um DV que não fecha é o OCR lendo errado um código **REAL**, e descartá-lo
apagaria a única chave de dedup do título — a política do projeto para barcode suspeito é
**não deixar que ele decida** (o gate de `authoritative_barcode_due_date`), não apagá-lo.
Já um código **colado** por captura frouxa não é o código de nada: rejeitar é o único
destino correto. **Impacto em produção: zero** — o caminho frouxo do PDF só é alcançado no
fallback de `pdf_text` (quando a chamada ao Claude falha), e todos os barcodes `pdf_text`
passam no DV.

**Risco medido e deliberadamente NÃO "otimizado":** `_extract_body_invoice_rows` roda 2×
por e-mail (uma em `extract_from_email_body`, outra em `try_extract_from_body`). Medido no
pior corpo real (11 KB): **445 µs**, ou **2,2%** de `extract_from_email_body` e ~0,04% do
custo de IMAP+Claude por e-mail. Um cache traria risco de aliasing/estado velho (as linhas
são dicts mutáveis) por ganho nulo — mantido como está, de propósito.

**Verificação (A/B contra dados reais):** os **139** corpos gravados e os **764** assuntos
distintos foram reprocessados com o código de HEAD e com o novo. Corpos: muda só a **694**
(`invoice_number`, `payment_method`) e o fornecedor muda **só** nela; assuntos: **4 de 764**
mudam, **todos** de nome-lixo para vazio (`referente ao pedido`, `REF. PEÇAS PARA HR`…) —
ou seja, a guarda só deixa de criar cadastro-lixo. Testes:
`tests/test_body_platform_invoice.py`.

> **Oportunidade NÃO implementada — o boleto está atrás de LINK** (`visualizacao.gerencianet.com.br`).
> Verificado em 2026-07-28: o link **responde** e devolve o boleto em **HTML** (não PDF) com
> linha digitável, beneficiário e CNPJ. Hoje `extract_pdf_links` **não o reconhece** (a âncora é
> "Acessar", a URL não tem `.pdf`) e `download_pdf_from_url` só aceita PDF — o PDF real é montado
> por **JS** (`download.sejaefi.com.br/<id>.pdf`), mesma classe do handler adiado da SIEG. Um
> handler Efí resolveria os três defeitos na origem, pelo caminho canônico de PDF. O `barcode` da
> conta 694 foi preenchido **manualmente** a partir dessa página (registrado em
> `processing_notes`) — não é capacidade do pipeline.

### Auto-resolução de fornecedor

> **Três regras de fornecedor moram na seção "Normalização de `document_type`"**, junto do caso de
> documento que as originou — procure lá antes de concluir que uma situação não é tratada:
> **CEDENTE do boleto vence o EMITENTE do CT-e** (fatura SSW), **Beneficiário Final vence
> Beneficiário/Cedente** (boleto securitizado) e **Fornecedor ROTULADO no corpo vence o nome do
> ANEXO sem identificador forte** (`_body_supplier_identity` — anexo que é pedido/recibo lido por
> Vision). As três sobrepõem o fornecedor **extraído**, antes de `_finalize_supplier`; os fallbacks
> desta seção só entram quando nada foi extraído.

**ASSUNTO como ÚLTIMO recurso para o nome do fornecedor (não regredir):** e-mail INTERNO de
pagamento ("PAGAMENTO BOLETO HYOSUNG 181063-3", "ENC: GUIA GNRE", "PAGAMENTO PIX FULANO")
encaminha um boleto/imagem cujo anexo **não traz nome/CNPJ/CPF**, e o remetente interno
(`@otimotex`/`@lebianco`) é **bloqueado** como fornecedor (migration 046) — a RPC então lança
"nenhum identificador válido" e a conta (com valor + código de barras) era **PERDIDA** como
`db_erro`. Correção: `_finalize_supplier`, quando não há nome/CNPJ/CPF extraído, deriva o nome
do favorecido do **assunto** via `_supplier_name_from_subject` (remove prefixos de
encaminhamento `ENC:/RES:/RE:/FWD:`, as palavras de ação `pagamento/boleto/pix/guia/…` e a
cauda de número de documento). Vale para TODOS os caminhos (PDF/imagem/corpo — todos passam
por `_finalize_supplier` com `payload['subject']` preenchido). Conservador: só roda como
último recurso e devolve `''` para assunto sem nome utilizável — inclusive quando o que
resta **começa por conectivo** (`_SUBJECT_NON_NAME_START_RE`: `referente`, `ref.`,
`relativo`, `sobre`, `conforme`, `acerca`), que é continuação de frase, não nome; sem essa
guarda "Boleto **referente à** assinatura 1040983896 de Manutenção - ot" virava um
fornecedor-lixo (conta 694 — ver "NOTIFICAÇÃO DE COBRANÇA DE PLATAFORMA"). **`de`/`da`/`do`
ficam FORA da guarda** de propósito: iniciam razão social real ("DE NADAI ALIMENTAÇÃO").
Testes: `tests/test_supplier_from_subject.py`, `tests/test_body_platform_invoice.py`. **Efeito colateral conhecido:** o assunto pode
criar um fornecedor com nome "curto" (ex.: `HYOSUNG`) divergente de um cadastro canônico
existente (`HYOSUNG SC`, CNPJ 11703922000181) — o operador funde os dois em `/fornecedores`
quando forem o mesmo (não há merge automático, pois o boleto não trouxe CNPJ para provar).

**REMETENTE ORIGINAL encaminhado no CORPO — fallback ABAIXO do assunto ancorado em sigla
(não regredir — caso real conta 669, 2026-07-23):** quando o e-mail é um encaminhamento interno
(funcionário reencaminha um lembrete/notificação externa) e o CORPO preserva, numa linha
`De:`/`From:` mais profunda da cadeia, o remetente ORIGINAL com sigla de razão social
(`Contabil Esquema LTDA <lembrete@contabilesquema.com.br>`), esse nome é usado quando o
**assunto não traz uma âncora própria**. Falha real: a conta 669 (mesmo remetente recorrente da
668, corrigida horas antes) foi gravada sob um fornecedor fragmentado ("esquema") porque (a) o
corpo não tinha rótulo explícito `Fornecedor:`/`Favorecido:`, (b) o `Documento:` citado no corpo
é o **CNPJ da própria empresa pagadora** (bloco do destinatário, descartado por
`_finalize_supplier` — ver "O CNPJ DA PRÓPRIA EMPRESA PAGADORA" acima) e (c) sem nome nem CNPJ
válido, o fallback caía direto no assunto truncado ("boleto esquema", sem sigla). Correção:
`_supplier_from_forwarded_sender(body_text)` — via `_FORWARDED_FROM_LINE_RE` (mesmo padrão de
`forwarded_subjects_from_body` para `Assunto:`/`Subject:`), varre as linhas `De:`/`From:`, ignora
as de domínio interno (`otimotex.com.br`/`lebianco.com.br`) e usa a **última** que ancorar numa
sigla de razão social (`_supplier_name_by_legal_suffix` — conservador, evita capturar nome de
PESSOA como o funcionário que encaminhou).

**Ordem importa — achado em code review (2026-07-23), corrigido antes do merge:** a 1ª versão
inseria este fallback ACIMA do assunto (e também, redundantemente, dentro de
`extract_from_email_body`) — uma linha `De:` de um TERCEIRO da cadeia (ex.: um
intermediário/repassador que também tem sigla LTDA no nome) venceria um assunto **já correto**
como "FATURAMENTO -- MOVVI LOGISTICA LTDA" (caso real id 401), silenciosamente atribuindo a
conta ao fornecedor errado — pior que o bug original, que ao menos falhava de forma óbvia
("esquema"). Corrigido: em `_finalize_supplier`, o assunto ancorado em sigla
(`_supplier_name_by_legal_suffix(payload['subject'])`, sinal do PRÓPRIO e-mail) roda **fallback
2, ANTES** de `_supplier_from_forwarded_sender` (**fallback 3**, lendo
`payload['email_body_excerpt']`), que só entra em jogo quando o assunto não tiver âncora; a
chamada duplicada dentro de `extract_from_email_body` foi **removida** — é fonte única em
`_finalize_supplier` (comentário no código explica o porquê, para não reintroduzir). Testes:
`tests/test_forwarded_supplier_name.py` (inclui o caso MOVVI/intermediária, que falha sem o
fix). Correção pontual da conta 669 aplicada em 2026-07-23 (fornecedor → sk_supplier 1052
"CONTABIL ESQUEMA", mesmo da
conta 668; nº documento → "20880"; classificação contábil → 9/61, herdada do fornecedor).

**E-MAIL do remetente ORIGINAL encaminhado — fallback 1b (migration 134, 2026-08-19):** o
fallback 3 acima extrai o **NOME** e exige âncora de SIGLA de razão social, âncora que uma
**pessoa física nunca satisfaz**. Caso real: a conta **1101** — um DAR da Junta Comercial de Mato
Grosso que o despachante (`JOSE RICARDO PRUDENTE <ricardo.advogado@yahoo.com.br>`) mandou para a
funcionária, que o encaminhou. O PDF de guia não traz favorecido, o remetente imediato é interno
(bloqueado desde a 046) e o único sinal do credor é o `De:` da cadeia. A conta foi gravada sob a
própria **OTIMOTEX**, herdando a classificação default dela — *Recursos Humanos / Festas e
Confraternizações* numa guia da Junta Comercial.

`_forwarded_sender_email(body_text)` extrai o **ENDEREÇO** (não o nome) da linha `De:` mais
profunda que não cite domínio interno, e `SupabaseControl.find_supplier_by_email` o consulta pela
RPC `find_supplier_by_email` (migration 134).

🔴 **SÓ IDENTIFICA, NUNCA CRIA.** O endereço diz quem **MANDOU** o documento, não quem
**RECEBE** o pagamento — e qualquer pessoa pode encaminhar uma guia. A RPC é consulta pura; usar
`resolve_supplier` no lugar dela **compila e passa no caminho feliz**, mas faria cada encaminhador
virar fornecedor no primeiro e-mail, pelo auto-insert de `resolve_supplier_id`.

🔴 **RODA ANTES DA REGRA DE IMPOSTO**, e é esse o ponto: a regra de imposto faz `return True`
**incondicional**, então tudo depois dela é inalcançável para guia de tributo sem favorecido —
exatamente o caso a resolver. Não regride a família do id 374 porque exige, CUMULATIVAMENTE:
(a) nenhum favorecido extraído, (b) linha `De:` de domínio NÃO interno, (c) um endereço nessa linha
e (d) esse endereço **já cadastrado e ativo** em `supplier`.

🔴 **A GUARDA DE PRECEDÊNCIA (achada em autorrevisão, antes do merge):** rodar antes da regra de
imposto é também rodar **antes do fallback 2** (assunto ancorado em sigla). Sem guarda, um
INTERMEDIÁRIO cadastrado venceria um assunto já correto — exatamente a regressão da **conta 401**
descrita acima, só que por e-mail em vez de nome, e **em silêncio**. A guarda separa os dois mundos:

| Documento | O 1b… | Porquê |
|---|---|---|
| **guia de tributo** (`_is_tax_document`) | **vence o assunto** | ali o assunto NUNCA foi fonte — a regra de imposto o curto-circuita de propósito, porque assunto de guia produz fornecedor-lixo ("IMPOSTOS"). O 1b só concorre com "OTIMOTEX". É o caso 1101, cujo assunto **tem** sigla ("R. A UTILIDADES E ACESSORIOS LTDA") — mas essa é a empresa do processo, não o credor |
| **qualquer outro** | só entra **se o assunto não tiver âncora** | preserva a ordem documentada: assunto ancorado > linha `De:` da cadeia |

Os dois lados têm teste próprio e foram validados por mutante nas duas direções (afrouxar a guarda
quebra a 401; apertá-la devolve a 1101 para a OTIMOTEX).

🔴 **O corpo chega por PARÂMETRO (`body_text`), não por `payload['email_body_excerpt']`** — essa
coluna **nunca é povoada no caminho de ANEXO** (`FINANCIAL_FIELDS` só tem as colunas do CSV do
`extract_pdf`). Consequência colateral descoberta aqui: o **fallback 3 (por NOME) está MORTO no
caminho de PDF** desde que foi escrito — só funciona para conta vinda do corpo. Não foi
"revivido" de propósito: ele desemboca em `resolve_supplier`, que **cria** fornecedor, e mudar
isso alteraria o comportamento de todo e-mail com anexo cujo corpo tenha um `De:` externo.
Povoar `email_body_excerpt` no caminho de anexo também está **descartado**: faria
`apply_contact_writeback` varrer o corpo inteiro e **escrever contato no cadastro** do fornecedor.

🔴 **O SINAL É MARCADO COMO FRACO, e isso governa o WRITE-BACK** (achado do review light de
2026-08-19, corrigido no mesmo dia): o bloco grava `payload['_supplier_signal'] =
'forwarded_email'`, e `apply_forced_classification` **suprime o write-back** quando o sinal está em
`SUPPLIER_SIGNAL_WEAK`. Sem isso, um despachante/contador **cadastrado** que encaminhasse uma
GNRE/DARE teria a classificação **tributária** gravada no seu cadastro — reescrevendo a curadoria
manual e valendo para **todas as contas futuras** dele. Antes do 1b esse caso caía na OTIMOTEX, que
`apply_forced_classification` já isentava: a proteção existia **por acidente do destino**, e o 1b a
removeria em silêncio. `TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS` (que salva o sk 1262) **não
resolve a classe** — é allowlist reativa, só protege quem alguém já descobriu.
**A conta continua recebendo a classificação forçada; só o cadastro não é tocado.**

🔴 **Chave efêmera do payload nasce com `_` e morre na FRONTEIRA de gravação.**
`register_financial` serializa o payload **inteiro** (`json.dumps`) para o PostgREST, então
qualquer chave que não seja coluna faz o INSERT ser recusado com **PGRST204** — e o efeito não é
uma linha errada, é a **conta deixar de ser gravada**. `strip_transient_fields(payload)` é chamada
lá, em **ponto único**, e é genérica **por prefixo** e não por lista de nomes: a próxima chave
efêmera é limpa sem ninguém precisar lembrar. Travado por
`tests/test_transient_payload_fields.py`, que inspeciona o **corpo realmente enviado** na
requisição — testar só a função pura não cobriria o call site, que é onde o defeito mora.

O dado que faz a regra funcionar é curadoria: `supplier.email` do sk **1262** recebeu
`ricardo.advogado@yahoo.com.br`. Testes: `tests/test_supplier_forwarded_email.py` (inclui o caso
que **executa `extract_and_store_accounts`** para provar que o call site do PDF repassa
`body_text` — guarda por texto não cobriria isso), `tests/test_transient_payload_fields.py`
(cadeia inteira: marca → supressão → limpeza), `tests/test_classification_overrides.py`
(`WeakSupplierSignalTest`) e o caso de anti-regressão em `tests/test_supplier_imposto.py`.

**E-mail de PLATAFORMA não identifica fornecedor — e IDENTIFICADOR FORTE vence e-mail
(migration 109, 2026-08-03 — não regredir):** o Passo 4 (e-mail) resolvia o fornecedor mesmo
quando a extração trouxera um **CNPJ** que simplesmente ainda não estava cadastrado. Como
`no-reply@sswsistemas.com.br` é o endereço da **plataforma SSW** (usada por dezenas de
transportadoras) e estava em **4 fornecedores distintos**, a conta ia para o primeiro que casasse.
Caso real (conta **794**, fatura SSW nº 79399): a extração devolveu corretamente `PANTANAL
LOGISTICA E TRANSPORTES LTDA` + CNPJ `08662661000194`, e a conta foi gravada sob `TRANSPORTADORA
J.D.F.`. **O defeito atinge exatamente o fornecedor NOVO** — a PRIMEIRA fatura de cada
transportadora era atribuída a uma antiga, e as seguintes acertavam (aí o cadastro já existia e
casava por nome/CNPJ); por isso passou despercebido: das 14 contas de faturas SSW, **só 1** estava
errada (conferido lendo o beneficiário de cada PDF no bucket). Duas travas independentes:
1. **CNPJ/CPF informado que não casou ⇒ fornecedor NOVO ⇒ auto-insert**, nunca casar por e-mail
   (`v_has_strong` em `resolve_supplier_id`). Vale para QUALQUER plataforma (SIEG, Efí…), sem
   lista para manter.
2. **`_is_platform_email`** (domínios `sswsistemas.com.br`/`ssw.inf.br`) — mesmo raciocínio do
   `_is_internal_email` da 046: endereço compartilhado por vários fornecedores identifica o
   INTERMEDIÁRIO, não quem recebe. Bloqueia casar, armazenar no auto-insert **e propagar por
   `_add_supplier_email`** — sem o terceiro, a limpeza dos cadastros se desfaria na fatura
   seguinte. Ao descobrir plataforma nova (endereço presente em fornecedores de empresas
   diferentes), acrescente o domínio na função **e** limpe os cadastros que já o capturaram.
**Não regride** (verificado contra o banco com rollback): CNPJ cadastrado casa; nome cadastrado
casa; e-mail LEGÍTIMO sem CNPJ **segue casando** (regra da 054 preservada). Correção de dados:
conta 794 → PANTANAL (sk 1323) com a classificação de transporte preservada.

✅ **Reverificado por EXECUÇÃO em 2026-08-04** (não por leitura da migration): rodando
`resolve_supplier_id('<CNPJ novo>', NULL, '<transportadora nova>', 'no-reply@sswsistemas.com.br')`
dentro de um `BEGIN … ROLLBACK`, a RPC devolve um **sk NOVO** (auto-insert) — antes da 109 devolvia
o **241** (`TRANSPORTADORA J.D.F.`). Complementos conferidos no mesmo momento: o e-mail da
plataforma não está mais em **nenhum** dos 4 campos de e-mail de nenhum cadastro, e a única conta
que ainda aponta para o sk 241 é a **371**, de 02/07 e **cancelada**. Reproduzir a situação que
causou o bug é a única forma de provar a correção — "a migration está aplicada" não prova
comportamento. Este bloco de verificação nasceu de um falso alarme: ver o aviso de DIAGNÓSTICO em
`lib/appendUniqueById.ts` (grid exibindo valor antigo depois de correção feita no banco).

**SIGLA DE RAZÃO SOCIAL (LTDA) como âncora do nome no assunto (não regredir):** a razão social
quase sempre TERMINA numa sigla societária (`LTDA`/`EIRELI`/`EPP`/`MEI`/`S.A.`), então ela é a
âncora mais confiável para isolar o nome do fornecedor no assunto. `_supplier_name_by_legal_suffix`
(usado com **preferência** dentro de `_supplier_name_from_subject`) pega o SEGMENTO do assunto que
termina na sigla — descartando prefixos (`FATURAMENTO --`, `ENC:`, `PAGAMENTO`) e a cauda de
data/número após a sigla. Ex.: `"ENC: FATURAMENTO -- MOVVI LOGISTICA LTDA 03/07/2026"` →
`"MOVVI LOGISTICA LTDA"` (a heurística genérica devolvia `"FATURAMENTO -- MOVVI LOGISTICA LTDA"`,
que não casava o cadastro pelo `normalize_search`). Fica com a **última** sigla quando há mais de
uma (o pagador pode aparecer antes do fornecedor). Siglas `ME`/`SA` **isoladas** (sem separador)
ficam de fora — ruidosas demais. Caso de origem: conta id 401 (fatura MOVVI que caíra sob OTIMOTEX).

**O CNPJ DA PRÓPRIA EMPRESA PAGADORA (OTIMOTEX) NUNCA é o fornecedor (não regredir):** e-mails de
faturamento reencaminhados trazem o **bloco do destinatário** no corpo (ex.: `TÊXTIL E CONF.OTIMOTEX
/ CNPJ: 47273917/0001-23`), e a extração capturava esse CNPJ como se fosse do fornecedor — gravando
a conta sob a OTIMOTEX (sk=1) mesmo com o favorecido real nomeado no assunto. `_finalize_supplier`
descarta o `supplier_cnpj` extraído quando ele é igual ao CNPJ da empresa pagadora
(`ctrl.company_cnpj()`, sk_company=1) — assim a resolução segue pelo nome/assunto (âncora LTDA
acima). É a **guarda que habilita** a âncora de assunto nesse caso (sem ela, o CNPJ do pagador
venceria a resolução por CNPJ antes do fallback de assunto). **Não** afeta a regra de imposto nem o
fallback de pagador, que gravam OTIMOTEX explicitamente quando NÃO há favorecido. Best-effort (se o
ctrl não expõe `company_cnpj`, a guarda é pulada). **Comparação pela RAIZ do CNPJ (8 primeiros
dígitos), não pelo número completo de 14 dígitos (correção 2026-07-23 — caso id 668/e-mail 1004):**
OTIMOTEX/LEBIANCO/FARDOS (e outras filiais do mesmo grupo) compartilham a mesma raiz `47273917`,
divergindo só no sufixo de filial/DV (`0001-23`/`0002-23`/`0003-23`/...). O bloco do destinatário
pode trazer OUTRA filial não cadastrada em nenhum `sk_company` (ex.: `47273917/0003-95`) — o match
exato deixava passar e a conta era resolvida sob o `sk_supplier` de uma filial da própria OTIMOTEX
já mal-cadastrada como "fornecedor" (id real: conta 668 foi gravada sob sk_supplier=404, trade_name
"CDI", legal_name = a própria OTIMOTEX). Testes em `tests/test_supplier_from_subject.py`
(`test_cnpj_de_outra_filial_do_mesmo_grupo_e_descartado`).

**Um TIPO DE DOCUMENTO ou TIPO DE PAGAMENTO NUNCA vira fornecedor (não regredir):** o assunto
"ENC: GUIA GNRE" reduzia a "GNRE" (um `document_type`) e virava fornecedor — errado.
`_is_non_supplier_term` (set `_NON_SUPPLIER_TERMS`, espelha `DOCUMENT_TYPES`/acrônimos de tributo
de `WORD_KEYWORDS` + `PAYMENT_METHODS` de `@sheild/shared`) rejeita o candidato quando ele É, no
todo, um tipo (`GNRE`, `BOLETO`, `PIX`, `DARF SP` — acrônimo isolado + UF/número). **Não** rejeita
nomes que apenas CONTÊM a palavra (`Porto Seguro`, `Vale Fertilizantes`). O filtro vale para o nome
**extraído** E o derivado do assunto.

**Fallback final — o PAGADOR (último recurso de todos):** quando esgotam CNPJ/CPF/nome/e-mail/
assunto e o pagador está claro, `_resolve_supplier_by_payer` usa `payer_cnpj` (14 dígitos, casa o
fornecedor por CNPJ) ou `payer_name` (ex.: `OTIMOTEX`) como fornecedor — garante que a conta a pagar
**nunca se perca** por falta de fornecedor identificável; o operador reclassifica em `/consulta`. É o
que as 5 guias GNRE internas usam (favorecido real = a SEFAZ da UF, que a extração não captura → caem
no pagador OTIMOTEX). A guarda `sem_fornecedor` (PDF) também aceita assunto/pagador como chave, para
não barrar a conta antes do fallback rodar. Ordem completa: **extraído → assunto → e-mail (RPC) →
PAGADOR**.

**GUIA DE IMPOSTO sem favorecido real → OTIMOTEX (sk=1) — precede o assunto (não regredir):** o
credor de uma guia de tributo é o **Fisco** (SEFAZ/RFB/prefeitura), que a extração não captura; o
"favorecido" derivado do assunto vira lixo (ex.: id 374 — `document_type='darf'`, assunto
"PAGAMENTO IMPOSTOS" → criava o fornecedor fictício **"IMPOSTOS"**; idem "GNRE -PAGAMENTO",
"DARE - REF"). Regra (`_finalize_supplier`): quando `document_type` é imposto
(`_is_tax_document` → `_TAX_DOCUMENT_TYPES` = `darf, das, gru, dae, dar / dare, gnre, ipva, iptu, dam,
duam, dam / duam, iss, itbi, gare, tributo` — `dam`/`duam` avulsos são redundância defensiva, pois
`_normalize_doc_type` sempre produz o canônico `dam / duam`, que também está no set;
**`gps`/INSS e `multa` ficam de fora**, por decisão do usuário) **E**
não há favorecido REAL extraído (`supplier_name`/`supplier_cnpj`/`supplier_cpf` do documento), a conta
é lançada sob o **FORNECEDOR OTIMOTEX** (`OTIMOTEX_SK_SUPPLIER = 1` — o **fornecedor-placeholder de
imposto próprio do grupo**, usado para QUALQUER empresa pagadora; ver a nota abaixo),
**curto-circuitando os fallbacks de assunto e pagador**.
Favorecido real extraído (ex.: "PREFEITURA
DE SÃO PAULO", "CONTABIL ESQUEMA") **NÃO** dispara a regra e é preservado. A guarda `sem_fornecedor`
(PDF) também aceita `_is_tax_document` como chave, para uma guia de imposto sem nenhum outro
identificador não ser barrada antes da regra. Testes: `tests/test_supplier_imposto.py`.

> ✅ **COMPORTAMENTO CORRETO — NÃO "consertar" (multi-empresa):** esta regra grava **`sk_supplier`**
> (o fornecedor), **não** a empresa pagadora. **Não existe — nem deve existir — regra ligando
> documento tributário a `sk_company`**: a guia pega a empresa pela precedência geral (LE BLANC → 4 ·
> ester → 3 · lebianco → 2 · senão → 1), sem tratamento especial.
> **`sk_company` (PAGADORA) e `sk_supplier` (FORNECEDOR) são INDEPENDENTES** — decisão do usuário,
> reafirmada em 2026-07-17: *"company pode ser lebianco ao mesmo tempo que supplier otimotex"*.
> Logo, as **13 guias tributárias da LEBIANCO com `sk_supplier=1` (OTIMOTEX)** que existem hoje
> estão **CERTAS**, não são inconsistência. O `OTIMOTEX_SK_SUPPLIER` é o **fornecedor-placeholder de
> imposto próprio do grupo** (o credor real é o Fisco, que a extração não captura) — ele **não**
> afirma que a OTIMOTEX recebeu o valor, e por isso **não** precisa acompanhar a empresa pagadora.
> **Não** criar fornecedor LEBIANCO/FARDOS para "corrigir" isso nem fazer backfill.
> Nota: o `supplier` sk 1 **continua chamado "OTIMOTEX"** (o rename de 2026-07-17 foi só de
> `company.trade_name`; os dois cadastros são independentes).

#### Empresa pagadora LE BLANC (`sk_company = 4`, 2026-09-11)

`company` 4 = **LE BLANC ADMINISTRACAO DE BENS PROPRIOS LTDA**, CNPJ `20584679000110` — raiz
**diferente** do grupo OTIMOTEX (`47273917`). Decisões do usuário: qualquer menção marca a conta
como da LE BLANC, **inclusive quando ela é a FORNECEDORA**; ela **vence a ester**; vale para todo
e-mail da caixa (não só o remetente financeiro@).

- **Grafias:** `_LE_BLANC_RE` casa `le` + separador opcional (espaço, `_`, `.`, `-`) + `blanc` sobre o
  texto sem acento. Casos reais: assunto "ENC: Le Blanc - Boleto Porto Saúde", "Certificado Digital
  LE BLANC HOLDING S A", anexos `BOLETO_LEBLANC_-_POR.pdf` e `NOTA_FISCAL_LE_BLANC.pdf`. Fronteira
  por lookaround (não `\b`, que trata `_` como letra) e, à direita, impede "LEBLANCO".
- **CNPJ:** `payer_cnpj`/`supplier_cnpj` de 14 dígitos com a raiz `20584679`. CNPJ com dígito
  deslocado por OCR (conta 759: `02058467900011`) **não** casa de propósito — ali quem classifica é o
  assunto.
- **Fornecedor:** `_finalize_supplier` remove `supplier_name`/`supplier_cnpj` do payload; por isso
  os dois call sites capturam `_le_blanc_supplier_signal(payload)` **antes** dele e repassam
  `le_blanc=` a `apply_sk_company`. Validado por mutante: sem a captura, a fornecedora lida só pelo
  Vision volta a cair em 1.
- **Fornecedora continua possível:** a exclusão "a pagadora nunca é fornecedor" usa só o CNPJ do
  sk 1 (`company_cnpj()`), então a LE BLANC resolve como fornecedor normalmente. `company_cnpjs()`
  passa a oferecer o CNPJ dela nas senhas candidatas de boleto cifrado.
- **Backfill:** migration 135 (4 contas: 348, 759, 1350, 1351).
- **Riscos aceitos:** assinatura de grupo citando "Le Blanc" marcaria tudo como 4 (mesmo modo de
  falha da conta 167 com "Le Bianco"); nome de cor/linha de tecido "LE BLANC" num anexo marcaria a
  conta; aluguel emitido pela LE BLANC contra a OTIMOTEX nasce com pagadora 4 (correção manual pela
  tela — o trigger da 084 preserva a curadoria); a flag é por MENSAGEM, como a da LEBIANCO.

Backfill
único aplicado em 2026-07-03 (ids 331/333/334/373/374 → OTIMOTEX; fornecedores-lixo 1243/1247/1248
**hard-deletados** a pedido do usuário — eram fictícios, sem CNPJ/curadoria, e sem contas após o
remapeamento; exceção pontual à regra de soft delete de `supplier`).

O pipeline resolve o fornecedor **antes do INSERT** via RPC `resolve_supplier_for_account`
(`migration 040`; `_finalize_supplier` → `SupabaseControl.resolve_supplier`), que chama
`resolve_supplier_id(cnpj, cpf, name, email)` + `_add_supplier_email`. Ordem de busca
(**migration 054**): **CNPJ → CPF → nome normalizado → e-mail exato (não interno) → auto-insert**
em `supplier`. **O e-mail é um FALLBACK após o nome falhar** (não exige ausência de nome): se o nome
extraído não casa nenhum cadastro, a busca por `email`/`email2`/`email3`/`email4` ainda roda — o
nome mantém precedência (só quando ele falha o e-mail entra). Histórico: 027/028 punham o e-mail
ANTES do nome (colapsava fornecedores por remetente interno); a **046** corrigiu pondo o e-mail
depois, mas restringiu demais (**só "na ausência total de nome"**) — então, quando o corpo sem nome
confiável usava o próprio e-mail como "nome" (`v_has_name=true`), a busca por e-mail era PULADA e o
pipeline criava fornecedor DUPLICADO (ex.: `financeiro@smartwebservices.com.br`, já no email2 do
fornecedor 1213, virou um fornecedor novo). A **054** restaura a intenção documentada ("a RPC casa
por e-mail, passo email/2/3/4") sem reabrir o problema da 046, porque o **bloqueio de e-mail interno
é mantido** (`_is_internal_email`). **REGRA ROBUSTA do lado Python (não regredir):** em
`extract_from_email_body`, sem nome confiável (sinais/mapa por remetente falham) o nome fica **VAZIO**
— o e-mail NUNCA vira nome ANTES da busca; é passado à RPC como chave própria e **só vira nome no
auto-insert (último recurso) quando NÃO é encontrado** em nenhum fornecedor. Isso impede recriar o
"shadow supplier" nomeado pelo e-mail (que venceria o Passo 3). **Domínios internos não viram
fornecedor** (`migration 046`): `_is_internal_email` — **função SQL da RPC (migration 046),
NÃO uma função Python** — (`%@otimotex.com.br`/`%@lebianco.com.br`)
bloqueia esses e-mails tanto no `_add_supplier_email` quanto no Passo 4 e no auto-insert do
`resolve_supplier_id` (todos SQL). O lado Python não tem esse helper; o bloqueio de remetente
interno é imposto no banco pela RPC. A precedência **anexo → corpo**
do nome é garantida antes, no pipeline Python (o corpo só alimenta o resolver quando o anexo não
gera conta). **`normalize_search(txt)` = `lower(unaccent(txt))` é `IMMUTABLE PARALLEL SAFE STRICT`
e `SECURITY INVOKER`** (conferido no catálogo em 2026-07-28: `prosecdef = false` — este texto
dizia "SECURITY DEFINER", o que estava **errado**; sem impacto prático, já que a função não lê
tabela alguma, mas induzia a erro em análise de RLS). Ser `IMMUTABLE` é o que permite os
**índices funcionais** que existem sobre ela em `supplier`: `idx_supplier_trade_name_trgm` e
`idx_supplier_legal_name_trgm` (GIN `gin_trgm_ops`) mais as versões btree normalizadas. **Toda
busca por nome de fornecedor deve chamar `normalize_search(coluna)`** — escrever
`unaccent(lower(coluna))` inline devolve o mesmo resultado e **não usa o índice**, porque o
planner só casa expressão idêntica. `financial_account_control`
referencia o fornecedor **apenas pela FK `sk_supplier`** (surrogate key snowflake, NOT NULL —
`migration 042`): a RPC e as funções de resolução retornam/keyam `sk_supplier`; `supplier_id`
virou **chave de negócio** e ficou só na tabela `supplier` (NOT NULL UNIQUE, igualada ao `sk`
nos fornecedores criados pela extração via trigger de espelho, podendo divergir em cargas
externas). As antigas colunas denormalizadas `supplier_name`/`supplier_cnpj`/`supplier_cpf` e
o trigger `trg_fe_supplier_id` foram **removidos** (`migration 041`); nome/CNPJ vêm do JOIN com
`supplier`. A extração (`extract_pdf.py`/corpo) ainda **produz** nome/CNPJ — são a **entrada**
do resolver, descartados por `_finalize_supplier` depois de obter o `sk_supplier`.

- **Reconhecimento por e-mail** (`027`): na falta de CNPJ/CPF, o **remetente** (`sender_email`)
  é a chave — regra de negócio: o e-mail é estável por fornecedor e raramente um fornecedor
  tem o e-mail como `trade_name`/`legal_name`. Por isso, ao casar, um nome cadastrado em
  formato de e-mail é **promovido** ao nome real quando este chega (`_enrich_supplier_name`).
  Match por **e-mail exato** (case-insensitive) — seguro até em domínios públicos; match por
  **domínio** foi deliberadamente evitado (risco com `gmail.com`/`hotmail.com`).
- **Múltiplos e-mails** (`028`): `supplier` tem `email`, `email2`, `email3`, `email4` e o
  match considera os quatro. O trigger **acrescenta** o remetente no primeiro campo vazio
  (`_add_supplier_email`) em vez de sobrescrever `email` — sem duplicar (dedup case-insensitive);
  com os 4 cheios, o excedente é ignorado. A extração grava
  `financial_account_control.sender_email` (de `email_control.sender_email`) e o trigger o
  propaga ao resolver/criar o fornecedor.

### Caminho `email_body`

Acionado em `process_message()` **só quando o anexo NÃO respondeu por nenhum pagável**
— assim o corpo nunca conflita com um arquivo anexado válido ("o boleto sempre vence o
corpo"). O gate usa a flag **`attachment_account`** (4º retorno de
`extract_and_store_accounts`), que é True quando um PDF anexado gerou uma conta pagável
**criada como nova OU casada/atualizada por DEDUP** contra uma conta já existente (mesmo
documento chegado por outro e-mail). **Não regredir para `accounts_saved == 0`** (só
contas NOVAS): um boleto **deduplicado** tem `accounts_saved==0` e, com o gate antigo, o
corpo criava uma conta ESPÚRIA com dados divergentes — falha real id 510 (OBER,
R$ 5.576,66): o boleto anexado deduplicou contra o id 159 (venc. 18/07 pelo fator do
código de barras), mas o corpo gravou uma 2ª conta com venc. 11/07 (lido do texto, sem
barcode), que ainda foi auto-baixada para `pago` por causa da data errada. Cobertura:
`tests/test_boleto_dedup_suppresses_body.py`. **Limpeza retroativa** (2026-07-13): hard
delete do id 510 (id 159 preservado). ✅ **FECHADO** — este bloco descrevia como "caso ainda
ABERTO" o id 7 (corpo, ESPRO R$ 304) duplicando o id 176 (boleto), não deduplicado por o tipo
divergir (`outro`×`boleto`). Os dois lados foram resolvidos **no mesmo dia** e o texto não
acompanhou: a **impressão 3 deixou de exigir `document_type` igual** (ver "Impressão 3 casa por
`sk_supplier`+valor+vencimento, INDEPENDENTE do `document_type`") e o **id 7 foi hard-deletado**
na limpeza daquela regra. Conferido em 2026-08-04: a conta 7 **não existe**.

**MÚLTIPLAS PARCELAS no corpo → UMA conta por boleto (NUNCA somar — não regredir):**
quando o corpo lista uma TABELA de boletos (documento, parcela, emissão, vencimento,
valor, dias — caso OBER, em que o webmail quebra cada campo em uma linha `\r`),
`_extract_body_installments()` (regex `_BODY_INSTALLMENTS_RE`) detecta as linhas e o
`try_extract_from_body` cria **uma conta por boleto** (clona o payload com o fornecedor já
resolvido e sobrescreve nº=`{doc}/{parcela}`, valor, vencimento e emissão por linha; dedup
por linha). A linha **"Total"** nunca vira conta (não casa o padrão doc+parcela+2datas+valor).
Dispara só com **≥2 linhas** e vencimentos OU (doc,parcela) distintos; caso contrário cai no
caminho de conta única, em que `_extract_body_amount` mantém a soma para pagamento único com
componentes (ex.: "Valor R$ 297,08 + R$ 6,96 / Total R$ 304,04"). **Neste layout** as contas
saem **sem código de barras** (a tabela da OBER não traz a linha digitável — ela só está no
PDF), por isso o caminho de PDF (quando legível) é preferível; **não** vale como regra geral
do corpo: quando o próprio corpo escreve a linha digitável, ela é capturada — ver "TABELA DE
FATURAS achatada" logo abaixo. Teste: `tests/test_body_installments.py`.

**TABELA DE FATURAS achatada: rótulo no CABEÇALHO, valor lá embaixo (conta 693 — não
regredir):** quando o corpo é uma tabela HTML **achatada pelo webmail em uma linha por
CAMPO**, todos os rótulos ficam no cabeçalho e os valores vêm depois — então **nenhum**
regex ancorado em rótulo alcança o dado, porque todos limitam a distância rótulo→valor
(`_BODY_ISSUE_RE` `\D{0,10}`, `_BODY_DUE_RE` `\D{0,15}`, `_BODY_BARCODE_RE` `\D{0,10}`) e
entre eles ainda há **dígitos** (as outras colunas), que `\D` não cruza. Efeito na conta
**693** (MOVVI): nº do documento virou o **sintético** `fatura_250726`, e emissão **e
vencimento** caíram no fallback pela **data do e-mail** (25/07) em vez de 22/07 e **01/08**
— vencimento errado alimenta a marcação de vencido/baixa automática. Correção em três
frentes independentes:

- **A FORMA da linha identifica o pagável, sem rótulo** — `_BODY_INVOICE_ROW_RE` +
  `_extract_body_invoice_rows` casam `documento + emissão + vencimento + R$ valor`
  contíguos (só espaço entre os campos). Consumido como **preenchimento de LACUNA**
  (`_row_field` em cadeia `rotulado or tabela or padrão`): rótulo explícito **continua
  vencendo** a tabela. Guardas contra falso positivo: documento em início de campo
  (`(?<![\w/-])`), com ao menos um **dígito** e **≥4 caracteres** — este último é o que
  impede casar a coluna **`Parcela` ("001")** do layout de 6 campos da OBER, que pertence
  ao `_extract_body_installments` (e mantém a precedência dele).
- **Linha digitável sem rótulo adjacente** — `_extract_body_linha_digitavel` reusa o
  extrator determinístico canônico `extract_pdf.extract_linha_digitavel` (fonte única, 5
  campos FEBRABAN) como fallback do `_BODY_BARCODE_RE`. Com o barcode, o corpo passa a ter
  **dedup por código de barras** (impressão 1) e **vencimento autoritativo pelo fator**
  (`_apply_barcode_due_date`). Foi exatamente o que faltou nas contas **397/401**: mesma
  linha digitável, mas sem barcode a dedup não casou e a 401 nasceu **duplicada** (cancelada
  à mão depois).
  `extract_pdf.extract_linha_digitavel` ganhou o **padrão 5** — separador **entre** os
  campos também por PONTO (`23793.39100.90000.004375.…`), forma usada em e-mail HTML; os
  padrões 1-4 exigem `\s+` entre campos. É o **último** da ordem: não altera nenhuma entrada
  que já casava.
- **N faturas na tabela → uma conta por fatura** (`_extract_body_invoice_table`, mesmo gate
  ≥2/distintas e o mesmo laço do de parcelas — NUNCA somar). Cada linha leva o barcode do
  **seu próprio segmento** (do início dela até o início da próxima): herdar o da primeira
  faria as demais colidirem na dedup por código de barras e sumirem em silêncio. A
  **última** linha não tem "próxima" que a delimite, então o segmento dela é limitado por
  **`_INVOICE_ROW_BARCODE_WINDOW`** (500 chars — a distância real documento→linha
  digitável na tabela é ~120): sem o teto ela varreria até o fim do corpo e poderia
  adotar um boleto do rodapé, que não é dela.

**Verificação (A/B contra dados reais, não só fixtures):** os 139 corpos já gravados foram
reprocessados com o código de HEAD e com o novo, mesmos insumos — **8 mudam, todos na direção
da correção** (nº sintético→real, data do e-mail→data impressa, barcode recuperado) e **131
ficam idênticos**; **0** corpo histórico passa a disparar o caminho multi-fatura. Além da 693
(MOVVI), a mudança corrige a mesma classe no remetente **ITW/PPF** ("Aviso de vencimento",
tabela `Título / Emissão / Vencimento / Valor`: ids 3/181/261/451/524), cujo vencimento
gravado era sistematicamente o do **envio do e-mail** (~1 dia antes do real). Essas 7 contas
estão **fechadas** (pago/cancelado) e **não** foram reescritas — só a 693 foi corrigida.
Testes: `tests/test_body_invoice_table.py`.

> **Boletos protegidos por senha + carnê (OBER `info.ober.com.br`) — RESOLVIDO no PDF:** o boleto
> é um PDF **criptografado** (senha derivada do CNPJ do pagador) e um **carnê de N
> boletos** (N páginas). `extract_pdf.process_pdf` (orquestrador) trata os dois: (1) PDF cifrado
> (`_pdf_is_encrypted`) → tenta as senhas candidatas `[:3]→[:4]→[:5]→[:6]→**CNPJ completo**
> (`_decrypt_pdf`; candidatos gerados por
> `read_emails.pdf_password_candidates(_payer_cnpjs(ctrl))` e threaded por
> `run_extraction`→`extract_to_csv(pdf_passwords=...)`), gravando uma cópia descriptografada
> temporária; (2) **multi-pagável** (`_payable_pages` acha ≥2 páginas com instrumento de pagamento
> — ver "Split multi-pagável por INSTRUMENTO DE PAGAMENTO") → divide em
> 1 PDF por página (`_write_single_page`) e roda `_extract_records` em cada um → **1 registro por
> pagável** (com a linha digitável/PIX de cada). `process_pdf` agora devolve **lista** de registros;
> `extract_to_csv` itera, e o loop de `extract_and_store_accounts` (que já cria 1 conta por linha
> do CSV) gera as contas individuais com código de barras. **Esgotadas as senhas** → registro de
> falha → fallback do corpo (que também cria parcelas individuais, porém sem barcode). **Por que
> bundled:** decrypt SEM a emissão por boleto regrediria (o carnê viraria 1 conta somada e o corpo
> não rodaria). Requer `pypdf` (em `server/requirements.txt`). Testes:
> `tests/test_pdf_decrypt.py` (decrypt + candidatos) e a validação dos helpers contra o PDF real
> (`_payable_pages`/`_write_single_page`). **Importante (produção):** copiar `extract_pdf.py` **e**
> `read_emails.py` juntos e instalar `pypdf` na máquina do scheduler — ver "Deploy manual".

> **Senha = CNPJ COMPLETO e prefixo de 3 (2026-08-18):** o e-mail "PAGAMENTO BOLETO CABERNET
> 0108-1408" (`email_control` 1563, erro 314) caiu em `extracao_falhou` porque a senha do anexo é o
> **CNPJ inteiro, sem pontuação** — fora dos prefixos 4/5/6 então cobertos. O mesmo remetente já
> tinha perdido o e-mail "0107-1507" (erro 257) pelo mesmo motivo. A regra virou
> `PDF_PASSWORD_CNPJ_LENGTHS = (3, 4, 5, 6, None)` (`None` = número completo), constante nomeada
> porque é regra de **negócio**, e o mínimo de dígitos exigido da fonte é **derivado** dela
> (`PDF_PASSWORD_MIN_DIGITS`) — prefixo novo maior sem ajuste do mínimo geraria senha truncada em
> silêncio.
>
> 🔴 **As candidatas saem do CNPJ de TODAS as empresas pagadoras** (`ctrl.company_cnpjs()`), não só
> da `sk_company=1`: as três filiais compartilham a raiz `47273917` — logo os **prefixos coincidem**
> —, mas o **CNPJ completo difere** (`...0001-23`/`...0002-23`/`...0003-23`), e a empresa pagadora
> do e-mail só é resolvida no **Passo 2**, depois da extração. Tentar uma senha errada custa uma
> chamada local ao `pypdf`; omitir a filial certa perde o boleto **em silêncio**. A ordem das
> candidatas só afeta o número de tentativas: a comparação do `pypdf` é exata, então um prefixo
> curto nunca abre um PDF cifrado com senha mais longa. Duplicatas são removidas preservando a
> ordem. `_payer_cnpjs(ctrl)` degrada para `company_cnpj()` e para lista vazia — controle sem CNPJ
> não vira erro, só não tenta descriptografar.

> **Boleto cifrado só com senha de DONO (usuário vazia) — RESOLVIDO via pdfplumber (não regredir):**
> caso distinto do OBER (SB Crédito / HYOSUNG via "SB CREDITO SECURITIZADORA"): o PDF é cifrado
> **só com senha de DONO** (senha de USUÁRIO vazia + flags de restrição). O `pypdf` marca
> `is_encrypted=True` e `reader.decrypt('')` devolve `0` (**não** decifra), mas o **pdfplumber/
> pdfminer lê o arquivo transparentemente**. Antes, o gate de decrypt descartava esses boletos
> legíveis como "protegido por senha" → `extracao_falhou` em massa (14 boletos SB Crédito presos).
> Correção em `process_pdf`: quando `_pdf_is_encrypted` E `_decrypt_pdf` retorna `None` **MAS**
> `_pdf_text_readable(pdf_path)` (o pdfplumber lê texto) → segue com o PDF **ORIGINAL**
> (`work = pdf_path`) em vez de emitir `_failure_record`. Só quando o pdfplumber TAMBÉM não lê
> (senha real desconhecida — ex.: boleto dos **Correios** "Sua Fatura Correios Empresas") é que
> vira falha/manual. Testes: `tests/test_pdf_decrypt.py` (`TestEncryptedOwnerOnlyFallback`).
> **Deploy:** só `extract_pdf.py` (sem dependência nova).
`extract_from_email_body()` faz parsing por regex. **Fornecedor** (`_BODY_NAME_RE`): rótulo no
início da linha — `fornecedor`/`responsável`/`prestador`/`nome` (+ `favorecido`/`beneficiário`/
`cedente`/`razão social`/`empresa`). O **separador `:`/`-` é OPCIONAL** (aceita só espaço,
ex.: "Nome MATEUS JAE WON AHN"); para não capturar continuação de frase ("Responsável **pela**
compra"), o valor **deve começar por maiúscula/dígito** (char class `[A-ZÀ-Þ0-9]` case-sensitive;
só o rótulo é case-insensitive via `(?i:...)`), e `\b` evita casar prefixo ("Nomeação"). **Cuidado
CRLF:** o fim da linha é `[ \t\r]*$` — o `\r` do `\r\n` bloqueia o `$` se esquecido (bug já
corrigido; teste usa `\r\n`). **Fallback do rótulo — bloco "Dados do emissor"**
(`_BODY_ISSUER_RE`: `dados do emissor|beneficiário|cedente|sacador`): o `_BODY_NAME_RE` exige o
valor na **MESMA linha**, e nas notificações de plataforma o nome vem na **linha SEGUINTE** ao
rótulo (HTML achatado) — sem ele o fornecedor caía no assunto e virava lixo (conta 694). Mesma
exigência de maiúscula/dígito; `emitido por` **não** é rótulo aceito (no rodapé nomeia a
plataforma). **Sem rótulo nem documento**, tenta sinais (`_supplier_from_signals`):
assinatura titulada (`Prof./Dr. <Nome>`) e destinatário do pagamento (`pix/pagar p/|para <Nome>`,
com stopwords cortando a captura). Depois o **mapa por remetente** (`_supplier_from_sender`/
`_SENDER_SUPPLIER_MAP`: `correios.com.br` → `Correios`) e só então cai para `sender_email`.
**Valor** (`_extract_body_amount`): (1) "Total"/"Valor Total" com `R$` tem precedência; (2) valores
`R$` somados; (3) **fallback sem `R$`** (`_BODY_LABELED_AMT_RE`) — número rotulado por `Valor`/`Total`
no formato BR com **2 a 3 casas** (`Valor 50,00`, `Total 1.250,00`), usado só quando não há
nenhum `R$` (exige rótulo + centavos p/ não pegar número solto/`NF 1087`; "Total" tem precedência
sobre "Valor"). **3ª casa decimal (não regredir):** aceita `,\d{2,3}` — o 3º dígito é digitação com
zero a mais (ex.: `VALOR: 1.799,960` → R$ 1.799,96, id 186, nota interna NIKE); `_brl_to_decimal`
trata vírgula como decimal BR com `,\d{1,3}$` e `round(…,2)` normaliza (sem o fix, caía no ramo de
milhar e virava R$ 1,80). O fallback tolera **um conectivo curto colado ao número** (`de`/`da`/`do`)
entre o rótulo e o valor — `o valor de 172,39` casa; um substantivo no meio (`Total da nota 1.250,00`,
sem `R$`) **não** casa (conservador, evita falso positivo; o caminho com `R$` cobre o caso comum).
`payment_method='pix'` se o termo aparecer (ou sempre, p/ honorários). **Vencimento** (`_BODY_DUE_RE`
= "vencimento"): fallback **`_BODY_PAYDATE_RE`** reconhece `DATA (PARA/DE) PAGAMENTO: DD/MM/AA` (rótulo
das notas internas, id 186) antes de cair na data de emissão/extração. **Valida
fornecedor+valor**: sem valor → não grava conta (vira `falha`). `email_body_excerpt` (migration 016)
guarda o corpo completo. Testes: `tests/test_body_amount.py`. O **barcode do corpo** é
resolvido por `_resolve_body_barcode`, em que a **FORMA vence o RÓTULO**: primeiro
`_extract_body_linha_digitavel` (estrutura dos 5 campos FEBRABAN validada) e só então
`_BODY_BARCODE_RE`, que fica como fallback por cobrir a arrecadação de 48 dígitos — ver
"Barcode do corpo: a FORMA vence o RÓTULO". A normalização é `_normalize_body_barcode`,
que reusa `extract_pdf.normalize_barcode` (import lazy) — mesma regra canônica do caminho
de PDF (44/48 dígitos mantidos, 47 → 44, outros → None), em vez de um `re.sub` solto que
aceitava qualquer sequência de 44-48 (F2). As guardas contra código **inventado** (o
invariante do `8` na arrecadação e o DV do boleto) vivem na função **canônica** do
`extract_pdf`, não aqui — ver "As guardas do barcode vivem na função CANÔNICA".

**Corpo SÓ-HTML** (ex.: Correios — `noreply_componentes@correios.com.br`, assunto "Pagamento
Boleto Fatura"): quando o anexo não vem e o link é portal HTML sem PDF, `get_body_text()`
volta vazio. `process_message` então usa `_html_to_text(get_body_html(msg))` (remove tags,
desescapa, colapsa espaços) para alimentar a extração — recupera "Fatura nº: 3918439"
(→ `invoice_number`), "Valor da fatura R$ 1.530,47" (→ `amount`) e classifica
`document_type='fatura'` (keyword `fatura` em `_BODY_DOC_KEYWORDS`, fallback antes de
`outro`). Prioridade segue **anexo → link → corpo**. O status da conta do corpo é **sempre
`pendente`** — a baixa/atualização é feita pelo usuário, mesmo quando o corpo diz "pagamento
realizado com sucesso". Dedup do corpo (`find_financial_duplicate`) evita duplicar conta já
registrada. Testes: `tests/test_body_html_extraction.py`.

**Corpo PLACEHOLDER — "não-vazio" não quer dizer "tem conteúdo" (2026-08-03, não regredir):**
o fallback acima só disparava com `if not body_text` (corpo VAZIO). A plataforma **SSW** manda um
`text/plain` de **55 caracteres** — *"O conteúdo deste e-mail está somente disponível em HTML"* —
que é não-vazio, então o HTML nunca era lido. Consequências medidas: **29 e-mails** gravaram o
aviso como se fosse o corpo, a guarda do cedente (`_ssw_cedente_from_body`) **nunca teve texto
para ler** — foi por isso que a correção de julho para faturas SSW não impediu a reincidência — e
o `body_full` da Onda 2 ficou inútil para a tool `buscar_emails`. Fix: `_plain_body_is_placeholder`
+ `if not body_text or _plain_body_is_placeholder(body_text)`, e só substitui quando o HTML rende
algo (HTML ilegível não pode apagar o pouco que havia). **Deliberadamente conservador** — exige o
padrão do aviso **E** texto curto (`_PLACEHOLDER_BODY_MAX_CHARS = 200`): corpo curto legítimo é a
NORMA aqui (`"FORNECEDOR X / VALOR / VENCIMENTO"` cabe em 90 chars, e há dezenas na base), logo um
critério por tamanho descartaria justamente o texto de onde saem fornecedor, valor e vencimento.
Testes: `tests/test_placeholder_body.py` — inclui **guarda `ast`** de que `process_message` de fato
consulta o detector: a primeira versão do teste reimplementava a regra e passava no mutante.

**Fallbacks de campo (corpo E PDF — `build_financial_payload`):** `issue_date` vazio →
data do e-mail (`received_at`); `due_date` vazio → `issue_date` → hoje; `invoice_number`
vazio → `"{document_type}_{ddmmyy(vencimento|emissão)}"`. Um **identificador de fornecedor**
extraído (nome **ou** CNPJ **ou** CPF) e `amount` são obrigatórios para gerar conta — o nome/CNPJ
extraído alimenta a resolução do `sk_supplier` (`_finalize_supplier`) e depois é descartado;
não vira coluna em `financial_account_control`.

### Registrar TODOS os e-mails + filtro de assunto (`KEYWORDS_DEFAULT`)

`run_reader()` registra **todos** os e-mails da caixa em `email_control` — `/emails`
espelha o webmail inteiro (o app substitui abrir a caixa). O filtro de keyword decide
**o que extrair**, não o que registrar:

- **Dedup primeiro** (`message_id` em `known_ids`) → pula.
- **Sem keyword** no assunto → `ctrl.register({... status:'ignorado'})` sem baixar/
  extrair (`has_attachment` fica NULL). Respeita `--dry-run` (não grava).
- **Com keyword** → `process_message` (baixa + extrai) define o status via `status_for_result`,
  por **prioridade**: conta do PDF (`accounts_saved>0`) → `extraído` · **NF-e pura sem conta**
  (`subject_is_pure_nfe`) → `ignorado` · CSV do PDF → `extraído` · conta do corpo → `recebido`
  (**vale mesmo com anexo** cujo PDF não gerou CSV — antes virava um falso `pendente`) ·
  **duplicidade** (pagável do corpo duplica conta já registrada) → `duplicidade` · anexo
  salvo sem conta → `pendente` · **notificação sem anexo/conta** (`subject_is_ignorable_notification`)
  → `ignorado` · nada → `falha`. Ver migrations 022/031 e `tests/test_status_for_result.py`.
- **Regra de DUPLICIDADE** (`try_extract_from_body` → `BODY_CREATED`/`BODY_DUPLICATE`/`BODY_NONE`):
  quando o pagável extraído do corpo casa uma conta já existente (`find_financial_duplicate`),
  a conta **não** é recriada e o e-mail vira `duplicidade` (status próprio, migration 031) — não
  `falha`. Cobre a thread original + seu `RES:`/encaminhamento. `email_rec['duplicate_of']` guarda
  o id da conta; `notes` registra "Duplicata — conta já registrada (id N)". Vale no pipeline e no
  `scripts/reprocess_body_emails.py`. Testes: `tests/test_body_duplicate.py`.
- **Corpo é fallback só quando o anexo NÃO respondeu por nenhum pagável**
  (`attachment_account==False` — conta nova **ou** boleto deduplicado; ver "Caminho
  `email_body`") — havendo conta de arquivo anexado válido (mesmo deduplicada), o corpo é
  ignorado (sem conflito).

**Matching de keyword (`match_keyword`, `tests/test_match_keyword.py`)** — comparação
**sem acento** (NFD + lowercase). Dois modos:
- **Acrônimos de tributo/câmbio** (`WORD_KEYWORDS`: `dar, darf, das, dae, dare, dam, duam, gps,
  gru, gnre, gare, ipva, iptu, iss, itbi, cambio`) casam por **palavra inteira** (`\b…\b`) —
  evita falso positivo de substring (`das` em "ca**das**tro"/"executa**das**", `iss` em
  "em**iss**ão", `gru` em "**gru**po", `cambio` em "inter**câmbio**").
- **Demais termos** (frases e siglas distintivas: `boleto, nota fiscal, nf-e, conhecimento
  de transporte, dacte`…) seguem **substring**.
- **Câmbio**: lê `cambio` **ou** `câmbio` (sem acento), mas a keyword gravada/retornada é
  sempre `câmbio` (forma gramatical correta na lista).

**Remetente de SISTEMA → `ignorado`** (`is_ignored_sender`, `IGNORED_SENDER_LOCALPARTS`,
`tests/test_match_keyword.py`): e-mails cujo **local-part** do remetente está na lista
(hoje `postmaster`) — NDR/bounce/aviso de servidor (ex.: "Undeliverable: …") — viram
`ignorado` **sem baixar nem extrair**, e o filtro roda **antes** do match de keyword (no loop
de `run_reader`), então vale **mesmo que o assunto case uma palavra-chave**. Motivo: um aviso
de não-entrega frequentemente cita o corpo da cobrança original (com valor), e sem esse filtro
o pipeline criava uma conta a pagar **falsa** a partir do bounce. Match por local-part
(case-insensitive, qualquer domínio); a lista é um `set` extensível. O registro `ignorado` é
compartilhado com o filtro de assunto via `_register_ignored`.

**Confirmação de pagamento → `ignorado` (não é conta a pagar — não regredir):** e-mail cujo
ASSUNTO indica que o **pagamento JÁ foi realizado** (`subject_is_payment_confirmation`,
`_PAYMENT_CONFIRMATION_RE`) vira `ignorado` **sem baixar nem extrair**, e o filtro roda **antes**
do match de keyword no loop de `run_reader` (logo após `is_ignored_sender`), então vale **mesmo
que o assunto case uma palavra-chave** (ex.: "Pagamento confirmado - boleto 123"). Motivo: uma
confirmação/comprovante de pagamento é um **recibo**, não uma cobrança — antes o pipeline criava
uma conta a pagar **falsa** (ex.: "Confirmação de Pagamento da fatura 18292"). O regex (assunto
sem acento) casa `confirmação de/do pagamento`, `comprovante de pagamento/pix/transferência/
depósito`, `confirmado (o) pagamento` e `pagamento (foi/já) confirmado/processado/efetuado/
realizado/aprovado/recebido` — o **particípio no passado** evita casar "REALIZAR pagamento" /
"pagamento A realizar" (que SÃO conta a pagar). Distinto de `NOTIFICATION_PHRASE_TERMS` (que só
vira `ignorado` na ausência de anexo/conta): aqui o e-mail é ignorado **antes** de processar,
sempre. Teste: `tests/test_match_keyword.py` (`PaymentConfirmationTest`). Limpeza retroativa
(2026-07-06): **hard delete de 10 contas** de confirmação de pagamento em `financial_account_control`
(8 "Confirmação de Pagamento da fatura" + 2 "pagamento foi aprovado") + os `email_control`
correspondentes → `ignorado`.

**Extensão "Recebemos o seu pagamento" (2026-07-28, pedido do usuário — não regredir):** o
regex ganhou a alternativa `recebemos (o|a)? (seu|sua)? pagamento`, que cobre o aviso do
**credor** de que recebeu ("Recebemos o seu pagamento" / "Recebemos pagamento" / "Recebemos o
pagamento da fatura X"). "Pagamento recebido" já casava pela alternativa do particípio.
**GUARDA OBRIGATÓRIA — a forma NEGADA inverte o sentido** (`_PAYMENT_NOT_RECEIVED_RE`:
`não recebemos|não identificamos|não consta`): *"(ainda) **NÃO** recebemos o seu pagamento"* é
uma **COBRANÇA** de título em aberto, e `subject_is_payment_confirmation` devolve **False**
para ela, deixando o e-mail seguir a extração normal. Sem essa guarda o pagável seria perdido
em silêncio — viés deliberado: **na dúvida NÃO ignorar** (uma conta a revisar é melhor que um
pagável perdido). A extensão vale automaticamente para
`body_forwards_payment_confirmation` (confirmação encaminhada com assunto reescrito), que
reusa a mesma função. Limpeza retroativa (2026-07-28): **hard delete da conta 716**
("Leadster | Recebemos o seu pagamento", R$ 362,62, criada pelo corpo) + `email_control` 1115
→ `ignorado`. Auditoria: era o **único** caso na base — todos os demais e-mails que casam a
regra já estavam `ignorado`.

**Assunto com "lembrete" → `ignorado` (não é conta a pagar — não regredir):** e-mail cujo ASSUNTO
contém a palavra **`lembrete`** (substring, sem acento — `subject_is_reminder`) vira `ignorado`
**sem baixar nem extrair**, no mesmo ponto do loop de `run_reader` (logo após
`subject_is_payment_confirmation`), então vale **mesmo com keyword/anexo** (ex.: "Lembrete de
Pagamento: vencimento 10/06/2026", de `boleto@smartwebservices.com.br`). Decisão do usuário: **o foco
é a palavra `lembrete`** — um lembrete/aviso não é a cobrança em si; `vencimento` sozinho **NÃO**
basta (pode ser um boleto real, ex.: "Boleto vencimento 10/07"). Distinto de
`NOTIFICATION_PHRASE_TERMS` (nível FRACO — só `ignorado` sem anexo/conta) e de
`subject_is_payment_confirmation` (pagamento JÁ feito). Substring pega o plural `lembretes`. Teste:
`tests/test_match_keyword.py` (`ReminderSubjectTest`). Limpeza retroativa (2026-07-07): **hard
delete de 5 contas** com "lembrete" no assunto (4 "Lembrete de Pagamento: vencimento" de
`boleto@smartwebservices` + 1 "ENC: Lembrete Sua Fatura") + os `email_control` correspondentes → `ignorado`.

**Confirmação de pagamento ENCAMINHADA com assunto REESCRITO → `ignorado` no caminho do CORPO (não
regredir; revisado em 2026-07-23 — ver correção abaixo):** a guarda `subject_is_payment_confirmation`
roda no `run_reader` **só sobre o assunto RECEBIDO**. Quando um usuário interno **reencaminha** uma
confirmação de pagamento e **reescreve o assunto visível** (ex.: "pagamento Sua Fatura"), a
confirmação original some do assunto e sobrevive apenas no CORPO, na linha `Assunto: <original>` do
bloco encaminhado — o e-mail passa no filtro de keyword (tem "fatura") e, **sem anexo**, a extração
do corpo gera uma conta indevida (um comprovante de algo já pago nunca é um pagável, com ou sem
duplicata). Correção (`read_emails.py`): `forwarded_subjects_from_body(body)` extrai as linhas
`Assunto:`/`Subject:` (com marcador de citação `>` opcional; captura até o fim da linha **sem** o
âncora `$`, pois o `\r` do CRLF ficaria fora dele no modo MULTILINE) e
`body_forwards_payment_confirmation(body)` reavalia cada assunto original contra
`subject_is_payment_confirmation`. O guard fica no **topo de `try_extract_from_body`** → retorna
`BODY_IGNORED` (não gera conta), **antes** de tocar o `ctrl`. **Escopo mínimo/robusto:** (a) só no
caminho do CORPO (fallback sem anexo pagável) — um **boleto real anexado** a uma confirmação
encaminhada segue pelo caminho de PDF e é pago; (b) só o gate forte de confirmação, **não** as
`NOTIFICATION_PHRASE_TERMS` (fracas) nem "fatura"/"nota fiscal" encaminhadas normais, que **não** são
bloqueadas. Testes: `tests/test_forwarded_reminder.py`.

> **CORREÇÃO 2026-07-23 (caso id 668/e-mail 1004) — 'lembrete' encaminhado deixou de ser bloqueado
> incondicionalmente:** o guard original (2026-07-22) bloqueava **também** `subject_is_reminder`
> (a mesma função de `Assunto com "lembrete" → ignorado` acima, reavaliada sobre o assunto
> ORIGINAL encaminhado) — inclusive quando o lembrete era a **ÚNICA** fonte de uma fatura ainda não
> registrada em nenhum outro canal. Falha real: e-mail 1004 (conta **668**, R$ 2.950,00, vencimento
> em 2 dias, `email_body`, fatura Contabil Esquema Nº 20879) foi descartado silenciosamente pelo
> guard mesmo **sem** existir nenhuma conta correspondente — a fatura teria sido perdida se não
> notada manualmente. `subject_is_reminder` foi **removido** de `body_forwards_payment_confirmation`
> (renomeada de `body_forwards_ignorable_subject`): lembrete encaminhado agora segue a **extração
> normal do corpo**; reenvios PERIÓDICOS do MESMO lembrete (mesmo fornecedor+documento/valor+
> vencimento) são suprimidos pela **dedup de conteúdo já existente** (`find_financial_duplicate`,
> chamada logo depois em `try_extract_from_body`) → `BODY_DUPLICATE`/`ignorado`, não por este guard.
> A conta 627 (motivo original do guard de 2026-07-22) permanece corretamente hard-deletada — não é
> reintroduzida por esta mudança, pois um reprocessamento dela hoje cairia na dedup se já existisse
> conta equivalente, ou criaria uma conta nova (comportamento agora considerado correto: o mesmo
> valor a pagar, se nunca capturado por outro canal, deve existir no sistema). Testes:
> `tests/test_forwarded_reminder.py` (`test_lembrete_encaminhado_gera_conta_quando_nao_e_duplicata`,
> `test_lembrete_encaminhado_repetido_vira_duplicata`).

Limpeza retroativa (2026-07-22): **hard delete da conta 627** + `email_control` 992 → `ignorado`
(varredura confirmou ser a única afetada, antes da revisão de 2026-07-23). Correção manual pontual
(2026-07-23): conta **668** reprocessada e corrigida (fornecedor + nº de documento — ver também
"O CNPJ DA PRÓPRIA EMPRESA PAGADORA" acima, mesmo caso). **Deploy:** copiar só `read_emails.py` (o
`extract_pdf.py` NÃO muda; sem `.env`, sem passo de banco). Validação (esperado `True False`):
`py -3 -c "import sys; sys.path.insert(0,'skills/email-reader/scripts'); import read_emails as R; print(R.body_forwards_payment_confirmation('Assunto: Comprovante de pagamento\\r\\n') is not None, R.body_forwards_payment_confirmation('Assunto: Lembrete Sua Fatura\\r\\n') is not None)"`

Lista padrão em `KEYWORDS_DEFAULT`, **sobrescrita por `EMAIL_KEYWORDS` no `.env`** (fonte de
verdade usada hoje). **NF-e "pura"** (`subject_is_pure_nfe`): assunto com `nota fiscal/nfe/
nf-e/nfse/nfs-e` **por palavra inteira** (não casa "co**nfe**cções") e **sem** indício de
pagável (`boleto/fatura/vencimento/`acrônimos…) que **não** gera conta a pagar vira
`ignorado` em vez de `falha` — notificação fiscal não é conta a pagar.

**Notificações → `ignorado`** (`subject_is_ignorable_notification`, `tests/test_match_keyword.py`):
e-mails de aviso/confirmação **sem anexo e sem conta no corpo** (gatilho no lugar do antigo
`falha`) viram `ignorado`. Termos: palavra inteira `nfe, nf-e, informe, sieg, cte, ct-e, dacte`;
frases `informativo, confirmado (o) pagamento, confirmação de/do pagamento, pagamento confirmado,
pagamento processado, aviso de vencimento, título a vencer, lembrete de vencimento, títulos
próximos do vencimento, comprovante de pix, protesto, protestado, cartório, comunicado,
fatura a vencer, aviso de fatura, conhecimento de transporte, forma de pagamento, meio de
pagamento, agendamento de coleta, confirmação de recebimento, confirmação recebimento`.

**MAIS DUAS FONTES alimentam `notification` desde 2026-08-04 (não regredir)** — nasceram da
varredura dos 21 e-mails em `falha`, em que **nenhum era recuperável** (`reprocess_link_emails`
e `reprocess_body_emails` devolveram **0** dos dois lados: 16 já não estavam na INBOX e os 5
restantes não tinham link nem dados no corpo). Não eram falhas do pipeline — eram e-mails que
**nunca poderiam virar conta**, e marcá-los `falha` os punha em `/erros` competindo por atenção
com extração que de fato quebrou:

- **`email_sem_conteudo_extraivel(has_attachment, pdf_links, body_text)`** — sem anexo, sem
  link e sem corpo útil. 🔴 **A condição do LINK é o que impede a guarda de mascarar falha
  real:** com link, o e-mail TINHA de onde extrair e o download fracassou (portal que mudou,
  SSRF barrando destino legítimo, PDF removido) — isso continua `falha` e visível. 🔴 O critério
  é **AUSÊNCIA de conteúdo, nunca tamanho**: corpo curto é a NORMA aqui (`FORNECEDOR X R$ 250,00
  venc 10/08` cabe em 33 chars e É um pagável), então exige não sobrar **um** caractere
  alfanumérico. Medido: os 11 casos reais tinham o corpo literalmente vazio (thread `RES:` cujo
  conteúdo ficou só no assunto).
- **`is_disposable_sender(sender_email)`** — subdomínio descartável de campanha de phishing
  (`@servidor` + hash, ex.: `setorfinanceiro@servidor9n3xa9.powerallynigeria.com`). Os assuntos
  **imitam cobrança** ("Segue NFs e BOLETOS 60582"), então casam keyword; um deles, se um dia
  trouxesse anexo, viraria **conta a pagar FALSA**. O padrão é deliberadamente ESTREITO (o
  literal `servidor` + ≥5 de hash): um filtro genérico por domínio desconhecido barraria
  fornecedor novo, que é o que o pipeline precisa aceitar. Não casa `contato@servidor.com.br`.

As três fontes alimentam `notification`, que **só** produz `ignorado` quando não houve
anexo/CSV/conta — nenhuma delas pode esconder conta que o pipeline conseguiu extrair. Testes:
`tests/test_email_sem_pagavel.py` (11 casos, **5 mutantes**), incluindo **guarda de wiring** que
lê `process_message` e exige as três dentro do argumento `notification=` — a função pura não
prova que o call site a usa (§2 item 5, reincidente).
**Reclassificação de 2026-08-04:** as guardas foram aplicadas aos históricos com as **mesmas
funções** do pipeline → **21 `falha` → 8**, e `/erros` de 35 → 22. Os **8 que permanecem** têm
corpo com conteúdo e são falha legítima, para revisão humana: Lmed ×2 (portal com CAPTCHA,
adiado), SEGUROS SURA, Romplas, PUCOMEX, LE BIANCO/PERIPAN, duartecobranca e um `boleto teste`.
> ⚠️ **Os 8 oscilaram para 21 no MESMO dia — e não foi regressão da classificação.** Horas depois
> desta reclassificação, a regressão do `pdf_links` (ver o DEPLOY 5º do dia) jogou **13 e-mails com
> anexo** em `falha`. A correção de dados de 2026-08-04 os devolveu ao status correto e o total
> voltou a **8** / `/erros` a **22**. O critério **não** foi carimbar `extraído` nos 13: 11 tinham
> conta gravada (`accounts_saved > 0`, ramo de maior prioridade) e os **2 sem conta viraram
> `duplicidade`** — os PDFs foram baixados do bucket e a linha digitável extraída com
> `febraban.extract_linha_digitavel` bateu EXATAMENTE contas já existentes (829 e 860), ou seja,
> dedup pela impressão 1. Marcá-los `extraído` teria violado o invariante da linha acima
> ("`extraído` significa gerou conta"). **Ao corrigir status em massa, derive o valor da MESMA
> regra do `status_for_result` e prove o caso ambíguo com dado, não com o assunto do e-mail.**
**CT-e/transporte (não regredir):** os termos `cte`/`ct-e`/`dacte`/`conhecimento de transporte`
fecham a lacuna da notificação de CT-e **sem anexo/link** (ex.: SSW "Arquivos de Conhecimento de
Transporte Eletronico", "OCORRENCIA CTE …") — a regra CT-e-sem-boleto de `extract_and_store` só
atua quando há PDF extraído; aqui não há anexo, então cai neste nível. Como `notification` é o
**último** critério de `status_for_result`, um boleto de transporte real gera conta ANTES e não é
escondido. **`fatura a vencer`/`aviso de fatura`** cobrem o aviso da transitobrasil ("Aviso de
fatura a vencer"). Testes: `tests/test_nonpayable_rules.py` + `tests/test_match_keyword.py`.
**Nota:** qualquer assunto com a palavra `lembrete` já é ignorado **antes** deste ponto, no
nível FORTE (`subject_is_reminder`, sem baixar/extrair — ver "Assunto com 'lembrete'"), então o
termo `lembrete de vencimento` aqui é redundante; os demais termos de vencimento (`aviso de
vencimento`/`título a vencer`/`títulos próximos do vencimento`, sem "lembrete") seguem valendo só
neste nível FRACO (só `ignorado` na ausência de anexo/conta).
Avisos sem termo generalizável (oferta de frete, "nova área do cliente", "taxa de
agendamento") são marcados por **Message-ID** em `EXPLICIT_IGNORE_IDS`
(`scripts/reprocess_ignored_emails.py`). **Não** há exclusão por boleto/fatura aqui — o
gatilho já exige ausência de anexo/conta (sem anexo nem dado no corpo ⇒ é só um aviso); com
anexo, o PDF vira `pendente` (revisão), nunca `ignorado`. Reprocesso histórico (e Message-IDs
avulsos marcados à mão, ex.: alerta de protesto SPC/Serasa) via
`scripts/reprocess_ignored_emails.py`. **SIEG** (atualizado 2026-06-17): avisos/confirmações
da SIEG **sem pagável** (ex.: "identificamos o pagamento", NF-e) seguem `ignorado`; já as
**faturas SIEG** (mensalidade R$ 426,80, link JS quebrado — ver A1) **geram conta `recebido`
pelo corpo** (fornecedor SIEG, valor, vencimento). O `bill=NNN` do link SIEG
(`_BODY_SIEG_BILL_RE`) vira `invoice_number` (`sieg_<bill>`), fazendo os dois lembretes
("Vencimento Próximo" + "Hoje") da mesma fatura **deduplicarem** (antes geravam 2 contas/mês
porque o nº saía de data relativa e divergia). Isso **revoga** a regra anterior de manter
faturas SIEG em `ignorado`; o handler A1 (baixar o boleto real) segue como melhoria futura.
