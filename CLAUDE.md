# CLAUDE.md

Guia do Claude Code (claude.ai/code) para o repositório `pagamentos`.

## Onde está cada coisa

Este arquivo é carregado **automaticamente** em toda sessão. Por isso ele guarda **só o
invariante** — o que alguém quebraria amanhã sem perceber. Tudo mais tem endereço próprio.

| Destino | Carrega quando | Conteúdo |
|---|---|---|
| **este arquivo** | sempre | invariantes 🔴, arquitetura, contratos, o que não pode regredir |
| **skill** (`.claude/skills/`) | o gatilho da tarefa dispara | procedimento acionado por uma tarefa |
| **`docs/`** | alguém abre | o porquê, a medição, o caso real, o histórico |
| **`progress.md`** | alguém abre | **verdade que expira** — status, pendências, contadores |

### Skills

| Precisa de… | Skill |
|---|---|
| Criar/aplicar migration, mexer em RLS, policy, grant, trigger | `.claude/skills/migrations-supabase` |
| Boleto não extraiu, e-mail em falha, dedup, fornecedor, `read_emails`/`extract_pdf` | `.claude/skills/pipeline-extracao` |
| Grid, filtros de `/consulta`, dashboards, componente, token de cor | `.claude/skills/frontend-ui` |
| Chat de IA, tool nova, `analytics`, prompt caching, SSE | `.claude/skills/chat-ia-gateway` |
| Escrever teste, rodar gates, lint, SonarCloud, PR vermelho | `.claude/skills/testes-e-gates` |
| Acessibilidade, axe, contraste, teclado | `.claude/skills/a11y-wcag` |
| Publicar em produção, paridade, tarefas agendadas | `.claude/skills/deploy-producao` |
| Reprocessar, backfill, purga, varredura histórica | `.claude/skills/scripts-manutencao` |

### Documentos

| Precisa de… | Vá para |
|---|---|
| Status das ondas, pendências, último deploy, contadores | [progress.md](progress.md) |
| Detalhe das regras de extração (casos reais, medições) | [docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md) |
| Catálogo dos CRUDs da Next API, auth, papéis, anexos | [docs/knowledge/api-crud.md](docs/knowledge/api-crud.md) |
| Detalhe dos dois dashboards | [docs/knowledge/dashboards.md](docs/knowledge/dashboards.md) |
| Detalhe operacional de autenticação | [docs/knowledge/auth.md](docs/knowledge/auth.md) |
| O que cada migration já aplicada fez | [docs/db/historico-migrations.md](docs/db/historico-migrations.md) |
| O que cada deploy fez e a lição de cada um | [docs/deploy/historico-deploys.md](docs/deploy/historico-deploys.md) |
| Gate de qualidade por stack | [docs/padrao-execucao.md](docs/padrao-execucao.md) |
| Chat de IA — desenho e histórico das fases | [docs/arquitetura-chat-ia-pagamentos.md](docs/arquitetura-chat-ia-pagamentos.md) |
| Roadmap de dados — plano das 9 ondas | [docs/roadmap-enriquecimento-dados.md](docs/roadmap-enriquecimento-dados.md) |
| RBAC desenhado (não implementado) | [docs/design/permissoes-por-grupo.md](docs/design/permissoes-por-grupo.md) |

🔴 **Contador que um comando responde melhor NÃO se escreve aqui.** Número da próxima migration,
total da suíte e entradas do manifesto se **derivam** (`ls supabase/migrations | tail -1`,
`npm test`, `len(deploy-manifest.json)`). Aqui fica a **regra de leitura do estado**, nunca o
estado — há guarda de teste travando isso.

`tests/test_doc_links.py` garante que nenhum ponteiro daqui aponte para arquivo inexistente, que
nenhum extraído fique órfão e que este arquivo não estoure o teto de linhas.

---

## O que é este projeto

`pagamentos` é um **pipeline financeiro de contas a pagar**, não um app CRUD comum.
Fluxo central (entrada): e-mail (IMAP) → download de PDF → extração via Claude API → gravação no
Supabase → consulta/exportação pela interface web.

Há mais **quatro** rotinas agendadas: **cobrança de vencidos** (saída, Firebird → SMTP), **backup
do Supabase** (infra), **baixa automática** (reconciliação) e **gatilhos do roadmap** (medição
mensal). Detalhe de todas na skill `deploy-producao` (`pipelines.md`).

> **Arquitetura: monorepo Sheild com backend híbrido** (reestruturação de 2026-06-09).
> - **Pipeline Python permanece** (`server/` Flask + `skills/`): IMAP, extração de PDF (Claude
>   Vision via base64 + pdfplumber). É o coração do sistema; não foi reescrito.
> - **`apps/api-backend`** (Next 16 + TS, :3000) é a camada de dados/CRUD. Aciona o pipeline
>   Python por **ponte HTTP** (`lib/python-bridge.ts` → Flask), não por subprocess.
> - **`apps/frontend-vite`** (React 19 + Vite 8, :5173) é o app interno, 100% TypeScript, **sem
>   shadcn/ui**. Lê o Supabase por **REST direto com `fetch`**; só a sessão usa o SDK oficial.
> - **`apps/portal-next`** (Next 16 + Tailwind v4, :3002) é o portal público (scaffold).
> - **`packages/shared`** (`@sheild/shared`) — schemas Zod, fonte de verdade de tipos.
>
> **Stack:** Vite **8** (Rolldown) · Vitest **4** · React **19** · TypeScript **6** · ESLint **10**
> no frontend-vite (apps Next em **9** — carve-out) · **React Compiler** ativo (regras + transform)
> · Tailwind **v4 CSS-first** (`@theme`/`@utility` em `src/index.css`; **não há
> `tailwind.config.ts`**) · Zod **4**.
>
> Não aplique os templates Sheild Canvas nem shadcn aqui. O fluxo de autenticação em 3 etapas e a
> regra de não-autorregistro foram seguidos, adaptados para `.tsx` com Tailwind.
>
> **Desvio justificado:** migrations ficam em `supabase/migrations/` (não `server/db/migrations/`)
> — preserva a convenção numérica e o fluxo manual de aplicação.

---

## Regras mandatórias

Valem para **todo** código novo ou alterado, sem exceção.

### 1 — Atomic Design + Tailwind (frontend)

Detalhe e armadilhas de layout: skill `.claude/skills/frontend-ui`.

- Todo componente pertence a uma camada: `atoms/` (sem filhos de domínio), `molecules/`
  (composição de atoms) ou `organisms/` (com estado e lógica de negócio).
- Estilo **exclusivamente via classes Tailwind** — `style={{}}` inline é proibido quando existir
  token ou classe equivalente.
- 🔴 **Tokens de cor são a fonte única.** `loginGreen-*` nas telas de auth, `status-*` no restante.
  Nunca hex hardcoded **nem cor default do Tailwind** (`red-*`, `amber-*`…) para estado semântico.
  Exceção explícita: o **chrome neutro do `DataGrid`** (`slate-*`/`zinc-*`), que não é semântico.
- **Tokens de tamanho:** usar o token mais próximo (`text-sm`, não `text-[15px]`). Valores sem
  token equivalente (`object-[center_25%]`) são exceção justificada.
- 🔴 **Tailwind JIT: string literal completa em ternário.** `${on ? 'bg-a' : 'bg-b'}` é correto;
  **nunca** concatenar nome de classe (`bg-${var}`) — o JIT não gera CSS e o componente fica sem
  estilo, em silêncio.
- Preferir `hover:`/`disabled:`/`focus:` a handlers JS.
- **CVA para variantes** (`class-variance-authority` + `cn()` de `src/lib/cn.ts`). Definição `cva`
  que não é componente vai em `*.variants.ts` (senão dispara `react-refresh/only-export-components`);
  exceção aceita é `cva` **local e não exportado**.

### 2 — Todo componente tem teste

Lições completas, ferramentas e o método de validação por mutante: skill
`.claude/skills/testes-e-gates`.

- **Todo componente novo ou alterado de forma relevante tem ao menos um teste** cobrindo
  renderização e a interação principal.
- 🔴 **Teste que promete uma garantia tem de entregá-la.** Se o nome afirma "**ANTES** de",
  "**alinhado com** X" ou "**cabe no limite** de Y", a asserção precisa **observar aquilo** — não
  um número mágico, não uma chamada isolada, não uma contagem do próprio array. Três testes verdes
  deste projeto não travavam o que o nome dizia.
- 🔴 **Toda guarda que faz parsing leva sanidade do parser** — um regex que para de casar
  transforma o teste em `0 === 0`, verde para sempre.
- 🔴 **Validação por mutante:** introduza o defeito de propósito e confirme o VERMELHO. Teste que
  não falha com o defeito instalado não é teste, é decoração.
- 🔴 **Testar a função PURA não cobre o CALL SITE**, e **guarda de wiring por TEXTO não cobre o
  call site EXECUTADO** — ela prova que a chamada existe, não que funciona (não vê escopo, ordem
  de atribuição, exceção nem tipo). Acrescente sempre um caso que **execute a função de topo**.
- 🔴 **Teste não pode depender de estado LOCAL herdado do ambiente** — arquivo de estado
  (checkpoint, cache, lock) se isola no `setUp` com `tempfile.TemporaryDirectory`.
- **Suítes:** Vitest em `apps/frontend-vite` (jsdom), `apps/api-backend` (node) e
  `packages/shared` (node); pytest em `tests/` para o pipeline Python.
- ⚠️ **pytest roda no `py -3`** (deps de `requirements-dev.txt`) — o `.venv` da raiz é o runtime e
  **não tem pytest**: `No module named pytest` ali é ambiente, não suíte quebrada.
- 🔴 **`packages/shared` NÃO tem `vitest.config.ts`, e é deliberado** — um `.ts` na raiz do pacote
  ficaria fora do `include: ["src"]` do tsconfig e quebraria o lint type-aware.
- 🔴 **`src/index.test.ts` valida o barrel E faz a cobertura enxergar o pacote inteiro** (o v8 só
  reporta arquivo efetivamente carregado).
- ⚠️ **Medir o `frontend-vite` com `--maxWorkers=1`** — em paralelo o sandbox derruba ~9 casos de
  a11y por esgotamento de recursos. Falha espalhada em arquivo que a mudança não tocou é sintoma
  de ambiente.

> 🔴 **O total da suíte NÃO se escreve aqui** — derive com `npm test` / `pytest`. Ao fechar uma
> onda, cite o **incremento** (propriedade da onda, não envelhece), medido contra o commit
> anterior num `git worktree` isolado.

### 3 — REST no backend

Catálogo completo dos CRUDs, com rotas, status codes e o raciocínio de cada decisão:
[docs/knowledge/api-crud.md](docs/knowledge/api-crud.md). Os invariantes abaixo valem para **toda
rota nova**.

Duas camadas, dois envelopes — **não misturar**:

| Camada | Onde | Envelope |
|---|---|---|
| Flask (Python) | `server/app.py` | `{"ok": bool, ...}` — legado, manter |
| Next API (TS) | `apps/api-backend/app/api/**/route.ts` | `{ success, data?, error?, meta? }` |

