# Code Review — Features / empresa pagadora LE BLANC (2026-09-11)

## Resumo
Alvo: regras novas da company LE BLANC (sk_company = 4, migration 135) — docs/knowledge/pipeline-extracao.md § "Empresa pagadora LE BLANC"
Modo: light (sem passo de ataque, sem verificação adversarial)
Delta: 30 arquivos alterados, 2 novos (135_sk_company_le_blanc.sql, test_sk_company_le_blanc.py), +395/−105 linhas versionadas + 389 linhas novas
Régua: CLAUDE.md (raiz), .claude/skills/pipeline-extracao/SKILL.md, docs/knowledge/pipeline-extracao.md, docs/padrao-execucao.md
Gates: pytest 1637 passed · Vitest api-backend 620 · Vitest shared 53 · lint 0/0 · typecheck OK · check_deploy_parity 32/32 · Vitest frontend-vite não executado (delta ali é só comentário) · e2e não executado (exige navegador) · SonarCloud não executado (roda no CI)

A regra LE BLANC está correta e bem posicionada: precedência 4 → 3 → 2 → 1 implementada em
ponto único (`resolve_sk_company`), sinal do fornecedor capturado antes de `_finalize_supplier`
nos dois call sites, regex com fronteira por lookaround (protege contra "LEBLANCO"), `None`-safe,
migration com trava `DO $$` e idempotente. Nenhum bloqueante na regra-alvo. Dois testes não
travam o que prometem, e o diff completo carrega um risco fora do alvo, na query da cobrança.

## Achados

### 🔴 Bloqueantes
Nenhum.

### 🟡 Recomendados

- [skills/cobranca-vencidos/scripts/db_firebird.py:38-43 e 56-61] A exclusão por grupo usa `CD_GP_NO <> '...'`, que descarta também toda linha com `CD_GP_NO` NULL.
  Falha:     cliente SEM grupo econômico (CD_GP_NO NULL) → `NULL <> 'INBRANDS'` é UNKNOWN → título vencido excluído da cobrança, sem erro nem log.
  Evidência: diff substitui `CD_ID <> 8949` por 6 predicados `<>` sem tratamento de NULL; o deploy de 2026-08-31 registra queda de 115–185 títulos/dia para 18, compatível com o filtro pretendido E com a perda por NULL — os dois não são distinguíveis sem consultar o Firebird.
  Correção:  `AND (PK.CD_GP_NO IS NULL OR PK.CD_GP_NO NOT IN ('INBRANDS','RESTOQUE','SHOULDER','SKAI','SOMA','LOJAS MEL'))` nos dois blocos do UNION — só após confirmar a nulidade no banco.
  Regra:     CLAUDE.md global § Critérios obrigatórios ("tratar nulos … de forma explícita").

- [tests/test_sk_company_le_blanc.py:37] `test_valores_espelham_a_tabela_company` promete espelhar a tabela `company`/migration 135, mas compara a constante com o literal `4` digitado no próprio teste.
  Falha:     alguém muda a trava da 135 (ou `SK_COMPANY_LE_BLANC`) para outro par sk/CNPJ → o teste continua verde, porque nunca lê a outra camada.
  Evidência: corpo do teste = `assertEqual(LE_BLANC, 4)` + `CNPJ_LE_BLANC.startswith(...)`, ambos com valores locais.
  Correção:  ler a trava `DO $$` da migration 135 (par `sk_company = N AND cnpj = '...'`), com sanidade do parser, e comparar com `R.SK_COMPANY_LE_BLANC` e `R.LE_BLANC_CNPJ_ROOT`.
  Regra:     CLAUDE.md § Regra 2 ("Teste que promete uma garantia tem de entregá-la"; "guarda que faz parsing leva sanidade do parser").

- [skills/email-reader/scripts/read_emails.py:5379] O ramo que marca a mensagem pelo NOME de outro anexo (`pdf_path.name`) não é travado por nenhum teste.
  Falha:     remover `pdf_path.name` do `_has_le_blanc_reference(...)` → e-mail com `BOLETO_LEBLANC.pdf` + outro anexo cujo pagável vem do segundo arquivo passa a gravar a conta em 1 — e a suíte continua verde.
  Evidência: mutante aplicado → `tests/test_sk_company_le_blanc.py` 28/28 verdes. O caso `test_nome_do_arquivo_anexo` passa pelo `source_file` do próprio payload, não pela flag da mensagem.
  Correção:  caso de call site com dois anexos, nome LE BLANC num e linha extraída do outro.
  Regra:     CLAUDE.md § Regra 2 ("Validação por mutante").

### 🔵 Opcionais
- [supabase/migrations/135_sk_company_le_blanc.sql:50,67] `LIKE '20584679%'` não exige 14 dígitos, ao contrário de `_is_le_blanc_cnpj` — divergência de critério entre backfill e regra viva (migration já aplicada; só registrar).
- [skills/email-reader/scripts/read_emails.py:5360] `body_text` inclui o histórico citado de respostas/encaminhamentos: um reply a uma thread que um dia citou a LE BLANC marca todas as contas como 4 — risco mais largo que o de "assinatura" já aceito no doc (decisão de negócio).
- [apps/frontend-vite/src/components/organisms/AiChatPanel.tsx:102] Sugestão do chat "Compare os gastos entre OTIMOTEX TECIDOS, LEBIANCO e OTIMOTEX FARDOS" não cita a LE BLANC.

## Pendências (trabalho incompleto)
- [working tree] `read_emails.py` e a migration 135 estão em PRODUÇÃO (paridade 32/32) mas **não commitados** — o que roda em produção não existe em nenhum commit. Três intenções misturadas (LE BLANC, vínculo de anexo no dedup, cobrança 10:00/CD_GP_NO) — recomendada (commits separados por intenção, quando você pedir).
- [apps/api-backend/lib/ai-chat/tools.ts, gateway.ts] Filtro do chat pela empresa 4 aguarda PR para `main` (já registrado em progress.md) — recomendada.
- [db_firebird.py] Verificação da nulidade de `CD_GP_NO` no Firebird — recomendada (ver achado).

## Drift código × documentação
- packages/shared/src/schemas/financial-account-control.schema.ts:347-352 lista só 1/2/3 e descreve a origem como "regra LEBIANCO" — decisão pendente do usuário.
- docs/arquitetura-chat-ia-pagamentos.md:456 e :554 falam em "três empresas" (1/2/3) — decisão pendente do usuário.
- apps/frontend-vite/src/hooks/useCompanyOptions.ts:2 descreve o cadastro como "OTIMOTEX/LEBIANCO" — decisão pendente do usuário.
- apps/frontend-vite/src/components/organisms/ContaForm.test.tsx:52 comenta "as 3 empresas" — decisão pendente do usuário.

## Não coberto
- Vitest do frontend-vite e e2e (Playwright) não executados; SonarCloud só no CI.
- Migration 135 não reexecutada nem consultada no banco (distribuição 4 → 5 contas não reconferida).
- Nulidade de `CD_GP_NO` não verificada — exige acesso ao Firebird.
- Arquivos de documentação da cobrança/DKIM lidos pelo diff, sem conferência externa do DNS.
- `resolve_company_sk` (trigger da 084, INSERT com sk_company NULL) não reavaliada para a raiz da LE BLANC — o CRUD manual exige `sk_company` positivo, então o caminho é residual.
