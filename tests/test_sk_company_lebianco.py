"""
test_sk_company_lebianco.py — regra da EMPRESA PAGADORA (sk_company) na extracao.

Regra por PRECEDENCIA (decisao do usuario, 2026-07-17):
  1o remetente ester@otimotex.com.br -> sk_company = 3 (OTIMOTEX FARDOS)
  2o referencia a "lebianco" (assunto / corpo / ANEXO / remetente / dominio) -> 2
  3o nenhum dos dois -> 1 (OTIMOTEX TECIDOS)
Desde 2026-09-11 a mencao a LE BLANC (sk 4) vem ANTES de tudo, inclusive da ester — coberta
em tests/test_sk_company_le_blanc.py. Nenhum caso deste arquivo cita LE BLANC, entao todos
seguem valendo.

Regra anterior (preservada nos ramos 2o/3o):
  - referencia a "lebianco" no assunto / corpo / ANEXO / remetente / dominio -> sk_company = 2
  - SEM mencao -> SEMPRE OTIMOTEX (sk_company = 1)
  - a REFERENCIA VENCE o CNPJ: conta da LEBIANCO pode ter o CNPJ da OTIMOTEX no boleto, e o
    CNPJ sozinho (sem mencao) NAO classifica.
  - sk_company (PAGADORA) e independente de sk_supplier (FORNECEDOR).

Funcoes PURAS com literais (sem rede).
"""

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "email-reader" / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import read_emails as R  # noqa: E402

LEBIANCO = R.SK_COMPANY_LEBIANCO   # 2
OTIMOTEX = R.SK_COMPANY_DEFAULT    # 1 — OTIMOTEX TECIDOS
FARDOS = R.SK_COMPANY_FARDOS       # 3 — OTIMOTEX FARDOS
CNPJ_LEBIANCO = "47273917000223"
CNPJ_OTIMOTEX = "47273917000123"


class IsLebiancoSenderTest(unittest.TestCase):
    def test_dominio_exato(self):
        self.assertTrue(R._is_lebianco_sender("fulano@lebianco.com.br"))

    def test_subdominio(self):
        self.assertTrue(R._is_lebianco_sender("nf@mail.lebianco.com.br"))

    def test_caixa_alta(self):
        self.assertTrue(R._is_lebianco_sender("FULANO@LEBIANCO.COM.BR"))

    def test_outro_dominio(self):
        self.assertFalse(R._is_lebianco_sender("fulano@otimotex.com.br"))

    def test_none(self):
        self.assertFalse(R._is_lebianco_sender(None))


class IsFardosSenderTest(unittest.TestCase):
    """Endereco EXATO — e o que faz a regra "vencer o dominio otimotex.com.br"."""

    def test_endereco_exato(self):
        self.assertTrue(R._is_fardos_sender("ester@otimotex.com.br"))

    def test_caixa_alta_e_espacos(self):
        self.assertTrue(R._is_fardos_sender("  Ester@Otimotex.COM.BR "))

    def test_outro_usuario_do_mesmo_dominio_nao_casa(self):
        # O dominio otimotex.com.br NAO basta — so a ester.
        self.assertFalse(R._is_fardos_sender("rose@otimotex.com.br"))
        self.assertFalse(R._is_fardos_sender("financeiro@otimotex.com.br"))

    def test_local_part_ester_em_outro_dominio_nao_casa(self):
        self.assertFalse(R._is_fardos_sender("ester@gmail.com"))

    def test_none_e_vazio(self):
        self.assertFalse(R._is_fardos_sender(None))
        self.assertFalse(R._is_fardos_sender(""))