URL = substantivo no plural · `GET` leitura, `POST` criação/ação, `PUT`/`PATCH` atualização,
`DELETE` remoção · `200`/`201`, `400`/`422`, `401`/`403`, `404`, `5xx` · sessão stateless via
`Authorization: Bearer`.

Rotas novas de CRUD/dados vão na **Next API** (Repository → Service → Route). Exceção aceita: a
leitura de e-mails usa POST + corpo porque é **ação de disparo**, não recurso CRUD.

**Autorização e visibilidade**

- **Modelo single-org:** toda sessão autenticada opera o app — criação/edição usam só
  `requireAuth`. **Três exceções:** `POST /api/users` exige `requireAdmin` (`app_metadata.role`),
  o **hard delete** exige `requireAdminGroup` (`user_profile.group_id = 1`) e o **chat de IA**
  exige `assertAiChatAllowed`. Gate de UI é cosmético; a autorização é imposta no servidor.
- 🔴 **Visibilidade por LINHA é dimensão à parte do papel.** Grupo com
  `user_group.sees_only_own_accounts` (hoje só o Comercial) só vê — e só edita — o que é seu.
  É imposto no **BANCO** (RLS 076/078/080/081), não na tela.
- 🔴 **`user_group` carrega DUAS flags ORTOGONAIS:** `sees_only_own_accounts` decide quais
  **LINHAS** o usuário enxerga; `ai_chat_enabled` decide se ele pode **CHAMAR** uma feature paga.
  São independentes e têm **defaults opostos, de propósito** — a 076 usou `false` para *não
  restringir* quem já trabalhava; a 120 usa `false` para *negar*, porque o recurso custa dinheiro
  por uso.
- ⚠️ **Exposição ACEITA:** a policy de `user_group` é `USING (true)`, então qualquer usuário logado
  lê o catálogo inteiro. É **configuração, não segredo**, e é o que permite o gate de UI funcionar
  com o token do próprio usuário. Uma auditoria vai levantar isso; a decisão é mantê-la.
- 🔴 **`canSeeConta` (`lib/auth.ts`) é obrigatório em rota que recebe id de conta.** A Next API lê
  com **service_role, que IGNORA a RLS**: sem o guard, um id alheio devolve (e edita) a conta de
  outro. Responde **404, não 403** — 403 revelaria que a conta existe. Sem ele, o `upload-url`
  entregaria credencial de ESCRITA no Storage para a conta de outro.
- 🔴 **Exceção deliberada: o DELETE de conta NÃO chama `canSeeConta`** — o propósito é o
  Administrador excluir qualquer conta.
- 🔴 **Toda VIEW nova sobre `auth.*` precisa de `REVOKE` explícito de escrita.** View simples é
  auto-atualizável e o Supabase concede grants default: a `app_user` permitia a qualquer usuário
  logado trocar o e-mail de outro e tomar a conta por "esqueci minha senha".
- 🔴 **Nunca `REVOKE UPDATE` em `financial_account_control` nem `email_control`** — derrubaria os
  **grants por COLUNA** que sustentam a curadoria inline.

**Contrato de escrita**

- 🔴 **Schemas de create/update derivam de `manualEditSchema` via `.pick()`** — colunas de
  pipeline/auditoria não são graváveis pelo cliente. `has_invoice`/`has_bank_slip` ficam **FORA do
  pick**: elas têm `.default(false)` e o `.partial()` do Zod **não remove default**, então um PATCH
  que as omitisse **apagaria a curadoria**.
- 🔴 **Erro nunca vaza detalhe interno:** `failFromError` só ecoa a mensagem de um
  **`ApiServiceError`** com status < 500 **ou marcado `clientSafe`**; qualquer outra coisa vira 500
  genérico. Classe de erro nova **DEVE estender `ApiServiceError`**. Erro de escrita passa por
  `mapWriteError`: default **500**, não 422.
  🔴 **`clientSafe` é opt-in e default `false`.** Nasceu porque o corte em 500 descartava as três
  mensagens 503 que `translateAnthropicError` produz de propósito. Marcar um 5xx genérico com ele
  reabre o vazamento que a classe existe para fechar.
  🔴 **Ecoar NÃO desliga o log** — o 5xx marcado passa por `console.error` antes do eco. A marca
  resolve o que o **usuário** lê, não o que o **operador** precisa ver. 4xx curado segue **sem** log.
  🔴 **Quem enuncia o corte tem de enunciar a exceção** — travado por guarda: doc que diga "ecoa se
  status < 500" sem citar `clientSafe` descreve comportamento que o código não tem. Numa regra de
  segurança isso é o pior modo de falha: quem lê acredita e reporta um vazamento que não existe.
- **Remoção padrão de conta = `PATCH status_id = cancelado`.** Hard delete é a exceção do grupo
  Administrador. Fornecedor usa **soft delete**; os 6 cadastros de Tabelas usam hard delete
  bloqueado por FK (409).
- **Ordenação server-side** valida a coluna contra o allowlist `SORTABLE_COLUMNS`; 🔴 **`applyOrder`
  desempata pela PK** — sem isso, empate + paginação por offset faz linha aparecer duas vezes e
  outra sumir.
- **`checkClassificationPair`** é a validação autoritativa do par centro × plano (422).

**Anexos (`financial_account_attachment`)**

- 🔴 **A chave do objeto é gerada no SERVIDOR** (`manual/{account_id}/{ts}_{rand8}_{nome}.{ext}`) e
  o guard valida o **FORMATO INTEIRO**, não o prefixo: o Supabase Storage **normaliza `..`**.
- 🔴 **O register grava `size`/`mimetype` REAIS do `storage.info()`** — a URL assinada não valida
  conteúdo.
- 🔴 **O repository filtra `deleted_at` EXPLICITAMENTE** — a policy esconde o removido de
  `authenticated`, mas a Next API usa service_role e o anexo reapareceria no grid.
- **Remoção é soft delete**; anexo `origin='pipeline'` é irremovível (auditoria → 403).

### 4 — Conventional Commits (todo o projeto)

```
tipo(escopo): mensagem
```

`feat` · `fix` · `test` · `docs` · `chore` · `refactor`. Escopo = área afetada (`login`,
`email-reader`, `consulta`, `migrations`…).

**Sem co-autoria do Claude (não regredir):** NÃO incluir `Co-Authored-By: Claude ...` na mensagem
nem rodapé de assistente no corpo do PR — pedido explícito do usuário, que sobrepõe a instrução
padrão do harness.

> 🔴 **NENHUMA operação de escrita em git roda sem pedido EXPLÍCITO na mensagem ATUAL** — `commit`,
> `push`, `gh pr create`, `gh pr merge`, `merge`, `rebase`, `reset --hard`. **Autorização é por
> TURNO e não tem efeito residual.** Também não se infere de "resolva os itens", "atualize o
> CLAUDE.md", "implante" ou "garanta" — esses pedem terminar o trabalho e APRESENTAR. Regra
> reafirmada 3× pelo usuário.
>
> **A regra tem barreira MECÂNICA desde 2026-07-30, porque só documentação não bastou.** A causa da
> 3ª reincidência foram **coringas de allow** no `settings.json` global, acumulados de cliques em
> "sempre permitir" — o harness tinha autorização permanente e nenhum prompt aparecia. Os seis
> foram removidos e `.claude/settings.local.json` declara `permissions.ask` para as mesmas
> operações em Bash e PowerShell. **NÃO reintroduzir coringa de escrita no allow** — o sintoma é a
> ausência de prompt, não um erro.

