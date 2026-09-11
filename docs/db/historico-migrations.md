# Histórico de migrations — `pagamentos`

> **Extraído do `CLAUDE.md` em 2026-08-18.** É o registro do que cada migration **já aplicada**
> fez, com as lições que cada uma cobrou. Histórico: não envelhece, mas também não precisa estar
> no contexto de toda sessão.
>
> - **As regras de DDL que valem para migration NOVA** (GRANT/REVOKE explícito, `DROP FUNCTION`
>   apaga grants, coluna gerada exige DROP+ADD, RLS sem policy = 0 linhas, sonda `DO $$`) vivem
>   na skill **`migrations-supabase`** (`.claude/skills/migrations-supabase/`).
> - **O número da última migration aplicada** sai de `ls supabase/migrations | tail -1` — nunca
>   de um número escrito aqui ou no `CLAUDE.md`.
> - **Caveats operacionais** (ordem de aplicação, migrations não re-executáveis, bootstrap):
>   `supabase/migrations/README.md`.

---

## Changelog (mais recente primeiro)

**A `135` faz o backfill da 4ª empresa pagadora, LE BLANC (`sk_company = 4`, CNPJ
`20584679000110`)** — sem DDL (a linha em `company` foi cadastrada pelo usuário; o trigger da 084
respeita valor explícito, então o valor gruda). Move para 4 toda conta com menção a LE BLANC em
qualquer grafia ("le blanc", "leblanc", "le_blanc") na própria conta, no e-mail de origem ou no
fornecedor, ou com `payer_cnpj` de raiz `20584679` — espelho da regra Python de
`resolve_sk_company`, onde a LE BLANC **vence a ester**. Diferente da 084, o **fornecedor**
LE BLANC também classifica (decisão do usuário). Abre com uma trava `DO $$` **deliberada**: aborta
se o sk 4 não for a LE BLANC, porque o UPDATE gravaria FK válida para a empresa errada sem erro
nenhum. Medido antes de aplicar: exatamente **4 contas** (348, 759, 1350, 1351); a 1468 já estava
em 4 por curadoria manual.

**A `134` cria a RPC `find_supplier_by_email(text)` — lookup de fornecedor por e-mail que NUNCA
cria** (aplicada via Supabase MCP em 2026-08-19). Nasceu para o pipeline poder identificar o
fornecedor pelo remetente ORIGINAL de um bloco ENCAMINHADO no corpo (caso da conta 1101, guia da
Junta Comercial mandada por um despachante). 🔴 **A separação CONSULTA × CRIAÇÃO é a razão de
ela existir:** o endereço de quem encaminha diz quem **MANDOU** o documento, não quem **RECEBE** o
pagamento, e reusar `resolve_supplier_for_account` faria qualquer pessoa que encaminhasse uma guia
virar fornecedor no primeiro e-mail, pelo auto-insert. 🔴 **REUSA `_is_internal_email` (046) e
`_is_platform_email` (109)** em vez de reescrever as regras em Python — uma 2ª cópia divergiria da
do banco no primeiro ajuste, sem erro. Duas diferenças deliberadas em relação ao Passo 4 de
`resolve_supplier_id`: filtra `deleted_at IS NULL` (soft delete passa a ter efeito no roteamento) e
usa `ORDER BY sk_supplier` antes do `LIMIT` — o Passo 4 tem `LIMIT` sem `ORDER BY`, e o vencedor de
um empate varia com o plano de execução, o que aqui decidiria sob qual fornecedor o dinheiro é
lançado. `SECURITY DEFINER` + `REVOKE` de `PUBLIC/anon/authenticated` + `GRANT` só a `service_role`.
Sonda de 6 passos, incluindo a prova de que a contagem de `supplier` **não muda** ao consultar um
e-mail desconhecido. **Motivo de a consulta REST direta ter sido recusada:** o `eq` do PostgREST é
case- e whitespace-sensitive, enquanto o Passo 4 compara `lower(trim(...))` — um cadastro com
`"Ricardo.Advogado@Yahoo.com.br "` responderia "não encontrei", indistinguível de "não está
cadastrado" (e `ilike` não resolve: em LIKE o `_` continua curinga).

**A `133` consolida `dar` e `dare` no ÚNICO tipo `dar / dare`, com backfill** (aplicada via
Supabase MCP em 2026-08-19). DAR e DARE nomeiam o **mesmo instrumento** — a guia de arrecadação
estadual — e o acrônimo impresso varia por estado (MT imprime `DOCUMENTO DE ARRECADAÇÃO - DAR
MODELO 1 - AUT`; SP imprime `DARE`). Mantê-los separados criava dois rótulos para a mesma coisa no
filtro de `/consulta`, no `ContaForm` e nos dashboards. Mesmo padrão de `dam / duam`. **26 contas**
migradas de `dare`. 🔴 **ORDEM OBRIGATÓRIA: DROP do CHECK → UPDATE → ADD do CHECK** — invertendo,
o `ADD CONSTRAINT` falharia com 23514 nas 26 linhas que ainda diziam `dare`. ⚠️ **O UPDATE dispara as
triggers BEFORE**: medido antes de aplicar, 24 das 26 contas estavam em `pago` (8), que
`fn_set_status_from_due_date` não toca, e as 2 em `a vencer` (3) ainda não tinham vencido — nenhuma
situação mudou. ⚠️ **A trilha registra o backfill como `ator_via = 'servico'`, e isso é correto:**
backfill de migration **é** automação; o GUC `app.audit_actor` é para edição humana atribuível, e
usá-lo aqui atribuiria a uma pessoa uma mudança que ela não fez. **A `132` não foi editada** —
migration aplicada é IMUTÁVEL, e o registro honesto é que `dar` existiu como valor próprio entre a
132 e a 133.