class FardosPrecedenciaTest(unittest.TestCase):
    """NAO REGREDIR: a ester vence o dominio E a mencao a lebianco (decisao do usuario).

    A ordem em resolve_sk_company e o que garante isso — mover a checagem da ester para
    depois dos ramos de lebianco inverteria a regra.
    """

    def test_ester_vence_mencao_a_lebianco(self):
        self.assertEqual(
            R.resolve_sk_company(sender_email="ester@otimotex.com.br",
                                 subject="PAGAMENTO LEBIANCO"),
            FARDOS)

    def test_ester_vence_lebianco_no_corpo_e_no_anexo(self):
        self.assertEqual(
            R.resolve_sk_company(sender_email="ester@otimotex.com.br",
                                 body_text="conta da lebianco", pdf_lebianco=True),
            FARDOS)

    def test_ester_vence_o_dominio_otimotex(self):
        self.assertEqual(R.resolve_sk_company(sender_email="ester@otimotex.com.br"), FARDOS)

    def test_outro_usuario_otimotex_e_o_default(self):
        self.assertEqual(R.resolve_sk_company(sender_email="rose@otimotex.com.br"), OTIMOTEX)

    def test_ester_citada_no_texto_sem_ser_remetente_nao_classifica(self):
        # So o REMETENTE conta — citar o e-mail dela no corpo nao torna a conta da FARDOS.
        self.assertEqual(
            R.resolve_sk_company(sender_email="x@fornecedor.com.br",
                                 body_text="favor falar com ester@otimotex.com.br",
                                 payer_name="ester@otimotex.com.br"),
            OTIMOTEX)

    def test_lebianco_segue_valendo_sem_a_ester(self):
        self.assertEqual(R.resolve_sk_company(sender_email="ana@lebianco.com.br"), LEBIANCO)
        self.assertEqual(R.resolve_sk_company(subject="NF LEBIANCO 998"), LEBIANCO)


class ApplySkCompanyFardosTest(unittest.TestCase):
    def test_payload_da_ester_grava_3(self):
        payload = {"subject": "PAGAMENTO BOLETO", "sender_email": "ester@otimotex.com.br"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], FARDOS)

    def test_payload_da_ester_com_lebianco_grava_3(self):
        payload = {"subject": "PAGAMENTO LEBIANCO", "sender_email": "ester@otimotex.com.br"}
        R.apply_sk_company(payload, body_text="lebianco")
        self.assertEqual(payload["sk_company"], FARDOS)


class NaoConfundirEmpresaComFornecedorTest(unittest.TestCase):
    """NAO REGREDIR: OTIMOTEX_SK_SUPPLIER (fornecedor) != SK_COMPANY_DEFAULT (empresa).

    Os dois valem 1 e tem o mesmo nome, mas sao de TABELAS diferentes (supplier x company).
    Este teste existe para um find-replace descuidado quebrar aqui, e nao em producao.
    """

    def test_sao_constantes_distintas(self):
        self.assertEqual(R.OTIMOTEX_SK_SUPPLIER, 1)   # supplier.sk_supplier
        self.assertEqual(R.SK_COMPANY_DEFAULT, 1)     # company.sk_company
        self.assertEqual(R.SK_COMPANY_FARDOS, 3)

    def test_regra_de_empresa_ignora_o_fornecedor(self):
        # Fornecedor LEBIANCO nao torna a conta da LEBIANCO (quem paga seria a OTIMOTEX).
        payload = {"subject": "PAGAMENTO", "sender_email": "x@fornecedor.com.br",
                   "supplier_name": "LEBIANCO PLASTICOS"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)


class HasLebiancoReferenceTest(unittest.TestCase):
    def test_variacoes_de_caixa(self):
        for termo in ("Lebianco", "lebianco", "LEBIANCO", "LeBiAnCo"):
            with self.subTest(termo=termo):
                self.assertTrue(R._has_lebianco_reference(f"PAGAMENTO {termo} NF 123"))

    def test_dentro_de_email_e_nome_de_arquivo(self):
        # Substring (nao \b): precisa casar dentro de dominio e de nome de arquivo.
        self.assertTrue(R._has_lebianco_reference("envio de nf@lebianco.com.br"))
        self.assertTrue(R._has_lebianco_reference("boleto_lebianco_072026.pdf"))

    def test_razao_social(self):
        self.assertTrue(R._has_lebianco_reference("LEBIANCO PLASTICOS"))

    def test_com_acento_no_texto_ao_redor(self):
        self.assertTrue(R._has_lebianco_reference("Pagamento à LEBIANCO PLÁSTICOS"))

    def test_sem_mencao(self):
        self.assertFalse(R._has_lebianco_reference("PAGAMENTO BOLETO DAMSP", "Fatura 123"))

    def test_none_e_vazio_sao_ignorados(self):
        self.assertFalse(R._has_lebianco_reference(None, "", "boleto"))

    def test_qualquer_um_dos_textos_basta(self):
        self.assertTrue(R._has_lebianco_reference(None, "sem nada", "ref lebianco aqui"))