**Nome do PR:** se o usuário informar, usar exatamente esse. Se **não** informar (ou disser "pr seu
nome"), o Claude escolhe um título descritivo e abre direto — **não perguntar**. PRs seguem de
`Features` → `main`.

**FIM DE LINHA — o repositório é LF, normalizado por `.gitattributes` (não regredir).** O
desenvolvimento é em Windows, então qualquer ferramenta que reescreva arquivo em **modo texto** o
converte para CRLF. Caso real: `pathlib.write_text` reescreveu 7 arquivos e o diff saltou de 716
para **12.578** linhas.

**O estrago não é cosmético:** com o arquivo inteiro marcado como alterado, o **SonarCloud trata
CADA LINHA como código NOVO** — todo o passivo pré-existente entra no gate de *new code* e reprova
o PR, com o motivo real escondido atrás de milhares de linhas de ruído. Além de destruir o `git
blame`.

Práticas: ao gerar arquivo por script, **grave em BYTES** (`write_bytes`) ou passe `newline="\n"`.
Se o `--stat` estiver desproporcional, compare com `git diff --stat --ignore-cr-at-eol`: se a
diferença sumir, é EOL. E **normalização vai em commit PRÓPRIO**.

### 5 — Lint limpo e análise estática

Achados recorrentes, consulta ao SonarCloud e o precedente de refactor: skill
`.claude/skills/testes-e-gates`.

- **`npm run lint` na raiz deve passar com 0 erros e 0 warnings** em todos os workspaces.
- 🔴 **`npm run lint` verde NÃO garante o PR verde** — o ESLint local e o **SonarCloud** do CI têm
  conjuntos de regras diferentes, e o Sonar **reprova o merge** (`new_reliability_rating > 1`: um
  único bug MINOR no código novo já reprova).
- 🔴 **`coverage/` fica FORA do lint nos 3 apps** — o lcov-report do istanbul traz
  `/* eslint-disable */` e derruba o "0 warnings". **Não some apagando a pasta**: o workflow do
  Sonar a recria.
- **Regras do React Compiler ativas** e **transform de build habilitado** — disables justificados
  onde o effect é a ferramenta correta. 🔴 **Não adicionar `useMemo`/`useCallback`/`React.memo`
  manuais.**
- 🔴 **`tsconfigRootDir: import.meta.dirname`** em todo `eslint.config.mjs`; nos apps Next, **não**
  habilitar `projectService: true`.
- 🔴 **`.vscode/settings.json` com `eslint.workingDirectories: [{ "mode": "auto" }]` não se remove**
  — o monorepo só tem flat config por-app.
- 🔴 **`typecheck` dos apps Next EXCLUI `.next/dev/types`** — cache do `next dev` que faz o `tsc`
  acusar erro de sintaxe em código que ninguém escreveu. `exclude` vence `include`.
- **ts-prune deve reportar 0.** Export público intencional sem consumidor leva
  `// ts-prune-ignore-next` na linha **IMEDIATAMENTE** anterior ao `export` — com um comentário no
  meio o marcador é **ignorado em silêncio**. `packages/shared` deliberadamente não tem `prune`.
- ⚠️ **Nunca encadeie `| tail`/`| grep` ao medir gate** — o exit code é o do último comando e o
  `tsc` imprime erro em **stdout**.

### 6 — Acessibilidade (WCAG 2.1 AA)

Camadas de verificação, mínimos de contraste e os achados que só o navegador pegou: skill
`.claude/skills/a11y-wcag`.

- **Todo controle de formulário tem nome acessível + `id`/`name`.** Botão só-ícone leva
  `aria-label`.
- **Contraste:** texto ≥ 4.5:1, ícone/UI ≥ 3:1. Ao criar ou alterar token de cor, **validar o
  ratio** — há duas guardas de teste (por token e por uso), porque o axe em **jsdom** não avalia
  `color-contrast`.
- 🔴 **O axe em jsdom NÃO VARRE NADA DENTRO DE UM `<dialog>` — e passa VERDE.** Sonda isolada:
  `<img>` sem `alt` num `<div>` → 1 violação; dentro de `<dialog open>` → **0**. Não é o atributo
  `open` que falta, nem a config do runner. **Não escreva caso novo de axe para conteúdo de
  modal** — seria decoração; trave o requisito por asserção **direta no DOM** e deixe a cobertura
  real para a camada e2e.
- **Camada em NAVEGADOR REAL** (Playwright + axe, `e2e/`) cobre o que o jsdom não vê: contraste sob
  render efetivo, ordem de foco, região rolável. 🔴 **Não executar no sandbox do agente** — o
  renderer crasha ao montar a SPA completa.
- 🔴 **Rota cujo DOM muda por interação declara ESTADOS extras**, um `test` cada — escanear só a
  abertura cobre metade do que a tela renderiza.
- ⚠️ **Mudança de grupo, papel ou flag exige conferir o `a11y.yml`** — o usuário do CI já apareceu
  **duas vezes** como dependência não-óbvia de uma mudança de autorização.

---

## Padrão de execução e robustez técnica

**Fonte completa: [docs/padrao-execucao.md](docs/padrao-execucao.md)** — ler antes de reportar
qualquer rotina como concluída. Não afirmar "está tudo ok" sem ter executado a verificação.

Resumo dos gates: **aceite** — nulos/vazios/edge cases explícitos, controle transacional, exceção
com log (nunca `except`/`catch` vazio), validação de contratos (Zod é fonte única) e checagem de
regressão. **Padrões por stack** — Python, TS/React 19/Next 16, PostgreSQL/Supabase, Firebird 5,
DW/ETL idempotente e PowerShell. **Fechamento** — autorrevisão adversarial ("o que quebra isto?")
+ escopo reforçado em alto risco (migrations, transação, ETL, concorrência, deploy manual).

---

## Autenticação (Supabase Auth)

Telas, criação de usuário, troca de e-mail pela Admin API e o storage híbrido da sessão:
[docs/knowledge/auth.md](docs/knowledge/auth.md).

- 🔴 **Sem auto-cadastro.** Usuários criados apenas pelo admin no Supabase Dashboard;
  `supabase.auth.signUp()` nunca é chamado pelo frontend.
- 🔴 **Alterar e-mail de usuário: Admin API, NUNCA `UPDATE` em `auth.users`** — o e-mail vive em
  dois lugares e o SQL deixa a identidade inconsistente. E **duas regras casam por TEXTO** do
  e-mail (a RLS de `/emails`/`/erros` e `resolve_user_for_account`), então meça o impacto antes.
- 🔴 **Troca de senha OBRIGATÓRIA no 1º acesso**, por marca POSITIVA `password_changed` em
  **`app_metadata`** — **server-controlled**, só gravável via Admin API/`service_role`. Nunca voltar
  a marcar/ler em `user_metadata`, que é client-writable: era o furo que tornava a troca cosmética.
  **Ausência da marca = senha ainda é a temporária.**
- **Persistência da sessão:** storage híbrido pelo "Lembrar-me" (`localStorage` × `sessionStorage`).
- **Logout por inatividade: teto de 30 min, vale nos dois modos** — o early-out no
  `AuthContext.init()` é o que impede "Lembrar-me" de virar sessão eterna.
- **Rotas protegidas:** `ProtectedRoute.tsx`. RLS: policies de leitura são `TO authenticated`, e
  `services/supabase.ts` envia `access_token` no header.

---

## Arquitetura e fluxo de dados

Monorepo (npm workspaces): `apps/frontend-vite` (:5173), `apps/api-backend` (:3000),
`apps/portal-next` (:3002), `packages/shared` + camada Python (`server/`, `skills/`).

```
IMAP (Locaweb SSL)                  apps/frontend-vite (React+Vite TS, :5173)
      │                                        │
      │                          ┌─────────────┼────────────────────────────┐
      │                          │ /emails     │ /consulta      /erros      │
      │                          │   fetch direto Supabase REST            │
      │                          │   (apikey: anon + Authorization: token) │
      │                          └─────────────┼────────────────────────────┘
      │                                        │ POST /read/start + GET /progress (poll)
      │                                        ▼  (proxy /api → Flask :8000)
read_emails.run_reader() ◄───────── server/app.py (Flask, porta 8000)
      │                                        ▲
      │  1. deduplica via email_control.message_id (UNIQUE)
      │  2. SEM keyword no assunto → 'ignorado'  │ ponte HTTP (lib/python-bridge.ts)
      │  3. COM keyword → salva PDF em data/pdfs_inbox/
      │  4. in-process → extract_pdf.extract_to_csv (Claude API)
      │  5. UPSERT em email_control + fallback CSV      apps/api-backend (:3000)
      ▼
Supabase (PostgreSQL)  ── financial_account_control (dados extraídos)
                       ├─ email_control     (controle/dedup)
                       ├─ email_processing_errors (log de falhas)
                       └─ supplier          (fornecedores — auto-criados + curadoria)
```

> **Topologia de portas (dev):** o frontend chama o Flask **direto** via proxy `/api` para a
> leitura. A Next API é camada de dados independente; não intercepta o caminho atual.
>
> 🔴 **Ponte com timeout (`lib/python-bridge.ts`):** `triggerReader`/`probePythonHealth` passam
> `AbortSignal.timeout(...)` — um Flask travado (IMAP pendurado) **não** pendura o handler Next
> junto. Teto de **300 s** na leitura e **5 s** no health; timeout vira `PythonBridgeError(504)`.

---

## Chat de IA

Invariantes do gateway, das tools e do streaming: skill `.claude/skills/chat-ia-gateway`.
Desenho e histórico das fases: [docs/arquitetura-chat-ia-pagamentos.md](docs/arquitetura-chat-ia-pagamentos.md).

Chat conversacional embarcado para análise **read-only** dos dados (linguagem natural → texto +
tabela). Gateway em `apps/api-backend/lib/ai-chat/` + duas rotas (JSON e SSE); camada semântica no
schema `analytics`; UI é um widget global do `frontend-vite`.

Os quatro pilares, que nenhuma alteração pode remover:

- 🔴 **Nunca `service_role` no caminho de leitura** — `getAnonClient()` + JWT do usuário. Só o log
  usa service_role, e é exceção deliberada.
- 🔴 **Views e funções de `analytics` são `SECURITY INVOKER`** — é isso que faz a RLS valer para o
  chat. `DEFINER` aqui seria escalada de privilégio silenciosa.
- 🔴 **Tool calling sobre funções de negócio** é a via primária (não text-to-SQL, adiado).
- 🔴 **Toda interação é logada, antes de responder e aguardando** — em serverless a function é
  congelada no `return`, então `void gravarLog()` depois dele não roda e nada acusa a perda.

Três invariantes de autorização que vivem aqui porque atravessam camadas:

- 🔴 **O acesso é opt-in POR GRUPO e imposto no SERVIDOR** (`user_group.ai_chat_enabled`, migration
  120); grupo não liberado ⇒ 403 com mensagem curada, e a tentativa **é auditada**.
- 🔴 **`gate.ts` NÃO pode ser fundido ao `rate-limit.ts`** — o gate é AUTORIZAÇÃO e falha
  **FECHADO**; o rate limit é VOLUME e falha **ABERTO**. Sob um arquivo só, o refactor natural vira
  bypass de autorização, sem erro e sem teste vermelho.
- 🔴 **O grupo vem de `user_profile`, NUNCA do JWT** — o claim existe em 2 dos 13 usuários e nos
  dois está errado; ler dali autorizaria e negaria as pessoas erradas **sem levantar erro**.

🔴 **A ORDEM na rota é dependência de DADOS**, não convenção: `assertWithinRateLimit(user.id, gate)`
consome o retorno do gate. **Chamar o gate e ignorar o retorno COMPILA** (o parâmetro é opcional) e
deixa a cota do grupo **inerte, sem sintoma** — é o defeito mais provável desta área.

🔴 **`assertChatAllowed` vive em `session.ts` e é obrigatório em TODA rota de chat** — nunca
copiado entre rotas. O modo de falha da cópia não é código feio: é a rota nova nascer **sem o
gate**, respondendo perfeitamente bem enquanto entrega um recurso pago a quem não tem direito.

🔴 **SÃO DUAS ROTAS PARA O MESMO RECURSO, E UM SÓ LOOP** — o streaming é observação lateral, nunca
um segundo loop. Duplicar criaria duas cópias do teto de iterações, do pareamento
tool_use/tool_result e da tradução de erro. **A regra de eco é a MESMA, pelo MESMO helper**
(`describeClientError`): uma segunda cópia dessa regra é o pior lugar possível para divergir.

🔴 **A FRONTEIRA DO STATUS HTTP:** tudo que pode ser recusado **antes** do corpo abrir (401/400/422
da sessão, 403 do gate, 429 da cota) é recusado com JSON e status — por isso `assertChatAllowed`
fica **FORA** do `ReadableStream`. Depois do primeiro byte, a falha vira evento `error`.

🔴 **A POLÍTICA DE FALLBACK É ESTREITA** — só cai para a rota JSON em **404** e em **200 sem
`text/event-stream`**, casos em que nada foi cobrado. 403/429/5xx sobem como erro: reenviar faria a
MESMA pergunta rodar duas vezes.

🔴 **Auditoria ANTES de `controller.close()`**, não antes do `return` — em serverless a function é
congelada quando o corpo termina, então gravar depois de fechar é gravar em nada.

🔴 **CANCELAMENTO É PONTA A PONTA** — "Parar" aborta um `AbortController` que chega ao modelo; sem
isso, desistir era só fechar o painel enquanto o servidor seguia gastando tokens. **Quem decide se
foi aborto é o SIGNAL, não o tipo do erro** (`instanceof` num membro do namespace **lança** se a
classe não existir, e dentro de um `catch` essa exceção substituiria o erro real).

🔴 **GUARDA DE GERAÇÃO (`generationRef`)** — resposta de geração anterior é **descartada**. Sem ela,
"Nova conversa" com requisição em voo anexa a resposta a uma conversa **vazia** (balão sem
pergunta, histórico começando em `assistant`), sem erro nenhum — e a janela é larga porque a
requisição leva dezenas de segundos.

🔴 **O CUSTO DO TURNO É O TAMANHO DA RESPOSTA** — ≈ 3,9 s fixos + ~10 ms por token de **saída**.
Não é o banco (tools em 0,4% do turno), não é o input, não é cold start (~1 s). **Daí o teto de 15
linhas no SYSTEM_PROMPT** — um caso real gerou 4.970 tokens e levou 53 s; depois do teto, 25 s.
⚠️ **O teto vale para a LISTAGEM, nunca para a RESSALVA** — apertar a concisão é exatamente o que
faria o modelo sacrificar cobertura e balde parcial.

🔴 **Trocar `ANTHROPIC_MODEL` pode desligar o prompt caching EM SILÊNCIO** — o mínimo de prefixo
cacheável varia por modelo e **não é monotônico entre gerações**. Abaixo do mínimo o
`cache_control` é ignorado sem erro: só zera a coluna e aumenta a fatura. Quem vigia é
`warnIfCachingDisabled`, a cada turno.

⚠️ **São 12 tools** — a lista viva é `lib/ai-chat/tools.ts`, travada por teste (acrescentar tool
invalida os 3 níveis de cache). Menções a "6 funções" no doc descrevem a Fase 1.

🔴 **A `ANTHROPIC_API_KEY` do `.env` da RAIZ não vale para a Next API** — o Next carrega env do
diretório do próprio app (`apps/api-backend/.env.local`).

---

## Roadmap de enriquecimento de dados — 9 ondas

**Status de cada onda: [progress.md](progress.md).** Plano, protocolo de 5 passos e o que cada
onda entregou: [docs/roadmap-enriquecimento-dados.md](docs/roadmap-enriquecimento-dados.md).
**Ler antes de mexer em qualquer item.** Execução **uma onda por vez**.

🔴 **Número de migration NÃO se reserva com antecedência** — o plano reservou 109/110/111 para a
Onda 5 e os três foram consumidos por outro trabalho antes de ela começar. A tabela de um roadmap
escrito há meses não é fonte de verdade para isso.

**Decisões que NÃO devem ser reabertas sem evidência nova:**

- **SEM tabelas agregadas / materialized view.** As tools respondem em 44–230 ms contra turnos de
  6–53 s: o SQL é **0,4%** do tempo. O que domina é o número de tokens **gerados**, que nenhuma
  pré-agregação reduz. Gatilho para reabrir: alguma tool passar de **~500 ms** warm.
- **SEM tabela de dados de boleto** — já estão na fato; duplicar criaria 2ª fonte de verdade.
- **DRE completo DESCARTADO** — o sistema tem **0 receitas**, é contas a **pagar**. O substituto é
  a tool `demonstrativo_despesas`, com esse nome — não "DRE".
- **DPO (o indicador contábil) segue FORA** — exige o passivo e o CMV da empresa, não o que chegou
  por e-mail. O que entrou foi **pontualidade**, e o prompt **nega o nome** em vez de ignorá-lo.
- **Cupom fiscal não eletrônico fora** (sem chave, 0 ocorrências); **`amount_paid`/`approved_by`
  automáticos fora** (trigger inventaria dado); **`is_overdue`/aging como COLUNA fora** (mudaria
  com o tempo sem UPDATE — foi o bug da 095).

**Invariantes que as ondas criaram e valem para toda função nova de `analytics`:**

- 🔴 **TODA FUNÇÃO COM `LIMIT` DECLARA O TOTAL** (`count(*) OVER ()` — janela, nunca subconsulta,
  que herdaria o `LIMIT`). Truncar é **indistinguível de "acabou"**. É a **5ª ocorrência** da mesma
  armadilha; por isso virou regra, não mais um caso.
- 🔴 **`ORDER BY` + `LIMIT` exige ordem TOTAL** — sem desempate único o conjunto truncado varia com
  o plano de execução, e o sintoma é "o ranking mudou sozinho".
- 🔴 **Função que agrupa por período DECLARA o balde parcial** (`is_partial`, `days_covered`) — o
  número está certo, e a ausência da ressalva faz o leitor concluir o oposto. Mesma família de
  `fora_da_cobertura` e `total_encontrado`.
- 🔴 **`gasto_por_fornecedor` é RANKING TRUNCADO — somar suas linhas NÃO dá o total do período.**
- 🔴 **`competence_date` NUNCA pode virar DATE** — contém `YYYY-MM` e é contrato de 3 camadas
  (prompt, template do CSV, schema Zod). Converter faria **todo INSERT do reader falhar**.
- 🔴 **`installment_total` NÃO existe e não deve ser criada** — o total não está na origem; criá-la
  escreveria "3 de 3" num carnê de 12. O substituto é `analytics.parcelamentos()`.
- 🔴 **Documento fiscal NUNCA soma em relatório financeiro** — o frete já entra como boleto e a
  NF-e é a origem da mercadoria. Desde a Onda 5 a barreira é **declarada** (COMMENT + tool +
  prompt) e sustentada por guarda de teste, não mais estrutural.
- 🔴 **`ator_via='servico'` NÃO significa "ninguém"** — significa automação ou edição não
  atribuível. Ler como "não houve alteração" inverteria a conclusão de uma auditoria.
- 🔴 **`audit_log.usuario_id` NÃO tem FK para `auth.users`, e é DELIBERADO** — as três opções
  destroem a trilha: CASCADE apaga o histórico do usuário removido, SET NULL o transforma em
  "(automação)" e RESTRICT impede remover usuário. O desenho correto é guardar o uuid e resolver o
  nome na leitura (`analytics.audit_actor_label`, três estados).
- 🔴 **A POPULAÇÃO da pontualidade é só a do CARIMBO REAL.** As contas do backfill da 096 têm
  `days_late = 0` por construção; incluí-las devolve "atraso médio zero" — o número falso que
  adiou o item por meses. **Qualquer consulta futura sobre `days_late` precisa do mesmo corte.**
- 🔴 **A data de corte vive em `analytics.payment_date_confiavel_desde()`, e SÓ ali** — o literal
  pode aparecer **1× no repositório inteiro** (guarda validada por mutante). Uma 2ª cópia diverge
  no primeiro ajuste e o backfill volta para dentro da conta, sem erro nenhum.
- 🔴 **Período 100% fora da cobertura devolve UMA LINHA DE AVISO, nunca vazio** — "não existe" e
  "existe mas não dá para medir" são respostas diferentes, e vazio faria o modelo responder "não
  houve pagamento no período", invertendo a conclusão.
- 🔴 **`atraso_medio_dias` soma SÓ as atrasadas e vem NULL quando não houve nenhuma** — vazio ali
  significa "não houve atraso", nunca zero. Incluir antecipações produziria um número menor que o
  atraso real com o nome de atraso.
- 🔴 **Cadência se mede entre DATAS DISTINTAS, não entre contas** — medindo entre contas os
  intervalos vêm cheios de zeros, a mediana desaba e sai "cadência semanal" para série sem cadência
  nenhuma. E as **bandas são fixas e disjuntas**: derivá-las de uma tolerância produz faixas
  sobrepostas.

---

## Comandos

```powershell
npm run dev            # Flask (:8000) + os 3 apps Node — falham de forma INDEPENDENTE
npm run dev:vite       # só o frontend (preferido em sessão de frontend)
npm run dev:flask      # backend Flask — leitura de e-mails
npm run dev:api ; npm run dev:portal
```

Scripts da raiz: `npm test` · `npm run typecheck` · `npm run lint` · `npm run prune` (todos os
workspaces). Builds: `npm run build:vite|build:api|build:portal`.
Dependências: `npm install` na **raiz** (lockfile único) e `pip install -r server/requirements.txt`
(fonte de verdade das deps Python, pinadas com `~=`).

> **`--kill-others-on-fail` foi removido do `dev`** — antes, uma falha do Flask (porta ocupada)
> derrubava o Vite junto.

> 🔴 **Node 20.9+ ou 22 LTS, NUNCA a 24.** O `next dev` do Next 16 **crasha o worker** no Node 24 e
> TODAS as rotas `/api/*` autenticadas passam a devolver **HTML de erro 500** em vez do envelope
> JSON. **Só no dev local** (a Vercel roda LTS), o que confunde o diagnóstico. `.nvmrc` = `22` +
> `engines.node` travam a versão. **Sintoma-chave:** 500 HTML em todas as rotas autenticadas mas
> `/api/health` respondendo JSON ⇒ é o worker do dev, não o código.

Acessibilidade em navegador (runner separado, **não** roda no `npm test`):

```powershell
cd apps\frontend-vite
npx playwright install chromium
npm run test:e2e
```

**Scripts de manutenção** (reprocessar, backfill, purga, varredura): skill
`.claude/skills/scripts-manutencao`. 🔴 Todos rodam no DEV e escrevem na Supabase compartilhada
dev+prod; os destrutivos exigem `--dry-run` antes, sempre.

**Publicar em produção e verificar paridade:** skill `.claude/skills/deploy-producao`.

---

## Frontend — componentes e design system

Catálogo, `DataGrid`, filtros de `/consulta`, dashboards e tokens: skill
`.claude/skills/frontend-ui`. Dashboards em detalhe:
[docs/knowledge/dashboards.md](docs/knowledge/dashboards.md).

**Dois estilos visuais de auth coexistem** — não misturar componentes: **v2 loginGreen**
(`LoginPage`, tokens `loginGreen-*`, `font-jakarta`) e **auth gradient** (`Forgot`/`Reset`,
`bg-gradient-auth`).

Estrutura em `apps/frontend-vite/src/`: `components/atoms|molecules|organisms|dashboard/`,
`hooks/`, `lib/`, `pages/`, `services/`. Infra de teste e guardas de configuração ficam em
`tests/` (fora de `src/`); camada de navegador em `e2e/`.

**Menu da sidebar — 5 grupos:** Recebimentos (`/emails`, `/erros`) · Envios (`/cobranca/*`) ·
Contas (`/consulta`, `/contas`, `/fornecedores`) · Tabelas (6 CRUDs contábeis) · Dashboards
(`/dashboard_despesas`, `/dashboard_vencimentos`). Não há mais item "breve".

Invariantes que atravessam camadas:

- 🔴 **Sort e filtro são SERVER-SIDE** — nunca ligar os row models client-side do TanStack, que
  agiriam sobre um subconjunto.
- 🔴 **Paginação por offset exige desempate único** (`lib/stableOrder.ts` → anexa a PK). Sem ele
  uma linha aparece **duplicada** e outra **some** — o pior sintoma, porque não gera erro. Empates
  são a norma: ordenar por Situação empata 682 de 682. Na Next API o equivalente é `applyOrder`, e
  `lib/sort.guard.test.ts` reprova listagem paginada nova que chame `.order()` direto.
- 🔴 **Filtro em recurso embutido exige `!inner`** — sem ele a consulta devolve a tabela **inteira**
  com HTTP 200, e a tela mostra a base completa como se estivesse filtrada.
- 🔴 **A hierarquia de embeds precisa vir nos DOIS caminhos de leitura** (o `SELECT_WITH_EMBEDS` do
  frontend e o `SELECT_WITH_SUPPLIER` da Next API, cuja resposta é mesclada in-place no grid). Se
  só um trouxer, a célula fica **parcial após salvar a edição** e só corrige no refresh.
- 🔴 **`cancelado` aparece no GRID, mas NÃO nos KPIs** — divergência **intencional** entre o rodapé
  e o card "Total de registros".
- 🔴 **KPI que conta um predicado tem de FILTRAR o mesmo predicado** — o card "A vencer em 7 dias"
  dizia 68 e o clique trazia 69, e a divergência **cresce com a operação normal**.
- 🔴 **A varredura dos KPIs PAGINA** — o PostgREST corta no "Max rows" (1.000) devolvendo **HTTP
  200**. Sem paginar, os cinco cards passariam a subnotificar em ~12 dias.
- 🔴 **Todo popover dentro do `overflow-x-auto` precisa de PORTAL** — `overflow-x: auto` faz o Y
  computar para `auto`, e o menu nasce **clipado**, inutilizável sem erro nenhum. ⚠️ **Portal só
  fora de `<dialog>`**: dentro de um modal aberto com `showModal()` (top layer), portal para o body
  ficaria **atrás** dele — troca um sumiço por outro.
- 🔴 **A barra de filtros de `/consulta` é UMA grade de 8 colunas, com `col-start-*` explícitas.**
  O cursor do auto-placement nunca anda para trás, então mexer numa posição desloca as seguintes
  **em silêncio** — já aconteceu duas vezes. Tracks em comprimento explícito, **nunca `1fr` cru**
  (o `min-content` de um `<select>` é a opção mais longa). O `overflow-x-auto` + `w-max`/`w-full`
  do wrapper faz as duas linhas rolarem **juntas** e impede o scroll lateral de vazar para o
  `DataGrid`, cujas colunas fixadas quebrariam.
- 🔴 **Aplicar filtro no `onChange` de cada controle é a regressão a evitar** — daria ~14
  requisições ao compor um filtro de 7 campos, e `<select>` nativo no Firefox emite `change` a cada
  opção percorrida com as setas. O portão único (`queueApply`) coalesce numa janela de 300 ms.
  ⚠️ **Teste de "não consulta" com `flush()` (0 ms) é falso guarda** — não alcança a janela.
- 🔴 **O nome acessível do botão "Buscar" é DINÂMICO, porque o efeito é** — sem intervalo ele zera
  mês/ano; **com** intervalo ele o **preserva**, e a frase antiga ("em todos os períodos") era
  **falsa** nesse estado. A instrução do grid vazio cita a MESMA ação, com guarda cruzada.
- 🔴 **O FILTRO oferece só os planos EM USO; o FORMULÁRIO, o cadastro inteiro** — restringir o
  formulário impediria a PRIMEIRA conta de um plano novo. São fontes distintas por variante:
  611 planos cadastrados × 84 descrições em uso.
- 🔴 **O `else if` dos dois seletores de data mantém os ramos MUTUAMENTE EXCLUSIVOS** (intervalo
  tem precedência sobre mês/ano). Virar `if` filtraria as duas colunas por AND, devolvendo quase
  sempre vazio, sem erro. E **`rangeCol` e `periodCol` são separados** — colapsá-los num
  `??` compila, passa no lint e faz a consulta responder **200 filtrando pela coluna errada**.
- 🔴 **`ColumnDef.size`/`minSize` são IGNORADOS sem `enableColumnManagement`** — em grid
  não-gerenciado o fix é `className: 'whitespace-nowrap'`; aumentar o `size` não tem efeito nenhum.
- 🔴 **Recuperação de chunk lazy obsoleto** (`lib/chunkReload.ts` + `ErrorBoundary` raiz): deploy
  novo invalida os hashes e a rota lazy 404 — sem a rede, **tela branca**.
- **Disparo de leitura IMAP é OCULTO em produção** (`EMAIL_READER_ENABLED`) — não há Flask na
  Vercel. Os botões que só recarregam do Supabase não são afetados.

**Deploy (Vercel):** push em `main` → production; `Features`/PR → preview. `pagamentos-web` serve
**pag.otimotex.com.br**; `pagamentos-api-backend` entra por rewrite `/data-api`.
🔴 **O Vercel valida o `vercel.json` ANTES do build** — erro de schema não aparece nos logs, o
deploy vai a ERROR e o **alias de produção fica preso no deploy anterior**. **JSON não tem
comentário**; guarda em `tests/vercel-config.test.ts`. Os assets levam `immutable` (todo arquivo
tem hash no nome); o `/index.html` **não** casa essa regra e segue revalidando — cacheá-lo
prenderia o usuário na versão antiga.

---

## Pipeline de extração — invariantes

Regras completas, casos e procedimento: skill `.claude/skills/pipeline-extracao`.
Detalhe e medições: [docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md).

🔴 **`run_reader()` (`skills/email-reader/scripts/read_emails.py`) é a única fonte de verdade da
leitura** — o CLI e o Flask chamam a mesma função. Nunca duplicar lógica no Flask.
⚠️ O Flask **não tem auto-reload**: reiniciar após mexer no pipeline.

**Precedência:** anexo PDF → anexo imagem → anexo `.docx` → PDF por link → imagem inline → corpo.

🔴 **O corpo é fallback SÓ quando o anexo não respondeu por nenhum pagável.** O gate é
`attachment_account` (True também para boleto **deduplicado**); usar `accounts_saved == 0` fazia o
corpo criar conta espúria.

🔴 **`extract_and_store_accounts` roda em DOIS PASSOS** — não regredir para o loop anexo-a-anexo,
que era cego ao resto do e-mail. Isso torna as regras **independentes da ordem** dos anexos.

**O que vira conta:** fatura+boleto ⇒ só o boleto (decidido por **barcode + VALOR**, nunca por
`document_type`) · extrato/demonstrativo junto de boleto ⇒ descartado · seguradora ⇒ só com linha
digitável válida (contexto **só pelo ASSUNTO** — ampliar destruiria contas existentes) · CT-e sem
boleto ⇒ `ignorado`, não `falha` · NF-e/NFS-e pura ⇒ pulada, **exceto** combinada com boleto no
mesmo PDF.

🔴 **A guarda de VALOR preserva o 2º boleto escaneado** — o descarte só vale quando o valor
COINCIDE com um boleto real. Valor distinto é outra dívida. Bias intencional: **preservar a
conta**; perda silenciosa é pior que uma linha a revisar.

🔴 **Vencimento é AUTORITATIVO pelo fator do código de barras, com dois gates:** o valor embutido
tem de bater o `amount` (barcode corrompido por OCR não dita data) e `venc >= emissão`. No caminho
`pdf_text`, a data **impressa** vence o LLM e o fator. `ref_date` é a data do **documento**, nunca
"hoje". **Fator 0 = boleto à vista**, legítimo.

🔴 **Barcode que se REFUTA é DESCARTADO** — o OCR de scan desloca dígitos e produz código de
comprimento válido com valor 10×. É proteção **contra duplicata**: código corrompido não casa o
boleto real na 2ª via, e nasce conta duplicada. Releitura **não** recupera.

🔴 **Resposta do modelo TRUNCADA nunca vira dado.** JSON cortado virava registro vazio e o e-mail
era logado como `sem_valor` — a falha do EXTRATOR disfarçada de "documento sem valor". Custou 21
boletos e **R$ 315.556,57**. Uma leitura pode devolver **N pagáveis** (array), mas só no caminho
**visual**.

**Dedup — 4 impressões**, nesta ordem, todas escopadas por `sk_supplier` (resolvido **antes** da
dedup, nunca por texto de fornecedor):

1. **barcode**
2. **nosso número** — 🔴 **com GUARDA DE TÍTULO (`_same_title`)**: o campo que o LLM extrai às
   vezes é o **código agência/conta do cedente**, igual em todos os boletos do fornecedor; sem a
   guarda, mensalidades de meses distintos se fundiam e o pagável sumia em silêncio. Ela é
   **conservadora**: devolve "pode deduplicar" quando um dos lados não tem nº próprio.
3. nº do documento (≥6) + valor — 🔴 **ignora número SINTÉTICO** (`_is_synthetic_invoice_number`),
   senão dois boletos distintos de mesmo valor colidiam e um era perdido.
4. **valor + vencimento** — 🔴 **NÃO exige `document_type` igual**: o tipo varia entre os
   documentos que descrevem a mesma dívida (`boleto` no PDF, `fatura` no corpo). Distinção que
   permanece: doc **com** barcode só casa candidato **sem** barcode.

🔴 **A consulta de dedup RE-TENTA em falha de rede.** Um hiccup faria `find_financial_duplicate`
devolver "sem duplicata" e o pipeline **gravaria conta duplicada**. Resultado vazio não é erro.

**Reemissão** (vencimento mais recente) atualiza a conta existente, não cria outra. 🔴 **Dedup que
descarta tudo do PDF ⇒ status `duplicidade`**, nunca `extraído` — é o que torna a perda auditável.

🔴 **Boleto casado por dedup TAMBÉM vincula o anexo à conta EXISTENTE** (`register_attachment` no
bloco de dedup, não só no de conta nova). O PDF já está no Storage desde o Passo 1; sem o vínculo,
a conta ficava sem nenhum comprovante — sem erro, sem status distinto (achado 2026-09-04,
fornecedor ALKO — contas 1238/1239/1240 sem PDF por dois dias).

### Tipo de documento e classificação

Catálogo dos tipos e os casos que originaram cada regra:
[docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md).

- 🔴 **O enum `DOCUMENT_TYPES` (`@sheild/shared`) e o CHECK do banco são espelhos**, e
  `tests/test_doc_type_domain_consistency.py` trava isso lendo a migration mais recente. Ao
  acrescentar tipo, rode esse teste — ele falha se as camadas divergirem.
- 🔴 **`pix` NÃO é tipo de documento** (removido na 075) — é só forma de pagamento.
- 🔴 **Guia de ARRECADAÇÃO: valor = total a recolher (do BARCODE) e vencimento = data-limite**,
  não o valor principal nem o vencimento do tributo. `amount_charged` recebe o total **direto** —
  aplicar a aritmética de boleto somaria os juros duas vezes.
  🔴 **A regra vale em TODA fonte** — os dois builders (`_build_records_text` e
  `_build_records_vision`) a aplicam. A simetria é o invariante: enquanto o ramo visual era
  código inline no dispatcher, guia **escaneada** dependia só do que o modelo leu.
  🔴 **`apply_arrecadacao_deadline` é a FONTE ÚNICA da decisão do vencimento**, e o gate é o
  **barcode** (`arrecadacao_44`), nunca o `document_type` do LLM. Muda só a *procedência* da
  data: no texto sai por regex; no visual vem do campo **`payment_deadline`** do prompt — o
  modelo **transcreve**, o código decide. Texto disponível **vence** o campo do modelo.
  🔴 **A data transcrita pelo modelo CRUZA com o vencimento que ele leu do MESMO documento**
  (`arrecadacao_deadline_refuted`; teto de **180 dias**, contra folga real medida de **0–3**).
  O `_iso_date` valida só a FORMA: um dígito de ANO trocado atravessa a validação inteira, e
  `payment_deadline` de **2126** grava conta que **NUNCA vence** — invisível em KPI, aging e
  cobrança, sem erro nenhum. **Duas direções** (ao contrário da guarda de valor: aqui a recusa
  preserva a data do DOCUMENTO nos dois sentidos) e **opt-in pela PROCEDÊNCIA** — a data vinda
  do TEXTO é determinística e entra sem cruzamento. ⚠️ Erro de **mês/dia** (≤31 dias) NÃO é
  separável de uma validade longa legítima: a guarda não o cobre, de propósito. Sem `due_date`
  lido, **não há referência a inventar** — usar "hoje" refutaria a data certa num
  reprocessamento histórico.
  🔴 **No visual o valor passa por uma 2ª barreira** (`arrecadacao_value_refuted`,
  `ocr_barcode=True`): ali o próprio código é OCR, e o DV geral deixa passar ~13% das
  corrupções de um dígito. Total ≥ **10×** o valor lido = deslocamento de dígito ⇒ **não
  sobrescreve** e anota. É de **uma direção só** — refutar o lado oposto gravaria a menor,
  que é o estrago original. Detalhe em [docs/knowledge/pipeline-extracao.md](docs/knowledge/pipeline-extracao.md).
- 🔴 **Beneficiário Final vence Beneficiário/Cedente** (boleto securitizado) — **só quando há
  CNPJ** ao lado do rótulo; "Beneficiário Final" também é rótulo de COLUNA em dezenas de boletos.
  E o **CEDENTE do boleto vence o EMITENTE do CT-e** em fatura de transporte agregada.
- **Classificação contábil FORÇADA para guias tributárias** (por tipo/contexto do imposto, não pelo
  fornecedor) tem precedência máxima e faz write-back no `supplier` — exceto OTIMOTEX, funcionário
  e os `sk_supplier` excluídos.
- **Acrônimo de tributo no ASSUNTO sobrepõe a classificação do PDF/corpo** (DAR/DARE × GARE × GNRE
  são visualmente quase idênticas).
- 🔴 **`dar / dare` é UMA entrada, não duas** (133) — DAR e DARE nomeiam a mesma guia estadual e o
  acrônimo varia por estado. **`dar` é o acrônimo mais perigoso do domínio:** prefixo de `darf` **e**
  verbo comum ("dar baixa", "padaria"), então só é classificado por **rótulo explícito**
  (`_DOC_TYPE_NORM`, lookup EXATO) ou **frase** — nunca pela forma pura. Precedente: `das` e
  `dam / duam`. ⚠️ **`documento de arrecadacao estadual` NÃO é gatilho dele** — é o nome por extenso
  do **DAE** em PE e no CE, e incluí-la faria todo DAE virar DAR, sem erro.

### Caminho `email_body`

🔴 **MÚLTIPLAS PARCELAS/FATURAS no corpo ⇒ UMA conta por boleto, NUNCA somar.** Dispara só com ≥2
linhas e vencimentos ou (doc, parcela) distintos; a linha "Total" nunca vira conta. **Cada linha
leva o barcode do SEU segmento** — herdar o da primeira faria as demais colidirem na dedup e
sumirem.

🔴 **Barcode do corpo: a FORMA vence o RÓTULO.** Primeiro `_extract_body_linha_digitavel` (valida
os 5 campos FEBRABAN), só então o regex de rótulo — que aceita `[\d.\s]{47,60}` e, com `\s`
cruzando quebra de linha, pode COLAR números soltos em 48 dígitos e **inventar** um código de
arrecadação, envenenando a dedup.

🔴 **As guardas de barcode vivem na função CANÔNICA (`febraban.py`), não no leitor** — os dois
caminhos (PDF e corpo) precisam da mesma regra. `normalize_barcode` valida por padrão;
`normalize_barcode_allow_misread` é o opt-in explícito. ⚠️ **`allow_misread` NÃO é a última
palavra no caminho Vision:** ele só relaxa o **DV**, e o OCR produz código com os campos
DESLOCADOS, que nenhum DV pega — daí o `barcode_self_refuted` rodar depois dele.

**Corpo só-HTML** é convertido; **corpo PLACEHOLDER** também (exige o padrão do aviso **E** texto
curto, porque corpo curto legítimo é a norma aqui). **Conta vinda do corpo nasce `pendente`**,
mesmo que o texto diga "pagamento realizado".

🔴 **Confirmação de pagamento ENCAMINHADA** (assunto reescrito) é barrada no caminho do CORPO,
lendo o `Assunto:` original do bloco encaminhado. **`lembrete` encaminhado NÃO é barrado** — pode
ser a única fonte de uma fatura; reenvios repetidos são suprimidos pela dedup, não por este guard.

🔴 **`status_for_result`: CONTA GRAVADA ⇒ STATUS QUE DECLARA CONTA.** Nenhum sinal que descreve o
**anexo** pode ser avaliado antes dos dois sinais de conta. `body_created` estava abaixo de
`nonpayable` e escondeu **13 e-mails / ~R$ 80 mil** atrás do card "Ignorados". A guarda é o
invariante exaustivo (2^8) com anti-vacuidade dupla.

🔴 **`run_reader` registra TODOS os e-mails** — a keyword decide **o que extrair**, não o que
registrar. `match_keyword` casa acrônimo de tributo por **palavra inteira** (substring pegaria
"ca**das**tro"). A forma **negada** inverte o sentido: "**não** recebemos o seu pagamento" é
cobrança.

🔴 **Resolução de fornecedor:** CNPJ → CPF → nome → e-mail → auto-insert. **Identificador forte que
não casou ⇒ fornecedor NOVO** (o e-mail de uma **plataforma** atribuía a conta ao primeiro que
casasse). O CNPJ da própria pagadora nunca é fornecedor (raiz de 8 dígitos). Tipo de documento
nunca vira fornecedor.

🔴 **O E-MAIL do remetente ORIGINAL encaminhado SÓ IDENTIFICA, NUNCA CRIA** (fallback 1b,
migration 134). Ele diz quem **MANDOU** o documento, não quem **RECEBE** o pagamento — trocar
`find_supplier_by_email` (consulta pura) por `resolve_supplier` **compila** e faria cada
encaminhador virar fornecedor no 1º e-mail, pelo auto-insert. **Roda ANTES da regra de imposto**,
que faz `return True` incondicional e tornaria o bloco inalcançável para guia sem favorecido.
🔴 **Mas rodar antes dela é rodar antes do ASSUNTO** — daí a guarda de duas condições: em **guia
de tributo** o 1b vence (o assunto nunca foi fonte ali, a regra de imposto o curto-circuita); em
**qualquer outro** documento ele só entra se o assunto **não tiver âncora de sigla**, preservando a
lição da conta 401 (um intermediário cadastrado venceria "FATURAMENTO -- MOVVI LOGISTICA LTDA").
⚠️ O corpo chega por **parâmetro `body_text`** — `email_body_excerpt` NUNCA é povoado no caminho de
anexo, e é por isso que o **fallback 3 (por NOME) está morto ali** (não revivido de propósito: ele
desemboca no auto-insert).

🔴 **Fornecedor vindo de SINAL FRACO não faz WRITE-BACK no cadastro** (`_supplier_signal` +
`SUPPLIER_SIGNAL_WEAK`). A guia é do **Fisco**, não de quem a encaminhou: gravar a classificação
tributária no cadastro do encaminhador reescreveria a curadoria manual e valeria para **todas as
contas futuras** dele. A conta recebe a classificação; o cadastro, não. Antes do 1b o caso caía na
OTIMOTEX e a isenção existia **por acidente do destino** —
`TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS` é allowlist **reativa** e só protege quem já foi
descoberto. 🔴 **Chave efêmera nasce com `_` e é removida em `strip_transient_fields`, na FRONTEIRA
de gravação** (`register_financial` serializa o payload inteiro; chave que não é coluna faz o
PostgREST recusar o INSERT com **PGRST204** e a conta deixa de ser gravada).

🔴 **Empresa pagadora (`sk_company`) — a ORDEM é a regra:** menção a LE BLANC em qualquer grafia
e fonte, ou pagador com CNPJ raiz `20584679` (4, **vence a ester**) → remetente exato da FARDOS (3)
→ menção a "lebianco" (2, **vence o CNPJ**) → TECIDOS (1, default). **Assimetria deliberada:**
fornecedor LE BLANC classifica, fornecedor LEBIANCO não — e o sinal do fornecedor é capturado
**antes** de `_finalize_supplier`, que remove as colunas. A menção vale por **MENSAGEM**, como a
da LEBIANCO: nome ou texto de QUALQUER anexo marca todas as contas do e-mail. ⚠️ `OTIMOTEX_SK_SUPPLIER` (=1) ≠
`SK_COMPANY_DEFAULT` (=1) — tabelas diferentes; nunca find-replace nos dois.

### Documentos NÃO-pagáveis e casos de leitura

- **Baixa/cancelamento de RECEBÍVEL próprio** e **conteúdo visual sem valor** (assinatura de
  e-mail, apresentação de marketing) caem em `skipped_nonpayable` ⇒ e-mail `ignorado`, **não**
  `falha`, e sem logar erro. **Conservador:** o visual só dispara com `amount<=0` **E** sem código
  de barras — recibo legítimo tem valor e/ou linha digitável.
- 🔴 **Assinatura de e-mail é descrita pelo CONTEÚDO, não pelo termo.** O Vision descreve a
  `image001.png` do rodapé como "Rua … | CEP … | (37) 3249-4200", nunca como "assinatura". O
  detector exige **≥2 sinais de contato E nenhum termo financeiro** — qualquer sinal de documento
  real **desqualifica** o descarte.
- **PDF com texto ESPELHADO** (pdfplumber entrega cada linha invertida) vai ao **Vision**, que lê a
  página renderizada. A heurística é **por LINHA**, não por contagem global — o PDF é misto
  (páginas normais + a do boleto espelhada) e um placar agregado poderia empatar.
- **Split multi-pagável é por INSTRUMENTO DE PAGAMENTO**, não por "é boleto": linha digitável
  **ou** arrecadação de 48 **ou** PIX EMV. Página de detalhamento não tem instrumento ⇒ não conta.
  O gate `>=2` preserva "1 pagável ⇒ 1 registro".

🔴 **Guard anti-SSRF do download por link não se remove** — conteúdo de remetente desconhecido
controla a URL. Bloqueio por **IP interno** + revalidação de cada redirect + contenção em
`PDF_INBOX`. **NÃO há allowlist de portas** (barrava caminho legítimo em host público) — e não
reintroduzir: a proteção real é o teste de IP interno, e provado que o destino é externo, a porta
não dá acesso a serviço interno nenhum.

🔴 **`_PinnedHTTPSHandler` não pode referenciar `self._check_hostname`** — atributo removido no
Python 3.12+; sob o 3.14 (produção) quebrava **todo** download HTTPS, e o e-mail caía em `falha`.

**SSW (transportadoras):** preferir o link de **FATURA** (`F`, que traz o boleto) e **descartar os
DACTE** (`D`/`E`/`X`) — o 1º byte do `id` em hex→ASCII indica o tipo. Sem isso, baixava o DACTE
(sem linha digitável) e a conta caía em `ignorado` pela regra de transporte.

🔴 **Erro de código não se disfarça de "link inacessível"** — separar falha de **rede esperada** de
erro **inesperado** (`log.exception` com traceback). Um `except Exception` mudo escondeu um bug de
compatibilidade do Python 3.14 por dias.

**Robustez que não se remove:** IMAP com timeout e retry (sem timeout, um `fetch` que estanca
congela o run **para sempre**) · IMAP fechado em `try/finally` · Claude API com timeout ·
**extração IN-PROCESS** (o spawn de subprocesso a partir do Flask falhava com `rc=0xC0000142` em
100% dos casos) · `_rfc822_from_fetch` (o `imaplib` intercala respostas e `data[0][1]` devolve um
`int`).

**`extraction_source`** ∈ `pdf_text` · `pdf_vision` · `image_vision` · `docx_text` ·
`docx_vision` · `email_body` · `falha`. Rótulos amigáveis por `badgeLabel()`.

🔴 **`.docx`: TRÊS CAMADAS, nesta ordem** — texto do XML → maior imagem embutida via Vision →
falha explícita. **A camada 1 exige pagável PROVADO** (linha digitável, arrecadação ou PIX EMV):
um `.docx` não é documento financeiro por natureza, e mandar o texto de qualquer Word ao Claude
gastaria dinheiro em prosa e criaria conta espúria. Afrouxar depois é fácil; o inverso não.
🔴 **Sem parser XML, de propósito** — o texto sai por regex, o que torna XXE/billion-laughs
**estruturalmente impossíveis** sem `defusedxml`.
🔴 **`attachment_kind`/`attachment_ext` são a FONTE ÚNICA da seleção de anexo** — eram três cópias
que só podiam concordar por disciplina. O ramo `.docx` vem **antes** do de PDF (`is_pdf` casa
"pdf" em qualquer lugar do nome, e `boleto_pdf.docx` morreria no pdfplumber).
🔴 **`VISION_SOURCES` é a fonte única das fontes cuja resposta é JSON** — roteamento por tupla
literal deixaria a resposta do Vision cair no parser de TEXTO, produzindo registro vazio que o
pipeline leria como "documento sem valor", sem erro.
🔴 **A concatenação dos runs do Word é SEM separador dentro do parágrafo** — o Word parte a linha
digitável em vários `<w:t>`, e um `" ".join` a destruiria.

**Imagens e documentos fiscais**

- **Anexo de IMAGEM** (jpg/png/gif/webp) é documento (`image_vision`). Imagem **inline** só é
  processada como **fallback** quando não houve anexo nem PDF por link, e apenas a **maior**
  acima de 50 KB — logos e assinaturas ficam de fora.
- 🔴 **A validação da chave de acesso fiscal tem CINCO camadas e nenhuma é dispensável** — UF
  IBGE, mês 01-12, **ano em [2006, corrente+1]**, modelo no domínio e DV. Sequência aleatória
  fecha módulo 11 em ~1/11 dos casos: dos 8 barcodes de 44 dígitos não-boleto já gravados, **7 são
  lixo**, e um "Boleto de Aluguel" passou nas quatro primeiras camadas.
- 🔴 **NÃO reusar `barcode_dv_refuted` para a chave fiscal** — o DV do boleto fica na posição 4 com
  resto→1 e o da SEFAZ na 43 com resto→0; trocar um pelo outro devolve veredito plausível e
  errado, sem levantar erro.
- 🔴 **O SEPARADOR da chave inclui a BARRA** — transportadoras imprimem o CNPJ formatado dentro
  dela, o run de dígitos partia em 14+30 e **nenhum pedaço chegava a 44**. Eram **61 CT-e perdidos
  em silêncio**, com sintoma que se lê como "documento sem CT-e", não como falha de leitura.
- 🔴 **Classificar documento fiscal por ASSUNTO ou NOME DE ARQUIVO ERRA** — 34 PDFs com assunto
  `CT-e - NNNN` tinham registrado só a chave da NF-e citada dentro do DACTE. Toda contagem por
  grupo tem de sair do TEXTO do PDF.
- 🔴 **O gancho fiscal fica no Passo 1 e ANTES do `run_extraction`** — ponto único, cobre o
  documento mesmo quando a linha vira conta, e captura a chave ainda que a Claude API esteja fora.
  O registro é **não-fatal** e sem efeito colateral. **O gancho de CONTEÚDO roda DEPOIS do registro
  das chaves**: conteúdo é UPDATE, então antes do INSERT ele grava em nada — sem erro, com o log
  dizendo "registrado".
- 🔴 **A purga preserva o PDF fiscal** — CT-e sem boleto **nunca** tem linha em
  `financial_account_attachment` nem em `financial_account_control`, então sem consultar
  `fiscal_document.storage_key` a purga apagaria exatamente o que a Onda 3 registra. **72 objetos**
  só sobrevivem por causa dessa consulta.
- 🔴 **O parser de fatura de CT-e é FAIL-CLOSED pelo SUB-TOTAL impresso** — se a soma não bate,
  devolve **nada daquele PDF**. Os números são um **rateio**; rateio a que falta uma linha atribui
  frete ao conjunto errado e a soma passa a discordar da conta sem que nada acuse.

> ⚠️ **Um extrator que erra PARA MENOS não gera erro, não gera linha em `/erros` e não quebra
> teste** — ele produz um acervo menor que parece completo. A única verificação que o encontra é
> comparar o **conteúdo da fonte** com o que foi gravado.

---

## Banco de dados (Supabase)

Regras de DDL, GRANT/REVOKE, sondas e como aplicar: skill `.claude/skills/migrations-supabase`.
O que cada migration já aplicada fez: [docs/db/historico-migrations.md](docs/db/historico-migrations.md).
Caveats operacionais: `supabase/migrations/README.md`.

🔴 **O número da próxima migration se DERIVA** (`ls supabase/migrations | tail -1`), nunca de um
número escrito aqui. **Nunca reserve número com antecedência.**
🔴 **A base é COMPARTILHADA dev+prod** — migration aplicada vale para os dois; não há passo de
banco separado no deploy.

| Tabela | Propósito |
|---|---|
| `email_control` | Dedup/controle. `status` ∈ (`extraído`, `recebido`, `pendente`, `falha`, `ignorado`, `duplicidade`), calculado em `process_message` pelo resultado real. **Visibilidade por REMETENTE** (078) para grupo restrito. `body_preview` é truncado em 500 (é o preview da tela); **`body_full`** guarda o corpo inteiro — não unificar. **`body_search`** é `tsvector` GERADO de assunto+corpo. `body_full` **NULL = "ainda não temos o corpo"**, distinto de string vazia |
| `financial_account_control` | Tabela principal — uma linha por documento; alimentada pelo pipeline **e** por CRUD manual. Fornecedor só pela FK `sk_supplier`. **Classificação contábil** `cost_center_id`/`chart_account_id` (NOT NULL DEFAULT 0; id 0 = sentinela). **Autoria** `created_by` (DONO, base da visibilidade), `updated_by`, `status_changed_by/at` — carimbados pelo servidor/trigger. **`payment_date`** escrita SÓ pela trigger. 🔴 Essas colunas guardam só o **ÚLTIMO** autor; o histórico vive em `audit_log`. **Colunas GERADAS** (`competence_month`, `days_late`, `extraction_confidence`, `installment_number/base`) são só leitura e entram no `.omit()` do schema Zod |
| `financial_cost_center` / `financial_chart_of_account` | Cadastros de classificação (**preservar em limpezas**), com CRUD próprio. O plano relaciona-se ao centro — base da CASCATA; só os `is_postable` são lançáveis |
| `email_processing_errors` | Log de falhas com `raw_payload`. Visibilidade por remetente (078) |
| `financial_account_attachment` | **Anexos (N) de uma conta** — padrão único das duas origens (`pipeline` × `manual`). Soft delete; anexo de pipeline é irremovível. UNIQUE `(account_id, storage_key)`, **não** global (um PDF com N boletos gera N contas que compartilham o objeto) |
| `dim_date` | Calendário 2015-2045. Segue o calendário **BANCÁRIO**, não a letra da lei — o que importa é se o dinheiro anda. **Preservar em limpezas** |
| `audit_log` | Trilha de auditoria (Onda 7). UPDATE guarda o **delta**; DELETE guarda a **linha inteira** (única cópia que resta); UPDATE sem mudança real não gera linha. `registro_dono` desnormaliza o dono para a RLS **sobreviver à conta apagada**. **Alvo de limpeza** |
| `fiscal_document` | Documento fiscal pela chave de 44 dígitos. **Append-only**, tabela de PROVENIÊNCIA. 🔴 `storage_key` é o que faz a purga **preservar** o PDF |
| `supplier` | Fornecedores. Auto-criados, mas **cadastro PRESERVADO** (curadoria manual de e-mails, contatos e classificação default) — **nunca truncar** |
| `company` | Empresa pagadora. **Preservada em limpezas** |
| `status` | Dimensão de situação (ids 1..10) = domínio de `status_id`. **Preservar** |
| `user_group` | Catálogo de grupos + as **duas flags ortogonais**. Editado só via Supabase. **Preservar** |
| `cobranca_envios_log` / `cobranca_erros_log` | Logs da cobrança. `document_id` UNIQUE no primeiro = chave de dedup; o segundo **sem** UNIQUE (reprocessável). Alvo de limpeza |

🔴 **`status_id` é a FONTE ÚNICA da situação** — a coluna `status` (texto) foi removida (069). O
nome de exibição vem do embed `status_dim`. A trigger `fn_set_status_from_due_date` grava 3/2 a
partir de `due_date` × **a data de HOJE** e **apenas quando EM ABERTO** (`{1,2,3}`), preservando os
estados fechados.

> 🔴 **A trigger usava `extracted_at` (congelado) até a migration 095** — bug de fundação desde a
> 034: qualquer UPDATE numa conta em aberto recalculava contra a data de extração e **nunca**
> corrigia para `vencido`. Medido: das 123 contas que deveriam estar vencidas, só **3** persistiam.
> O `PATCH` respondia 200 e o valor final era decidido pela trigger.

**Schemas Zod (`packages/shared`) = fonte única de tipos.** Os tipos TS são `z.infer` (não há tipo
escrito à mão para divergir) e os `z.enum` espelham 1:1 os CHECK do banco — **ao alterar um CHECK,
atualizar o enum**. `financialAccountControlInputSchema` omite embeds de leitura e as colunas
geradas. **Colunas de pipeline/auditoria não são graváveis** por POST/PATCH manual — o Zod as
descarta (strip), protegendo a trilha e a dedup.

🔴 **Coluna GERADA nova tem de entrar no `.omit()`** — o PostgreSQL recusa com **428C9** qualquer
INSERT/UPDATE que cite coluna gerada. O `.pick()` de `manualEditSchema` já as excluiria, mas um
write path futuro que use o InputSchema direto quebraria a gravação.

🔴 **`payment_date` e as colunas de autoria são `nullable` na LEITURA e OMITIDAS no input** — quem
as grava é a trigger, não o cliente. `authenticated` **não** tem grant de coluna em `payment_date`.

**Invariantes da camada `analytics` (valem para toda função nova):**

- 🔴 **`SECURITY INVOKER` + `GRANT`/`REVOKE` explícitos nos dois sentidos** — o PostgreSQL concede
  `EXECUTE` a `PUBLIC` por default, então sem o `REVOKE` a função é chamável com a **anon key
  pública, sem login**. Já aconteceu com 4 funções recriadas pela 104.
- 🔴 **Despacho de parâmetro por `CASE` + `IN (...)`, nunca SQL dinâmico** — tudo viaja como bind,
  e valor fora do domínio devolve **vazio** em vez de agregar errado em silêncio.
- 🔴 **`cancelado` (id 9) fica fora dos totais**; "em aberto" é `status_id IN (1,2,3)`; e **aging é
  por `due_date < CURRENT_DATE`**, nunca pelo rótulo `status_name`, que é defasado pela trigger +
  batch diário.
- 🔴 **Âncora de teste NÃO pode ser número absoluto** — o dado deriva em 24 h. Usar oráculo
  diferencial (tool × query de controle) ou janela histórica fechada.
- 🔴 **`service_role` precisa de GRANT explícito para gravar `ai_chat_log`** — ele burla RLS mas
  **não é superuser**; sem `USAGE` + `INSERT` o write falha com `42501` e, como `logInteraction`
  nunca lança, o pilar de auditoria ficaria **morto em produção sem nenhum sintoma**. Toda tabela
  nova em `analytics` repete esse cuidado.

**Invariantes da trilha de auditoria (`audit_log`):**

- 🔴 **A trigger de linha é AFTER; a de TRUNCATE é BEFORE.** As 5 triggers da fato são **todas
  BEFORE** e alteram `NEW`: auditar antes delas gravaria o valor que ainda será sobrescrito —
  registro plausível e **falso**. Já o TRUNCATE precisa ser BEFORE, senão a contagem de linhas
  destruídas seria inalcançável.
- 🔴 **`fn_audit_row` é `SECURITY DEFINER`, e isso não é estilo** — `authenticated` teve INSERT
  revogado em `audit_log`; uma trigger INVOKER faria **toda a curadoria de `/consulta`** quebrar
  com `permission denied` (regressão classe 074).
- 🔴 **`OLD.updated_by` NUNCA é fonte de ator** — ele é o editor ANTERIOR, e usá-lo atribuiria a um
  humano uma mudança que ele não fez: **acusação falsa**, pior que ausência de dado. Ordem:
  `auth.uid()` → header `x-audit-actor` → GUC → NULL. **O JWT vir primeiro é invariante de
  SEGURANÇA** — com o header antes, um usuário logado forjaria a assinatura de outro.
- 🔴 **O ator vem de FORA e pode vir malformado — validar antes de converter.** Um valor não-uuid
  faz o `::uuid` levantar 22P02 e, sendo a trigger fail-closed, **derruba a gravação da conta**.
  A distinção: fail-closed vale para o **registro** da auditoria, não para **interpretar** uma dica
  de atribuição não-confiável.
- 🔴 **A policy espelha a 076 por `registro_dono` DESNORMALIZADO** — o padrão `EXISTS` dos anexos
  não serve: a linha de auditoria precisa **sobreviver à conta apagada**, e com `EXISTS` o registro
  de DELETE ficaria invisível exatamente quando passa a ser a única cópia.
- 🔴 **Coluna de escrituração e coluna GERADA ficam FORA do delta** — ninguém "alterou"
  `days_late`, que é consequência. Coluna gerada nova precisa entrar nessa lista.
- 🔴 **USUÁRIO REMOVIDO ≠ AUTOMAÇÃO — são TRÊS estados.** Um `COALESCE` local fazia a ação humana
  de um usuário apagado ser contada junto com o batch: a trilha não **perdia** o evento, ela o
  **reatribuía** a uma categoria que inocenta todo mundo, sem erro. Fonte única:
  `analytics.audit_actor_label`.
- ⚠️ **`criado_em` é o timestamp da TRANSAÇÃO** — eventos de uma ação em lote compartilham o
  instante e o desempate é um uuid aleatório. Para achar UM evento, filtre pelo **conteúdo**,
  nunca por "o mais recente".

**RLS:** leitura `TO authenticated`, escrita `TO service_role`. Exceções deliberadas: leitura por
**DONO** (076) e por **REMETENTE** (078), e UPDATE por `authenticated` com **grant restrito às
colunas** `has_invoice`/`has_bank_slip`/`status_id` (curadoria inline) e `reviewed_at`.

🔴 **São DUAS travas independentes:** o GRANT por coluna diz **quais colunas**; o predicado da
policy diz **quais linhas**. As policies de UPDATE eram `USING (true)` e passaram a usar o MESMO
predicado do SELECT (081) — o usuário só edita o que a tela lhe mostra.

🔴 **A visibilidade do STORAGE herda a da conta** (080). A policy original era
`USING (bucket_id='attachments')` **sem filtro de dono**: o grupo restrito via 36 contas na tela e
alcançava os **565 objetos** do bucket pela API — e a chave é obtível pelas rotas de conta/anexo.
Hoje o objeto só é liberado se existir uma linha **visível para o próprio usuário**; as
subconsultas rodam como `authenticated`, então a policy **herda a regra sozinha**.

🔴 **Duas chaves Supabase, dois papéis:** `anon` (frontend, respeita RLS) e `service_role` (scripts
Python/Flask e Next API, **ignora RLS**). É por isso que `canSeeConta` existe do lado da API.

**Baixa automática no ATO da edição:** ao marcar a 2ª flag (NF ou BOL) de uma conta vencida e em
aberto, o frontend também grava `status_id = pago` — best-effort (falha não reverte a flag; o
batch diário reconcilia). A regra vive em `qualifiesForAutoPago` e **espelha** o batch Python.
🔴 **O update otimista espelha também a trigger de `payment_date`** (preenche ao entrar em pago,
limpa ao sair) — sem isso a linha ficaria "paga sem data" até o próximo refresh.

🔴 **Objeto novo nasce gravável — o `REVOKE` faz parte da migration.** O Supabase concede
INSERT/UPDATE/DELETE a `authenticated` em toda tabela/VIEW nova, e em view
`security_invoker = false` **a RLS não salva** (foi escalada de privilégio real na `app_user`).
⚠️ **Tabela criada pelo Table Editor do dashboard nasce gravável e truncável por `anon`** — o
default do papel `supabase_admin` não é alterável sem superuser. **Crie tabela sempre por
migration.**

### Limpeza / reset de dados (SEMPRE preservar os cadastros)

**Preservar:** `company` · `status` · `supplier` · `financial_account` · `financial_bank` ·
`financial_chart_of_account` (+ grupo e subgrupo) · `financial_cost_center` · `user_group` ·
`dim_date`.

**Alvos** (`TRUNCATE ... RESTART IDENTITY CASCADE`): `email_control`,
`financial_account_control`, `email_processing_errors`, `audit_log` — e, para testes de cobrança,
`cobranca_envios_log` + `cobranca_erros_log`. Mais o bucket **`attachments`** e o cache local
(`data/pdfs_inbox`, `data/csv_output`). `financial_account_attachment` não precisa entrar: a FK é
`ON DELETE CASCADE`.

🔴 **`supplier` NÃO é alvo** — acumula curadoria manual, é a fonte da busca por fornecedor em
`/consulta`, e truncá-lo com `RESTART IDENTITY` desalinharia `sk_supplier` das contas.

🔴 **A ORDEM importa (Onda 7):** `TRUNCATE` em `financial_account_control` dispara a trigger que
**grava em `audit_log`** quantas linhas foram destruídas — truncar a `audit_log` **depois** apagaria
essa prova. Truncar a `audit_log` **primeiro**, ou aceitar perder o registro da própria limpeza.

**Storage:** `DELETE` por `authenticated` é bloqueado por policy RESTRICTIVE (073). Esvaziar o
bucket via Storage API com `SUPABASE_SERVICE_KEY`.

---

## Pipelines agendados

O que cada um faz, o que copiar e como validar: skill `.claude/skills/deploy-producao`
(`pipelines.md`). Status do último deploy: [progress.md](progress.md).

| Pipeline | Papel | Frequência |
|---|---|---|
| **Email Reader** | entrada — IMAP → extração → Supabase | 5 min |
| **Cobrança de vencidos** | saída — Firebird → SMTP Locaweb | 10:00 |
| **Backup do Supabase** | infra — `pg_dump` + bucket | 02:00 |
| **Baixa automática** | reconciliação — 2 regras independentes | 08:00 |
| **Gatilhos do roadmap** | medição mensal dos 7 gatilhos da Onda 9 | dia 1, 07:00 |

> 🔴 **A máquina de PRODUÇÃO fica em OUTRO LOCAL FÍSICO e o Claude NUNCA executa nada nela.**
> `C:\Sheild\API\Pagamentos` não existe no ambiente de desenvolvimento e **não é clone git** (não
> há `git pull` lá). Toda cópia de arquivo, `setup-*-task.ps1`, `pip install` ou reinício é feita
> **pelo próprio usuário, manualmente** — não tente, não se ofereça para tentar, não simule que foi
> feito. Seu trabalho termina no código correto no repositório e nas instruções copiáveis.
>
> **`scheduler/check_deploy_parity.py` é a fonte da verdade de "produção está atualizada?"** —
> compara o SHA-256 dos arquivos de deploy com o manifesto e sai com **exit 1** em divergência.
> 🔴 No DEV, após alterar qualquer script de deploy, **regrave o manifesto no mesmo commit**
> (`--update`) — ele **viaja junto** com os `.py` na cópia.

**Baixa automática — duas regras independentes** (falha numa não impede a outra; não é transação):

| Regra | Condição | Efeito |
|---|---|---|
| **1 — Baixa** | `has_invoice` **e** `has_bank_slip` **e** `due_date <= hoje` **e** `status_id ∈ {1,2,3}` | → `pago` (8) |
| **2 — Vencidos** | `status_id ∈ {1,3}` **e** `due_date < hoje` (**estritamente**) | → `vencido` (2) |

🔴 **Por que o batch existe:** a trigger só recalcula em INSERT/UPDATE da linha — sem nenhuma
edição, uma conta não transiciona sozinha com o passar do dia.
🔴 **A Regra 1 é ESPELHADA no frontend** (`qualifiesForAutoPago`, que sai pelo Vercel). Ao mudar
essa regra, ajuste **os dois lados**.
🔴 **Setar `status_id=2` só é seguro DEPOIS da 095** — o `2` **está** em `{1,2,3}`, então a trigger
recalcula a cada UPDATE e, com a referência congelada, revertia para `3` **na mesma transação**.
O `PATCH` respondia 200 e o valor final era decidido pela trigger.

🔴 **A cobrança classifica falha por REGRA DE NEGÓCIO:** `smtp_falha` = instabilidade (retenta
sozinho); `smtp_bloqueio` = negação (exige ação humana). Mensagens de `error_message` são
**leigas** (a coluna "Motivo" da UI); o técnico vai em `error_detail`.
🔴 **O exit code separa DADO de OPERAÇÃO** — cliente sem e-mail no Firebird **não reprova** a
tarefa; antes, um run 100% OK com clientes sem e-mail aparecia como `0x1` no Agendador.
🔴 **To primeiro; se o principal falhar, o Cc NÃO é enviado.** ⚠️ `smtplib.SMTPException` herda de
`OSError` — o catch de queda usa `(SMTPServerDisconnected, ConnectionError, TimeoutError)`,
**nunca** `OSError`, senão uma recusa definitiva (451/5xx/auth) seria reenviada.
🔴 **Segurança do e-mail (não regredir):** `html.escape` nos campos vindos do Firebird, Subject
normalizado (`_strip_crlf`) e Cc com quebra de linha **descartado** — no header **e** no envelope.
STARTTLS com `minimum_version = TLSv1_2`.

⚠️ **`send_core.py` é o núcleo compartilhado** pelo batch e pelo reenvio manual — não duplicar
entre os fluxos.
⚠️ **Backup é COMPLETO todo dia** (re-baixa o bucket inteiro), não incremental; e `pg_dump ≥ 17` é
pré-requisito **externo** na máquina.

🔴 **Guarda CSRF dos endpoints de disparo** (`_reject_trigger_request` em `server/app.py`): exigem
`Content-Type: application/json` (→ 415) e, com `FLASK_TRIGGER_TOKEN` no `.env`, o header
`X-Trigger-Token` (→ 401). A barreira primária continua sendo o bind em `127.0.0.1`.
