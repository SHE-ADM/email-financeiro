"""
test_sk_company_le_blanc.py — empresa pagadora LE BLANC (sk_company = 4).

Decisao do usuario (2026-09-11), por PRECEDENCIA:
  1o mencao a LE BLANC (qualquer grafia/fonte, inclusive o FORNECEDOR) ou pagador impresso
     com a raiz de CNPJ 20584679                               -> 4 (LE BLANC)   <- VENCE tudo
  2o remetente ester@otimotex.com.br                            -> 3 (OTIMOTEX FARDOS)
  3o referencia a "lebianco"                                    -> 2 (LEBIANCO)
  4o nenhum dos anteriores                                      -> 1 (OTIMOTEX TECIDOS)

Assimetria DELIBERADA: fornecedor LEBIANCO nao classifica; fornecedor LE BLANC classifica.

Duas camadas: funcoes PURAS com literais e o CALL SITE executado (extract_and_store_accounts
e try_extract_from_body) — testar a funcao pura nao prova que o pipeline a chama com o sinal
certo, e o fornecedor so existe no payload ANTES de _finalize_supplier.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "skills" / "email-reader" / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import read_emails as R  # noqa: E402

LE_BLANC = R.SK_COMPANY_LE_BLANC   # 4
FARDOS = R.SK_COMPANY_FARDOS       # 3
LEBIANCO = R.SK_COMPANY_LEBIANCO   # 2
OTIMOTEX = R.SK_COMPANY_DEFAULT    # 1
CNPJ_LE_BLANC = "20584679000110"
CNPJ_OTIMOTEX = "47273917000123"
ESTER = "ester@otimotex.com.br"

_MIGRATION_135 = _REPO_ROOT / "supabase" / "migrations" / "135_sk_company_le_blanc.sql"
# Par (sk, cnpj) da trava DO $$ da 135: "WHERE sk_company = N AND cnpj = '<14 digitos>'".
_GUARD_RE = re.compile(r"sk_company\s*=\s*(\d+)\s+AND\s+cnpj\s*=\s*'(\d{14})'", re.IGNORECASE)


class ConstantesTest(unittest.TestCase):
    def test_valores_espelham_a_trava_da_migration_135(self):
        # Le a OUTRA camada: a trava da 135 e o que garante, no banco, que o sk 4 e a LE BLANC.
        # Se o Python e a migration divergirem, o pipeline grava FK valida para a empresa errada.
        pares = _GUARD_RE.findall(_MIGRATION_135.read_text(encoding="utf-8"))
        # Sanidade do parser: sem isto um regex que parasse de casar viraria verde para sempre.
        self.assertEqual(len(pares), 1, f"trava da 135 nao encontrada/ambigua: {pares}")
        sk, cnpj = pares[0]
        self.assertEqual(int(sk), R.SK_COMPANY_LE_BLANC)
        self.assertTrue(cnpj.startswith(R.LE_BLANC_CNPJ_ROOT), cnpj)
        self.assertTrue(R._is_le_blanc_cnpj(cnpj))


class HasLeBlancReferenceTest(unittest.TestCase):
    def test_grafias_que_casam(self):
        for texto in ("LE BLANC", "LEBLANC", "Le Blanc", "le_blanc", "Le-Blanc", "Le.Blanc",
                      "LE   BLANC", "x@leblanc.com.br",
                      "Pagamento de boleto - Certificado Digital LE BLANC HOLDING S A",
                      "NOTA_FISCAL_LE_BLANC.pdf", "barbara_BOLETO_LEBLANC_-_POR.pdf",
                      "ENC: Le Blanc - Boleto Porto Saúde - 2026 07"):
            with self.subTest(texto=texto):
                self.assertTrue(R._has_le_blanc_reference(texto))

    def test_grafias_que_NAO_casam(self):
        # LEBLANCO: leitura OCR plausivel de LEBIANCO (outra empresa) — a fronteira a direita
        # e o que impede. "blanc" sozinho e cor/tecido comum num fornecedor textil.
        for texto in ("LEBLANCO", "LE BIANCO", "LEBIANCO PLASTICOS", "TECIDO BLANC",
                      "leblancx", "xleblanc", "blanc", "Le Blanche", "PAGAMENTO BOLETO"):
            with self.subTest(texto=texto):
                self.assertFalse(R._has_le_blanc_reference(texto))

    def test_none_e_vazio_sao_ignorados(self):
        self.assertFalse(R._has_le_blanc_reference(None, "", "boleto"))
        self.assertTrue(R._has_le_blanc_reference(None, "", "ref le blanc"))


class IsLeBlancCnpjTest(unittest.TestCase):
    def test_cnpj_puro_e_formatado(self):
        self.assertTrue(R._is_le_blanc_cnpj(CNPJ_LE_BLANC))
        self.assertTrue(R._is_le_blanc_cnpj("20.584.679/0001-10"))

    def test_filial_da_mesma_raiz(self):
        self.assertTrue(R._is_le_blanc_cnpj("20584679000290"))

    def test_outros_cnpjs(self):
        self.assertFalse(R._is_le_blanc_cnpj(CNPJ_OTIMOTEX))
        self.assertFalse(R._is_le_blanc_cnpj(None))
        self.assertFalse(R._is_le_blanc_cnpj(""))
        self.assertFalse(R._is_le_blanc_cnpj("20584679"))   # so a raiz nao e CNPJ

    def test_cnpj_deslocado_por_ocr_nao_casa(self):
        # Caso real (conta 759): digito deslocado. Quem classifica ali e o assunto.
        self.assertFalse(R._is_le_blanc_cnpj("02058467900011"))


class PrecedenciaTest(unittest.TestCase):
    """NAO REGREDIR a ordem: LE BLANC -> ester -> lebianco -> OTIMOTEX."""

    def test_le_blanc_vence_a_ester(self):
        self.assertEqual(R.resolve_sk_company(sender_email=ESTER, subject="Boleto Le Blanc"),
                         LE_BLANC)

    def test_le_blanc_vence_a_ester_pela_flag_e_pelo_cnpj(self):
        self.assertEqual(R.resolve_sk_company(sender_email=ESTER, le_blanc=True), LE_BLANC)
        self.assertEqual(R.resolve_sk_company(sender_email=ESTER, payer_cnpj=CNPJ_LE_BLANC),
                         LE_BLANC)

    def test_le_blanc_vence_lebianco(self):
        self.assertEqual(
            R.resolve_sk_company(subject="LEBIANCO", body_text="conta da LEBLANC",
                                 pdf_lebianco=True),
            LE_BLANC)

    def test_ester_sem_mencao_continua_fardos(self):
        self.assertEqual(R.resolve_sk_company(sender_email=ESTER, subject="PAGAMENTO LEBIANCO"),
                         FARDOS)

    def test_lebianco_sem_mencao_continua_lebianco(self):
        self.assertEqual(R.resolve_sk_company(subject="NF LEBIANCO 998"), LEBIANCO)

    def test_nada_e_otimotex(self):
        self.assertEqual(R.resolve_sk_company(), OTIMOTEX)
        self.assertEqual(R.resolve_sk_company(payer_cnpj=CNPJ_OTIMOTEX,
                                              subject="PAGAMENTO BOLETO"), OTIMOTEX)


class FontesTest(unittest.TestCase):
    def test_cada_fonte_classifica_sozinha(self):
        casos = {
            "subject": "ENC: Le Blanc - Boleto Porto Saude",
            "body_text": "segue boleto da LE BLANC",
            "sender_email": "financeiro@leblanc.com.br",
            "description": "Seguro saude LE BLANC",
            "source_file": "NOTA_FISCAL_LE_BLANC.pdf",
            "payer_name": "LE BLANC ADMINISTRACAO DE BENS PROPRIOS LTDA",
            "email_body_excerpt": "pagar pela leblanc",
            "payer_cnpj": CNPJ_LE_BLANC,
        }
        for campo, valor in casos.items():
            with self.subTest(campo=campo):
                self.assertEqual(R.resolve_sk_company(**{campo: valor}), LE_BLANC)


class FornecedorLeBlancTest(unittest.TestCase):
    """Decisao 2026-09-11: fornecedor LE BLANC TAMBEM classifica (qualquer mencao vale)."""

    def test_supplier_name(self):
        payload = {"subject": "ALUGUEL", "supplier_name": "LE BLANC ADMINISTRACAO"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], LE_BLANC)

    def test_supplier_cnpj(self):
        payload = {"subject": "ALUGUEL", "supplier_cnpj": CNPJ_LE_BLANC}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], LE_BLANC)

    def test_assimetria_fornecedor_lebianco_nao_classifica(self):
        # Trava a assimetria: a regra antiga da LEBIANCO segue valendo.
        payload = {"subject": "PAGAMENTO", "sender_email": "x@fornecedor.com.br",
                   "supplier_name": "LEBIANCO PLASTICOS"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)

    def test_respeita_valor_ja_presente(self):
        payload = {"subject": "Le Blanc", "sk_company": OTIMOTEX}
        R.apply_sk_company(payload, le_blanc=True)
        self.assertEqual(payload["sk_company"], OTIMOTEX)


# ---------------------------------------------------------------------------
# Call site EXECUTADO — extract_and_store_accounts / try_extract_from_body
# ---------------------------------------------------------------------------
class FakeControl:
    """Stub minimo de SupabaseControl (molde de test_boleto_dedup_suppresses_body)."""

    def __init__(self):
        self.financial_calls = []
        self.error_calls = []

    def upload_attachment(self, pdf_path):
        return True

    def company_cnpj(self):
        return CNPJ_OTIMOTEX

    def register_financial(self, payload):
        self.financial_calls.append(dict(payload))
        return len(self.financial_calls)

    def register_attachment(self, account_id, file_name, size_bytes=0, uploaded_by=None):
        return True

    def resolve_user(self, sender_email):
        return None

    def register_error(self, email_rec, error_type, error_message, raw_payload=None):
        self.error_calls.append((error_type, error_message))
        return True

    def unique_invoice_number(self, base):
        return base

    def find_financial_duplicate(self, payload):
        return None

    def resolve_supplier(self, payload):
        return 777

    def supplier_defaults(self, sk_supplier):
        return (0, 0)

    def update_supplier_contact(self, *args, **kwargs):
        return True


def _row(**over):
    row = {
        "source_file": "boleto.pdf",
        "document_type": "boleto",
        "barcode": "",
        "amount": "190.00",
        "supplier_name": "FORNECEDOR QUALQUER LTDA",
        "invoice_number": "123456",
        "due_date": "2026-09-30",
        "extraction_source": "pdf_vision",
    }
    row.update(over)
    return row


def _run(row, subject="PAGAMENTO BOLETO", sender="barbara@otimotex.com.br",
         attachment_text="", body_text=""):
    ctrl = FakeControl()
    saved = [Path(row["source_file"])]

    def fake_run_extraction(pdf_path, pdf_passwords=None):
        return (pdf_path.name, None)

    with patch.object(R, "run_extraction", fake_run_extraction), \
         patch.object(R, "read_extracted_rows", return_value=[row]), \
         patch.object(R, "_attachment_text", return_value=attachment_text):
        R.extract_and_store_accounts(
            saved, "<MID>", ctrl,
            email_rec={"received_at": "2026-09-11T10:00:00+00:00",
                       "subject": subject, "sender_email": sender},
            body_text=body_text)
    return ctrl


class CallSiteAnexoTest(unittest.TestCase):
    def test_fornecedor_le_blanc_lido_so_pelo_vision(self):
        # Sem texto cru, sem mencao no assunto/corpo: o UNICO sinal e o supplier_name, que
        # _finalize_supplier remove — prova a captura ANTES do finalize.
        ctrl = _run(_row(supplier_name="LE BLANC ADMINISTRACAO DE BENS PROPRIOS LTDA"))
        self.assertEqual(len(ctrl.financial_calls), 1, ctrl.error_calls)
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], LE_BLANC)

    def test_texto_do_anexo_com_leblanc(self):
        ctrl = _run(_row(), attachment_text="PAGADOR: LEBLANC ADMINISTRACAO")
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], LE_BLANC)

    def test_nome_do_arquivo_anexo(self):
        ctrl = _run(_row(source_file="NOTA_FISCAL_LE_BLANC.pdf"))
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], LE_BLANC)

    def test_nome_de_OUTRO_anexo_marca_a_mensagem(self):
        # Flag de MENSAGEM: o nome LE BLANC esta num anexo que NAO gera conta; o pagavel vem
        # do segundo arquivo, cujo source_file nao cita a LE BLANC. So o ramo pdf_path.name
        # do Passo 1 carrega o sinal ate a linha.
        ctrl = FakeControl()
        row = _row(source_file="boleto.pdf")
        saved = [Path("BOLETO_LEBLANC_-_POR.pdf"), Path("boleto.pdf")]

        def fake_run_extraction(pdf_path, pdf_passwords=None):
            return (pdf_path.name, None)

        with patch.object(R, "run_extraction", fake_run_extraction), \
             patch.object(R, "read_extracted_rows", side_effect=[[], [row]]), \
             patch.object(R, "_attachment_text", return_value=""):
            R.extract_and_store_accounts(
                saved, "<MID>", ctrl,
                email_rec={"received_at": "2026-09-11T10:00:00+00:00",
                           "subject": "PAGAMENTO BOLETO",
                           "sender_email": "barbara@otimotex.com.br"},
                body_text="")
        self.assertEqual(len(ctrl.financial_calls), 1, ctrl.error_calls)
        self.assertEqual(ctrl.financial_calls[0]["source_file"], "boleto.pdf")
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], LE_BLANC)

    def test_ester_com_assunto_le_blanc(self):
        ctrl = _run(_row(), subject="Le Blanc - Boleto Porto Saude", sender=ESTER)
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], LE_BLANC)

    def test_ester_sem_mencao_continua_fardos(self):
        ctrl = _run(_row(), sender=ESTER)
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], FARDOS)

    def test_sem_mencao_e_otimotex(self):
        ctrl = _run(_row())
        self.assertEqual(ctrl.financial_calls[0]["sk_company"], OTIMOTEX)


PIX_BODY = (
    "Nome: Fornecedor Exemplo\n"
    "Valor: R$ 1.250,00\n"
    "Vencimento: 20/06/2026\n"
    "Chave Pix: exemplo@empresa.com.br"
)


class CallSiteCorpoTest(unittest.TestCase):
    def _body(self, subject, sender, body=PIX_BODY):
        ctrl = FakeControl()
        outcome = R.try_extract_from_body(
            {"message_id": "<m>", "subject": subject, "sender_email": sender}, body,
            "2026-06-10T00:00:00+00:00", "<m>", ctrl, sender_email=sender)
        self.assertEqual(outcome, R.BODY_CREATED, ctrl.error_calls)
        return ctrl.financial_calls[0]["sk_company"]

    def test_corpo_citando_le_blanc(self):
        self.assertEqual(self._body("Pagamento", "x@fornecedor.com.br",
                                    PIX_BODY + "\nPagador: LE BLANC"), LE_BLANC)

    def test_assunto_le_blanc_vence_a_ester(self):
        self.assertEqual(self._body("PIX LEBLANC", ESTER), LE_BLANC)

    def test_ester_sem_mencao_continua_fardos(self):
        self.assertEqual(self._body("PIX", ESTER), FARDOS)


if __name__ == "__main__":
    unittest.main()