class LeBiancoComEspacoTest(unittest.TestCase):
    """NAO REGREDIR: "LE BIANCO" (com espaco) vale SO NO ASSUNTO.

    No assunto e referencia deliberada ("LE BIANCO - PAGAMENTO FORNECEDOR"); no CORPO ela
    aparece na ASSINATURA do grupo ("Departamento Financeiro | Otimotex / Le Bianco") e
    marcaria como LEBIANCO contas que sao da OTIMOTEX (falso positivo real: conta 167).
    """

    def test_assunto_com_espaco_classifica(self):
        self.assertEqual(
            R.resolve_sk_company(subject="LE BIANCO - PAGAMENTO FORNECEDOR (NF 12)"), LEBIANCO)

    def test_assunto_com_espaco_caixa_baixa(self):
        self.assertTrue(R._subject_has_lebianco("Re: le bianco - pagamento"))

    def test_assunto_forma_junta_tambem(self):
        self.assertTrue(R._subject_has_lebianco("COBRANCA LEBIANCO"))

    def test_assunto_sem_mencao(self):
        self.assertFalse(R._subject_has_lebianco("COBRANCA OTIMOTEX TECIDO"))
        self.assertFalse(R._subject_has_lebianco(None))

    def test_assinatura_no_CORPO_nao_classifica(self):
        # Caso real (conta 167): conta da OTIMOTEX; "Le Bianco" so na assinatura do grupo.
        self.assertEqual(
            R.resolve_sk_company(
                subject="Re: COBRANCA OTIMOTEX TECIDO",
                sender_email="meira.gabriela@handred.com",
                body_text="Att\n[image: Departamento Financeiro | Otimotex / Le Bianco]"),
            OTIMOTEX)

    def test_espaco_na_descricao_nao_classifica(self):
        self.assertEqual(R.resolve_sk_company(description="Otimotex / Le Bianco"), OTIMOTEX)


class ResolveSkCompanyTest(unittest.TestCase):
    def test_remetente_lebianco(self):
        self.assertEqual(
            R.resolve_sk_company(subject="PAGAMENTO BOLETO", sender_email="ana@lebianco.com.br"),
            LEBIANCO)

    def test_assunto(self):
        self.assertEqual(R.resolve_sk_company(subject="NF LEBIANCO 998"), LEBIANCO)

    def test_corpo(self):
        self.assertEqual(
            R.resolve_sk_company(subject="Boleto", body_text="favor pagar pela Lebianco"),
            LEBIANCO)

    def test_descricao_do_anexo(self):
        self.assertEqual(R.resolve_sk_company(description="Boleto LEBIANCO PLASTICOS"), LEBIANCO)

    def test_nome_do_arquivo_anexo(self):
        self.assertEqual(R.resolve_sk_company(source_file="boleto_lebianco.pdf"), LEBIANCO)

    def test_payer_name(self):
        self.assertEqual(R.resolve_sk_company(payer_name="LEBIANCO PLASTICOS"), LEBIANCO)

    def test_texto_do_pdf(self):
        # Flag vinda de _pdf_mentions_lebianco (texto cru do anexo).
        self.assertEqual(R.resolve_sk_company(subject="Boleto", pdf_lebianco=True), LEBIANCO)

    def test_email_body_excerpt(self):
        self.assertEqual(R.resolve_sk_company(email_body_excerpt="ref lebianco"), LEBIANCO)

    def test_sem_mencao_e_otimotex(self):
        self.assertEqual(
            R.resolve_sk_company(subject="PAGAMENTO BOLETO DAMSP",
                                 body_text="segue boleto para pagamento",
                                 sender_email="financeiro@otimotex.com.br",
                                 description="Boleto DAMSP", payer_name="OTIMOTEX LTDA"),
            OTIMOTEX)

    def test_tudo_vazio_e_otimotex(self):
        self.assertEqual(R.resolve_sk_company(), OTIMOTEX)


