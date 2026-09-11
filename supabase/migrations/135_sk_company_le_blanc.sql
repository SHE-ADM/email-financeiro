-- 135_sk_company_le_blanc.sql
-- 4a empresa pagadora: LE BLANC ADMINISTRACAO DE BENS PROPRIOS LTDA (sk_company = 4,
-- CNPJ 20584679000110). Backfill das contas ja extraidas — decisao do usuario (2026-09-11):
--
--   1o  mencao a LE BLANC / pagador LE BLANC -> sk_company = 4 (LE BLANC)   <- VENCE tudo
--   2o  remetente ester@otimotex.com.br      -> sk_company = 3 (OTIMOTEX FARDOS)
--   3o  referencia a "lebianco"              -> sk_company = 2 (LEBIANCO)
--   4o  nenhum dos anteriores                -> sk_company = 1 (OTIMOTEX TECIDOS)
--
-- A regra em si vive no Python (read_emails.py: resolve_sk_company/_LE_BLANC_RE) — esta
-- migration so corrige os dados ja gravados, espelhando as fontes que o banco guarda.
--
-- Projeto: pagamentos | Data: 2026-09-11
--
-- SEM DDL: a linha sk_company=4 ja existe em `company` (cadastrada pelo usuario) e o trigger
-- da 084 respeita sk_company explicito (e e no-op no UPDATE) — o valor GRUDA e a proxima
-- curadoria nao o reverte. Idempotente (IS DISTINCT FROM 4): re-run reporta UPDATE 0.
--
-- GRAFIAS: "le" + separador opcional (espaco/_/./-) + "blanc" — casa "LE BLANC", "LEBLANC",
-- "Le_Blanc" (nome de arquivo). A fronteira a direita impede "LEBLANCO" (OCR de LEBIANCO).
-- O texto CRU dos anexos nao fica no banco: o que o Python ve ali, o backfill nao ve.
--
-- FORNECEDOR: diferente da 084 (LEBIANCO), o fornecedor LE BLANC TAMBEM classifica —
-- decisao explicita do usuario: qualquer mencao vale.
--
-- Esperado na aplicacao (medido em 2026-09-11): 4 contas — 348, 759, 1350, 1351.
-- A conta 1468 ja esta em 4 (curadoria manual) e nao e tocada.

BEGIN;

-- Trava DELIBERADA: abortar aqui e o objetivo. Se sk_company=4 nao for a LE BLANC, o
-- UPDATE abaixo gravaria FK valida para a empresa ERRADA, sem erro nenhum.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1
                   FROM company
                  WHERE sk_company = 4
                    AND cnpj       = '20584679000110') THEN
    RAISE EXCEPTION '135: company.sk_company = 4 nao e a LE BLANC (cnpj 20584679000110) — abortado';
  END IF;
END $$;

WITH p AS (
  SELECT '(^|[^[:alnum:]])le[[:space:]_.-]*blanc([^[:alnum:]]|$)'::text AS re
)
UPDATE financial_account_control f
   SET sk_company = 4
  FROM p
 WHERE f.sk_company IS DISTINCT FROM 4
   AND (   f.payer_cnpj         LIKE '20584679%'
        OR f.subject            ~* p.re
        OR f.sender_email       ~* p.re
        OR f.email_body_excerpt ~* p.re
        OR f.description        ~* p.re
        OR f.source_file        ~* p.re
        OR f.payer_name         ~* p.re
        OR EXISTS (SELECT 1
                     FROM email_control e
                    WHERE e.message_id = f.gmail_message_id
                      AND (   e.subject                           ~* p.re
                           OR e.sender_email                      ~* p.re
                           OR coalesce(e.body_full, e.body_preview) ~* p.re
                           OR e.attachment_names                  ~* p.re))
        OR EXISTS (SELECT 1
                     FROM supplier s
                    WHERE s.sk_supplier = f.sk_supplier
                      AND (   s.cnpj       LIKE '20584679%'
                           OR s.legal_name ~* p.re
                           OR s.trade_name ~* p.re)));

COMMIT;

-- ============================================================================
-- VERIFICACAO (rodar apos aplicar):
--
--   -- (1) distribuicao (esperado em 2026-09-11: 4 -> 5 contas):
--   SELECT sk_company, count(*) FROM financial_account_control GROUP BY 1 ORDER BY 1;
--
--   -- (2) as contas movidas:
--   SELECT id, sk_company, payer_cnpj, left(subject, 60)
--     FROM financial_account_control WHERE sk_company = 4 ORDER BY id;
--   -- 348, 759, 1350, 1351, 1468
--
--   -- (3) o backfill GRUDA (fix do trigger da 084): UPDATE no-op nao reverte a empresa:
--   BEGIN;
--     UPDATE financial_account_control SET has_invoice = has_invoice WHERE id = 348;
--     SELECT sk_company FROM financial_account_control WHERE id = 348;   -- 4
--   ROLLBACK;
--
--   -- (4) idempotencia: reaplicar esta migration -> UPDATE 0.
-- ============================================================================