**A `132` acrescenta `dar` ao CHECK de `document_type`** (aplicada via Supabase MCP em 2026-08-19;
**consolidada pela 133 poucas horas depois** — ver acima). Origem: a conta **1101**, guia da Junta
Comercial de Mato Grosso, que o extrator gravou no fallback genérico `tributo` por não ter o tipo.
A lição que ela deixa é sobre o **acrônimo mais perigoso do domínio**: `dar` é prefixo de `darf`
**e** verbo comum do português. 🔴 Por isso o tipo é auto-classificado **apenas por rótulo
explícito** (`_DOC_TYPE_NORM`, lookup EXATO) ou por **frase inequívoca** — nunca pela forma pura,
que `KEYWORDS` (substring) casaria em "padaria" e `_SUBJECT_TAX_DOC_KEYWORDS`/`_BODY_DOC_KEYWORDS`
(palavra inteira) casariam em "favor **dar** baixa". Mesmo precedente de `das` (só por "simples
nacional"/"simei") e `dam / duam` (só por "duam"). 🔴 **`documento de arrecadacao estadual` ficou
FORA das frases-gatilho, e isso tem prova no acervo:** é o nome por extenso do **DAE** em Pernambuco
(conta 1103) e no Ceará (conta 821) — incluí-la faria todo DAE ser gravado como DAR, sem erro nenhum.

> ⚠️ **O comentário DENTRO de `132_doc_type_dar.sql` está DESATUALIZADO neste ponto** e não pode ser
> corrigido — migration aplicada é imutável. Ele lista `documento de arrecadacao estadual` **entre**
> as frases inequívocas, porque foi escrito antes de a prova do acervo (contas 1103/821) aparecer.
> A decisão final é a deste parágrafo, e é ela que o código e `tests/test_doc_type_dar.py`
> implementam. Quem ler só o arquivo da 132 conclui o oposto — daí este aviso.

**A `131` acrescenta `docx_text`/`docx_vision` ao domínio de `extraction_source`** (aplicada via
psql em 2026-08-17; idempotente, reexecução verificada como no-op). Regra e invariantes do .docx em
"`extraction_source` — origem dos dados". Três lições que a aplicação cobrou:
🔴 **A migration abre transação EXPLÍCITA (`BEGIN`/`COMMIT`)** — o `psql` roda em autocommit, e a
primeira tentativa deixou o banco em estado **parcial** (CHECK novo aplicado, coluna gerada ainda
antiga) quando o `DROP VIEW` falhou; além disso, `CREATE TEMP TABLE … ON COMMIT DROP` fora de
transação é destruída no commit implícito do próprio CREATE, e as sondas não achariam o baseline.
🔴 **São DUAS views a dropar e recriar, não uma** — `analytics.vw_payables` (que lê
`extraction_confidence`, coluna GERADA cuja regra só muda com DROP+ADD) **e**
`analytics.vw_aging_vencidos`, que depende da primeira. A consulta que fiz em `pg_depend`
perguntava quem depende da **COLUNA**, e a resposta ("só vw_payables") estava certa para a pergunta
errada. Ao dropar view, pergunte pela VIEW — ou leia o erro do primeiro DROP, que nomeia a
dependente. **Nunca `CASCADE`**: ele derrubaria a dependente em silêncio e o chat ficaria sem as
tools que a leem. Os `GRANT` não sobrevivem ao DROP e são reemitidos.
🔴 **Sonda não usa número mágico** — a P4 compara o conjunto de colunas das duas views com um
baseline capturado ANTES do DROP, nos dois sentidos. A primeira versão escrevia "41 colunas" à mão,
e o número **estava errado** (são 40): número mágico numa guarda testa a memória de quem o digitou,
não o que o banco tinha.

**A `130` desfaz o dano do bug de precedência de `status_for_result`** (backfill de dado, sem DDL;
aplicada via Supabase MCP em 2026-08-17): **13 e-mails** em `ignorado` cujas contas vieram TODAS do
corpo passaram a `recebido` — `recebido` 171 → **184**, e os e-mails com conta em `ignorado` caíram
de 14 para **1**. Regra e caso de origem em "`status_for_result`" acima. Duas decisões que não
devem ser desfeitas: 🔴 **o conjunto é CONGELADO por id numa TEMP TABLE antes do UPDATE** — o
predicado inclui `status = 'ignorado'`, que o próprio UPDATE altera, então reavaliá-lo depois
devolveria zero linhas e as sondas passariam **sem provar nada**; e 🔴 **o sufixo `#N` de múltiplos
pagáveis casa por `starts_with`, NUNCA por `LIKE message_id || '#%'`** — Message-ID contém `_`, que
é **curinga no LIKE**, e um e-mail casaria a conta de outro, mudando o status errado em silêncio
(medido: **62 dos 1.462** Message-IDs têm `_`). As 4 sondas abortam a migration em divergência,
incluindo um **controle negativo** (a população com conta de PDF/imagem tem de ficar intacta) e a
reavaliação do predicado do zero, que a TEMP TABLE congelada não poderia dar. **Deliberadamente
FORA:** o e-mail 1292 (conta criada por reprocessamento manual, `pdf_text` — outra causa) e os 13
e-mails em `extraído` cuja única conta veio do corpo (imprecisos, mas não escondem conta).
Reexecução verificada como **no-op**.

**A `129` registra QUAL MODELO serviu cada turno do chat** (`analytics.ai_chat_log.model TEXT`,
aplicada via psql em 2026-08-15; aditiva, idempotente, nullable, **sem backfill**). O log tinha
latência, os quatro campos de token, `truncated` e `iterations` — tudo menos quem produziu a linha,
e a base **já misturava dois modelos** (Opus 5 até 10/08, Sonnet 5 a partir de 14/08). A única
separação possível era por DATA: inferência que morre assim que alguém troca `ANTHROPIC_MODEL` sem
anotar, que não separa dois modelos ativos no mesmo dia, e que produz resposta confiante e
não-falsificável. Mesma classe da 101 (tokens de cache) e da 102 (`truncated`/`iterations`) — número
que já existia no processo e não era persistido, cuja ausência não gera erro, só análise errada.
- 🔴 **É o modelo SERVIDO (`response.model`), não o pedido.** O pedido é um alias que a API resolve,
  e com `fallbacks` server-side um turno recusado é reexecutado em OUTRO modelo e responde 200 —
  gravar o pedido registraria justamente quem NÃO respondeu.
- 🔴 **A coluna nunca sai vazia: sem nenhuma resposta, cai para `CONFIGURED_MODEL`.** É isso que dá
  a `model IS NULL` um sentido ÚNICO — "linha anterior à 129" — em vez de confundir-se com "não
  sei". Travado por caso próprio em `route.test.ts` porque o TypeScript **não** protege: um
  `?? ''` compila igual e reintroduz a ambiguidade.
- **Sem backfill** por data: reproduziria no banco a mesma inferência que a migration elimina, com o
  agravante de virar dado de aparência autoritativa. Mesma decisão da 102.
- ⚠️ **Verificado pelo caminho REAL de gravação** (PostgREST + `service_role`), não só por SQL: o
  PostgREST mantém cache de schema e poderia rejeitar a coluna nova com `PGRST204` — que
  `logInteraction` engoliria, matando a auditoria em silêncio. Sonda gravou e leu de volta; a linha
  foi removida em seguida (42 linhas, 0 com modelo, estado idêntico ao anterior).

**A `128` torna `analytics.demonstrativo_despesas` DINÂMICA** (aplicada via `psql`/
`SUPABASE_DB_URL` em 2026-08-14 — MCP do Supabase indisponível na sessão; `CREATE OR REPLACE`,
assinatura idêntica). A 127 tinha corrigido o caso do tipo 9 **acrescentando-o ao `CASE`** — o
que garantia que o PRÓXIMO tipo novo (10, 11...) reproduziria o mesmo bug, porque a lista de
linhas continuava decorada em código. A 128 fecha essa classe de bug: duas colunas novas em
`public.financial_type_group` — **`demonstrativo_line_order`** (SMALLINT, nullable — NULL = "este
tipo não vira linha própria", preservando Receitas/Despesas-genérico/Ativo/Custo-genérico/
sentinela 0 em "Não classificado") e **`demonstrativo_line_label`** (override do rótulo; NULL usa
`type_group_description` do próprio catálogo) — com **UNIQUE parcial** (`WHERE ... IS NOT NULL`)
e **CHECK** reservando os sentinelas 900 ("Não classificado") e 999 ("Total de saídas"). A função
passou de um `CASE` hardcoded para dois `LEFT JOIN` (subgrupo vence grupo na precedência, mesma
regra da 104/127) filtrados por `demonstrativo_line_order IS NOT NULL` — um tipo novo cadastrado
no catálogo (só via SQL/`service_role`, sem CRUD) passa a aparecer como linha nova **sem migration
nenhuma**. 🔴 **Decisão de arquitetura deliberada: NADA de "dinamismo cego"** — só um tipo com
`demonstrativo_line_order` preenchido vira linha; sem esse opt-in explícito, qualquer
`type_group_id` presente nos dados (inclusive Receitas/Ativo, por erro de cadastro) vazaria como
linha num relatório que é só de saídas. **8 sondas no `DO $$` (P0-P7)**: seed exato nos 5 tipos
esperados (P0); **oráculo de regressão** — a saída nova bate, linha a linha, com uma réplica
inline do `CASE` antigo da 127, sobre toda a base histórica (P1, zero divergências); soma das
linhas fecha com o total (P2, invariante de origem da 104); **prova de dinamismo** — muda o rótulo
de um tipo JÁ existente (id 6) sem tocar na função, mede a linha nova, restaura explicitamente e
CONFIRMA a restauração antes de prosseguir (P3, no padrão "mutar/medir/restaurar/confirmar" da
Regra 2); UNIQUE e CHECK disparam para duplicata/sentinela, via `BEGIN...EXCEPTION` (savepoint
implícito, P4/P5); grants intactos (P6). 🔴 **P7 — guarda de ESCOPO por `applies_to`** (achado R1
do `/meu-code-review` light de 2026-08-14, corrigido ANTES do primeiro merge — não uma migration
129 à parte): os dois `LEFT JOIN` não validavam se o tipo casado pertencia ao lado GRUPO ou
SUBGRUPO do catálogo — sem `AND sg_tg.applies_to IN ('subgroup','both')` /
`AND g_tg.applies_to IN ('group','both')`, um subgrupo cadastrado por engano com o
`type_group_id` de um tipo GROUP-only (ex.: id 4/Passivo) casaria pelo `LEFT JOIN` do subgrupo
mesmo assim, classificando contas de forma cruzada; a sonda prova a guarda contra o catálogo
(id 4 não casa via subgrupo, id 7 não casa via grupo) — no dado de hoje o oráculo P1 já provava
que a lacuna era NO-OP (nenhum subgrupo real referencia um tipo `group`-only), então o achado era
latente, não um bug ativo. Resultado no mesmo período medido na 127: **idêntico, centavo a
centavo** (196 contas, R$ 5.097.447,64, as mesmas 6 linhas com os mesmos valores) — só o
mecanismo mudou. `apps/api-backend/lib/ai-chat/tools.ts` deixou de prometer uma lista fechada de
linhas ("a lista é dinâmica, pode ganhar categoria nova sem aviso"). **Fix irmão no mesmo commit**
(achado ao mapear consumidores, mesma classe de bug do lado TypeScript): o dashboard
`/dashboard_despesas` (`apps/frontend-vite/src/services/supabase.ts`) também decorava os ids
5/6/7 na partição dos donuts e não reconhecia o tipo 9 — as contas contavam certo no TOTAL
(`isExpenseRow` olha a NATUREZA do grupo, não o tipo do subgrupo) mas não apareciam em nenhum dos
3 donuts de tipo. Ganharam um **4º donut** "Custos de Importação" (`TYPE_GROUP_ID_CUSTO_IMPORTACAO
= 9` em `packages/shared`), na mesma ordem do `line_order` da SQL — escopo **deliberadamente
contido** (paridade com o que a SQL já mostra; não virou N-donuts dinâmico, que seria redesenho
de UI fora do pedido). Testado com o padrão da Regra 2 item 6 (teste de WIRING via clique na
tela, não só a função pura `filterExpenseDetailRows`).

**A `127` ensina `analytics.demonstrativo_despesas` sobre o tipo 9 do catálogo
`financial_type_group`** ("Custos de Importações", aplicada via Supabase MCP em 2026-08-14,
`CREATE OR REPLACE` — assinatura idêntica, sem DROP, grants preservados e verificados). O tipo 9
foi criado direto no banco às 18:03 do mesmo dia (não há CRUD para `financial_type_group`) e a
função, escrita em 31/07, não o reconhecia: contas do grupo "Despesas com Importação e Aquisição"
(natureza Custos) caíam em "Não classificado" e as de natureza Passivo eram contadas como
"Tributos" — achado pelo próprio assistente de IA numa resposta ao usuário, que sinalizou a fatia
de "Não classificado" como incomum. Nova linha "Custos de Importação" com precedência sobre o
teste de natureza=4 (mesma lógica dos tipos 7/5/6). **4 sondas no `DO $$`**, todas por oráculo
diferencial (função × consulta de controle independente): a linha nova bate com
`chart_subgroup_type_id=9 OR chart_group_nature_id=9` (>= 29 contas — o caso de origem); a linha
Tributos recalculada bate com a MESMA exclusão; a soma das 6 linhas fecha com o Total de saídas
(invariante de origem, 104); grants intactos. Medido no período do achado (01–14/08): "Não
classificado" 24→1 conta, "Tributos" 15→9 contas, "Custos de Importação" nasce com 29 contas/
R$ 2.534.248,29, total inalterado (R$ 5.097.447,64). `gasto_por_classificacao` com
`group_by='tipo'` **nunca teve o problema** (lê o catálogo dinamicamente, sem `CASE` fechado) —
só a tool `demonstrativo_despesas` e o texto do `SYSTEM_PROMPT`/descrição da tool (que enumeravam
as linhas) precisaram de ajuste. A decisão registrada aqui ("tornar a função dinâmica ficou como
plano futuro") **foi implementada na migration 128**, no mesmo dia — ver a entrada dela acima.

**A `126` corrige a classificação contábil de 48 contas da usuária `ester@otimotex.com.br`**
(aplicada via Supabase MCP em 2026-08-14, idempotente, sem DDL). Contas gravadas em **72.1.06 —
Estamparia** (chart_account_id 528/cc 10 — 15 contas) ou **21.1.01 — Mercadorias para Revenda**
(chart_account_id 152/cc 19 — 33 contas) passaram para **83.1.01 — Acordos de Terceiros**
(chart_account_id 631/cc 12), par validado contra `financial_chart_of_account.cost_center_id` —
a mesma regra de `checkClassificationPair`. Escopo restrito a `created_by = ester` (WHERE
casando o par ANTIGO, idempotente); contas de outros usuários para os mesmos fornecedores
(792/611, também classificados nesses dois códigos) **não foram tocadas** — verificado antes e
depois (8+13 contas de 792/152 e 2 de 611/528 permanecem intactas). O default de classificação
dos 4 fornecedores envolvidos (`sk_supplier` 611, 792, 883, 1317 — Modart, Hyosung SC, Grife
Têxtil, Singular) foi atualizado para 631/12, por decisão do usuário, para que contas NOVAS
desses fornecedores não voltem a nascer na classificação antiga — decisão deliberada mesmo para
792/611, cujo default também alimentava contas de outros usuários (efeito é só no
pré-preenchimento futuro). `ator_via = 'servico'` na trilha de auditoria (Onda 7) — correção
administrativa via SQL direto, não uma ação executada pela ester (o mesmo princípio do
`OLD.updated_by` não ser fonte de ator: atribuir a um humano uma mudança que ele não fez é
"acusação falsa, pior que ausência de dado").

**A `125` estende o balde parcial a `pontualidade_pagamento` com `group_by='mes'`** (aplicada via
psql em 2026-08-13, idempotente, ensaiada antes em `BEGIN … ROLLBACK`) — invariante e medições em
"Chat de IA". Mesmo `DROP`+`CREATE`/42P13 e mesma reemissão de grants da 124. ⚠️ **A guarda da
Onda 9 obrigou a 125 a RE-PROVAR os invariantes da 123** (oráculo diferencial, anti-vacuidade e a
sonda do piso alto do achado B1): `test_onda9_pontualidade.py` ancora na definição **vigente**, e
foi ela que reprovou a primeira versão da 125 por não trazer as sondas. É o comportamento
desejado — quem reescreve o corpo herda o ônus de provar de novo o que aquele corpo garantia.

**A `124` faz `analytics.gasto_por_periodo` DECLARAR o balde incompleto** (aplicada via psql em
2026-08-13, idempotente, ensaiada antes em `BEGIN … ROLLBACK`). Acrescenta `bucket_end`,
`days_covered`, `days_total` e `is_partial`; o invariante e o porquê estão em "Chat de IA".
🔴 **Exigiu `DROP` + `CREATE`** — coluna nova no `RETURNS TABLE` muda o tipo de retorno e o
PostgreSQL recusa `CREATE OR REPLACE` com **42P13** —, **e o DROP apaga os grants**: a ACL medida
antes (`{postgres=X, authenticated=X}`) é reemitida ao fim do arquivo. É a lição da 116 pela
terceira vez. De carona, fecha o **domínio** de `p_granularity` e `p_date_field`, que caíam em
`ELSE 'month'`/`ELSE due_date` — granularidade inválida virava mês e campo inválido virava
vencimento, **em silêncio**, contrariando o invariante da própria camada. A sonda que mais importa
é o **oráculo diferencial**: os 7 agregados da série de exemplo saíram idênticos aos de antes, o
que é o contrato desta migration — ela acrescenta ressalva, não mexe em número.

**A `123` corrige DOIS achados do code review da Onda 9**
([docs/review/2026-08-13-Features-light-onda9.md](docs/review/2026-08-13-Features-light-onda9.md))
— aplicada via psql em 2026-08-13, idempotente. (1) **B1**, bloqueante:
o ramo do aviso da `pontualidade_pagamento` testava `agrupado` em vez de `confiaveis`, então um
`p_min_contas` alto fazia a tool **afirmar** que não há dado confiável no período — medido, 118
contas reais numa janela de 7 dias com `min_contas=10`. É `CREATE OR REPLACE` (assinatura e
`RETURNS TABLE` idênticos ⇒ **sem DROP e os grants sobrevivem**, o inverso da armadilha da 116).
(2) **R1**: a sonda P4b da 122 comparava o `measured_at` contra um marco anterior aos **dois**
INSERTs, e por isso passava com ou sem a trigger de touch; a sonda corrigida (aqui, porque a 122 é
artefato aplicado) compara o 2º carimbo **contra o 1º**. A migration reprova a si mesma nas duas
frentes e refaz a prova sob o papel `authenticated`, porque o **corpo da função mudou**.
✅ **Verificado em produção no mesmo dia:** os cenários que devolviam a linha de aviso falsa (7 dias
+ `min_contas=10` sobre **118 contas reais**) passaram a devolver **vazio**, o aviso legítimo do
período fora da cobertura continua saindo, e uma reexecução do medidor provou o `touch` da 122 no
mundo real — 7 linhas, 7 gatilhos distintos (não duplicou) e `measured_at` avançando de 14:16 para
17:48. A tool também foi exercitada **pelo PostgREST** pela primeira vez: `42501` com a anon key,
não `PGRST202` — o que prova, de uma vez, que o cache de schema a enxerga e que `anon` está barrado.

**A `122` cria `analytics.roadmap_trigger_snapshot`** — a série histórica dos 7 gatilhos
condicionais da Onda 9 (aplicada via psql em 2026-08-13, idempotente), alimentada pela skill
**`roadmap-gatilhos`**. 🔴 **A UNIQUE `(trigger_key, measured_on)` é a característica central, não
um detalhe**: a gravação é UPSERT por dia, então remedir CORRIGE o ponto em vez de duplicar — sem
ela, qualquer média ou gráfico sobre a série passaria a mentir depois da primeira reexecução, que
acontece o tempo todo (teste manual, retomada após falha). O CHECK fecha o domínio das chaves para
que um typo no script não crie série órfã (a série certa pararia de crescer em silêncio), e
`criterion` guarda a régua aplicada **naquela** medição — sem ela, um `fired = false` de hoje fica
inauditável quando o limiar mudar. Ela também concede a `service_role` o `EXECUTE` de
`payment_date_confiavel_desde()`: sem isso o medidor teria de **fixar a data de corte por conta
própria**, criando a 2ª fonte de verdade que a 121 existe para impedir.
🔴 **`measured_at` usa `clock_timestamp()` e é mantido por TRIGGER, não por DEFAULT** *(achado da
autorrevisão adversarial, reproduzido antes de corrigir)*. `DEFAULT` só vale no INSERT, e o UPSERT
do PostgREST atualiza apenas as colunas **enviadas** — o script não manda o horário de propósito,
para o relógio ser sempre o do banco. Resultado: na 2ª medição do dia, `metrics` mudava e o
carimbo ficava congelado no horário da **primeira**, ou seja, a coluna cuja única função é dizer
*quando se mediu* passava a informar outra hora, em silêncio. E o `clock_timestamp()` não é
estilo: com `now()` (que é o instante em que a **transação** começou) o valor do UPDATE seria
idêntico ao do INSERT, tornando o invariante **improvável por construção** — a sonda não teria como
distinguir "a trigger funcionou" de "a trigger não existe".

**A `121` é a Onda 9 — pontualidade de pagamento** (aplicada via psql em 2026-08-13, idempotente).
Cria `analytics.payment_date_confiavel_desde()` (a data de corte do backfill, **fonte única**) e a
12ª tool `analytics.pontualidade_pagamento(...)`. 🔴 **Ela mede só o que tem carimbo real da
trigger da 096** — as 441 contas do backfill têm `days_late = 0` por construção, e agregá-las junto
devolve "atraso médio zero", que foi o motivo de o item ficar adiado por meses. A tool **exclui e
CONTA** (`fora_da_cobertura`, `excluidas_venc_alterado`): número que esconde a própria cobertura
não é auditável. Invariantes e as duas armadilhas do dado em "O que a Onda 9 entregou".
⚠️ Antes de aplicar, a migration inteira foi **ensaiada dentro de `BEGIN … ROLLBACK`** — as 8
sondas rodaram e nada persistiu; é o jeito barato de testar DDL numa base compartilhada dev+prod.

**A `120` é a Onda 8 (item 8.3) — o gate do chat de IA por grupo** (aplicada via Supabase MCP em
2026-08-12, idempotente). Acrescenta a `public.user_group` três colunas — `ai_chat_enabled`
(`NOT NULL DEFAULT false`, opt-in) e `ai_chat_limit_per_hour`/`_per_day` (NULL = teto do `.env`) —
mais o CHECK `chk_user_group_ai_chat_limits`, e libera na semente os grupos **1 Administrador, 2
Diretor e 7 Financeiro**. 🔴 **Não cria função helper de RLS, e isso é decisão:** RLS responde
"quais linhas este papel lê", o gate responde "este usuário pode chamar o endpoint" — e quem lê é
o `gate.ts` com `service_role`, para quem um `SECURITY DEFINER` baseado em `auth.uid()` seria
**inalcançável** — e `get_advisors` após aplicar mediu **0 achados novos**, confirmando que a
decisão evitou engordar aquela lista (os ERROR/WARN que aparecem são todos pré-existentes e já
triados). 🔴 A semente é `SET ... = true WHERE group_id IN (...)`, **nunca**
`SET ... = (group_id IN (...))`: a segunda forma também é "idempotente" e **revogaria em silêncio**
todo grupo liberado à mão depois. O `DO $$` tem 6 sondas e aborta em qualquer uma; a mais valiosa é
a **P1**, que cruza `analytics.ai_chat_log` com a semente e falha se algum usuário que **já usou** o
chat ficasse sem acesso — pegando uma semente errada na aplicação, não em produção no dia seguinte.
⚠️ A sonda P2 avança a sequence IDENTITY de forma permanente (sequence não é transacional);
inócuo, mas registrado para não ser lido como vazamento.

**A `119` é a Onda 5 (item 5.3)** — conteúdo do CT-e em `fiscal_document` (rota, peso, NF
transportada, destinatário, frete) + `analytics.documentos_fiscais` devolvendo esses campos com
filtro `p_rota`. Aplicada via psql em 2026-08-12, idempotente (**exige os DOIS `DROP FUNCTION`**,
6 e 7 parâmetros — ver os invariantes da Onda 5) e com sonda `DO $$` que grava, consulta pela
tool, testa o filtro de rota nas duas pontas, verifica o CHECK e **aborta** em qualquer
divergência. 🔴 Ela é a migration que **acrescenta valor monetário** a `fiscal_document`, o que
substitui a barreira estrutural da Onda 3 por uma declarada + travada em teste.

**As `117`/`118` são a Onda 7 — a trilha de auditoria** (aplicadas via Supabase MCP em 2026-08-11,
idempotentes). A **117** popula `public.audit_log` por trigger em `financial_account_control` e
`supplier`, e antes disso **fecha o vazamento da própria tabela** (policy `TO public` + `GRANT
SELECT TO anon`, herdados de ela ter sido criada pelo dashboard) e corrige `registro_id` de **uuid**
para **bigint** — a PK da fato é bigint, então não havia onde gravar o id da conta. A **118** entrega
a 10ª e a 11ª tools (`analytics.auditoria_eventos`, `analytics.auditoria_resumo`). Invariantes,
achados e medições em "O que a Onda 7 entregou".

**A `116` faz as duas funções de `analytics` DECLARAREM a truncagem** (aplicada via Supabase MCP em
2026-08-10, idempotente) — correção do achado B1 do code review da Onda 6. `fornecedores_recorrentes`
devolvia **50 de 63** fornecedores com HTTP 200 e nenhum sinal do corte. Acrescenta
`total_encontrado`, fecha o `ORDER BY` na chave do agrupamento (sem ordem total, o recorte do
`LIMIT` varia com o plano) e protege o `LIMIT` contra negativo. 🔴 **Exige `DROP`+`CREATE`** (mudar
o `RETURNS TABLE` é mudar o tipo de retorno) e **reemite os `GRANT`/`REVOKE`**, que o `DROP` apaga.
Traz `DO $$` que **aborta** comparando o total declarado com o real. Detalhe em
[docs/review/2026-08-10-Features-light-onda6.md](docs/review/2026-08-10-Features-light-onda6.md).

> 🔴 **`financeiro@otimotex.com.br` é conta TÉCNICA do backend, não uma pessoa** *(decisão do dono
> do produto, 2026-08-13)*. Ela existe para ser o sentinela de autoria e **nunca terá acesso pelo
> app** — nunca logou (`last_sign_in_at` nulo desde a criação, em 07/08).
> **Está no grupo 7 (Financeiro) desde 2026-08-13, DE PROPÓSITO** — antes estava no 0, e foi movida
> a pedido, para que nenhum usuário fique no grupo sentinela. ⚠️ Isso lhe dá `ai_chat_enabled =
> true`: **risco conhecido e ACEITO**, porque a conta não é usada por ninguém. **Não "corrigir" de
> volta para o grupo 0** ao encontrá-la no Financeiro — não é engano.
> ⚠️ **A LINHA `user_group.group_id = 0` continua existindo e não pode ser removida**, mesmo sem
> nenhum usuário: `handle_new_user` insere `group_id = 0` **hardcoded** e a coluna é `NOT NULL
> DEFAULT 0` com FK. Removê-la faz a criação de QUALQUER usuário novo falhar por violação de chave
> estrangeira. Consequência a lembrar: todo usuário novo **nasce no grupo 0** e depende de um passo
> manual de designação — enquanto estiver lá, não vê nada e o chat responde 403, em silêncio.

**A `110` troca a IDENTIDADE do SENTINELA de autoria** — `teste@otimotex.com.br` →
`financeiro@otimotex.com.br` (aplicada via Supabase MCP em 2026-08-07, idempotente). Ver
"Substituição do sentinela de autoria" abaixo. Reescreve os **4 DEFAULT** (`created_by`,
`updated_by`, `status_changed_by` e `financial_account_attachment.uploaded_by`) e o **fallback
embutido em `resolve_user_for_account()`**. 🔴 **Os quatro DEFAULT e a RPC são independentes:
trocar só um deixa o sistema meio-migrado sem nenhum erro.** A RPC é a mais perigosa — ela
devolve o UUID do sentinela quando o remetente não casa usuário, e o reader grava esse retorno
em `created_by`; com o usuário antigo apagado e a RPC intocada, **todo INSERT de conta de
remetente desconhecido violaria a FK** e o pipeline pararia de gravar, com o sintoma longe da
causa.

> **Substituição do sentinela de autoria (2026-08-07) — o mapa completo.** A identidade aparece
> em **cinco** camadas independentes; migrar uma e esquecer outra não gera erro:
>
> | # | Onde | Como foi tratado |
> |---|---|---|
> | 1 | 3 DEFAULT em `financial_account_control` | migration 110 |
> | 2 | DEFAULT de `financial_account_attachment.uploaded_by` | migration 110 — **é o que quase passou batido**: o inventário inicial contou 3 defaults, não 4 |
> | 3 | fallback de `resolve_user_for_account()` | migration 110 |
> | 4 | `SENTINEL_AUTHOR_ID`/`_EMAIL` no frontend | `src/lib/sentinelAuthor.ts` (extraído de `Consulta.tsx`, que exporta componente e dispararia `react-refresh/only-export-components`) |
> | 5 | os DADOS já gravados | UPDATE à parte, fora da migration |
>
> 🔴 **O guarda que impede a 4 divergir da 1/2/3 é `src/lib/sentinelAuthor.test.ts`** — ele lê a
> **migration mais recente** que define o DEFAULT (não uma constante repetida) e compara com a
> do bundle, mais os 4 DEFAULT entre si e o fallback da RPC. Divergir não quebra nada: a tela
> só deixa de reconhecer o sentinela e passa a exibir "Última edição por: &lt;e-mail&gt;" em
> centenas de contas que ninguém editou — erro de dado plausível e silencioso. Validado por
> mutante (constante de volta ao UUID antigo → 3 vermelhos).
>
> 🔴 **Migração de DADOS de autoria roda com as triggers CONTIDAS e prova por HASH.** Um
> `UPDATE` comum dispara `trg_fe_updated_at`, que grava `updated_at = NOW()`
> **incondicionalmente** — 293 contas apareceriam como editadas naquele dia. O procedimento:
> `ALTER TABLE … DISABLE TRIGGER USER` → UPDATE → `ENABLE` → comparar `md5` do conteúdo (todas
> as colunas **menos** a que se migra), tudo num `DO` block **atômico** que levanta exceção se
> os hashes diferirem (rollback devolve as triggers). Congele o conjunto por **id** quando o
> predicado de destino não reproduzir as mesmas linhas depois do UPDATE. As outras 4 triggers
> foram medidas como no-op para o conjunto (`status_id`, `sk_company`, `payment_date`), mas
> desligar todas remove a dependência de a medição seguir válida no instante da escrita.
>
> ✅ **CONCLUÍDO em 2026-08-07: o `teste@otimotex.com.br` foi APAGADO.** A ordem foi o que
> tornou isso seguro, e ela vale como receita para o próximo caso:
>
> 1. **A dependência não-óbvia era o CI.** O secret `A11Y_TEST_EMAIL` logava com esse usuário
>    a cada PR — correlação medida: login em 2026-08-05 09:13 UTC, 8 min antes do merge do PR
>    #217. Apagar antes de trocar o secret derrubaria o `a11y.yml` em todo PR seguinte.
>    ⚠️ **O `financeiro@` NÃO servia de substituto no CI**: tinha `password_changed = null`, e o
>    1º login cairia em `/auth/change-password` — teria trocado uma quebra por outra. Foi criado
>    o usuário dedicado `teste-a11y@sheild.app.br` (ver o bloco do workflow a11y acima).
> 2. **Exclusão pela Admin API** (`DELETE /auth/v1/admin/users/{id}`), nunca `DELETE` em
>    `auth.users`: é o caminho suportado e o que mantém `auth.identities`/`sessions`
>    consistentes — mesma razão da regra de troca de e-mail. Verificado depois: 0 identities e
>    0 sessions órfãs; a linha em `user_profile` sumiu sozinha (`ON DELETE CASCADE`).
> 3. 🔴 **A prova que vale é FUNCIONAL, não a contagem de referências.** Zerar os `count(*)`
>    não demonstra que o pipeline continua gravando. O teste decisivo foi abrir transação,
>    inserir uma conta com `created_by = resolve_user_for_account('<remetente desconhecido>')`
>    — exatamente o caminho que cai no sentinela e que violaria a FK se a RPC ainda apontasse
>    para o usuário apagado — e desfazer com `ROLLBACK`. **Repita esse ensaio** antes de dar
>    por concluída qualquer remoção de usuário referenciado por DEFAULT ou por função.

**A `109` impede o E-MAIL de SEQUESTRAR o fornecedor** (aplicada via Supabase MCP em 2026-08-03,
idempotente) — ver "E-mail de PLATAFORMA não identifica fornecedor" em "Auto-resolução de
fornecedor". Cria `_is_platform_email`, faz o Passo 4 (e-mail) ser pulado quando há CNPJ/CPF
extraído que não casou, bloqueia o e-mail de plataforma no auto-insert e em `_add_supplier_email`,
e limpa os 4 cadastros que já o haviam capturado.

**As `107`/`108` são a Onda 3** (aplicadas via psql em 2026-08-01, idempotentes). A **107** cria
`public.fiscal_document` — o documento fiscal eletrônico pela chave de acesso de 44 dígitos, tabela
de proveniência **append-only** (sem valor monetário: documento fiscal NUNCA soma em relatório
financeiro), com RLS reusando o recorte por REMETENTE da 078 e `REVOKE` de escrita do papel
`authenticated`. A **108** entrega a **9ª tool** `analytics.documentos_fiscais`, `SECURITY INVOKER`
+ `GRANT`/`REVOKE` explícitos, devolvendo `total_encontrado` (`count(*) OVER ()`) para o modelo não
contar linhas truncadas. Ver "O que a Onda 3 entregou".

**As `103`–`106` são as Ondas 1 e 2** (colunas da `vw_payables` + `demonstrativo_despesas`;
`body_full`/`body_search` + `buscar_emails`) — ver o roadmap de enriquecimento.

**A `102` acrescenta `truncated`/`iterations` a `analytics.ai_chat_log`** (aplicada via psql em
2026-07-30, idempotente, aditiva e sem backfill). Achado da **primeira execução real** (§20.9): uma
interação registrou 6 chamadas à mesma tool — e o teto do loop é 6 —, ou seja, pode ter devolvido
resposta incompleta **sem que o log dissesse**. `truncated` já existia no `ChatResult` e não era
persistido; o nº de iterações não existia. Isso cega justamente a análise que o §11 apoia no log
("quais tools faltam"): a pergunta que estoura o teto é a mais cara E a mais informativa. Consulta
que a coluna habilita: `SELECT question, iterations FROM analytics.ai_chat_log WHERE truncated`.
Sem GRANT novo — coluna de tabela existente herda o privilégio de tabela concedido pela 101.
**Cadeia verificada EM PRODUÇÃO** (interação de 30/07 13:44): `truncated = false`, `iterations = 2`,
`error` nulo — as colunas são escritas por código real, não só por teste com mock. A verificação
importava porque `logInteraction` **nunca lança**: um erro no caminho de gravação apareceria apenas
como coluna eternamente `NULL`, sem erro e sem teste vermelho.

**A `101` conserta o GRANT que faltava ao `service_role` em `analytics.ai_chat_log` e acrescenta
as colunas de token de cache** (aplicada via Supabase MCP em 2026-07-29, idempotente). O GRANT é
correção de **bug**: a 098 concedeu tudo a `authenticated` e nada ao `service_role`, que é quem
grava a trilha do chat — e como `logInteraction` **nunca lança**, a auditoria ficaria morta em
produção sem sintoma. Concede `USAGE` no schema + `SELECT, INSERT` **só no log**; **nada** nas 6
funções nem nas views (o caminho de dados precisa do JWT do usuário para a RLS decidir). As colunas
`cache_read_input_tokens`/`cache_creation_input_tokens` existem porque `usage.input_tokens` é
**apenas o resto não-cacheado** — sem elas não há como estimar custo nem notar um invalidador
silencioso do cache. Ver "Chat de IA" e §19 do doc de arquitetura.

**A `100` restaura o DML do `service_role` no DEFAULT de tabelas novas** (aplicada via Supabase MCP
em 2026-07-29, idempotente), sem devolver nada a `anon`/`authenticated`. É a correção do efeito
colateral de desligar o toggle "Automatically expose new tables" — ver o bloco da `097` abaixo.
**Estado final verificado com uma tabela-sonda real** (CREATE TABLE + `has_table_privilege` +
ROLLBACK): tabela nova nasce com `service_role` = SELECT/INSERT/UPDATE/DELETE e **`anon` e
`authenticated` sem nada, nem SELECT** — melhor que o estado anterior ao toggle, em que os dois
nasciam com SELECT. Cobre os schemas `public` e `analytics`.

**A `099` fecha DUAS RPCs que CONTORNAVAM a RLS, executáveis por `anon` (sem login)** — aplicada
via Supabase MCP em 2026-07-29, idempotente. Achadas pelos advisors do Supabase durante a Fase 1 do
chat de IA. A RLS **não** estava falhando (acesso direto às tabelas como `anon` já devolvia 0
linhas); o problema eram dois `SECURITY DEFINER` de owner `postgres` e uma view
`security_invoker = false` — três caminhos que **passam por fora** dela:

- **`fn_delete_all_emails()`** (`RETURNS text`, logo chamável em `/rest/v1/rpc/`) fazia `DELETE` em
  `financial_account_control` + `email_control` + `email_processing_errors` e reiniciava as
  sequences: **qualquer um na internet apagava a base inteira com um POST**, porque a anon key é
  pública por design (vai no bundle do browser). A função foi **PRESERVADA (só `REVOKE`)** — é a
  ferramenta de "Limpeza / reset de dados" usada pelo SQL Editor, que roda como `postgres`.
- **`search_text(p_table, p_column, p_termo)`** — `EXECUTE format('SELECT * FROM %I ...')`. O `%I`
  barra SQL injection, e é por isso que ela "parecia" segura; o problema é rodar como `postgres` e
  **ignorar a RLS**, devolvendo 50 linhas de QUALQUER tabela escolhida pelo cliente. **Exploração
  confirmada como `anon`:** 50 contas com `amount`/`created_by` e 50 fornecedores — toda a
  visibilidade por dono (076/078/080/081) contornada por uma função. Pós-fix, o mesmo POST HTTP com
  a anon key devolve `42501 permission denied`.
- **View `app_user`** — `REVOKE SELECT FROM anon` (lia os 12 e-mails de todos os usuários **sem
  login** — lista pronta para phishing dirigido). **`authenticated` MANTÉM o SELECT**: sem ele o
  "Criado por" desaparece do painel de detalhe de `/consulta`. A **081** já fechara a ESCRITA nessa
  view (escalada de privilégio via view auto-atualizável); a LEITURA por `anon` passou batido.
- `DROP TABLE public.supplier_tmp` (staging residual de 464 linhas; 0 FKs, 0 views dependentes).

> **NÃO REGREDIR — `auth_group_sees_only_own()` NÃO pode ter o EXECUTE revogado**, embora os
> advisors a apontem: ela é chamada DENTRO das policies 076/078, avaliadas com o papel
> `authenticated`; sem EXECUTE, todo SELECT de contas levaria `42501` e a RLS inteira quebraria. É
> exatamente a regressão que a **074** teve de consertar após a 072. As 6 funções `RETURNS trigger`
> (`handle_new_user`, `fn_set_payment_date`, …) que os advisors listam são **ruído**: o PostgreSQL
> recusa chamada direta a função de trigger.
>
> **Lição que generaliza (a 3ª repetição do mesmo padrão — 072, 081, 099):** neste projeto o
> perímetro do PostgREST **não é só a policy**. Toda função `SECURITY DEFINER` e toda view
> `security_invoker = false` é um furo em potencial na RLS, e o default do PostgreSQL/Supabase é
> **permissivo** (`EXECUTE` a `PUBLIC` em função nova; escrita a `authenticated` em objeto novo).
> Ao criar qualquer um dos dois, o `REVOKE` explícito faz parte da migration — e rodar
> `get_advisors` depois de DDL é barato.

**A `098` cria o schema `analytics`** — a camada semântica read-only do chat de IA (Fase 1;
aplicada via Supabase MCP em 2026-07-29, idempotente). Views `vw_payables`/`vw_aging_vencidos`
(`security_invoker = true`), as 6 funções de tool calling (`SECURITY INVOKER` + `STABLE`),
`ai_chat_log` com RLS e os GRANT/REVOKE. **É o único schema fora do `public`** neste banco, e o
único cujo `ALTER DEFAULT PRIVILEGES` já nasce corrigido. Detalhes, invariantes e o passo de
dashboard pendente (expor `analytics` no PostgREST): ver "Chat de IA" acima.

**A `097` faz a HIGIENE dos grants default de escrita** de `anon`/`authenticated` no schema
`public` **e corrige a causa raiz** (aplicada via Supabase MCP em 2026-07-28). Três passos:
(1) `REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN` de **`anon`** em
todas as tabelas — ele mantém só `SELECT`; (2) `REVOKE TRUNCATE, REFERENCES, TRIGGER, MAINTAIN`
de **`authenticated`** — **sem `UPDATE` na lista**; (3) `ALTER DEFAULT PRIVILEGES FOR ROLE
postgres IN SCHEMA public` revogando a escrita dos dois papéis, para tabela nova **nascer sem
escrita**.

> ⚠️ **A assimetria entre o passo 2 e o passo 3 é LOAD-BEARING — não "unificar" (não regredir):**
> nos objetos **existentes**, `authenticated` não tem `UPDATE` de tabela e sim **GRANTs POR
> COLUNA** (030/033/068: `has_invoice`/`has_bank_slip`/`status_id` + `reviewed_at`). Como um
> `REVOKE UPDATE ON <tabela>` derruba **também** os privilégios por coluna do mesmo tipo, incluir
> `UPDATE` no passo 2 quebraria a curadoria inline de `/consulta` e o "revisado" de `/emails` —
> foi por isso que a 081 revogou só `INSERT, DELETE` ali. Já no passo 3 revogar `UPDATE` é seguro
> **porque default privileges valem apenas para objetos criados DEPOIS**, sem tocar em grant por
> coluna existente.
>
> **O que a 097 deliberadamente NÃO faz:** não revoga `SELECT` de `anon` (hoje inócuo — nenhuma
> policy o contempla, então ele lê 0 linhas; revogar trocaria "conjunto vazio" por "permission
> denied", diferença observável sem ganho real); não mexe em SEQUENCES/FUNCTIONS; e **não altera
> os default privileges do papel `supabase_admin`** (exigiria superuser). Como as tabelas deste
> projeto são criadas por `postgres`, o passo 3 cobre o caso real — mas objeto criado por
> `supabase_admin` ainda precisaria de `REVOKE` explícito.
>
> **MEDIDO em 2026-07-29 (a lacuna do `supabase_admin` é REAL, não teórica):** consultando
> `pg_default_acl`, o ACL default de TABELAS no schema `public` é
> `anon=r` / `authenticated=r` quando o criador é **`postgres`** (só leitura — o efeito da 097),
> mas **`anon=arwdDxtm` / `authenticated=arwdDxtm`** quando o criador é **`supabase_admin`** —
> isto é, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER e MAINTAIN. Na prática: **toda
> tabela criada pelo Table Editor do dashboard nasce gravável por `anon`**, e a 097 não a alcança.
>
> **O toggle "Automatically expose new tables" foi DESLIGADO em 2026-07-29 — e NÃO fechou essa
> lacuna (não regredir a conclusão errada):** medido depois de desligar, o default do
> **`supabase_admin` permaneceu `anon=arwdDxtm` / `authenticated=arwdDxtm`**. O toggle só mexeu no
> default do papel **`postgres`**. Como `ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin` exige
> superuser (o papel do MCP/psql é `postgres`, que **não é superuser nem membro de
> `supabase_admin`**), **não há correção por SQL**. → A mitigação é de **PROCESSO: criar tabela
> sempre por migration, nunca pelo Table Editor do dashboard.** Tabela criada pela UI nasce
> gravável e truncável por `anon` e precisa de REVOKE explícito no mesmo dia.
>
> **Efeito colateral do toggle, corrigido pela `100`:** desligá-lo também removeu o DML do
> **`service_role`** (o default do `postgres` virou `service_role=Dxtm` — sem INSERT/SELECT/UPDATE/
> DELETE), e é com esse papel que o pipeline Python e a Next API escrevem. Não quebrou nada na hora
> (default privileges só valem para objeto NOVO; as 19 tabelas existentes seguiram intactas), mas
> a próxima migration que criasse tabela geraria um `permission denied` no pipeline, longe da causa.
> Ver a **migration 100**.
>
> **Estado verificado após aplicar:** 0 privilégios de escrita de tabela sobrando para
> `anon`/`authenticated`; os **4** grants por coluna da curadoria **intactos**
> (`has_column_privilege` = `true` nos três de `/consulta` e no `reviewed_at`); `SELECT`
> preservado nas 21 relações; `service_role` inalterado; e o default ACL do `public` reduzido a
> `anon=r, authenticated=r`. Requer **PostgreSQL 17+** (privilégio `MAINTAIN`).

**A `096` cria `financial_account_control.payment_date`** — a **data em que a conta foi paga**,
que até então não existia no banco (coluna `DATE` nullable + índice parcial `ix_fac_payment_date`
+ backfill + trigger `trg_fac_payment_date`; aplicada via Supabase MCP em 2026-07-28).
`status_changed_at` **não serve de proxy**: o valor mínimo dele em toda a tabela é `2026-07-10` —
a data em que a própria 077 criou a coluna —, inclusive para contas vencidas em abril. A trigger
(BEFORE INSERT OR UPDATE, `fn_set_payment_date`) **preenche** com a data corrente
(`America/Sao_Paulo`, mesmo fuso da 095) quando `status_id` passa a **8** e `payment_date` está
NULL — respeitando data retroativa informada no MESMO comando — e **limpa** quando a conta deixa
de estar paga (`8 → outro`), para que uma baixa corrigida não deixe data órfã. O backfill
(`payment_date := due_date` nas contas já pagas) roda **antes de a trigger existir** e é
idempotente (`WHERE payment_date IS NULL`), então não há conflito entre ele e o carimbo
automático. `payment_date` **não** entra no grant de coluna de `authenticated` (que segue com
`has_invoice`/`has_bank_slip`/`status_id`) — quem a preenche é a trigger, não o cliente;
verificado que isso **não** impede a curadoria inline de `/consulta` (privilégio de coluna é
checado contra a lista `SET` do comando, não contra o que um trigger BEFORE altera).

> **`payment_date` é a DATA DE PAGAMENTO da conta — decisão do dono do produto (2026-07-28): usar
> como tal, sem ressalva, em dashboard e na camada analítica.** Auditoria estrutural da coluna
> (feita na Fase 0 do chat de IA), para quem precisar conferir número ou mexer no caminho de escrita:
>
> - **Quem escreve: só a trigger `trg_fac_payment_date`.** `payment_date` **não** está no grant de
>   coluna de `authenticated` (que segue exatamente com `has_invoice`/`has_bank_slip`/`status_id` —
>   conferido em `information_schema.column_privileges`) **e** está omitida do
>   `financialAccountControlInputSchema` de `@sheild/shared`, então o Zod a descarta (strip) no
>   POST/PATCH da Next API. Nenhum caminho do app a envia; o pipeline Python tampouco. Gravar data
>   explícita hoje exige SQL direto com `service_role` — o ramo "data informada no mesmo comando" da
>   096 existe, mas é **inalcançável pela aplicação**.
> - **A ordem das triggers BEFORE foi verificada no catálogo** (o Postgres dispara em ordem
>   alfabética): `trg_fac_authorship` → **`trg_fac_payment_date`** → `trg_fe_sk_company` →
>   `trg_fe_status_vencimento` → `trg_fe_updated_at`. A única que reescreve `status_id` é
>   `fn_set_status_from_due_date`, e **só** quando a conta está em aberto (`{1,2,3}`) — ela nunca
>   produz nem destrói o `8`. Por isso o resultado é **independente da ordem**: o preenchimento testa
>   `NEW.status_id = 8` e a limpeza testa a transição a partir de `OLD.status_id = 8`, e nenhuma das
>   duas condições é alterada pelas triggers vizinhas.
> - **Invariante conferida no dado:** `payment_date IS NULL` ⇔ `status_id <> 8`, com **0 linhas
>   fora**. Nenhuma data de pagamento no futuro; nenhuma conta paga com vencimento futuro.
> - **O único limite real, e ele é do HISTÓRICO, não da coluna:** as **443** contas pagas existentes
>   em 2026-07-28 têm `payment_date = due_date` **sem exceção** — vieram do backfill da 096, que
>   adotou o vencimento. Daí para frente, um carimbo da trigger só se distingue do backfill quando
>   `payment_date <> due_date`; pagamento feito **no** vencimento gera valores idênticos, então **não
>   há discriminador perfeito** entre backfill e carimbo — a coluna de origem foi deliberadamente
>   **não** criada. Na prática: série por `payment_date` até 2026-07-28 segue a curva de
>   **vencimento**; a partir daí é a data em que a baixa foi registrada.

**A `095` corrige um BUG DE FUNDAÇÃO da trigger `fn_set_status_from_due_date`** (idempotente,
`CREATE OR REPLACE FUNCTION`; aplicada via Supabase MCP em 2026-07-23) — ver "Trigger de situação
por vencimento usa a data de HOJE, não `extracted_at`" na seção "Pipeline de baixa automática"
abaixo para o relato completo do bug e da correção.

A **094** adiciona `financial_type_group.applies_to` (`'group'`/`'subgroup'`/`'both'` + CHECK) — o
discriminador de ESCOPO que separa as duas taxonomias do catálogo (Natureza do grupo × Tipo Fixa/
Variável do subgrupo) e escopa os lookups + a validação; ver "Escopo do `financial_type_group`".
Idempotente (`ADD COLUMN IF NOT EXISTS` + UPDATE guardado); aplicada via Supabase MCP em 2026-07-20
(Supabase compartilhada dev+prod → sem passo extra). As **091/092/093** são higiene/classificação de
dados dos cadastros contábeis: **091** corrige `financial_chart_of_account_group` (`group_type` NULL→'D'
nos códigos 26/63, sentinela id 0 `' '`→NULL, hard delete dos grupos órfãos 15/44); **092** classifica
`financial_chart_of_account_subgroup.type_group_id` em Fixa/Variável herdando o grupo em bloco; **093**
refina essa classificação POR SUBGRUPO (119 Fixa / 28 Variável / 8 não-despesa). Todas idempotentes,
aplicadas via Supabase MCP em 2026-07-20. A **087** estende o CHECK de `financial_account_control.document_type` com **`comprovante`**
(comprovante/recibo como DOCUMENTO da conta — ver "Normalização de `document_type`"); idempotente
(DROP+recria), **sem backfill**. Mesmo padrão da 086: o array espelha 1:1 o enum `DOCUMENT_TYPES`
(NÃO inclui `pix`, removido pela 075). **Aplicada via psql em 2026-07-18** (verificado: `comprovante`
aceito, `pix` ausente); Supabase compartilhada dev+prod → **sem passo de banco em produção**.
A **086** estende o CHECK de `financial_account_control.document_type` com **`cheque`** (o cheque
como DOCUMENTO da conta — ver "Normalização de `document_type`"); idempotente (DROP+recria), **sem
backfill**. **Não regredir:** o array do CHECK espelha 1:1 o enum `DOCUMENT_TYPES` e **NÃO inclui
`pix`** (removido pela 075) — copiar do enum, nunca de uma versão pré-075. **Aplicada via psql em
2026-07-17** (verificado: `cheque` aceito, `pix` rejeitado); a Supabase é compartilhada dev+prod,
então já vale para os dois — **sem passo de banco em produção**.
A **085** é o **backfill da 3ª empresa** (OTIMOTEX FARDOS): as contas cujo remetente/dono é a
ester (`ester@otimotex.com.br`) passam a `sk_company=3` — **37 linhas**. **Sem DDL** (a linha 3 já
existia em `company`) e idempotente; gruda porque o trigger da 084 não re-resolve no UPDATE. Ver
"Empresa pagadora (`sk_company`) — regra por PRECEDÊNCIA". Aplicada via psql em 2026-07-17.
Não há migration automática. A **084** implanta a **regra LEBIANCO** da empresa pagadora (ver
"Empresa pagadora (`sk_company`) — regra LEBIANCO"): (1) o trigger `trg_fe_resolve_company()`
passa a **respeitar `sk_company` explícito** (`IF NEW.sk_company IS NULL THEN resolve…`) — sem
isso o valor gravado pelo Python era descartado **e qualquer UPDATE re-resolvia a empresa**; e
(2) **backfill** das contas já extraídas (55 de 436 → `sk_company=2`). Aplicada via psql em
2026-07-17; **idempotente** (re-run = 0 linhas). A **083** introduz a **surrogate key snowflake `sk_company`**:
`sk_company` (BIGINT `GENERATED ALWAYS AS IDENTITY`) vira a **PK** de `company` e a **chave única
de relacionamento** do app; `company_id` passa a **campo de origem** (NOT NULL UNIQUE, do sistema
maior). `financial_account_control.company_id` é **substituída** por `sk_company` (backfill via
JOIN + FK `fk_fac_company` + índice `idx_fac_sk_company`; a coluna `company_id` é DROPADA). O
trigger de resolução (`trg_fe_resolve_company()`, SECURITY DEFINER — lição da 074) passa a gravar
`NEW.sk_company` via a nova função **`resolve_company_sk(payer_cnpj, payer_name)`** (REVOKE EXECUTE
de anon/authenticated + GRANT service_role, mirror 072; fallback = sk da empresa `company_id=1`); a
antiga `resolve_company_id` é DROPADA e o trigger foi renomeado `trg_fe_company_id`→
`trg_fe_sk_company`. Espelha a 042 (supplier). **One-time — não re-executável** (troca de PK/
IDENTITY, como 042/050/051). **Aplicada via psql em 2026-07-17** (`SUPABASE_DB_URL` +
`:5432/postgres`); a coluna `sk_company` criada pelo usuário tinha `DEFAULT 0` (sentinela) — a
migration o **dropa antes** do `ADD GENERATED ALWAYS AS IDENTITY` (não coexistem). As guardas de
string em `resolve_company_sk` usam `length(trim(x)) > 0` (não `trim(x) <> ''`) para evitar um
**falso positivo do SonarCloud** (analisador PL/SQL com semântica Oracle, onde `'' = NULL`);
comportamento idêntico em PostgreSQL. **NÃO reaplicar.** A **082** adiciona as colunas de CONTATO em `supplier`
(telefone/WhatsApp/chave PIX, 2 slots cada — ver "Contato do fornecedor"); aplicada **via psql**
(`SUPABASE_DB_URL` + `:5432/postgres`, com o Supabase MCP indisponível na sessão), idempotente
(`ADD COLUMN IF NOT EXISTS` + REVOKE de escrita do papel `authenticated`). (As `059`/`060`/`061`/`063`/`064`/`066`/`067`/
`068`/`069`/`070`/`071`/**`072`**/**`073`**/**`074`**/**`075`**/**`076`**/**`077`**/**`078`**/**`079`**/**`080`**/**`081`** foram aplicadas **direto via Supabase MCP** nesta
máquina — o arquivo numerado serve
de histórico; **não reaplicar** no SQL Editor (todas idempotentes, mas evite re-run). A **081**
(**fecha a ESCRITA por `authenticated` no caminho REST direto** — ver "Escrita direta por
`authenticated`" e "ESCALADA DE PRIVILÉGIO pela view `app_user`"): (a) **CRÍTICO** — `REVOKE
INSERT/UPDATE/DELETE` na view `app_user`, que era auto-atualizável e rodava como owner: qualquer
usuário logado alterava `auth.users` (trocar o e-mail do Administrador → tomar a conta pelo "esqueci
minha senha"); (b) as policies de UPDATE de `financial_account_control` e `email_control` eram
`USING (true)` → passam a usar o MESMO predicado do SELECT (076/078), preservando a curadoria por
clique (ester: 36 próprias × 0 alheias; barbara: 412); (c) `REVOKE` dos grants default residuais em
6 tabelas + `INSERT, DELETE` (nunca `UPDATE` — quebraria os grants por coluna) nas duas da
curadoria. A **080**
(**a leitura do BUCKET herda a visibilidade da conta** — ver "Visibilidade do Storage por dono"):
a policy da 021 era `USING (bucket_id='attachments')` **sem filtro de dono** — o grupo restrito via
36 contas pela tela mas alcançava os **565 objetos** do bucket pela API do Storage (a chave é
obtível: `GET /api/contas/:id` devolve `source_file` e `/attachments` o `storage_key`, ambos via
service_role). Agora o objeto só é liberado se existir uma linha **visível para o próprio usuário**
(`EXISTS` em `financial_account_attachment.storage_key` OU `financial_account_control.source_file`)
— as subconsultas rodam como `authenticated`, então a RLS da 076/079 se aplica dentro delas e a
policy **herda a regra sozinha**. Verificado: Comercial 565→**20** objetos (0 alheios), Financeiro
565→**233** (não perdeu nenhum). + índice `ix_fac_source_file`. A **079**
(**anexos MÚLTIPLOS por conta** — ver "Anexos de conta"): cria
`financial_account_attachment` (1:N → `financial_account_control`, `origin` manual/pipeline, soft
delete `deleted_at`/`deleted_by`, UNIQUE `(account_id, storage_key)`), RLS SELECT que **herda a
visibilidade da conta pai** via `EXISTS` (sem duplicar o predicado da 076 — verificado com o papel
`authenticated` real: Comercial vê 36 contas/26 anexos, Financeiro vê tudo) + **`REVOKE INSERT,
UPDATE, DELETE` do papel `authenticated`** (o Supabase concede esses grants por DEFAULT em toda
tabela nova; a RLS já bloqueava, mas o REVOKE é a mesma defesa em profundidade das 056/057 —
sem ele a tabela ficaria assimétrica com `supplier`/`status`) + backfill dos `source_file` existentes
como `origin='pipeline'` (na aplicação: 249 linhas / 228 objetos — há menos objetos que contas porque
um PDF com N boletos gera N contas). `source_file` **permanece** na conta (dedup/auditoria do
pipeline). A **078**
(**Etapa 1 estendida a `/emails` e `/erros`** — ver "Visibilidade de contas por dono"): reescreve as
policies SELECT de `email_control` e `email_processing_errors` (papel `authenticated`) para
`NOT public.auth_group_sees_only_own() OR lower(sender_email) = lower(auth.email())` — reusa o helper
e a flag `user_group.sees_only_own_accounts` da 076, mas casando o **remetente do e-mail** com o e-mail
do usuário logado (não o `created_by`/UUID). Grupo restrito (Comercial=6) vê só os e-mails/erros de que
é remetente; demais grupos veem tudo (preservado); `service_role` mantém bypass. A **077**
(**Etapa 2 — auditoria de autor** — ver "Visibilidade de contas por dono"): adiciona
`updated_by`/`status_changed_by`/`status_changed_at` (NOT NULL DEFAULT sentinela + FK) + backfill; a
trigger `trg_fac_authorship` (`fn_stamp_account_authorship`, SECURITY DEFINER — roda ANTES dos
`trg_fe_*` para ver o `status_id` do humano, não o recálculo) carimba o editor via
`coalesce(auth.uid(), explícito)`; e a view `public.app_user (id,email)` (grant `authenticated`) para
exibir o autor. A **076**
(**Etapa 1 — visibilidade de contas por DONO, por grupo** — ver "Visibilidade de contas por dono"):
adiciona `financial_account_control.created_by` (UUID → `auth.users`, NOT NULL DEFAULT sentinela
— `teste@otimotex.com.br` à época; **hoje `financeiro@otimotex.com.br`, ver migration 110** —,
FK `ON DELETE SET DEFAULT`) + backfill via `sender_email`; flag
`user_group.sees_only_own_accounts` (Comercial=6 → true); RPC `resolve_user_for_account(p_email)`
(service_role); helper `auth_group_sees_only_own()`; e troca a policy SELECT de
`financial_account_control` para `NOT auth_group_sees_only_own() OR created_by = auth.uid()`
(grupo sem flag vê tudo; Comercial vê só as próprias; service_role/admin com bypass). A **075**
remove **`pix`** do CHECK de `financial_account_control.document_type` (pix é só forma de pagamento)
+ backfill dos 39 registros `pix`→`outro` — ver "Normalização de `document_type`". A **074**
(correção de REGRESSÃO da 072 — não regredir): a 072 revogou `EXECUTE` de `resolve_company_id` de
`authenticated`, mas o trigger `BEFORE INSERT/UPDATE trg_fe_company_id` → `trg_fe_resolve_company()`
era **SECURITY INVOKER**, então passou a rodar como `authenticated` (sem EXECUTE) e **quebrou TODO
UPDATE do frontend** em `financial_account_control` — marcar NF/BOL e trocar situação em `/consulta`
davam `403 permission denied for function resolve_company_id`. Fix: a função do trigger vira
**`SECURITY DEFINER`** (owner `postgres`, que mantém EXECUTE) + `search_path` fixo + chamada
qualificada `public.resolve_company_id`. Comportamento inalterado (o `company_id` já era resolvido em
todo write); o vetor S2-1 segue FECHADO (RPC direta por `anon`/`authenticated` continua `42501`).
Verificado no banco: `prosecdef=true`, UPDATE de curadoria como `authenticated` não levanta 42501, e
`anon`→42501 na RPC. **Lição:** ao revogar EXECUTE de uma função chamada por TRIGGER, garantir que a
função do trigger seja SECURITY DEFINER (owner com EXECUTE) — senão todo write pelo papel restrito
quebra. A **073**
(higiene da auditoria de segurança 2026-07-08, idempotente): (1) **A5-2** — seed do grupo 1
"Administrador" em `user_group` (a 065 faz `UPDATE user_profile SET group_id=1`, mas a 063 só semeava
o sentinela 0 — em aplicação LIMPA a FK abortaria); (2) **S2-2** — policy **RESTRICTIVE**
`attachments_no_delete_authenticated` em `storage.objects`, bloqueio EXPLÍCITO de DELETE por
`authenticated` no bucket `attachments` (antes só o default-deny da RLS protegia; `service_role`
ignora RLS). A **072** (segurança **S2-1**, BLOQUEADOR de go-live — não regredir): `REVOKE EXECUTE`
de PUBLIC/anon/authenticated nas 6 RPCs `SECURITY DEFINER` de fornecedor (`resolve_supplier_id`,
`resolve_supplier_for_account`, `resolve_company_id`, `_enrich_supplier`, `_enrich_supplier_name`,
`_add_supplier_email`) + `GRANT` a `service_role` — fecha o vetor de escrita em `supplier` via
`/rest/v1/rpc` com a anon key (`SECURITY DEFINER` ignora RLS); verificado por `anon`→`42501`. A **071** adiciona
**`débito automático`** ao CHECK de `financial_account_control.payment_method` (débito direto em conta,
distinto do `débito` cartão) + re-mapeia as contas Sabesp de água (171/189/190) `débito`→`débito
automático` — ver "Forma de pagamento DECLARADA no corpo". A **070** cria a
trigger `trg_supplier_no_funcionario_classification` (fornecedor de FUNCIONÁRIO não carrega
classificação default — força `supplier.cost_center_id`/`chart_account_id`=0; ver "Classificação
default do fornecedor") + backfill. As **067/068/069**
são a migração faseada de **`status_id` como fonte única** da situação (remoção do `status` texto —
ver a nota da migração faseada em "Banco de dados"): **067** `status_id` NOT NULL DEFAULT 3; **068**
trigger id-primária + `GRANT UPDATE(status_id) TO authenticated`; **069** trigger só-id + DROP do CHECK
e da coluna `status`. A `066` amplia o CHECK de
`financial_account_control.document_type` com **`cartório`** (pagamento de/em cartório) + backfill dos
genéricos com contexto de cartório no assunto (id 400) — ver "Normalização de `document_type`"; ocupou
o nº que o roadmap de RBAC estimava (066–068), que desloca para 067+ quando for implementado. A `065`
cria **`public.user_profile`** (vínculo usuário→grupo por FK — ver "Grupos de usuário"). A `064` adiciona
`financial_account_control.additional_info` TEXT (nullable) — texto livre do usuário no cadastro
de contas, exibido no card de detalhe de `/consulta`; ver "CRUD de contas". A `063` cria o catálogo
**`public.user_group`** (fundação de permissões por grupo — id 0 sentinela + RLS read
`authenticated`/write `service_role`) + backfill `app_metadata.group_id=0` nos usuários; ver
"Grupos de usuário" na seção de papéis e o blueprint `docs/design/permissoes-por-grupo.md`.
(Há um `062` **duplicado** no diretório — `062_chart_account_default_level_3` e
`062_doc_type_multa_dare`; a numeração seguiu para 063.) A `061` adiciona
**`image_vision`** ao CHECK de `financial_account_control.extraction_source` — anexos de IMAGEM
lidos via Claude Vision; ver "extraction_source — origem dos dados". A `060` cria **índices de
performance** da pesquisa em `/consulta` (GIN trigram em `invoice_number`/`subject`/`sender_email`
para `ilike '%termo%'`, btree em `amount` e em `created_at DESC`). A `059` é o **backfill ÚNICO**
da classificação contábil das contas a partir do fornecedor (contas com `cost_center_id=0` E
`chart_account_id=0` herdam a classificação do supplier; fora do fluxo diário de extração).
A `058` adiciona a **FK direta**
`financial_chart_of_account.chart_account_group_id` → `financial_chart_of_account_group`
(+ índice parcial) — a coluna já existia (DEFAULT 0 = "não informado"), mas sem FK o PostgREST
não resolvia o embed `group` do grid/form de Plano de contas; sem valores órfãos, a constraint
não falha. A `057` é de **segurança**
(idempotente, defesa em profundidade): `REVOKE INSERT/UPDATE/DELETE` do papel `authenticated`
em `supplier`/`status` — o RLS já bloqueava (ambas só têm política de SELECT), isto remove o
grant default residual para simetria com a `056`; escrita segue só `service_role`. **O REVOKE do
grant default é um PADRÃO recorrente deste projeto, não um evento isolado** — o Supabase concede
INSERT/UPDATE/DELETE ao papel `authenticated` em toda tabela/VIEW nova do schema public, então
**toda migration que cria objeto novo deve terminar com o REVOKE do que o papel não escreve**
(056/057 nos cadastros, **079** no anexo, **081** nas 6 tabelas restantes + a view `app_user`,
onde deixar o default passar era uma escalada de privilégio real). Conferir com
`information_schema.role_table_grants` (grantee='authenticated', privilege_type in INSERT/UPDATE/
DELETE) — o esperado é a lista vazia, fora dos grants POR COLUNA intencionais das 030/033/068.

> **A receita acima está DESATUALIZADA em um ponto: conferir só `authenticated` não basta.**
> Levantado na Fase 0 do chat de IA (2026-07-28) e **corrigido pela migration 097**: a campanha
> 056/057/079/081 tratou o papel `authenticated`, mas **`anon` seguia com `INSERT`/`UPDATE`/
> `DELETE` de tabela inteira em 16 tabelas** do `public` (incluindo `financial_account_control`
> por completo) e **`TRUNCATE` estava concedido aos DOIS papéis** em quase tudo — sendo que
> **`TRUNCATE` não é filtrado por RLS**. Nenhum dos dois era explorável (RLS sem policy para
> `anon` ⇒ default-deny; e `TRUNCATE` não é exposto pelo PostgREST, com ambos os papéis sem
> `rolcanlogin`), mas o primeiro deixava a RLS como barreira **única** e o segundo é o que
> morderia se um dia existir caminho de SQL arbitrário sob esses papéis.
>
> **Receita corrigida** — usar `grantee IN ('anon','authenticated')` e `privilege_type IN
> ('INSERT','UPDATE','DELETE','TRUNCATE')`; o esperado hoje é **lista vazia** (a 097 zerou), com
> os grants POR COLUNA das 030/033/068 preservados à parte em `column_privileges`. E, desde a
> **097**, o `ALTER DEFAULT PRIVILEGES` do `public` já nasce sem escrita para os dois papéis —
> então **tabela nova não reintroduz o resíduo** (a ressalva fica por conta de objeto criado por
> `supabase_admin`, cujo default não é alterável sem superuser).

A `056` é de **segurança**
(idempotente): habilita RLS + leitura `authenticated` + **REVOKE de escrita** do papel
`authenticated` nos cadastros pré-existentes (`company`/`financial_account`/`financial_bank`/
grupos/subgrupos) e em `audit_log` se existir — fecha o ponto cego de RLS apontado na
auditoria de segurança. **Exige verificação no SQL Editor antes de aplicar** — ver
`supabase/migrations/README.md` e `docs/review/seguranca/RELATORIO-SEGURANCA.md` §2. A `055` é **só documentação**
(`COMMENT ON`, idempotente/re-executável): registra no banco o ON DELETE efetivo das FKs de
classificação (`NO ACTION` ≡ `RESTRICT`) e que `status` é a 4ª coluna gravável por `authenticated`
em `financial_account_control` (036). Caveats operacionais — aplicação uma-vez/ordem, migrations
não re-executáveis (042/050/051/053/039) e pré-requisito de bootstrap (cadastros pré-existentes +
`normalize_search`) — em `supabase/migrations/README.md`. A `054` redefine
`resolve_supplier_id`: a busca por e-mail vira **fallback após o nome** (não-interno), corrigindo a
criação de fornecedor duplicado quando o e-mail já consta num cadastro — ver "Auto-resolução de
fornecedor". A `053` adiciona a FK
`financial_account.status_id` → `status.status_id` — formaliza no banco a relação do lookup
de situação do CRUD de Contas; a dimensão `status` tem os ids 1–10 do ciclo de contas a pagar
+ o 30 = "ativo" das contas bancárias, então não há linha órfã.) (As `050`/`051` convertem PKs para
**`GENERATED ALWAYS AS IDENTITY`** — id gerado SEMPRE pelo banco, inclusão direta de id BLOQUEADA
(exige `OVERRIDING SYSTEM VALUE`): a `050` em `financial_chart_of_account.chart_account_id`; a `051`
em `financial_account.financial_account_id`, `financial_chart_of_account_group.chart_account_group_id`,
`financial_chart_of_account_subgroup.chart_account_subgroup_id` e `financial_cost_center.cost_center_id`
(todas eram `smallint NOT NULL DEFAULT 0`; cada uma teve `DROP DEFAULT` → `ADD GENERATED ALWAYS AS
IDENTITY` → `setval` acima do `max` atual). A `052` adiciona `supplier.cost_center_id`/`chart_account_id`
(SMALLINT NOT NULL DEFAULT 0 + FKs + índices) e faz o **merge/backfill** a partir de
`financial_account_control` (par não-zero por fornecedor; diagnóstico: 0 conflitos) — base do **sync de
classificação default do fornecedor**, ver "Classificação default do fornecedor — sync bidirecional".)
(A `049` adiciona policy de **SELECT
`TO authenticated`** em `financial_cost_center` e `financial_chart_of_account` — sem ela, o RLS
estava habilitado **sem nenhuma policy** (default deny) e o embed `cost_center`/`chart_account`
lido pelo frontend com papel `authenticated` voltava **null**, fazendo o grid/detalhe de `/consulta`
mostrar o **`#id`** em vez da descrição. A Next API não sofria por usar service_role.) (As `047`/`048` adicionam a **classificação
contábil** em `financial_account_control`: a `047` cria `cost_center_id` SMALLINT (FK →
`financial_cost_center`) e `chart_account_id` SMALLINT (FK → `financial_chart_of_account`) +
índices; a `048` torna ambas **NOT NULL DEFAULT 0** — id 0 = sentinela "não informado" (a linha id 0
existe nos dois cadastros, então o JOIN sempre casa; a UI trata 0 como vazio "—"). Ver "CRUD de
contas" e "Lookups de classificação contábil". A `046` corrige a **ordem de resolução de
fornecedor** — nome antes do e-mail; e-mail só sem nome — e bloqueia e-mails de domínio interno
(`otimotex.com.br`/`lebianco.com.br`) em `supplier`; ver "Auto-resolução de fornecedor". A `045`
adiciona `supplier.deleted_at`
+ índice parcial — soft delete do CRUD de fornecedores, ver "CRUD de fornecedores (Next API)".
A `037` cria as tabelas de log da
cobrança de vencidos — ver "Pipeline de cobrança de vencidos". A `040` cria a RPC
`resolve_supplier_for_account`; a `041` dropa o trigger de resolução, a RPC
`financial_dup_by_name` e as colunas `supplier_name`/`supplier_cnpj`/`supplier_cpf`. A `042`
introduz a **surrogate key snowflake** `sk_supplier`: vira a PK auto-incremental de `supplier`
e o alvo de toda FK; `supplier_id` passa a chave de negócio (NOT NULL UNIQUE, só em `supplier`);
`financial_account_control.supplier_id` é substituída por `sk_supplier` — **ver "Auto-resolução
de fornecedor"**. A `043` adiciona os tipos `conta de água`/`conta de luz`/`conta de telefone /
internet` ao CHECK de `document_type` e faz backfill — ver "Normalização de `document_type`".)