class ReferenciaVenceCnpjTest(unittest.TestCase):
    """NAO REGREDIR: o CNPJ nao participa da regra."""

    def test_cnpj_lebianco_sem_mencao_e_otimotex(self):
        # Caso real (conta 267): payer_cnpj e da LEBIANCO, mas nada menciona lebianco.
        payload = {"subject": "PAGAMENTO BOLETO DAMSP",
                   "sender_email": "financeiro@otimotex.com.br",
                   "payer_cnpj": CNPJ_LEBIANCO,
                   "payer_name": "TEXTIL E CONFECCOES OTIMOTEX LTDA."}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)

    def test_mencao_com_cnpj_otimotex_e_lebianco(self):
        # A conta pode ser da LEBIANCO mesmo com o CNPJ da OTIMOTEX impresso no boleto.
        payload = {"subject": "PAGAMENTO LEBIANCO",
                   "sender_email": "financeiro@otimotex.com.br",
                   "payer_cnpj": CNPJ_OTIMOTEX,
                   "payer_name": "TEXTIL E CONFECCOES OTIMOTEX LTDA."}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], LEBIANCO)


class IndependenciaDoFornecedorTest(unittest.TestCase):
    """NAO REGREDIR: sk_company (PAGADORA) != sk_supplier (FORNECEDOR).

    supplier_name/supplier_cnpj ficam FORA da varredura — se a LEBIANCO for o FORNECEDOR,
    quem paga e a OTIMOTEX. (_finalize_supplier ja remove essas chaves antes da gravacao.)
    """

    def test_supplier_name_nao_classifica(self):
        payload = {"subject": "PAGAMENTO BOLETO", "sender_email": "x@fornecedor.com.br",
                   "supplier_name": "LEBIANCO PLASTICOS"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)


class ApplySkCompanyTest(unittest.TestCase):
    def test_usa_o_body_text_do_parametro(self):
        payload = {"subject": "Boleto"}
        R.apply_sk_company(payload, body_text="conta da lebianco")
        self.assertEqual(payload["sk_company"], LEBIANCO)

    def test_usa_o_flag_do_anexo(self):
        payload = {"subject": "Boleto"}
        R.apply_sk_company(payload, pdf_lebianco=True)
        self.assertEqual(payload["sk_company"], LEBIANCO)

    def test_respeita_valor_ja_presente(self):
        # Idiom de created_by: o caminho que ja decidiu manda; a rede universal nao sobrescreve.
        payload = {"subject": "PAGAMENTO LEBIANCO", "sk_company": OTIMOTEX}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)

    def test_grava_sempre_mesmo_sem_mencao(self):
        # Gravar 1 explicitamente e o que impede o trigger de resolver pelo CNPJ.
        payload = {"subject": "Boleto DAMSP"}
        R.apply_sk_company(payload)
        self.assertEqual(payload["sk_company"], OTIMOTEX)


class PdfTextTest(unittest.TestCase):
    """`_pdf_text` substituiu `_pdf_mentions_lebianco` na Onda 3.

    O texto do anexo passou a ter DOIS consumidores (regra LEBIANCO e registro de documento
    fiscal), entao a leitura virou unica e a decisao de lebianco passa a ser
    `_has_lebianco_reference(texto)`. A garantia que NAO pode mudar e a mesma: best-effort —
    falha na leitura nunca derruba a gravacao da conta.
    """

    def test_arquivo_inexistente_nao_levanta(self):
        self.assertEqual(R._pdf_text(Path("nao_existe_xyz.pdf")), "")

    def test_texto_ilegivel_nao_marca_lebianco(self):
        self.assertFalse(R._has_lebianco_reference(R._pdf_text(Path("nao_existe_xyz.pdf"))))


if __name__ == "__main__":
    unittest.main()
