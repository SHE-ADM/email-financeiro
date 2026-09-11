"""
read_emails.py — Leitura de e-mails financeiros via IMAP + controle Supabase
Projeto: pagamentos | Skill: email-reader | v2.0.0

Deduplicação: tabela email_control no Supabase (message_id UNIQUE).
Nunca reprocessa um e-mail já registrado, independente de onde o script rodar.
"""

import os, sys, re, time, socket, imaplib, email, argparse, logging, csv, json, tempfile, faulthandler, unicodedata, ipaddress
import urllib.request, urllib.error, urllib.parse, http.cookiejar, http.client
from html import unescape as html_unescape
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

# ---------------------------------------------------------------------------
# Faulthandler — captura crashes nativos (segfault, stack overflow, etc.)
# Grava stack trace em arquivo antes do processo morrer.
# ---------------------------------------------------------------------------
_CRASH_LOG_DIR = Path(__file__).parents[3] / "logs" / "scheduler"
_CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
_CRASH_LOG = _CRASH_LOG_DIR / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_crash_file = open(_CRASH_LOG, "w", encoding="utf-8")
_crash_file.write(f"faulthandler ativado em {datetime.now().isoformat()}\n")
_crash_file.flush()
faulthandler.enable(file=_crash_file, all_threads=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("email-reader")


# Falha de API na extracao (creditos/auth/rate-limit). Interrompe o run com
# seguranca: o e-mail NAO e marcado como processado, sendo reprocessado quando
# a API voltar. Evita gravar dados incompletos (fornecedor vazio, valor errado).
class ApiUnavailableError(RuntimeError):
    """API Anthropic indisponivel durante a extracao — para o pipeline."""


# Console do Windows (cp1252) nao encoda os simbolos de log (✓/→/✗).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------
BASE_DIR       = Path(__file__).parents[3]
PDF_INBOX      = BASE_DIR / "data" / "pdfs_inbox"
CSV_OUTPUT     = BASE_DIR / "data" / "csv_output"
EXTRACT_SCRIPT = BASE_DIR / "skills" / "pdf-contas-pagar" / "scripts" / "extract_pdf.py"
EMAILS_LOG     = CSV_OUTPUT / "emails_log.csv"

# Content-Type por extensão do anexo (PDF ou imagem) para o upload no Storage.
_UPLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Header PostgREST reutilizado (evita literal duplicado — S1192).
_PREFER_MINIMAL = "return=minimal"

# Regex de tag HTML compilado UMA vez (reuso em _html_to_text e na extracao de
# texto de ancora de links — S1192 + performance).
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# Modulo canonico de codigos FEBRABAN, resolvido na 1a necessidade. `False` = ja tentou e
# falhou (nao re-tenta a cada e-mail nem re-emite o aviso).
_FEBRABAN = None
# Aviso unico por processo quando a canonica de barcode nao esta disponivel.
_CANONICAL_BARCODE_WARNED = False

# Idem para o parser de chave de acesso fiscal (Onda 3). Mesmo mecanismo, MESMO motivo:
# `fiscal_key` e um arquivo NOVO, entao um deploy que copie so o read_emails.py o deixa para
# tras — e a degradacao (nenhum documento fiscal registrado) seria silenciosa sem o aviso.
_FISCAL_KEY = None
_FISCAL_KEY_WARNED = False

# Idem para o conteudo do CT-e (Onda 5). Mesmo mecanismo, mesmo motivo: `cte_content` tambem e
# arquivo NOVO, e sem o aviso o deploy parcial deixaria de gravar peso/rota/frete em silencio.
_CTE_CONTENT = None
_CTE_CONTENT_WARNED = False

# Idem para a leitura de .docx. Mesmo mecanismo, mesmo motivo: `docx_content` e arquivo NOVO, e
# sem o aviso um deploy parcial deixaria a regra LEBIANCO e o gancho fiscal cegos ao Word — o
# mesmo tipo de silencio que fez o boleto do e-mail 1516 sumir.
_DOCX_CONTENT = None
_DOCX_CONTENT_WARNED = False


def _febraban():
    """Modulo `febraban` (codigo de barras / linha digitavel) — fonte UNICA das regras.

    Vive na pasta da skill de PDF mas NAO tem dependencia pesada: importa-lo custa ~7 ms,
    contra ~580 ms de `extract_pdf` (pandas + pdfplumber + PIL + pypdf) — que era o preco
    que o caminho do CORPO pagava so para normalizar digitos."""
    global _FEBRABAN  # noqa: PLW0603 — cache por processo
    if _FEBRABAN is None:
        try:
            if str(EXTRACT_SCRIPT.parent) not in sys.path:
                sys.path.insert(0, str(EXTRACT_SCRIPT.parent))
            import febraban
            _FEBRABAN = febraban
        except Exception:
            _FEBRABAN = False
    return _FEBRABAN or None


def _fiscal_key():
    """Modulo `fiscal_key` (chave de acesso SEFAZ), ou None AVISANDO UMA VEZ.

    Sem fallback local de proposito, ao contrario do `_body_barcode`: validar chave de acesso
    exige o DV, e um fallback "so pelo comprimento" registraria lixo como documento fiscal —
    medido no banco, 7 de 8 codigos de 44 digitos que nao sao boleto NAO sao chave. Melhor
    nao registrar nada e dizer isso no log."""
    global _FISCAL_KEY, _FISCAL_KEY_WARNED  # noqa: PLW0603 — cache/aviso por processo
    if _FISCAL_KEY is None:
        try:
            if str(EXTRACT_SCRIPT.parent) not in sys.path:
                sys.path.insert(0, str(EXTRACT_SCRIPT.parent))
            import fiscal_key
            _FISCAL_KEY = fiscal_key
        except Exception:
            _FISCAL_KEY = False
    if not _FISCAL_KEY and not _FISCAL_KEY_WARNED:
        _FISCAL_KEY_WARNED = True
        log.warning("  [FISCAL] modulo 'fiscal_key' indisponivel — documento fiscal NAO sera "
                    "registrado. Deploy parcial (falta fiscal_key.py)?")
    return _FISCAL_KEY or None


def _cte_content():
    """Modulo `cte_content` (conteudo do CT-e, Onda 5), ou None AVISANDO UMA VEZ.

    Sem fallback local, pelo mesmo motivo do `_fiscal_key`: o parser so e confiavel porque
    confere a soma extraida contra o SUB-TOTAL impresso na fatura. Um fallback "casa a linha e
    grava" perderia justamente o oraculo e produziria rateio incompleto — pior que nao gravar.
    """
    global _CTE_CONTENT, _CTE_CONTENT_WARNED  # noqa: PLW0603 — cache/aviso por processo
    if _CTE_CONTENT is None:
        try:
            if str(EXTRACT_SCRIPT.parent) not in sys.path:
                sys.path.insert(0, str(EXTRACT_SCRIPT.parent))
            import cte_content
            _CTE_CONTENT = cte_content
        except Exception:
            _CTE_CONTENT = False
    if not _CTE_CONTENT and not _CTE_CONTENT_WARNED:
        _CTE_CONTENT_WARNED = True
        log.warning("  [FISCAL] modulo 'cte_content' indisponivel — peso/rota/frete do CT-e NAO "
                    "serao gravados. Deploy parcial (falta cte_content.py)?")
    return _CTE_CONTENT or None


def _docx_content():
    """Modulo `docx_content` (leitura de .docx), ou None AVISANDO UMA VEZ.

    Sem fallback local: ler um ZIP+XML a mao aqui duplicaria as defesas contra zip bomb e path
    traversal que o modulo concentra — e uma segunda copia dessas guardas e exatamente o tipo de
    coisa que diverge e vira furo. Melhor nao ler o .docx e dizer isso no log."""
    global _DOCX_CONTENT, _DOCX_CONTENT_WARNED  # noqa: PLW0603 — cache/aviso por processo
    if _DOCX_CONTENT is None:
        try:
            if str(EXTRACT_SCRIPT.parent) not in sys.path:
                sys.path.insert(0, str(EXTRACT_SCRIPT.parent))
            import docx_content
            _DOCX_CONTENT = docx_content
        except Exception:
            _DOCX_CONTENT = False
    if not _DOCX_CONTENT and not _DOCX_CONTENT_WARNED:
        _DOCX_CONTENT_WARNED = True
        log.warning("  [DOCX] modulo 'docx_content' indisponivel — texto de anexo .docx NAO sera "
                    "lido (regra LEBIANCO e chave fiscal ficam cegas). Deploy parcial?")
    return _DOCX_CONTENT or None


def _febraban_fn(name: str):
    """Funcao canonica `name`, ou None AVISANDO UMA VEZ — ponto unico de degradacao.

    Cobre os DOIS modos de indisponibilidade com o mesmo aviso: modulo ausente (deploy
    PARCIAL — copiar read_emails.py sem febraban.py) e modulo presente SEM a funcao (a
    canonica foi renomeada). Este segundo caso e o traicoeiro: o despacho e por NOME, e
    sem o aviso a validacao cairia em silencio, devolvendo um resultado plausivel."""
    fn = getattr(_febraban() or None, name, None)
    if fn is None:
        global _CANONICAL_BARCODE_WARNED  # noqa: PLW0603 — aviso unico por processo
        if not _CANONICAL_BARCODE_WARNED:
            _CANONICAL_BARCODE_WARNED = True
            log.warning(f"  [BARCODE] 'febraban.{name}' indisponivel — validacao degradada. "
                        "Deploy parcial (falta febraban.py) ou funcao renomeada?")
    return fn


def _normalize_body_barcode(raw: str | None) -> str | None:
    """Normaliza E VALIDA o barcode do corpo — a escolha SEGURA (ver
    `febraban.normalize_barcode`). Use quando os digitos vierem de captura FROUXA."""
    return _body_barcode(raw, "normalize_barcode")


def _normalize_body_barcode_allow_misread(raw: str | None) -> str | None:
    """Normaliza SEM julgar o DV — para digitos de captura ESTRUTURADA, em que um DV que
    nao fecha e leitura corrompida de um codigo REAL (ver
    `febraban.normalize_barcode_allow_misread`)."""
    return _body_barcode(raw, "normalize_barcode_allow_misread")


def _body_barcode(raw: str | None, canonical_fn: str) -> str | None:
    """Normaliza o barcode do CORPO pela funcao canonica (44/48 mantidos, 47 -> 44,
    outros comprimentos -> None). Antes o corpo usava um re.sub solto que aceitava
    qualquer sequencia de 44-48 digitos (ex.: 45/46), podendo gravar barcode invalido.

    O try cobre SO a OBTENCAO da funcao — a CHAMADA fica fora dele, para que um erro
    DENTRO da canonica suba como erro, em vez de virar 'barcode nao validado'. Sem a
    canonica, o fallback aplica so o comprimento + o invariante do '8': nao ha como
    validar o DV, e rejeitar por nao-saber perderia barcode legitimo (na duvida,
    preservar)."""
    if not raw:
        return None
    fn = _febraban_fn(canonical_fn)
    if fn is None:
        digits = re.sub(r"\D", "", raw)
        if not 44 <= len(digits) <= 48:
            return None
        return None if len(digits) == 48 and not digits.startswith("8") else digits
    return fn(raw)


def _extract_body_linha_digitavel(text: str | None) -> str | None:
    """Linha digitavel (47 digitos) achada no TEXTO do corpo SEM depender de rotulo.

    Fallback de _BODY_BARCODE_RE, que exige o rotulo ('linha digitavel'/'codigo de
    barras') a <=10 caracteres do numero. Em e-mail cujo corpo e uma TABELA HTML
    achatada, os rotulos vem TODOS no cabecalho e os valores depois, entao o rotulo
    fica longe do numero e nada era capturado (conta 693, MOVVI). Reusa o extrator
    DETERMINISTICO canonico do extract_pdf — fonte unica, mesma validacao dos 5
    campos FEBRABAN (5-5 / 5-6 / 5-6 / 1 / 14), o que torna o falso positivo
    improvavel mesmo sem rotulo. Import lazy + fallback defensivo, como
    _normalize_body_barcode."""
    if not text:
        return None
    fn = _febraban_fn("extract_linha_digitavel")
    # Chamada FORA de try: erro dentro da canonica deve subir, nao virar "nao achou".
    return fn(text) if fn else None


def _apply_barcode_due_date(payload: dict) -> None:
    """Rede de seguranca UNIVERSAL contra inversao dia/mes do vencimento: a data de vencimento
    de um boleto e o FATOR DE VENCIMENTO do codigo de barras (deterministico), NAO a data lida —
    que o Vision/OCR pode inverter (falha grave: id 435 gravou 07/08 no lugar de 08/07). Antes de
    gravar QUALQUER conta com boleto FEBRABAN, sobrescreve o due_date pelo derivado do barcode se
    divergir. Aplicado em register_financial (choke point unico de toda gravacao do pipeline
    Python — PDF, corpo e reprocessos), alem da correcao em extract_pdf.build_record. So confia no
    fator quando o barcode e CONSISTENTE (valor embutido == amount — `authoritative_barcode_due_date`):
    um barcode mal lido pelo OCR (boleto escaneado) tem valor divergente e NAO dita a data (id 463).
    Best-effort: import lazy do extract_pdf; qualquer falha e ignorada (nao derruba a gravacao)."""
    if not payload.get("barcode"):
        return
    fn = _febraban_fn("authoritative_barcode_due_date")
    if fn is None:
        return
    try:
        # GATES: so sobrescreve pelo fator quando o barcode e CONSISTENTE com o valor E o
        # vencimento derivado nao e anterior a emissao (fator stale de boleto securitizado).
        bc_due = fn(
            payload.get("barcode"), payload.get("amount"),
            payload.get("issue_date") or payload.get("extracted_at"),
            issue_date=payload.get("issue_date"))
    except Exception:
        # Best-effort DELIBERADO: isto roda no choke point de TODA gravacao, e uma
        # correcao opcional de vencimento nao pode derrubar a conta. Mas LOGA com
        # traceback — engolir calado esconderia o bug de vez.
        log.exception("  [BARCODE] falha ao derivar vencimento pelo fator — mantido o extraido")
        return
    cur = str(payload.get("due_date") or "")[:10]
    if not bc_due or cur == bc_due:
        return
    payload["due_date"] = bc_due
    note = f"Vencimento corrigido pelo codigo de barras (fator FEBRABAN): {cur or '—'} -> {bc_due}"
    payload["processing_notes"] = (
        f'{payload["processing_notes"]} | {note}' if payload.get("processing_notes") else note)


def _is_boleto_barcode(barcode: str | None) -> bool:
    """True quando o barcode e um BOLETO pagavel (nao chave NF-e/CT-e). Reusa a
    funcao canonica do extract_pdf (44 FEBRABAN moeda '9' banco != '000', ou 48
    de arrecadacao); import lazy com fallback defensivo, como _normalize_body_barcode."""
    if not barcode:
        return False
    fn = _febraban_fn("is_boleto_barcode")
    if fn:
        return fn(barcode)                      # chamada FORA de try (ver _body_barcode)
    # Sem a canonica: espelha a regra, INCLUSIVE o invariante do '8' na arrecadacao de 48.
    # Uma copia defensiva que diverge da regra real e pior que nao ter copia: ela mente em
    # silencio justamente quando a fonte unica esta indisponivel.
    d = re.sub(r"\D", "", barcode)
    return ((len(d) == 48 and d.startswith("8"))
            or (len(d) == 44 and d[3:4] == "9" and d[:3] != "000"))

PDF_INBOX.mkdir(parents=True, exist_ok=True)
CSV_OUTPUT.mkdir(parents=True, exist_ok=True)

# Bucket de Storage onde os PDFs anexados sao publicados (migration 021).
# O frontend gera URL assinada a partir do source_file = nome do arquivo.
STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "attachments")

KEYWORDS_DEFAULT = [
    "boleto", "nota fiscal", "nf-e", "nfe", "fatura",
    "cobrança", "vencimento", "pagamento", "pagamentos",
    "pagamento fornecedor", "pagamentos fornecedores", "duplicata",
    "recibo", "nfs-e", "nfse", "notas fiscais", "danfe",
    # Guias de tributos federais/estaduais/municipais. Os acronimos curtos
    # (darf, das, dae, dam, duam, gru, gnre, gare, ipva, iptu, iss, itbi) casam
    # por PALAVRA INTEIRA — ver WORD_KEYWORDS/match_keyword — para nao casarem
    # dentro de palavras comuns ('das' em 'cadastro', 'iss' em 'emissao').
    "simples nacional", "simei", "darf", "das", "dae", "dam", "duam",
    "gps", "gru", "gnre", "gare", "ipva", "iptu", "iss", "itbi",
    "guia", "guia de pagamento", "guia de recolhimento",
    # Conhecimento de Transporte Eletronico
    "ct-e", "cte", "dacte", "transporte", "transportadora", "conhecimento de transporte",
    # Fechamento de conta / extrato mensal
    "fechamento",
    # Seguro
    "seguro",
    # Operacoes de cambio (le 'cambio' ou 'câmbio'; gravado como 'câmbio')
    "câmbio",
    # Honorários (serviços profissionais — geralmente pagos via PIX)
    "honorário", "honorários", "honorario", "honorarios",
    # Container (frete/demurrage/movimentação de contêineres)
    "container", "conteiner", "contêiner",
    # Cartório (custas de tabelionato/registro/protesto) — casa por substring (termo
    # distintivo, não colide com palavras comuns).
    "cartório", "cartorio", "tabelionato",
]

LOG_COLUMNS = [
    "message_id", "received_at", "sender_name", "sender_email",
    "subject", "body_preview", "has_attachment", "attachment_names",
    "attachment_saved", "pdf_extracted", "extraction_csv",
    "keyword_matched", "processed_at", "notes"
]
# body_full NAO entra em LOG_COLUMNS de proposito: esse CSV e o fallback de emergencia para quando
# o Supabase esta fora, e inflar cada linha com o corpo inteiro o tornaria pesado sem ganho — se o
# Supabase caiu, o e-mail nao foi registrado e sera reprocessado depois (a dedup consulta o banco),
# recapturando o corpo.

# Teto de sanidade do corpo completo (migration 105 / Onda 2).
#
# ALTO de proposito: o maior corpo ja gravado tem ~11 KB, entao 100 KB nao corta e-mail real —
# serve so para um e-mail patologico (HTML de megabytes) nao inflar a tabela.
#
# E o corte e DECLARADO no proprio texto, nunca silencioso: corte silencioso e EXATAMENTE o defeito
# que esta onda corrige (o `[:500]` cortava 53% dos corpos sem deixar sinal, e so se descobriu
# contando quantos batiam no teto). Com a marca, quem le sabe que ha mais texto e onde faltou.
BODY_FULL_MAX_CHARS = 100_000
_BODY_TRUNCATED_MARK = "\n\n[CORPO TRUNCADO — excedeu {limite} caracteres]"


def _body_full_for_storage(body_text: str | None) -> str | None:
    """Corpo completo para gravar, com teto declarado. None/vazio -> None (a coluna aceita NULL,
    e NULL significa 'ainda nao temos o corpo' — distinto de 'o corpo e vazio')."""
    if not body_text:
        return None
    if len(body_text) <= BODY_FULL_MAX_CHARS:
        return body_text
    return body_text[:BODY_FULL_MAX_CHARS] + _BODY_TRUNCATED_MARK.format(limite=BODY_FULL_MAX_CHARS)

# Situacao (financial_account_control) — a FONTE UNICA e status_id (FK -> dimensao
# `status`). O pipeline ainda rotula internamente por TEXTO ('pendente'/'falha'); a
# traducao para status_id acontece num UNICO ponto (register_financial), antes do UPSERT.
# A trigger id-primaria (migration 068) recalcula 'a vencer'(3)/'vencido'(2) por vencimento
# e deriva o texto a partir do id. Mapa = tabela `status` (nomes exatos, com acento).
STATUS_ID_A_VENCER = 3   # default do banco (migration 067) — conta normal sem vencimento
_STATUS_NAME_TO_ID = {   # 'falha' -> 10 (estado fechado, a trigger preserva)
    "pendente": 1, "vencido": 2, "a vencer": 3, "prorrogado": 4, "baixado": 5,
    "protestado": 6, "cartório": 7, "pago": 8, "cancelado": 9, "falha": 10,
}


def _apply_status_id(payload: dict) -> None:
    """Traduz o `status` TEXTO do payload para `status_id` (fonte unica) e remove o texto.
    'pendente' (ou vazio) NAO seta status_id -> o banco aplica o DEFAULT 3 ('a vencer') e a
    trigger recalcula por vencimento. Demais estados (ex.: 'falha') viram o id correspondente.
    Chamado so na gravacao; o extractor/build seguem rotulando por texto internamente."""
    txt = (payload.pop("status", None) or "").strip().lower()
    if txt and txt != "pendente":
        payload["status_id"] = _STATUS_NAME_TO_ID.get(txt, STATUS_ID_A_VENCER)


# ---------------------------------------------------------------------------
# Supabase — controle de deduplicação
# ---------------------------------------------------------------------------
# Retry da checagem de duplicidade: um hiccup de rede na consulta faria _find
# retornar None ("sem duplicata") e o pipeline GRAVARIA uma conta duplicada. Por
# isso a consulta de dedup e re-tentada em falha transitoria antes de desistir.
DUP_QUERY_ATTEMPTS = int(os.getenv("DUP_QUERY_ATTEMPTS", "3"))
DUP_QUERY_BACKOFF  = float(os.getenv("DUP_QUERY_BACKOFF", "1.5"))  # segundos * tentativa


class SupabaseControl:
    """Gerencia a tabela email_control no Supabase."""

    def __init__(self):
        self.base = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key  = os.getenv("SUPABASE_SERVICE_KEY", "")
        self.headers = {
            "apikey":        self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type":  "application/json",
        }
        self._available = self._check_connection()

    def _check_connection(self) -> bool:
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/email_control?limit=1",
                headers=self.headers
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            log.warning(f"Supabase indisponível — usando deduplicação local: {e}")
            return False

    def _company_cnpj_map(self) -> "dict[int, str]":
        """{sk_company: CNPJ só dígitos} de TODAS as empresas pagadoras, cacheado por
        instância. Dicionário vazio quando indisponível — nunca levanta, porque nenhum
        dos consumidores (exclusão de fornecedor, senha de boleto) é essencial ao fluxo."""
        cached = getattr(self, "_company_cnpj_cache", None)
        if cached is not None:
            return cached
        self._company_cnpj_cache = {}
        if self._available:
            try:
                req = urllib.request.Request(
                    f"{self.base}/rest/v1/company?select=sk_company,cnpj&order=sk_company.asc",
                    headers=self.headers,
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    rows = json.loads(r.read())
                for row in rows or []:
                    digits = re.sub(r"\D", "", str(row.get("cnpj") or ""))
                    if not digits:
                        continue
                    # `sk_company` é coagido (e não checado por isinstance): se o PostgREST
                    # devolvesse "1" em vez de 1, descartar a linha faria `company_cnpj()`
                    # virar None e a exclusão "a pagadora nunca é fornecedor" cair calada.
                    try:
                        sk = int(row["sk_company"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    self._company_cnpj_cache[sk] = digits
            except Exception as e:
                log.warning(f"Falha ao ler CNPJ das empresas pagadoras: {e}")
        return self._company_cnpj_cache

    def company_cnpj(self) -> "str | None":
        """CNPJ (só dígitos) da empresa pagadora PRINCIPAL (sk_company=1) — base da
        exclusão "a própria pagadora nunca é fornecedor" (raiz de 8 dígitos, comum às
        filiais). None quando indisponível."""
        return self._company_cnpj_map().get(SK_COMPANY_DEFAULT)

    def company_cnpjs(self) -> list[str]:
        """CNPJ (só dígitos) de TODAS as empresas pagadoras, ordenado por sk_company —
        base das senhas candidatas de boletos protegidos.

        REGRA: a senha do boleto é derivada do CNPJ do PAGADOR, e o pagador do e-mail só
        é resolvido DEPOIS da extração (Passo 2). Como as filiais compartilham a raiz mas
        têm CNPJ COMPLETO distinto, a lista traz todas — tentar uma senha errada custa uma
        chamada local ao pypdf, enquanto omitir a filial certa perde o boleto em silêncio."""
        return list(self._company_cnpj_map().values())

    def is_processed(self, message_id: str) -> bool:
        """True se o message_id já existe na tabela."""
        if not self._available:
            return False
        try:
            mid_enc = urllib.parse.quote(message_id, safe="")
            req = urllib.request.Request(
                f"{self.base}/rest/v1/email_control"
                f"?message_id=eq.{mid_enc}&select=id&limit=1",
                headers=self.headers
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                return len(data) > 0
        except Exception as e:
            log.warning(f"Erro ao verificar duplicidade: {e}")
            return False

    def register(self, rec: dict) -> bool:
        """Insere registro na tabela. Ignora conflito de message_id."""
        if not self._available:
            return False
        payload = {
            "message_id":       rec.get("message_id"),
            "received_at":      rec.get("received_at"),
            "sender_name":      rec.get("sender_name"),
            "sender_email":     rec.get("sender_email"),
            "subject":          rec.get("subject"),
            "body_preview":     (rec.get("body_preview") or "")[:500],
            # Corpo completo (migration 105). Enviado como None quando ausente — a coluna aceita
            # NULL, e NULL diz "ainda nao temos o corpo", diferente de string vazia.
            "body_full":        rec.get("body_full") or None,
            "keyword_matched":  rec.get("keyword_matched"),
            # has_attachment fica NULL quando desconhecido (e-mails 'ignorado', que
            # não são baixados) — assim não poluem o KPI "Sem anexo PDF" (eq.false).
            "has_attachment":   None if rec.get("has_attachment") is None else bool(rec.get("has_attachment")),
            "attachment_names": rec.get("attachment_names"),
            "attachment_saved": bool(rec.get("attachment_saved")),
            "pdf_extracted":    bool(rec.get("pdf_extracted")),
            "extraction_csv":   rec.get("extraction_csv"),
            "notes":            rec.get("notes"),
            # Status explícito (ex.: 'ignorado' p/ fora do filtro) tem prioridade;
            # caso contrário, deriva de pdf_extracted/attachment_saved/notes.
            "status":           rec.get("status") or self._derive_status(rec),
            "processed_at":     rec.get("processed_at"),
        }
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self.base}/rest/v1/email_control",
                data=data,
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if "duplicate" in body.lower() or e.code == 409:
                return False   # já existe — normal
            log.exception(f"Erro ao registrar no Supabase: {e.code} {body[:150]}")
            return False
        except Exception as e:
            log.exception(f"Erro ao registrar no Supabase: {e}")
            return False

    def register_error(self, email_rec: dict, error_type: str,
                       error_message: str, raw_payload: dict = None) -> bool:
        """Grava falha de processamento em email_processing_errors."""
        if not self._available:
            log.warning(f"[ERRO-LOG] {error_type}: {error_message[:120]}")
            return False
        payload = {
            "gmail_message_id": email_rec.get("message_id"),
            "sender_name":      email_rec.get("sender_name"),
            "sender_email":     email_rec.get("sender_email"),
            "subject":          email_rec.get("subject"),
            "received_at":      email_rec.get("received_at"),
            "source_file":      email_rec.get("source_file"),
            "error_type":       error_type,
            "error_message":    str(error_message)[:500],
            "raw_payload":      raw_payload,
        }
        try:
            data = json.dumps(payload, default=str).encode()
            req = urllib.request.Request(
                f"{self.base}/rest/v1/email_processing_errors",
                data=data,
                headers=self.headers,
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
            log.warning(f"  [ERRO-LOG] {error_type}: {error_message[:120]}")
            return True
        except Exception as e:
            log.exception(f"Falha ao gravar erro no Supabase: {e}")
            return False

    def register_financial(self, payload: dict):
        """UPSERT de uma conta extraida em financial_account_control (service_role).

        Deduplica/atualiza por gmail_message_id. A situacao e gravada por status_id (FONTE
        UNICA): _apply_status_id traduz o `status` texto do payload -> status_id e remove o
        texto. 'pendente' cai no DEFAULT 3 do banco; a trigger id-primaria (068) recalcula
        'a vencer'/'vencido' por due_date x extracted_at quando em aberto (preserva 'falha').

        Retorna o ID da conta gravada (int) ou None em falha. O id e necessario para
        registrar o anexo em financial_account_attachment (migration 079), que so pode ser
        feito DEPOIS da conta existir. Como o id (IDENTITY, comeca em 1) e sempre truthy e
        None e falsy, os call sites no formato `if ctrl.register_financial(payload):`
        continuam valendo sem alteracao.
        """
        if not self._available:
            return None
        try:
            # Copia — nao muta o dict do chamador ao traduzir status. 🔴 O
            # strip_transient_fields e' a FRONTEIRA: o payload e' serializado INTEIRO logo
            # abaixo, entao qualquer chave efemera ('_'-prefixada) que sobrasse viraria
            # coluna inexistente e o PostgREST recusaria o INSERT com PGRST204 — a conta
            # deixaria de ser gravada. Aqui, em ponto unico, e nao em cada call site.
            payload = strip_transient_fields(payload)
            _apply_barcode_due_date(payload)  # rede de seguranca: vencimento pelo fator do barcode
            _apply_status_id(payload)
            # Empresa pagadora (regra LEBIANCO) — rede de seguranca UNIVERSAL: os caminhos que
            # veem o corpo/anexo ja gravaram sk_company (e este no-op respeita), mas qualquer
            # outro caller (scripts de reprocessamento) cai aqui e resolve pelos campos do
            # payload. Sem isso, sk_company iria NULL e o trigger resolveria pelo CNPJ — o
            # oposto da regra (a referencia vence o CNPJ).
            apply_sk_company(payload)
            # Autoria (Etapa 1 — visibilidade por dono): dono = usuario do remetente
            # (sender_email -> UUID via RPC), padrao sentinela quando nao casa. So no INSERT;
            # se ja veio created_by, respeita. Falha/None -> DEFAULT da coluna (sentinela).
            if not payload.get("created_by"):
                owner = self.resolve_user(payload.get("sender_email"))
                if owner:
                    payload["created_by"] = owner
            data = json.dumps(payload).encode()
            # return=representation + select=id: devolve a linha GRAVADA (no upsert, a linha
            # MESCLADA), entao o id sai correto tambem no reprocessamento de um e-mail ja visto.
            req = urllib.request.Request(
                f"{self.base}/rest/v1/financial_account_control"
                f"?on_conflict=gmail_message_id&select=id",
                data=data,
                headers={
                    **self.headers,
                    "Prefer": "resolution=merge-duplicates,return=representation",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                rows = json.loads(r.read() or "[]")
            return rows[0]["id"] if rows else None
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            log.exception(f"Erro ao gravar conta no Supabase: {e.code} {body[:200]}")
            return None
        except Exception as e:
            log.exception(f"Erro ao gravar conta no Supabase: {e}")
            return None

    def unique_invoice_number(self, base: str) -> str:
        """Retorna o proximo invoice_number unico baseado em base.

        Se base nao existe na tabela, retorna base.
        Se existe, retorna base(2), base(3), etc., ate encontrar um livre.
        Em caso de falha na consulta, retorna base (nao bloqueia a insercao).
        """
        if not self._available:
            return base
        try:
            pattern = urllib.parse.quote(base + '%', safe='')
            req = urllib.request.Request(
                f"{self.base}/rest/v1/financial_account_control"
                f"?select=invoice_number&invoice_number=like.{pattern}&limit=50",
                headers=self.headers
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                existing = {row["invoice_number"] for row in json.loads(r.read())}
        except Exception as e:
            log.warning(f"Nao foi possivel verificar invoice_number no banco: {e}")
            return base

        if base not in existing:
            return base

        n = 2
        while f"{base}({n})" in existing:
            n += 1
        return f"{base}({n})"

    def upload_attachment(self, pdf_path) -> bool:
        """Publica o PDF no bucket de Storage (service_role, ignora RLS).

        A chave do objeto e o proprio nome do arquivo (= source_file gravado em
        financial_account_control), para o frontend gerar a URL assinada direto.
        Idempotente (x-upsert). Falha de upload e NAO-fatal: a conta ja foi
        gravada; o anexo pode ser re-publicado depois. Retorna True em sucesso.
        """
        if not self._available:
            return False
        try:
            data = pdf_path.read_bytes()
        except Exception as e:
            log.warning(f"Falha ao ler PDF p/ upload {pdf_path.name}: {e}")
            return False
        key = urllib.parse.quote(pdf_path.name, safe="")
        # Content-Type por extensão (PDF ou imagem) — para o frontend exibir o
        # anexo corretamente pela URL assinada (antes era fixo em application/pdf).
        content_type = _UPLOAD_CONTENT_TYPES.get(
            Path(pdf_path).suffix.lower(), "application/octet-stream")
        try:
            req = urllib.request.Request(
                f"{self.base}/storage/v1/object/{STORAGE_BUCKET}/{key}",
                data=data,
                headers={
                    "apikey":        self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type":  content_type,
                    "x-upsert":      "true",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=30)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"Falha ao subir anexo {pdf_path.name}: {e.code} {body[:150]}")
            return False
        except Exception as e:
            log.warning(f"Falha ao subir anexo {pdf_path.name}: {e}")
            return False

    def register_attachment(self, account_id: int, file_name: str,
                            size_bytes: int = 0, uploaded_by: str | None = None) -> bool:
        """Vincula o anexo ja publicado no Storage a uma conta (migration 079).

        Chamado DEPOIS da conta existir (o upload roda antes, quando ainda nao ha id) — e
        SEMPRE que a conta e criada, mesmo se o upload falhou: a tabela espelha a semantica
        do source_file (padrao unico com os anexos manuais). Objeto ausente cai no estado
        'notfound' do visualizador, que ja existe.

        storage_key == file_name: a chave do objeto do pipeline e o nome flat (sem pasta) —
        diferente do anexo manual, que usa `manual/{conta}/...`.

        Idempotente (on_conflict=account_id,storage_key + ignore-duplicates), para o
        reprocessamento do mesmo e-mail nao duplicar. NAO-FATAL, igual ao upload: a conta ja
        esta gravada e e o artefato primario.
        """
        if not self._available:
            return False
        payload = {
            "account_id":  account_id,
            "storage_key": file_name,
            "file_name":   file_name,
            "mime_type":   _UPLOAD_CONTENT_TYPES.get(
                Path(file_name).suffix.lower(), "application/octet-stream"),
            "size_bytes":  max(0, int(size_bytes or 0)),
            "origin":      "pipeline",
        }
        if uploaded_by:
            payload["uploaded_by"] = uploaded_by
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/financial_account_attachment"
                f"?on_conflict=account_id,storage_key",
                data=json.dumps(payload).encode(),
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"Falha ao registrar anexo {file_name}: {e.code} {body[:150]}")
            return False
        except Exception as e:
            log.warning(f"Falha ao registrar anexo {file_name}: {e}")
            return False

    def register_fiscal_document(self, doc: dict, storage_key: str | None = None,
                                 ctx: dict | None = None) -> bool:
        """Registra um documento fiscal identificado pela chave de acesso (migration 107).

        `doc` e o dict devolvido por `fiscal_key.parse_access_key` — ja validado (UF, mes,
        modelo e DV). Aqui nao se valida de novo: duplicar a regra criaria duas fontes de
        verdade que divergem no primeiro modelo novo.

        Idempotente por `on_conflict=access_key` + `ignore-duplicates`: a mesma chave chegando
        de novo (reenvio, encaminhamento, reprocessamento) e o MESMO documento.

        Devolve True SO quando a linha foi de fato INSERIDA — por isso o
        `return=representation`, que faz a duplicata voltar como lista vazia. Sem ele o
        metodo diria "registrado" tambem para o que ja existia, e o log em producao (a via
        pela qual se confere se a onda esta funcionando) mentiria no reprocessamento.

        NAO-FATAL, igual ao `register_attachment`: nao altera o status do e-mail, nao cria
        conta e engole a propria falha. Documento fiscal e PROVENIENCIA — perde-lo e ruim,
        mas derrubar a extracao financeira por causa dele seria pior.
        """
        if not self._available:
            return False
        ctx = ctx or {}
        payload = {
            "access_key":      doc["access_key"],
            "model":           doc["model"],
            "uf_code":         doc["uf_code"],
            "issue_yearmonth": doc["issue_yearmonth"],
            "emitter_cnpj":    doc["emitter_cnpj"],
            "series":          doc["series"],
            "doc_number":      doc["doc_number"],
            "storage_key":     storage_key,
            # Proveniencia: de qual e-mail veio. Sem FK de proposito (o registro e append-only
            # e nao-fatal; uma FK faria limpeza de e-mail abortar por causa dele).
            "gmail_message_id": ctx.get("message_id"),
            "sender_email":     ctx.get("sender_email"),
            "subject":          ctx.get("subject"),
            "received_at":      ctx.get("received_at"),
        }
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/fiscal_document?on_conflict=access_key&select=id",
                data=json.dumps(payload).encode(),
                headers={**self.headers,
                         "Prefer": "resolution=ignore-duplicates,return=representation"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                # Lista vazia = a chave ja existia (o INSERT virou no-op). Nao e erro.
                return bool(json.loads(r.read() or b"[]"))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"Falha ao registrar documento fiscal {doc.get('access_key')}: "
                        f"{e.code} {body[:150]}")
            return False
        except Exception as e:
            log.warning(f"Falha ao registrar documento fiscal {doc.get('access_key')}: {e}")
            return False

    def update_fiscal_content(self, item: dict) -> bool:
        """Grava o conteudo do CT-e (peso, rota, NF, frete) na linha ja registrada — Onda 5.

        ATUALIZA, nunca insere: o documento so ganha conteudo depois de existir, e quem o cria
        e o `register_fiscal_document`. Chave que ainda nao esta na tabela simplesmente nao
        recebe nada — inserir aqui criaria um documento sem passar pela validacao de 5 camadas
        da chave de acesso.

        Nao sobrescreve conteudo de OUTRA fonte (`content_source=neq.dacte_llm` no filtro), para
        o dia em que o DACTE for lido por LLM: um dado mais rico nao pode ser rebaixado por uma
        passada deterministica que rodou depois.

        NAO-FATAL, como todo o gancho fiscal: engole a propria falha e devolve False.
        """
        if not self._available or not item.get("access_key"):
            return False
        payload = {
            "awb":                  item.get("awb"),
            "origin":               item.get("origin"),
            "destination":          item.get("destination"),
            "service_date":         item["service_date"].isoformat() if item.get("service_date") else None,
            # Decimal nao e serializavel em JSON — str preserva a precisao (float nao).
            "cargo_weight_kg":      str(item["cargo_weight_kg"]) if item.get("cargo_weight_kg") is not None else None,
            "cargo_amount":         str(item["cargo_amount"]) if item.get("cargo_amount") is not None else None,
            "freight_amount":       str(item["freight_amount"]) if item.get("freight_amount") is not None else None,
            "linked_invoice":       item.get("linked_invoice"),
            "receiver_name":        item.get("receiver_name"),
            "content_source":       "braspress_invoice",
            "content_extracted_at": "now()",
        }
        url = (f"{self.base}/rest/v1/fiscal_document"
               f"?access_key=eq.{item['access_key']}&content_source=not.eq.dacte_llm")
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={**self.headers, "Prefer": "return=minimal"}, method="PATCH")
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"    [FISCAL] falha ao gravar conteudo de {item['access_key'][-8:]}: "
                        f"{e.code} {body[:150]}")
            return False
        except Exception as e:  # noqa: BLE001 — best-effort por design
            log.warning(f"    [FISCAL] falha ao gravar conteudo de {item['access_key'][-8:]}: {e}")
            return False

    def find_financial_duplicate(self, payload: dict) -> dict | None:
        """Retorna a conta existente que representa o MESMO documento, ou None.

        Cobre a duplicidade real que a dedup por message_id NAO pega: o mesmo
        remetente envia o MESMO documento em dois e-mails diferentes (Message-ID
        distintos). Considera duplicata se QUALQUER impressao digital casar:
          1. barcode (linha digitavel / codigo de barras / chave) — definitivo;
          1b. fornecedor + NOSSO NUMERO — identificador estavel do titulo no banco: a
             2a via / aviso de vencimento mantem o mesmo nosso numero mesmo mudando
             VALOR (juros) e VENCIMENTO, combinacao que 1/2/3 deixam passar (ids 323/560);
          2. fornecedor + numero do documento + valor — pega DAS reenviado com
             vencimento diferente (numero da guia identico);
          3. fornecedor + valor + vencimento (+ tipo) — pega o mesmo encargo
             emitido em documentos com numero proprio distinto (ex.: boleto x
             RPS da mesma fatura, ambos R$ X no mesmo vencimento). Quando o NOVO
             documento tem barcode (boleto autoritativo), casa a conta do CORPO/
             notificacao da mesma divida IGNORANDO o document_type — fecha o gap
             cross-e-mail em que o corpo gravou tipo generico ('outro') e o boleto
             chega depois como 'boleto' (ids 7/176, 217/218), que a exigencia de
             tipo igual deixava passar, criando conta duplicada.
        As impressoes 2 e 3 sao complementares — uma sozinha deixa passar casos
        que a outra pega; por isso ambas sao verificadas.

        Retorna a 1a conta encontrada (id, due_date, barcode) para o chamador
        decidir entre pular ou ATUALIZAR (ex.: reemissao com vencimento mais novo).
        Conservador: so deduplica com um identificador de fornecedor presente.
        Em caso de erro de consulta, retorna None (nao bloqueia a insercao).
        """
        if not self._available:
            return None

        def _eq_clause(col: str, v) -> str:
            if v in (None, ""):
                return f"{col}=is.null"
            sval = f"{float(v):.2f}" if col == "amount" else str(v)
            return f"{col}=eq.{urllib.parse.quote(sval, safe='')}"

        def _find(clauses: list, select: str = "id,due_date,barcode") -> dict | None:
            # Re-tenta em falha TRANSITORIA (rede/timeout): uma consulta que falha
            # e retorna None seria lida como "sem duplicata" e criaria conta
            # duplicada. Um resultado vazio (rows == []) NAO e erro — retorna None
            # de imediato (nao re-tenta). Esgotadas as tentativas, retorna None
            # (nao bloqueia a insercao indefinidamente) apos logar.
            filters = "&".join(clauses)
            last_err = None
            for attempt in range(1, DUP_QUERY_ATTEMPTS + 1):
                try:
                    req = urllib.request.Request(
                        f"{self.base}/rest/v1/financial_account_control"
                        f"?{filters}&select={select}&limit=1",
                        headers=self.headers,
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        rows = json.loads(r.read())
                        return rows[0] if rows else None
                except Exception as e:
                    last_err = e
                    if attempt < DUP_QUERY_ATTEMPTS:
                        time.sleep(DUP_QUERY_BACKOFF * attempt)
            log.warning(
                f"Falha na checagem de duplicidade de conteudo apos "
                f"{DUP_QUERY_ATTEMPTS} tentativas: {last_err}"
            )
            return None

        # 1. Barcode — identificador definitivo do documento de pagamento.
        barcode = (payload.get("barcode") or "").strip()
        if barcode:
            m = _find([f"barcode=eq.{urllib.parse.quote(barcode, safe='')}"])
            if m:
                return m

        # 2/3. Precisam do fornecedor ja resolvido (sk_supplier). A resolucao
        # (incl. normalizacao de nome/CNPJ via resolve_supplier_id) acontece em
        # _finalize_supplier ANTES da dedup, entao aqui basta casar por sk_supplier.
        sk_supplier = payload.get("sk_supplier")
        if not sk_supplier:
            return None  # sem fornecedor resolvido — nao deduplica

        supplier_clause = f"sk_supplier=eq.{int(sk_supplier)}"

        # 1b. fornecedor + NOSSO NÚMERO — identificador ESTÁVEL do título no banco. Uma
        # reemissão / 2ª via / aviso de vencimento mantém o MESMO nosso número, mesmo
        # mudando VALOR (juros) e VENCIMENTO — combinação que as impressões 1/2/3 deixam
        # passar (barcode difere pelo fator/valor; 2/3 exigem valor/vencimento iguais).
        # Falha real ids 323/560 (fatura SIEG reemitida: +juros e venc +1 dia → 3 boletos
        # distintos pela dedup, mas MESMO nosso número 000000091070-8). Escopo por
        # fornecedor (o nosso número é único por beneficiário/título). Só com nosso número
        # substancial (>= 8 dígitos, não-zero) para não fundir títulos distintos.
        # GUARDA (conta 316/TRT, 2026-08-04 — nao regredir): o campo extraido como "nosso
        # numero" NEM SEMPRE identifica o TITULO. Em alguns layouts o LLM copia o codigo
        # AGENCIA/CONTA do cedente (ex.: "0001/0000515-6"), que e o MESMO em todos os
        # boletos daquele fornecedor — e ai a 1b funde a mensalidade de agosto com a de
        # julho e o pagavel se PERDE em silencio (o e-mail fica 'extraido', sem conta).
        # Discriminador: uma REEMISSAO e o MESMO titulo, entao carrega o MESMO numero de
        # documento; boletos DISTINTOS tem numeros distintos. Medido: no caso que criou a
        # 1b (SIEG 323/560) `invoice_number` == `nosso_numero` nos dois; no TRT os numeros
        # diferem (00561066674 x 00569007593) com o nosso_numero identico.
        # So descarta quando AMBOS os numeros sao PROPRIOS (nao sinteticos) e diferem —
        # sem numero proprio de um dos lados, o nosso numero segue valendo sozinho.
        nosso = str(payload.get("nosso_numero") or "").strip()
        if _is_real_nosso_numero(nosso):
            m = _find([supplier_clause,
                       f"nosso_numero=eq.{urllib.parse.quote(nosso, safe='')}"],
                      select="id,due_date,barcode,invoice_number")
            if m and not _same_title(payload.get("invoice_number"), m.get("invoice_number")):
                log.info(
                    "    [DEDUP-1b] nosso numero igual mas Nº de documento diferente "
                    f"({payload.get('invoice_number')!r} x {m.get('invoice_number')!r}) — "
                    "titulos distintos, nao deduplica"
                )
                m = None
            if m:
                return m

        # 2. fornecedor + numero do documento + valor (numero substancial).
        # IGNORA numero SINTETICO (PIX_/{tipo}_{ddmmaa}): ele colide entre boletos
        # distintos do mesmo fornecedor/valor/vencimento (ex.: parcelas com
        # vencimento defaultado p/ a data da extracao), causando falso-positivo de
        # duplicidade. So o numero PROPRIO do documento e chave confiavel aqui; o
        # codigo de barras distingue os demais (impressoes 1 e 3).
        invoice = str(payload.get("invoice_number") or "").strip()
        if len(invoice) >= 6 and not _is_synthetic_invoice_number(invoice):
            m = _find([
                supplier_clause,
                f"invoice_number=eq.{urllib.parse.quote(invoice, safe='')}",
                _eq_clause("amount", payload.get("amount")),
            ])
            if m:
                return m

        # 3. fornecedor + valor + vencimento — MESMA divida, INDEPENDENTE do tipo.
        # Regra de negocio: se fornecedor + valor + vencimento coincidem, e a mesma
        # conta a pagar (o tipo varia entre os documentos que a descrevem — 'boleto'
        # no PDF, 'fatura'/'outro'/'pix' no corpo). Exigir tipo igual deixava passar
        # duplicatas cross-e-mail em QUALQUER ordem de chegada (ids 7/176, 217/218,
        # 511/512). O document_type NAO entra mais na impressao 3.
        base3 = [supplier_clause,
                 _eq_clause("amount", payload.get("amount")),
                 _eq_clause("due_date", payload.get("due_date"))]
        if barcode:
            # NOVO doc tem codigo de barras: so casa candidatos SEM barcode. Barcode
            # presente e DIFERENTE = documento distinto (a impressao 1 ja teria casado
            # se fossem o mesmo) — assim boletos distintos de mesmo valor/vencimento
            # (parcelas, guias GNRE de R$ 399,03) NAO se fundem, cada um com sua linha
            # digitavel. O candidato sem barcode e a conta do corpo/notificacao da
            # mesma divida.
            return _find(base3 + ["barcode=is.null"])
        # NOVO sem barcode (corpo / notificacao / reemissao): casa qualquer conta da
        # mesma divida (fornecedor+valor+vencimento), inclusive um boleto ja gravado
        # com barcode — o documento sem linha digitavel nunca e um 2o pagavel legitimo.
        return _find(base3)

    def resolve_supplier(self, payload: dict) -> int | None:
        """Resolve/cria o fornecedor via RPC resolve_supplier_for_account e devolve
        sk_supplier (surrogate key). Reusa a mesma logica antes embutida no trigger
        trg_fe_resolve_supplier (resolve_supplier_id por CNPJ->CPF->e-mail->nome->auto-insert
        + anexa o e-mail do remetente). Em erro de consulta, retorna None (o chamador trata
        como falha)."""
        if not self._available:
            return None
        body = json.dumps({
            "p_cnpj":  payload.get("supplier_cnpj"),
            "p_cpf":   payload.get("supplier_cpf"),
            "p_name":  payload.get("supplier_name"),
            "p_email": payload.get("sender_email"),
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/rpc/resolve_supplier_for_account",
                data=body, headers=self.headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())  # RPC escalar → o proprio bigint
        except Exception as e:
            log.warning(f"Falha ao resolver fornecedor (RPC): {e}")
            return None

    def find_supplier_by_email(self, email: str | None) -> int | None:
        """Fornecedor JA CADASTRADO e ATIVO cujo email/email2/email3/email4 casa `email`
        (RPC find_supplier_by_email, migration 134).

        🔴 CONSULTA PURA — NUNCA cria fornecedor. E' exatamente isto que a separa de
        `resolve_supplier`, cuja RPC termina em auto-insert. Usada para o remetente
        ORIGINAL de um bloco encaminhado no corpo, um sinal FRACO (diz quem MANDOU o
        documento, nao quem RECEBE o pagamento): ele pode identificar um cadastro
        curado, jamais criar um.

        A comparacao (lower/trim, e-mail interno e de plataforma barrados) vive na RPC,
        nao aqui — uma 2a copia da regra em Python divergiria da do banco no primeiro
        ajuste, sem erro nenhum.

        Devolve sk_supplier ou None (nao cadastrado, e-mail interno/plataforma, RPC
        ausente ou falha de rede). Em duvida NAO atribui: o chamador segue para os
        fallbacks que ja existiam."""
        if not self._available or not email:
            return None
        body = json.dumps({"p_email": email}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/rpc/find_supplier_by_email",
                data=body, headers=self.headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()) or None  # RPC escalar → o proprio bigint
        except Exception as e:
            log.warning(f"Falha ao consultar fornecedor por e-mail encaminhado (RPC): {e}")
            return None

    def resolve_user(self, sender_email: str | None) -> str | None:
        """Resolve o UUID do usuario dono da conta a partir do e-mail do remetente,
        via RPC resolve_user_for_account (migration 076). A RPC ja devolve o usuario-
        SENTINELA (financeiro@otimotex.com.br desde a migration 110; era teste@otimotex.com.br)
        quando o e-mail nao casa nenhum usuario — mantem 100% do relacionamento. Em erro/sem
        e-mail retorna None (o DEFAULT da coluna created_by assume o mesmo sentinela).

        NAO ha UUID embutido aqui, e e' deliberado: o fallback vive na RPC e no DEFAULT da
        coluna, do lado do banco. Trocar a identidade do sentinela e' migration, sem deploy
        do pipeline.

        CACHEADO por e-mail (case-insensitive) dentro da instancia: a mesma caixa repete
        muito o remetente, e o resultado nao muda durante um run. Sem o cache, cada conta
        gravada pagaria uma RPC — e o vinculo do anexo (register_attachment) pagaria outra.
        Cache lazy (getattr): a classe tambem e instanciada via __new__ nos testes, sem __init__.
        """
        if not self._available or not sender_email:
            return None
        cache = getattr(self, "_user_cache", None)
        if cache is None:
            cache = {}
            self._user_cache = cache
        key = sender_email.strip().lower()
        if key in cache:
            return cache[key]
        body = json.dumps({"p_email": sender_email}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/rpc/resolve_user_for_account",
                data=body, headers=self.headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                owner = json.loads(r.read())  # RPC escalar → UUID (str)
        except Exception as e:
            log.warning(f"Falha ao resolver usuario dono (RPC): {e}")
            return None   # NAO cacheia a falha: o proximo e-mail tenta de novo
        cache[key] = owner
        return owner

    def supplier_defaults(self, sk_supplier: int) -> tuple[int, int]:
        """Le a classificacao contabil DEFAULT do fornecedor (migration 052) para
        pre-preencher a conta na extracao. Retorna (cost_center_id, chart_account_id);
        (0, 0) em ausencia/erro (sentinela 'nao informado')."""
        if not self._available or not sk_supplier:
            return (0, 0)
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/supplier"
                f"?sk_supplier=eq.{int(sk_supplier)}"
                f"&select=cost_center_id,chart_account_id&limit=1",
                headers=self.headers,
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                rows = json.loads(r.read())
            if not rows:
                return (0, 0)
            row = rows[0]
            return (int(row.get("cost_center_id") or 0), int(row.get("chart_account_id") or 0))
        except Exception as e:
            log.warning(f"Falha ao ler classificacao do fornecedor {sk_supplier}: {e}")
            return (0, 0)

    def update_financial(self, record_id, fields: dict) -> bool:
        """PATCH de uma conta existente — ex.: atualizar vencimento/boleto de uma
        guia reemitida para os dados de pagamento mais recentes. Ignora campos None."""
        if not self._available:
            return False
        clean = {k: v for k, v in fields.items() if v not in (None, "")}
        if not clean:
            return False
        try:
            data = json.dumps(clean).encode()
            req = urllib.request.Request(
                f"{self.base}/rest/v1/financial_account_control?id=eq.{record_id}",
                data=data,
                headers={**self.headers, "Prefer": _PREFER_MINIMAL},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            log.exception(f"Falha ao atualizar conta {record_id}: {e.code} {e.read().decode(errors='replace')[:150]}")
            return False
        except Exception as e:
            log.exception(f"Falha ao atualizar conta {record_id}: {e}")
            return False

    def update_supplier_classification(self, sk_supplier, cost_center_id, chart_account_id) -> bool:
        """Write-back da classificacao contabil no cadastro do fornecedor (PATCH supplier).
        Equivalente Python do TS setSupplierClassification. Best-effort: falha NAO derruba a
        gravacao da conta. O chamador (apply_forced_classification) ja garante a excecao da
        OTIMOTEX (sk=1); aqui so validamos disponibilidade e sk.
        NOTA: fornecedor de FUNCIONARIO (trade_name com 'funcionario') e mantido em 0 pela
        trigger trg_supplier_no_funcionario_classification (migration 070) — este write-back
        vira no-op para eles (a despesa de funcionario varia por conta, sem default no cadastro)."""
        if not self._available or not sk_supplier:
            return False
        try:
            data = json.dumps({
                "cost_center_id":  int(cost_center_id),
                "chart_account_id": int(chart_account_id),
            }).encode()
            req = urllib.request.Request(
                f"{self.base}/rest/v1/supplier?sk_supplier=eq.{int(sk_supplier)}",
                data=data,
                headers={**self.headers, "Prefer": _PREFER_MINIMAL},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            log.warning(f"Falha no write-back de classificacao do fornecedor {sk_supplier}: "
                        f"{e.code} {e.read().decode(errors='replace')[:150]}")
            return False
        except Exception as e:
            log.warning(f"Falha no write-back de classificacao do fornecedor {sk_supplier}: {e}")
            return False

    def update_supplier_contact(self, sk_supplier, pix=None, phone=None, whatsapp=None) -> bool:
        """Write-back de contato (chave PIX / telefone / WhatsApp) no cadastro do
        fornecedor (PATCH supplier). Logica de 2 slots (mesma do trigger
        _add_supplier_email, migration 028): o 2o slot so recebe quando o 1o ja esta
        preenchido e e diferente. Best-effort — falha NAO derruba a gravacao da conta.
        Pula a OTIMOTEX (sk=1). `phone` e a tupla (ddd, fone)."""
        if not self._available or not sk_supplier or sk_supplier == OTIMOTEX_SK_SUPPLIER:
            return False
        if not (pix or phone or whatsapp):
            return False
        try:
            req = urllib.request.Request(
                f"{self.base}/rest/v1/supplier?sk_supplier=eq.{int(sk_supplier)}"
                "&select=pix_key1,pix_key2,phone_ddd1,phone1,phone_ddd2,phone2,"
                "whatsapp1,whatsapp2&limit=1",
                headers=self.headers,
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                rows = json.loads(r.read())
            if not rows:
                return False
            row = rows[0]
            updates: dict = {}
            if pix:
                updates.update(_contact_slot_update(
                    row.get("pix_key1"), row.get("pix_key2"), "pix_key1", "pix_key2", pix))
            if whatsapp:
                updates.update(_contact_slot_update(
                    row.get("whatsapp1"), row.get("whatsapp2"), "whatsapp1", "whatsapp2", whatsapp))
            if phone:
                updates.update(_phone_slot_update(row, phone[0], phone[1]))
            if not updates:
                return False
            data = json.dumps(updates).encode()
            req = urllib.request.Request(
                f"{self.base}/rest/v1/supplier?sk_supplier=eq.{int(sk_supplier)}",
                data=data,
                headers={**self.headers, "Prefer": _PREFER_MINIMAL},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            log.warning(f"Falha no write-back de contato do fornecedor {sk_supplier}: "
                        f"{e.code} {e.read().decode(errors='replace')[:150]}")
            return False
        except Exception as e:
            log.warning(f"Falha no write-back de contato do fornecedor {sk_supplier}: {e}")
            return False

    def classification_for_account_code(self, account_code: str) -> tuple[int, int]:
        """Resolve (cost_center_id, chart_account_id) da linha de financial_chart_of_account
        com o `account_code` informado (regra DAM/DUAM: classificacao vem do plano 4.1.06).
        Retorna a classificacao PROPRIA daquela linha (seu cost_center_id + seu chart_account_id,
        que e a PK/id do plano). (0, 0) quando indisponivel/ausente. Cacheado por codigo."""
        cache = getattr(self, "_chart_code_cache", None)
        if cache is None:
            cache = self._chart_code_cache = {}
        if account_code in cache:
            return cache[account_code]

        result = (0, 0)
        if self._available:
            try:
                code = urllib.parse.quote(account_code, safe="")
                req = urllib.request.Request(
                    f"{self.base}/rest/v1/financial_chart_of_account"
                    f"?account_code=eq.{code}&select=chart_account_id,cost_center_id&limit=1",
                    headers=self.headers,
                )
                rows = json.loads(urllib.request.urlopen(req, timeout=10).read())
                if rows:
                    row = rows[0]
                    result = (int(row.get("cost_center_id") or 0),
                              int(row.get("chart_account_id") or 0))
            except Exception as e:
                log.warning(f"Falha ao resolver classificacao do plano {account_code!r}: {e}")
        cache[account_code] = result
        return result

    def load_known_ids(self) -> set:
        """Carrega todos os message_id de email_control em um set local.

        Substitui N chamadas individuais is_processed() por uma única consulta,
        eliminando a latência por e-mail no loop principal de deduplicação.
        """
        if not self._available:
            return set()
        all_ids, offset, chunk = set(), 0, 1000
        try:
            while True:
                req = urllib.request.Request(
                    f"{self.base}/rest/v1/email_control"
                    f"?select=message_id&limit={chunk}&offset={offset}",
                    headers=self.headers,
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    rows = json.loads(r.read())
                all_ids.update(row["message_id"] for row in rows if row.get("message_id"))
                if len(rows) < chunk:
                    break
                offset += chunk
            return all_ids
        except Exception as e:
            log.warning(f"Falha ao carregar IDs em lote: {e}")
            return set()

    @staticmethod
    def _derive_status(rec: dict) -> str:
        # email_control.status (CHECK migration 022). Fallback quando process_message
        # nao definiu o status explicitamente (ex.: caminho de excecao).
        if rec.get("notes") and "erro" in str(rec.get("notes", "")).lower():
            return "falha"
        if rec.get("pdf_extracted"):
            return "extraído" if rec.get("has_attachment") else "recebido"
        if rec.get("attachment_saved"):
            return "pendente"
        return "falha"


import urllib.parse  # necessário para quote no is_processed

# ---------------------------------------------------------------------------
# Helpers de decodificação e utilitários
# ---------------------------------------------------------------------------
def decode_str(value: str) -> str:
    if not value: return ""
    parts = decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                # Charset desconhecido (ex: "unknown-8bit") — fallback para latin-1
                result.append(part.decode("latin-1", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result).strip()


def get_body_text(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body += part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/plain":
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                pass
    return body.strip()


def get_body_html(msg) -> str:
    """Extrai a parte HTML do e-mail para busca de links."""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/html" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                try:
                    html += part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            try:
                html = msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                pass
    return html


# Texto plano que NAO e conteudo: apenas avisa que a mensagem esta em HTML. Quem manda
# assim e o multipart/alternative cujo text/plain e so um aviso (ex.: a plataforma SSW,
# que envia "O conteudo deste e-mail esta somente disponivel em HTML" — 55 chars).
# Casa nos DOIS sentidos: o aviso tanto vem como "... disponivel em HTML" quanto como
# "enable HTML to view this email".
_PLACEHOLDER_BODY_RE = re.compile(
    r"\bhtml\b[^\n]{0,40}(?:dispon|visualiz|view|habilit|enable|suporte|support|leia|read)"
    r"|(?:dispon|visualiz|view|habilit|enable|somente|apenas|only|unicamente)[^\n]{0,40}\bhtml\b",
    re.IGNORECASE,
)
# Teto de tamanho: o aviso e UMA frase curta. Sem isso, um corpo real que mencione HTML
# (ex.: uma fatura de agencia web) seria descartado e o e-mail perderia o proprio texto.
_PLACEHOLDER_BODY_MAX_CHARS = 200


def _plain_body_is_placeholder(text: "str | None") -> bool:
    """O texto plano e apenas um aviso de "conteudo em HTML"?

    Serve para decidir se vale cair no HTML. NAO basta testar `if not body_text`: o aviso
    e uma string NAO-vazia, entao o fallback antigo nunca disparava para esses e-mails —
    e o corpo real (que traz o CEDENTE da fatura, entre outros dados) nunca era lido.

    Conservador de proposito: exige o padrao do aviso E um texto curto. Corpo curto e
    LEGITIMO e a norma neste projeto ("FORNECEDOR X / VALOR / VENCIMENTO" cabe em 90
    chars), logo um criterio por tamanho sozinho descartaria conteudo bom.

    >>> _plain_body_is_placeholder("O conteudo deste e-mail esta somente disponivel em HTML")
    True
    >>> _plain_body_is_placeholder("FORNECEDOR HORAS EXTRAS\\n\\nVALOR R$ 9.864,00")
    False
    """
    if not text:
        return False
    limpo = text.strip()
    if not limpo or len(limpo) > _PLACEHOLDER_BODY_MAX_CHARS:
        return False
    return bool(_PLACEHOLDER_BODY_RE.search(_ns_body(limpo)))


def _html_to_text(html: str) -> str:
    """Converte HTML em texto plano para a extracao de corpo de e-mails SO-HTML
    (ex.: Correios), onde get_body_text() volta vazio. Remove script/style, troca
    quebras de bloco por '\\n', tira as demais tags, desescapa entidades e colapsa
    espacos — preservando rotulos como 'Fatura nº: 3918439' e 'R$ 1.530,47'."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html_unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# Remetente (dominio) -> nome de fornecedor conhecido. Usado na extracao de corpo
# quando NAO ha rotulo ("Fornecedor:") nem CNPJ/CPF: para esses remetentes o nome
# real e estavel e o corpo nao o traz. Ex.: Correios envia de
# noreply_componentes@correios.com.br e o corpo so diz "Email Correios".
_SENDER_SUPPLIER_MAP = {"correios.com.br": "Correios"}


def _supplier_from_sender(sender_email: str | None) -> "str | None":
    """Nome de fornecedor conhecido a partir do dominio do remetente, ou None."""
    domain = (sender_email or "").split("@")[-1].lower().strip()
    if not domain:
        return None
    for known, name in _SENDER_SUPPLIER_MAP.items():
        if domain == known or domain.endswith("." + known):
            return name
    return None


# Assunto como ULTIMO recurso para o nome do fornecedor/favorecido.
# E-mails internos de pagamento ("PAGAMENTO BOLETO HYOSUNG 181063-3",
# "PAGAMENTO PIX FULANO") nomeiam o favorecido no assunto, mas o anexo/imagem
# nem sempre traz nome/CNPJ/CPF e o remetente interno (@otimotex/@lebianco) e
# BLOQUEADO como fornecedor (migration 046) — sem este fallback a conta (com
# valor e codigo de barras) era PERDIDA como db_erro. Remove prefixos de
# encaminhamento (ENC:/RES:/RE:/FWD:), as palavras de ACAO de pagamento e a
# cauda de numero de documento, devolvendo o nome do favorecido.
_SUBJECT_FWD_PREFIX_RE = re.compile(
    r"^\s*(?:enc|res|re|fw|fwd|encaminhar|encaminhado)\s*[:.]\s*", re.IGNORECASE)
_SUBJECT_PAYMENT_PREFIX_RE = re.compile(
    r"^(?:\s*(?:pagamento|pagar|pgto|pgmto|boleto|bol|pix|ted|doc|"
    r"transfer[eê]ncia|dep[oó]sito|fatura|guia|t[ií]tulo|cobran[cç]a|"
    r"comprovante|recibo)\b[ \t.:\-/]*)+",
    re.IGNORECASE)
# Cauda de numero de documento ("181063-3", "Nº 12345", "- 998407913").
_SUBJECT_DOCNUM_TAIL_RE = re.compile(
    r"[ \t]*[-–—]?[ \t]*(?:n[º°o.]?\s*)?\d[\d.\-/_]*\s*$", re.IGNORECASE)

# Siglas de razao social brasileira. Servem de ANCORA para achar o nome do
# fornecedor no assunto quando a extracao nao trouxe nome/CNPJ/CPF: a razao social
# quase sempre TERMINA numa dessas formas (o usuario pediu LTDA explicitamente; as
# demais sao formas societarias comuns). Casam como palavra inteira, sem acento/caixa.
# 'ME'/'SA' isolados (sem separador) ficam de fora — ruidosos demais.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:ltda|eireli|epp|mei)\b\.?"   # LTDA / EIRELI / EPP / MEI (+ ponto opcional)
    r"|\bs\s*[./]\s*a\b\.?",            # S/A, S.A, S.A. (formas com separador)
    re.IGNORECASE)
# Separadores que quebram o assunto em segmentos — o nome esta no segmento da sigla
# (o prefixo "FATURAMENTO --" / "ENC:" fica em outro segmento). Hifen SIMPLES nao
# separa (nomes usam hifen); virgula tambem nao (pode compor razao social).
_SUBJECT_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:-{2,}|[–—|:])\s*")
# Ruido de assunto de faturamento/cobranca removido do inicio do nome quando ele
# NAO foi isolado por um separador ("FATURAMENTO ACME LTDA" -> "ACME LTDA").
_SUBJECT_NOISE_PREFIX_RE = re.compile(
    r"^(?:\s*(?:faturamento|faturas|financeiro|cobran[cç]a)\b[ \t.:\-/]*)+",
    re.IGNORECASE)
# Conectivos que NUNCA iniciam uma razao social: o que resta do assunto comecando por
# eles e a continuacao da frase, nao um nome ("Boleto REFERENTE a assinatura ..." —
# conta 694). Guarda de _supplier_name_from_subject contra criar fornecedor-lixo.
# "de/da/do" ficam DE FORA de proposito: iniciam nomes reais ("DE NADAI ALIMENTACAO").
_SUBJECT_NON_NAME_START_RE = re.compile(
    r"^(?:referente|refer\.?|ref\.?|relativ[oa]|sobre|conforme|acerca)\b",
    re.IGNORECASE)


def _norm_term(s: str | None) -> str:
    """Normaliza (NFD strip de acentos + lowercase) para comparar termos."""
    return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()


# Termos que NUNCA sao fornecedor — TIPOS DE DOCUMENTO + TIPOS DE PAGAMENTO (e os
# acronimos de tributo). Um e-mail interno "ENC: GUIA GNRE" reduzia para "GNRE"
# (um tipo de documento) e virava fornecedor — errado. A lista espelha o CHECK de
# document_type (+ keywords de tributo de WORD_KEYWORDS / _DOC_TYPE_NORM) e os
# PAYMENT_METHODS do @sheild/shared. Tudo normalizado (sem acento, minusculo).
_NON_SUPPLIER_TERMS = frozenset(_norm_term(t) for t in {
    # tipos de documento (CHECK + sinonimos de extracao)
    "boleto", "cte", "ct-e", "nfe", "nf-e", "nfse", "nfs-e", "nota fiscal",
    "tributo", "seguro", "recibo", "contrato", "fatura", "fechamento",
    "cobranca", "cobrança", "outro", "outros", "honorario", "honorarios",
    "honorário", "honorários", "container", "conteiner", "contêiner",
    "conhecimento de transporte", "dacte", "guia", "cambio", "câmbio",
    "conta de agua", "conta de água", "conta de luz",
    "conta de telefone", "internet", "conta de telefone / internet",
    # acronimos de tributo / guias
    "dar", "darf", "gps", "das", "simples nacional", "simei", "gru", "dae", "dare",
    "gnre", "ipva", "iptu", "dam", "duam", "dam / duam", "iss", "itbi", "gare",
    "multa", "penalidade",
    # tipos de pagamento (PAYMENT_METHODS)
    "pix", "ted", "doc", "cartao", "cartão", "deposito", "depósito",
    "duplicata", "bancario", "bancário", "carteira", "vale", "credito",
    "crédito", "debito", "débito", "dinheiro", "transferencia", "transferência",
    "cheque",
})


def _is_non_supplier_term(name: str | None) -> bool:
    """True quando `name` E, no todo, um tipo de documento/pagamento (ex.: 'GNRE',
    'Boleto', 'PIX', 'DARF SP') — robustez para nao cadastrar um TIPO como
    fornecedor. NAO rejeita nomes que apenas CONTÊM a palavra ('Porto Seguro',
    'Vale Fertilizantes' permanecem fornecedores validos)."""
    n = re.sub(r"\s+", " ", _norm_term(name)).strip(" .-/")
    if not n:
        return False
    if n in _NON_SUPPLIER_TERMS:
        return True
    # acronimo isolado + ruido (UF de 2 letras / numero solto): "gnre mg", "darf sp".
    core = [t for t in re.split(r"[\s/]+", n) if t and not t.isdigit() and len(t) > 2]
    return len(core) == 1 and core[0] in _NON_SUPPLIER_TERMS


# Tipos de documento que sao IMPOSTO/tributo (guia de arrecadacao). Lista definida
# com o usuario. Para esses, quando a extracao NAO traz favorecido real, o credor e o
# orgao arrecadador (Fisco), que a extracao nao captura — a conta e lancada sob a
# OTIMOTEX (a empresa pagadora). Ver _finalize_supplier. NAO inclui 'gps' (INSS) por
# decisao do usuario; 'multa' tambem fica de fora (pode ter fornecedor proprio).
# SUPERSET proposital: alem dos valores do dominio, guarda as formas isoladas dos tipos
# COMPOSTOS ('dam'/'duam' e 'dar'/'dare'), que aparecem como rotulo cru antes da
# normalizacao. O teste de paridade valida a direcao dominio -> emitido, nao o inverso.
_TAX_DOCUMENT_TYPES = frozenset({
    "darf", "das", "gru", "dae", "gnre", "ipva", "iptu",
    "dar", "dare", "dar / dare",
    "dam", "duam", "dam / duam", "iss", "itbi", "gare", "tributo",
})

# sk_supplier (= supplier_id) da OTIMOTEX, a propria empresa pagadora (constante do
# sistema). Guias de imposto sem favorecido identificavel sao lancadas sob a OTIMOTEX.
OTIMOTEX_SK_SUPPLIER = 1

# Fornecedores EXCLUIDOS da classificacao contabil TRIBUTARIA deterministica. Ex.: "Dr. Ricardo"
# (sk 1262) e um despachante — suas contas sao REEMBOLSO de tributos, honorarios e outros tipos
# JURIDICOS (Juridico/Reembolsos), NUNCA conta fiscal/tributaria pura, ainda que o documento seja
# uma guia de arrecadacao (Junta Comercial etc.). Para esses, a regra tributaria NAO forca —
# preserva o default do fornecedor / o valor da extracao. Ver memoria dr-ricardo-reembolso.
TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS = frozenset({1262})

# ── Sinal de PROCEDENCIA do fornecedor (chave EFEMERA do payload) ───────────────────
# Registra COMO o sk_supplier foi resolvido, para que a classificacao forcada saiba se
# pode fazer WRITE-BACK no cadastro. A lista acima e' uma allowlist REATIVA — so protege
# quem alguem ja descobriu e cadastrou a mao; esta marca fecha a CLASSE inteira.
#
# 🔴 POR QUE ELA EXISTE: write-back grava no CADASTRO do fornecedor a classificacao
# derivada do TIPO do documento, e isso vale para TODAS as contas futuras dele. A premissa
# e' "este documento e' deste fornecedor" — verdadeira quando o favorecido foi extraido do
# DOCUMENTO, falsa quando o fornecedor veio de um sinal CIRCUNSTANCIAL do e-mail. Uma guia
# de tributo encaminhada por um despachante e' do FISCO, nao do despachante: gravar
# "tributario" no cadastro dele reescreveria a curadoria manual, em silencio, e propagaria.
#
# 🔴 O PREFIXO "_" E' CONTRATO, NAO ESTILO: `register_financial` serializa o payload
# INTEIRO para o PostgREST, entao qualquer chave que nao seja coluna derrubaria a gravacao
# (PGRST204). Toda chave efemera nasce com "_" e e' removida NA FRONTEIRA de gravacao, em
# ponto unico — nunca espalhada pelos call sites, que era o modo de falha a evitar.
SUPPLIER_SIGNAL_KEY = "_supplier_signal"
SUPPLIER_SIGNAL_FORWARDED_EMAIL = "forwarded_email"   # 1b: e-mail do remetente encaminhado
# Sinais considerados FRACOS para efeito de write-back. Conjunto (e nao um booleano) porque
# a proxima procedencia fraca entra aqui, e nao num `if` novo em apply_forced_classification.
SUPPLIER_SIGNAL_WEAK = frozenset({SUPPLIER_SIGNAL_FORWARDED_EMAIL})


def strip_transient_fields(payload: dict) -> dict:
    """Remove as chaves EFEMERAS (prefixo '_') de uma COPIA do payload.

    Elas carregam metadados de decisao entre etapas do pipeline (ex.:
    SUPPLIER_SIGNAL_KEY) e NAO sao colunas de financial_account_control. Chamada na
    fronteira de gravacao — ver register_financial.

    🔴 Generica por PREFIXO, nao por lista de nomes: uma chave efemera nova passa a ser
    limpa sem que ninguem precise lembrar de atualizar esta funcao. O oposto (allowlist de
    nomes) falharia justamente no caso novo, e o sintoma seria a conta PARAR de gravar.

    Nao muta o dict recebido — devolve outro."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _is_weak_supplier_signal(payload: dict) -> bool:
    """True quando o sk_supplier do payload veio de um sinal CIRCUNSTANCIAL do e-mail, e
    nao do documento. Nesse caso a classificacao forcada ainda vale para a CONTA, mas nao
    pode ser gravada no CADASTRO do fornecedor."""
    return payload.get(SUPPLIER_SIGNAL_KEY) in SUPPLIER_SIGNAL_WEAK


def _is_tax_document(document_type: str | None) -> bool:
    """True quando o document_type e uma guia de IMPOSTO/tributo (darf/das/gnre/...)."""
    return _norm_term(document_type) in _TAX_DOCUMENT_TYPES


def _supplier_name_by_legal_suffix(subject: str | None) -> str:
    """Deriva o nome do fornecedor do ASSUNTO ancorando na SIGLA de razao social
    (LTDA/EIRELI/EPP/MEI/S.A.). A sigla — LTDA em especial — quase sempre faz parte
    do nome do fornecedor, entao serve de ancora confiavel quando a extracao nao
    trouxe nome/CNPJ/CPF do favorecido. Toma o SEGMENTO do assunto que termina na
    sigla, descartando prefixos ("FATURAMENTO --", "ENC:", "PAGAMENTO") e a cauda de
    data/numero apos a sigla. Ex.:
      "ENC: FATURAMENTO -- MOVVI LOGISTICA LTDA 03/07/2026" -> "MOVVI LOGISTICA LTDA"
      "PAGAMENTO ACME COMERCIO S/A - 12345"                 -> "ACME COMERCIO S/A"
    Retorna '' quando nao ha sigla ou o nome resultante e curto/invalido."""
    if not subject:
        return ""
    s = " ".join(str(subject).split())
    m = None
    for m in _LEGAL_SUFFIX_RE.finditer(s):
        pass  # fica com a ULTIMA ocorrencia da sigla no assunto
    if m is None:
        return ""
    head = s[:m.end()]                                   # descarta data/numero apos a sigla
    segment = _SUBJECT_SEGMENT_SPLIT_RE.split(head)[-1]  # nome esta no ultimo segmento
    segment = _SUBJECT_FWD_PREFIX_RE.sub("", segment)    # ENC:/RE: no mesmo segmento
    segment = _SUBJECT_PAYMENT_PREFIX_RE.sub("", segment)  # PAGAMENTO/BOLETO/FATURA inicial
    segment = _SUBJECT_NOISE_PREFIX_RE.sub("", segment)  # FATURAMENTO/FINANCEIRO inicial
    segment = segment.strip(" -–—:.\t")
    if len(segment) < 4 or not re.search(r"[A-Za-zÀ-ÿ]", segment):
        return ""
    return segment


def _supplier_name_from_subject(subject: str | None) -> str:
    """Deriva um nome de fornecedor/favorecido do ASSUNTO (ultimo recurso).

    Retorna '' quando o assunto nao rende um nome utilizavel (vazio, so numeros,
    curto demais OU um TIPO DE DOCUMENTO/PAGAMENTO — ex.: 'GNRE', 'BOLETO').
    Conservador de proposito — so deve ser usado quando a extracao NAO trouxe
    nome/CNPJ/CPF do fornecedor. Ex.:
      "PAGAMENTO BOLETO HYOSUNG 181063-3"        -> "HYOSUNG"
      "ENC: PAGAMENTO PIX SORTEIO BLUSAS - JOAO" -> "SORTEIO BLUSAS - JOAO"
      "FATURAMENTO -- MOVVI LOGISTICA LTDA"      -> "MOVVI LOGISTICA LTDA" (ancora LTDA)
      "ENC: GUIA GNRE"                           -> ""  (tipo de documento)
    """
    if not subject:
        return ""
    # Preferencia: ancorar na SIGLA de razao social (LTDA/EIRELI/S.A./...), que quase
    # sempre nomeia o fornecedor real — nome mais limpo/assertivo que a heuristica
    # generica ("FATURAMENTO -- MOVVI LOGISTICA LTDA" -> "MOVVI LOGISTICA LTDA",
    # descartando o prefixo "FATURAMENTO --").
    by_suffix = _supplier_name_by_legal_suffix(subject)
    if by_suffix and not _is_non_supplier_term(by_suffix):
        return by_suffix
    s = " ".join(str(subject).split())  # colapsa espacos e quebras de linha
    # remove prefixos de encaminhamento repetidos (ENC: RES: RE: FWD:)
    prev = None
    while prev != s:
        prev = s
        s = _SUBJECT_FWD_PREFIX_RE.sub("", s)
    s = _SUBJECT_PAYMENT_PREFIX_RE.sub("", s).strip()
    s = _SUBJECT_DOCNUM_TAIL_RE.sub("", s).strip(" -–—:.\t")
    # precisa restar pelo menos uma LETRA e tamanho minimo (evita "12345", "-")
    if len(s) < 3 or not re.search(r"[A-Za-zÀ-ÿ]", s):
        return ""
    # O que sobra COMECANDO por conectivo nao e nome de empresa — e a continuacao da
    # frase do assunto. Sem esta guarda, "Boleto referente a assinatura 1040983896 de
    # Manutencao - ot..." (conta 694) virava o FORNECEDOR "referente a assinatura
    # 1040983896 de Manutencao - ot", criando um cadastro-lixo em supplier.
    # Deliberadamente NAO inclui "de/da/do": esses PODEM iniciar razao social
    # ("DE NADAI ALIMENTACAO") — a guarda so cobre conectivos que nunca iniciam nome.
    if _SUBJECT_NON_NAME_START_RE.match(s):
        return ""
    # robustez: um tipo de documento/pagamento nao e fornecedor (ex.: "GNRE").
    if _is_non_supplier_term(s):
        return ""
    return s


def safe_filename(text: str, max_len: int = 40) -> str:
    # Remove acentos e caracteres nao-ASCII (ex.: º, ª, ç, ã). O nome vira a
    # chave do objeto no Supabase Storage, que rejeita chaves com esses
    # caracteres (erro InvalidKey) — e tambem o source_file gravado no banco,
    # entao disco, source_file e Storage ficam consistentes.
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text)   # agora ASCII: mantem [A-Za-z0-9_], espaco, hifen
    text = re.sub(r"\s+", "_", text.strip())
    return text[:max_len]


# Acronimos de guias/tributos + cambio: casam como PALAVRA inteira (fronteira
# \b), nao substring — evita falsos positivos ('das' em 'cadastro'/'vendas',
# 'iss' em 'emissao', 'gru' em 'grupo', 'cambio' em 'intercambio'). A comparacao
# remove acentos e baixa a caixa, entao a chave 'cambio' casa 'cambio'/'câmbio'/
# 'CÂMBIO' e a forma gramatical correta 'câmbio' fica gravada como keyword.
# 'dar' entra aqui como DEFESA EM PROFUNDIDADE, nao porque seja keyword: o
# EMAIL_KEYWORDS de producao vem do .env (editado a mao, nao versionado). Se alguem
# um dia acrescentar 'dar' la, o casamento sera por PALAVRA inteira em vez de
# substring — sem isto, 'dar' casaria 'pa-dar-ia'/'aguar-dar'/'man-dar' e o pipeline
# passaria a extrair e-mails aleatorios. Custo zero, e travado por teste.
WORD_KEYWORDS = frozenset({
    "dar", "darf", "das", "dae", "dare", "dam", "duam", "gps", "gru", "gnre", "gare",
    "ipva", "iptu", "iss", "itbi", "cambio",
})

# Termos de NF-e/NFS-e no assunto. Um e-mail "puro" de NF-e (so notificacao de
# documento fiscal, sem indicio de pagavel) que nao gera conta vira 'ignorado'.
NFE_SUBJECT_TERMS = ("notas fiscais", "nota fiscal", "nfe", "nf-e", "nfse", "nfs-e")

# Indicios de que o e-mail traz algo PAGAVEL. Se aparecerem junto da NF-e no
# assunto (ex.: 'Boleto e NFS-e', 'NFSe e FATURA'), NAO classificar como
# 'ignorado' — pode ser um boleto/fatura cuja extracao falhou e precisa revisao.
PAYABLE_HINT_TERMS = ("boleto", "fatura", "cobranca", "guia", "pix", "duplicata",
                      "vencimento", "vencer", "pagar", "darf", "das", "dae",
                      "dare", "dam", "duam", "gps", "gru", "gnre", "gare", "ipva",
                      "iptu", "iss", "itbi", "tributo", "imposto", "taxa", "multa")


def _strip_accents_lower(s: str) -> str:
    """NFD + drop non-ASCII + lower — base da comparacao de keyword (sem acento)."""
    return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()


def _has_word(s_norm: str, term_norm: str) -> bool:
    """True se term_norm aparece como PALAVRA inteira em s_norm (ambos ja sem acento)."""
    return bool(term_norm) and re.search(rf"\b{re.escape(term_norm)}\b", s_norm) is not None


def match_keyword(subject: str, keywords: list) -> str | None:
    """Retorna a keyword casada (forma original) ou None. Comparacao sem acento;
    acronimos de tributo/cambio (WORD_KEYWORDS) exigem palavra inteira, o resto
    e substring."""
    s = _strip_accents_lower(subject)
    for kw in keywords:
        kw_norm = _strip_accents_lower(kw)
        if not kw_norm:
            continue
        if kw_norm in WORD_KEYWORDS:
            if re.search(rf"\b{re.escape(kw_norm)}\b", s):
                return kw
        elif kw_norm in s:
            return kw
    return None


def subject_is_pure_nfe(subject: str) -> bool:
    """True se o assunto e de NF-e/NFS-e SEM nenhum indicio de pagavel — usado
    para classificar como 'ignorado' quando nenhuma conta a pagar e gerada.

    Os termos casam por PALAVRA inteira: 'nfe' como substring casaria 'co-nfe-ccoes'
    (CONFECCOES), classificando erroneamente um aviso de transporte como NF-e pura.
    """
    s = _strip_accents_lower(subject)
    has_nfe = any(_has_word(s, _strip_accents_lower(t)) for t in NFE_SUBJECT_TERMS)
    if not has_nfe:
        return False
    return not any(_has_word(s, _strip_accents_lower(t)) for t in PAYABLE_HINT_TERMS)


# Termos de NOTIFICACAO: e-mails de aviso/confirmacao que, SEM anexo e SEM conta
# no corpo (i.e., nenhuma conta a pagar gerada), sao 'ignorado' em vez de 'falha'
# — sao notificacoes, nao contas a pagar. Diferente do PAYABLE_HINT do pure_nfe:
# aqui NAO ha exclusao por boleto/fatura, porque o gatilho ja exige ausencia de
# anexo/conta — se nao veio anexo nem dado no corpo, e so um aviso.
#   - palavra inteira (curtos/sigla): evita casar dentro de outras palavras.
#   - frase (substring): expressoes inequivocas de notificacao.
NOTIFICATION_WORD_TERMS = frozenset({
    "nfe", "nf-e", "informe", "sieg",
    # CT-e/transporte: documento FISCAL. A notificacao de disponibilizacao de CT-e
    # (ou aviso de OCORRENCIA de entrega) SEM boleto anexo/no corpo nao e conta a
    # pagar — vira 'ignorado'. Um boleto de transporte real gera conta ANTES (o
    # 'notification' e o ultimo criterio), entao nao e escondido por estes termos.
    "cte", "ct-e", "dacte",
})
NOTIFICATION_PHRASE_TERMS = (
    "informativo", "confirmado o pagamento", "confirmado pagamento",
    "confirmacao de pagamento", "confirmacao do pagamento", "pagamento confirmado",
    "pagamento processado", "aviso de vencimento",
    "titulo a vencer", "lembrete de vencimento", "titulos proximos do vencimento",
    "comprovante de pix", "protesto", "protestado", "cartorio", "comunicado",
    # Aviso/lembrete de fatura a vencer (ex.: transitobrasil "Aviso de fatura a
    # vencer") — e so um AVISO, nao a cobranca em si; sem anexo/conta → 'ignorado'.
    "fatura a vencer", "aviso de fatura",
    # Notificacao de disponibilizacao de CT-e (SSW/transportadora) sem boleto.
    "conhecimento de transporte",
    # Cadastro/alteracao de meio de pagamento (ex.: Locaweb "Nova forma de pagamento
    # registrada!"): avisa que um CARTAO foi cadastrado — nao ha valor nem documento.
    # Casava a keyword "pagamento" e caia em 'falha', poluindo /erros (ids 1262/1263).
    "forma de pagamento", "meio de pagamento",
    # Operacional de transportadora (SSW "AGENDAMENTO DE COLETA") — casa a keyword
    # "transporte" mas nao e cobranca (id 806).
    "agendamento de coleta",
    # Aviso de RECEBIMENTO de mercadoria/NF pelo destinatario — nao e conta a pagar
    # (id 429). Distinto de "confirmacao de PAGAMENTO", que ja e tratada acima e num
    # nivel FORTE (subject_is_payment_confirmation).
    "confirmacao de recebimento", "confirmacao recebimento",
)


# Subdominio DESCARTAVEL de campanha de phishing: "servidor" + hash aleatorio, sob um
# dominio que nada tem a ver com o suposto remetente — ex.:
# setorfinanceiro@servidor9n3xa9.powerallynigeria.com, no_responder@servidorj2tzqm.dkaitech.com.
# Os assuntos IMITAM cobranca ("Segue NFs e BOLETOS 60582", "Pagamento referente ao pedido"),
# entao casam keyword e caem em 'falha' — e um deles, se um dia trouxesse anexo, viraria conta
# a pagar FALSA. Padrao deliberadamente ESTREITO (o literal "servidor" + >=5 chars de hash):
# nao existe remetente legitimo com essa forma, e um filtro generico por dominio desconhecido
# barraria fornecedor novo. Ids 945/1083/1184.
_DISPOSABLE_SENDER_RE = re.compile(r"@servidor[a-z0-9]{5,}\.", re.IGNORECASE)


def is_disposable_sender(sender_email: "str | None") -> bool:
    """Remetente de subdominio descartavel (campanha de phishing). Ver _DISPOSABLE_SENDER_RE."""
    return bool(_DISPOSABLE_SENDER_RE.search(sender_email or ""))


def subject_is_ignorable_notification(subject: str) -> bool:
    """True se o assunto e de uma NOTIFICACAO (aviso/confirmacao/informe/SIEG/NF-e)
    que, sem anexo e sem conta no corpo, deve virar 'ignorado' em vez de 'falha'."""
    s = _strip_accents_lower(subject)
    if any(_has_word(s, t) for t in NOTIFICATION_WORD_TERMS):
        return True
    return any(_strip_accents_lower(t) in s for t in NOTIFICATION_PHRASE_TERMS)


# Confirmacao/comprovante de PAGAMENTO — o pagamento JA foi realizado, logo o e-mail NAO
# e conta a pagar e deve ser IGNORADO sempre (mesmo com keyword no assunto). Regex sobre o
# assunto sem acento. Diferente de NOTIFICATION_PHRASE_TERMS (que so vira 'ignorado' na
# ausencia de anexo/conta): aqui o e-mail e ignorado ANTES de baixar/extrair. O conector
# opcional (foi/ja) cobre "pagamento FOI processado"; o participio no PASSADO
# (confirmado/efetuado/…) evita casar "REALIZAR pagamento"/"pagamento A realizar" (conta a
# pagar). "confirmacao de/do pagamento" e "comprovante de pagamento/pix" sao inequivocos.
_PAYMENT_CONFIRMATION_RE = re.compile(
    r"confirmac[ao]{2} (de|do) pagamento"
    r"|comprovante (de )?(pagamento|pix|transferencia|deposito)"
    r"|confirmado o? ?pagamento"
    r"|pagamento (foi |ja |ja foi )?(confirmado|processado|efetuado|realizado|aprovado|recebido)"
    # "Recebemos o seu pagamento" / "Recebemos pagamento" / "Recebemos o pagamento da
    # fatura X" — o credor AVISA que recebeu; e recibo, nao cobranca (conta 716,
    # "Leadster | Recebemos o seu pagamento", que virou conta falsa de R$ 362,62).
    r"|recebemos (o |a )?(seu |sua )?pagamento"
)
# CONTRA-EXEMPLO que INVERTE o sentido: "(ainda) NAO recebemos o seu pagamento" e uma
# COBRANCA — o pagamento esta em aberto. Sem esta guarda a alternativa acima o trataria
# como recibo e o titulo seria perdido em silencio. Vies deliberado: na duvida NAO ignorar
# (uma conta a revisar e melhor que um pagavel perdido).
_PAYMENT_NOT_RECEIVED_RE = re.compile(r"\bnao (recebemos|identificamos|consta)\b")


def subject_is_payment_confirmation(subject: str) -> bool:
    """True se o assunto e de uma CONFIRMACAO/COMPROVANTE de pagamento (pagamento JA
    realizado). Esses e-mails NUNCA sao conta a pagar — devem ser ignorados sempre, mesmo
    com keyword financeira no assunto. Comparacao sem acento.

    A forma NEGADA ("nao recebemos o seu pagamento") e o oposto — cobranca de titulo em
    aberto — e devolve False, para o e-mail seguir o fluxo normal de extracao."""
    s = _strip_accents_lower(subject)
    if _PAYMENT_NOT_RECEIVED_RE.search(s):
        return False
    return bool(_PAYMENT_CONFIRMATION_RE.search(s))


# LEMBRETE — e-mail cujo ASSUNTO traz a palavra "lembrete" e um AVISO/lembrete, nao a cobranca em
# si. Decisao do usuario: existindo "lembrete" no assunto, ignorar SEMPRE (antes de baixar/extrair,
# mesmo com keyword/anexo). O foco e a palavra "lembrete": "vencimento" sozinho pode ser um boleto
# REAL ("Boleto vencimento 10/07"), entao NAO entra na condicao. Cobre "Lembrete de vencimento" e o
# caso real "Lembrete de Pagamento: vencimento DD/MM" (boleto@smartwebservices). Substring (sem
# acento) — pega o plural "lembretes"; "lembrete" nao e substring de outra palavra comum.


def subject_is_reminder(subject: str) -> bool:
    """True se o assunto contem a palavra 'lembrete' — e um lembrete/aviso, nao conta a pagar;
    ignorado SEMPRE (decisao do usuario)."""
    return "lembrete" in _strip_accents_lower(subject)



# Assunto ORIGINAL de um e-mail ENCAMINHADO — no corpo, aparece como uma linha
# "Assunto: ..." (Outlook/pt) ou "Subject: ..." (en), possivelmente com marcador de
# citacao (">"). Um usuario interno pode reencaminhar um lembrete/confirmacao trocando
# o assunto VISIVEL (ex.: "pagamento Sua Fatura"), escondendo o "lembrete" original das
# guardas do run_reader, que olham so o assunto RECEBIDO. Reavaliamos o assunto original.
# Captura ate o fim da LINHA ([^\r\n]+) sem depender do ancora `$` — o CRLF (\r antes do
# \n) do webmail deixaria o \r fora do `$` do modo MULTILINE. O .strip() no consumidor
# remove qualquer espaco/CR residual.
_FORWARDED_SUBJECT_RE = re.compile(
    r"^[ \t>]*(?:assunto|subject)[ \t]*:[ \t]*([^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)


def forwarded_subjects_from_body(body_text: str | None) -> list[str]:
    """Assuntos ORIGINAIS de blocos de e-mail ENCAMINHADO achados no corpo (linhas
    'Assunto:'/'Subject:'). Lista vazia se nao houver corpo ou linha de assunto."""
    if not body_text:
        return []
    return [m.group(1).strip() for m in _FORWARDED_SUBJECT_RE.finditer(body_text) if m.group(1).strip()]


# Remetente ORIGINAL de um bloco de e-mail ENCAMINHADO — no corpo, aparece como uma
# linha "De: <Nome> <email>" (Outlook/pt) ou "From: ..." (en), mesmo padrao de
# _FORWARDED_SUBJECT_RE para "Assunto:"/"Subject:".
_FORWARDED_FROM_LINE_RE = re.compile(
    r"^[ \t>]*(?:de|from)[ \t]*:[ \t]*([^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Dominios internos cujo "De:" NUNCA identifica o fornecedor (o funcionario que
# encaminhou, nao quem originou a cobranca). Mesmos dominios bloqueados na RPC
# (migration 046), mas aqui e uma checagem em Python sobre o texto do corpo.
_INTERNAL_EMAIL_DOMAINS = ("otimotex.com.br", "lebianco.com.br")


def _supplier_from_forwarded_sender(body_text: str | None) -> "str | None":
    """Nome de fornecedor a partir do REMETENTE ORIGINAL de um bloco de e-mail
    ENCAMINHADO no corpo ('De:'/'From:' — mesmo padrao de forwarded_subjects_from_body
    para 'Assunto:'). Cobre o caso em que o remetente IMEDIATO do e-mail e interno
    (@otimotex.com.br/@lebianco.com.br, bloqueado como fornecedor) mas o corpo ainda
    traz, mais abaixo na cadeia, o remetente ORIGINAL da notificacao — ex.: lembrete
    periodico de fatura ("Contabil Esquema LTDA <lembrete@contabilesquema.com.br>")
    encaminhado internamente com o assunto reescrito (caso real: contas 668/669, ver
    "Assunto como ULTIMO recurso" — aqui o corpo, nao o assunto, e a fonte).

    Conservador de proposito — SO aceita o nome quando ele ancora numa SIGLA de razao
    social (_supplier_name_by_legal_suffix): evita capturar o nome de uma PESSOA (o
    funcionario que encaminhou, ex.: "De: Eunice <eunice@otimotex.com.br>"). Usa a
    ULTIMA linha que qualificar (a mais profunda da cadeia == a mais proxima do
    remetente original) e descarta linhas que citem um dominio interno.

    Chamada em _finalize_supplier SOMENTE apos o assunto ancorado em sigla (fallback
    2) esgotar — o assunto e sinal do PROPRIO e-mail, mais confiavel que uma linha
    'De:' que pode pertencer a um TERCEIRO da cadeia (intermediario/repassador).
    Nao chamar a partir de extract_from_email_body: essa e a UNICA fonte de
    verdade, para nao regredir a precedencia do assunto (ver comentario em
    extract_from_email_body)."""
    if not body_text:
        return None
    for line in reversed(_FORWARDED_FROM_LINE_RE.findall(body_text)):
        if any(dom in line.lower() for dom in _INTERNAL_EMAIL_DOMAINS):
            continue
        display = line.split("<", 1)[0].strip()
        name = _supplier_name_by_legal_suffix(display)
        if name and not _is_non_supplier_term(name):
            return name
    return None


# Endereco de e-mail dentro de uma linha "De:" — o ENDERECO, nao o nome de exibicao.
_EMAIL_IN_LINE_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _forwarded_sender_email(body_text: "str | None") -> "str | None":
    """E-MAIL do remetente ORIGINAL de um bloco ENCAMINHADO no corpo ('De:'/'From:').

    COMPLEMENTA _supplier_from_forwarded_sender, que extrai o NOME e exige ancora de
    SIGLA DE RAZAO SOCIAL (LTDA/EIRELI/S.A.) — ancora que uma PESSOA FISICA nunca
    satisfaz. O caso real e' um despachante ("De: JOSE RICARDO PRUDENTE <...>") que
    encaminha guias de Junta Comercial: o nome nunca casaria; o ENDERECO, sim.

    Mesma semantica de percurso: da linha "De:" MAIS PROFUNDA para a mais rasa (a mais
    profunda == a mais proxima do originador da cadeia), DESCARTANDO a que citar dominio
    interno (o funcionario que encaminhou, nao quem originou a cobranca). Devolve o
    endereco em minusculas, ou None.

    🔴 O RETORNO DESTA FUNCAO NUNCA PODE ALIMENTAR AUTO-INSERT DE FORNECEDOR. Ele
    identifica quem MANDOU o documento, nao quem RECEBE o pagamento — e qualquer pessoa
    pode encaminhar uma guia. O unico consumidor legitimo e' um lookup que so CONSULTA
    (SupabaseControl.find_supplier_by_email -> RPC find_supplier_by_email, migration
    134). Passar isto a resolve_supplier faria cada encaminhador virar fornecedor no
    primeiro e-mail. Ver o chamador em _finalize_supplier.
    """
    if not body_text:
        return None
    for line in reversed(_FORWARDED_FROM_LINE_RE.findall(body_text)):
        if any(dom in line.lower() for dom in _INTERNAL_EMAIL_DOMAINS):
            continue
        m = _EMAIL_IN_LINE_RE.search(line)
        if m:
            return m.group(0).lower()
    return None


def body_forwards_payment_confirmation(body_text: str | None) -> str | None:
    """Se o corpo ENCAMINHA um e-mail cujo assunto ORIGINAL e uma CONFIRMACAO/
    COMPROVANTE de pagamento, devolve o MOTIVO (str) para ignorar; senao None.
    Fecha o vetor do reencaminhamento interno que reescreve o assunto visivel e esconde a
    'confirmacao' das guardas de assunto. Uma confirmacao de pagamento NUNCA e um pagavel
    — independente de ja existir ou nao uma conta correspondente — entao continua
    bloqueada aqui, incondicionalmente. Conservador: so no caminho do CORPO (sem anexo
    pagavel) — um boleto real anexado a uma confirmacao encaminhada segue sendo pago.

    NAO dispara para 'lembrete' (subject_is_reminder) encaminhado: um lembrete/aviso de
    disponibilidade de fatura PODE ser a UNICA fonte de uma fatura ainda nao registrada
    em nenhum outro canal (caso real: conta 668/e-mail 1004, fatura Contabil Esquema
    Nº 20879, R$ 2.950,00, vencendo em 2 dias — nao havia nenhuma conta correspondente e
    o guard antigo a descartou silenciosamente). Deixar o lembrete seguir a extracao
    normal do corpo e correto: a dedup de conteudo (find_financial_duplicate, chamada
    logo apos em try_extract_from_body) ja suprime reenvios PERIODICOS do MESMO lembrete
    (mesmo fornecedor+documento/valor+vencimento) devolvendo BODY_DUPLICATE — nao e
    preciso um segundo mecanismo de supressao aqui."""
    for subj in forwarded_subjects_from_body(body_text):
        if subject_is_payment_confirmation(subj):
            return f'Confirmacao de pagamento encaminhada — assunto original "{subj[:80]}" (nao e conta a pagar)'
    return None


# Remetentes de SISTEMA (NDR/bounce/aviso de servidor) — nunca sao conta a pagar.
# Match pelo local-part (antes do @), case-insensitive, em QUALQUER dominio.
IGNORED_SENDER_LOCALPARTS = {"postmaster"}


def is_ignored_sender(sender_email: str | None) -> bool:
    """True se o remetente e um endereco de SISTEMA (ex.: postmaster@) cuja
    mensagem (NDR/bounce/aviso de entrega) deve virar 'ignorado' sem baixar nem
    extrair — MESMO que o assunto case uma keyword financeira."""
    local = (sender_email or "").split("@")[0].strip().lower()
    return local in IGNORED_SENDER_LOCALPARTS


def append_log_csv(record: dict):
    """Fallback local: acrescenta registro no CSV. Falhas são logadas e ignoradas."""
    try:
        write_header = not EMAILS_LOG.exists()
        with open(EMAILS_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, delimiter=";",
                               extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(record)
    except Exception as e:
        log.warning(f"Falha ao gravar log CSV local: {e}")


# ---------------------------------------------------------------------------
# Gravacao da conta extraida em financial_account_control
# ---------------------------------------------------------------------------
# Colunas geradas pelo extract_pdf.py (na mesma ordem do CSV de saida).
FINANCIAL_FIELDS = [
    "source_file", "document_type", "extraction_source",
    "supplier_name", "supplier_cnpj", "supplier_cpf", "invoice_number",
    "competence_date", "due_date", "issue_date",
    "amount", "currency", "payment_method",
    "barcode", "description", "status",
    "nosso_numero", "discount", "other_deductions",
    "fine_interest", "other_additions", "amount_charged",
    "payer_name", "payer_cnpj",
    "processing_notes", "extracted_at",
]

# Campos de valor do boleto: em branco -> 0 (regra de negocio).
FINANCIAL_VALUE_FIELDS = [
    "discount", "other_deductions", "fine_interest",
    "other_additions", "amount_charged",
]

# Tipos de documento que NAO geram conta a pagar.
# Valores em lowercase — comparados com dtype.lower() em extract_and_store_accounts().
SKIP_ACCOUNT_TYPES = ("nfe", "nfse")


def _none_if_blank(value):
    """Normaliza vazios do CSV ('', 'None', 'nan') para None."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" or s.lower() in ("none", "nan", "null") else s


def read_extracted_rows(csv_path: str) -> list:
    """Le as linhas de um CSV gerado pelo extract_pdf.py (utf-8-sig, sep ';')."""
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))
    except Exception as e:
        log.warning(f"Falha ao ler CSV de extracao {csv_path}: {e}")
        return []


def build_financial_payload(row: dict, gmail_message_id: str,
                            received_at: str | None = None,
                            subject: str | None = None) -> dict:
    """Converte uma linha do CSV de extracao em payload para financial_account_control.

    Sanitiza os campos com restricao no schema (CHAR(14) do CNPJ e
    NUMERIC do amount) para evitar rejeicao do INSERT. A situacao de vencimento
    NAO e calculada aqui — a trigger grava em `status` no banco (migration 034).
    Aplica os mesmos fallbacks do caminho de corpo: emissao->data do e-mail
    (received_at); vencimento->emissao; numero->"{tipo}_{ddmmyy}".

    Conta de concessionaria (agua/luz/telefone-internet) e classificada pelo ASSUNTO
    do e-mail com precedencia maxima (o extract_pdf.py e cego ao assunto), antes do
    fallback de invoice_number para o tipo entrar no numero sintetico.
    """
    payload = {f: _none_if_blank(row.get(f)) for f in FINANCIAL_FIELDS}

    # CNPJ: CHAR(14) — exatamente 14 digitos
    cnpj = re.sub(r"\D", "", payload.get("supplier_cnpj") or "")
    payload["supplier_cnpj"] = cnpj if len(cnpj) == 14 else None

    pcnpj = re.sub(r"\D", "", payload.get("payer_cnpj") or "")
    payload["payer_cnpj"] = pcnpj if len(pcnpj) == 14 else None

    # CPF: CHAR(11) — exatamente 11 digitos
    cpf = re.sub(r"\D", "", payload.get("supplier_cpf") or "")
    payload["supplier_cpf"] = cpf if len(cpf) == 11 else None

    # amount NUMERIC(15,2): garante numero ou None
    payload["amount"] = _to_decimal(payload.get("amount"), None)

    # Campos de valor do boleto: em branco -> 0 (NOT NULL DEFAULT 0 no banco)
    for vf in FINANCIAL_VALUE_FIELDS:
        payload[vf] = _to_decimal(payload.get(vf), 0)

    # Emissão ausente → data do e-mail (received_at).
    if not payload.get("issue_date") and received_at:
        payload["issue_date"] = received_at[:10]

    # Vencimento ausente (ex.: CT-e/DACTE não traz vencimento) → emissão; sem
    # emissão, a data de hoje.
    if not payload.get("due_date"):
        payload["due_date"] = payload.get("issue_date") or datetime.now().strftime("%Y-%m-%d")

    # Conta de concessionária pelo assunto ou pela marca no nome do fornecedor
    # (precedência máxima) — antes do fallback de invoice abaixo, para o nº sintético
    # usar o tipo correto.
    utility = (_classify_utility_doc_type(subject)
               or _classify_utility_by_supplier(payload.get("supplier_name")))
    if utility:
        payload["document_type"] = utility
    else:
        # Guia tributária pelo ACRÔNIMO no assunto (DARE/GARE/GNRE/DARF…) sobrepõe a
        # classificação do PDF — guias estaduais são quase idênticas e o Claude troca
        # uma pela outra; o assunto traz o tipo que o remetente declarou.
        tax_subject = _classify_tax_doc_type_from_subject(subject)
        if tax_subject:
            payload["document_type"] = tax_subject

    # Boleto de TRANSPORTE (contexto cte/transporte/transportadora ou fornecedor de
    # transporte) → document_type='cte' (regra 4). Abaixo de utility/tax (uma
    # transportadora que manda um DARF continua DARF), acima de 'boleto'. Antes do
    # nº sintético para o prefixo usar 'cte'.
    payload["document_type"] = _apply_transport_boleto_doc_type(
        payload.get("document_type"), subject, payload.get("supplier_name"),
        payload.get("barcode"))

    # Cartório (contexto "cartorio" no assunto/fornecedor) → document_type='cartório'.
    # Abaixo de utility/tax/transporte; só re-rotula tipos genéricos. Antes do nº
    # sintético para o prefixo usar 'cartório'.
    payload["document_type"] = _apply_cartorio_doc_type(
        payload.get("document_type"), subject, payload.get("supplier_name"))

    # Seguradora (assunto com seguro/seguradora/apólice) → document_type='seguro'.
    # Abaixo de cartório e das guias: um tributo cobrado por seguradora continua sendo o
    # tributo. Antes do nº sintético para o prefixo usar 'seguro'. O caminho do CORPO
    # aplica a mesma regra na sua própria cadeia (extract_from_email_body).
    payload["document_type"] = _apply_seguro_doc_type(payload.get("document_type"), subject)

    # Nº documento em branco → sintético (pagamento PIX + tipo 'outro': 'pix_' + valor;
    # demais: tipo+vencimento) — mesma regra do corpo.
    if not payload.get("invoice_number"):
        synthetic = _synthetic_invoice_number(
            payload.get("document_type"), payload.get("amount"),
            payload.get("due_date") or payload.get("issue_date"),
            payload.get("payment_method"))
        if synthetic:
            payload["invoice_number"] = synthetic

    # Honorários: PIX por padrão, EXCETO quando há código de barras / linha digitável
    # de boleto válido — aí paga-se como boleto (honorário emitido em boleto). Boleto
    # vence a regra do PIX; a chave NF-e/CT-e de 44 dígitos não casa (segue pix).
    if (payload.get("document_type") or "").lower() == "honorários":
        payload["payment_method"] = (
            "boleto" if _is_boleto_barcode(payload.get("barcode")) else "pix")

    payload["currency"] = payload.get("currency") or "BRL"
    payload["status"]   = payload.get("status") or "pendente"
    payload["gmail_message_id"] = gmail_message_id
    return payload


def _resolve_supplier_by_payer(ctrl: "SupabaseControl", payload: dict) -> int | None:
    """ULTIMO RECURSO: usa o PAGADOR (payer_name/payer_cnpj — ex.: OTIMOTEX) como
    fornecedor, apenas quando TODAS as outras formas (nome/CNPJ/CPF/e-mail/assunto)
    falharam e o pagador esta claro. Garante que a conta a pagar nunca se perca por
    falta de fornecedor identificavel; o operador reclassifica depois em /consulta.

    Retorna sk_supplier ou None (pagador ausente/indefinido)."""
    payer_name = (payload.get("payer_name") or "").strip()
    payer_cnpj = re.sub(r"\D", "", str(payload.get("payer_cnpj") or ""))
    # filtro de robustez: nome do pagador nunca pode ser um tipo de documento/pagamento.
    if _is_non_supplier_term(payer_name):
        payer_name = ""
    if not payer_name and len(payer_cnpj) != 14:
        return None  # pagador nao esta claro — nao inventa fornecedor
    probe = dict(payload)
    probe["supplier_name"] = payer_name or None
    probe["supplier_cnpj"] = payer_cnpj if len(payer_cnpj) == 14 else None
    probe["supplier_cpf"]  = None
    sk = ctrl.resolve_supplier(probe)
    if sk:
        log.info(f"    [FORNECEDOR-PAGADOR] fornecedor ausente — usando o pagador: "
                 f"{(payer_name or payer_cnpj)!r}")
    return sk


def _finalize_supplier(ctrl: "SupabaseControl", payload: dict, body_text: str = "") -> bool:
    """Resolve o fornecedor (RPC), grava payload['sk_supplier'] e REMOVE as colunas
    denormalizadas supplier_name/supplier_cnpj/supplier_cpf — o fornecedor passa a
    ser referenciado APENAS pela FK sk_supplier (fonte de verdade: tabela supplier).

    Deve ser chamado APOS a validacao 'sem_fornecedor' (que usa os campos brutos
    extraidos) e ANTES de find_financial_duplicate (a dedup casa por sk_supplier).
    Retorna False quando a resolucao falha (chamador trata como erro de gravacao).

    `body_text` e' o corpo do e-mail, usado pelo fallback 1b. Vem por PARAMETRO, e nao
    por payload['email_body_excerpt']: no caminho de ANEXO essa coluna nunca e' povoada
    (FINANCIAL_FIELDS so tem as colunas do CSV do extract_pdf), e povoa-la ali teria
    efeito colateral em apply_contact_writeback, que passaria a varrer o corpo inteiro
    e a ESCREVER contato no cadastro do fornecedor.

    Ordem de fallback (cada um so quando o anterior esgota):
      1. nome/CNPJ/CPF EXTRAIDOS (descartando o nome que for um TIPO de
         documento/pagamento — robustez: 'GNRE'/'BOLETO' nao e fornecedor);
     1b. E-MAIL do remetente ORIGINAL de um bloco ENCAMINHADO no corpo, quando esse
         endereco JA ESTA CADASTRADO num fornecedor ativo — ver o bloco proprio abaixo,
         que explica por que ele roda ANTES da regra de imposto;
      2. nome do ASSUNTO ancorado numa SIGLA de razao social (LTDA/EIRELI/S.A./…)
         — sinal do PROPRIO e-mail (quem o classificou/encaminhou o rotulou), mais
         confiavel que uma linha "De:" solta no corpo, que pode pertencer a um
         TERCEIRO da cadeia (intermediario/repassador), nao ao fornecedor. Roda
         ANTES do fallback pelo corpo para nao regredir casos ja corretos (ex.: id
         401, "FATURAMENTO -- MOVVI LOGISTICA LTDA");
      3. remetente ORIGINAL de um bloco ENCAMINHADO no corpo ('De:'/'From:'
         ancorado em sigla de razao social — _supplier_from_forwarded_sender) —
         usado so quando o ASSUNTO nao tem ancora propria (fallback 2 esgotou);
         cobre o assunto reescrito pelo funcionario que encaminhou (ex.: "boleto
         esquema" em vez do nome completo do fornecedor; caso real: conta 669);
      4. nome derivado do ASSUNTO SEM ancora (heuristica generica, idem filtro de
         tipo) — e-mail interno de pagamento ("PAGAMENTO BOLETO HYOSUNG 181063-3")
         nomeia o favorecido;
      5. e-mail do remetente (nao interno) — dentro da RPC resolve_supplier;
      6. ULTIMO RECURSO: o PAGADOR (payer_name/payer_cnpj, ex.: OTIMOTEX) — so
         quando TODAS as formas acima falham e o pagador esta claro; garante que a
         conta a pagar nunca se perca e fique rastreavel pelo pagador."""
    # robustez: um TIPO de documento/pagamento extraido como "fornecedor" e descartado.
    if _is_non_supplier_term(payload.get("supplier_name")):
        payload.pop("supplier_name", None)
    # O CNPJ da PROPRIA empresa pagadora (OTIMOTEX) NAO e o fornecedor. E-mails de
    # faturamento reencaminhados trazem o bloco do destinatario ("TEXTIL E CONF.OTIMOTEX
    # / CNPJ: 47273917/0001-23") e a extracao capturava esse CNPJ como se fosse do
    # fornecedor, gravando a conta sob a OTIMOTEX (sk=1) — quando o favorecido real esta
    # nomeado no assunto (ex.: "MOVVI LOGISTICA LTDA"). Descarta o CNPJ do pagador para
    # que a resolucao siga pelo nome/assunto (a sigla LTDA no assunto costuma nomear o
    # fornecedor real). Nao afeta a regra de imposto nem o fallback de pagador, que
    # gravam OTIMOTEX explicitamente quando NAO ha favorecido.
    # Comparacao pela RAIZ do CNPJ (8 primeiros digitos), NAO pelo numero completo de
    # 14 digitos: OTIMOTEX/LEBIANCO/FARDOS (e outras filiais do mesmo grupo)
    # compartilham a MESMA raiz "47273917", divergindo so no sufixo de filial/DV
    # (0001-23/0002-23/0003-23/...). Um bloco de destinatario com OUTRA filial (ex.:
    # "47273917/0003-95", nao cadastrada em nenhum sk_company) escapava do match exato
    # e era resolvido como fornecedor de verdade — caso real: conta indevida sob o
    # sk_supplier de uma filial da propria OTIMOTEX mal-cadastrada como "fornecedor".
    own_cnpj = ctrl.company_cnpj() if hasattr(ctrl, "company_cnpj") else None
    if own_cnpj and len(own_cnpj) >= 8:
        extracted_cnpj = re.sub(r"\D", "", str(payload.get("supplier_cnpj") or ""))
        if extracted_cnpj and extracted_cnpj[:8] == own_cnpj[:8]:
            payload.pop("supplier_cnpj", None)
            log.info("    [FORNECEDOR] CNPJ do pagador (OTIMOTEX, mesma raiz/filial) "
                     "ignorado como fornecedor — segue pelo nome/assunto")
    has_real_supplier = any(str(payload.get(k) or "").strip()
                            for k in ("supplier_name", "supplier_cnpj", "supplier_cpf"))
    # ── fallback 1b: E-MAIL do remetente ORIGINAL do bloco ENCAMINHADO ──────────────
    # Caso real (conta 1101): o despachante manda a guia da Junta Comercial para a
    # funcionaria, que a encaminha. O PDF de guia NAO traz favorecido e o remetente
    # imediato e' interno, entao o "De:" da cadeia e' o unico sinal do credor.
    #
    # 🔴 RODA ANTES DA REGRA DE IMPOSTO, e esse e' o ponto todo: a regra abaixo faz
    # `return True` INCONDICIONAL, de modo que tudo depois dela e' inalcancavel para
    # guia de tributo sem favorecido — exatamente o caso que este bloco resolve. A 1101
    # foi gravada sob a OTIMOTEX com a classificacao default dela (Recursos Humanos /
    # Festas e Confraternizacoes) numa guia da Junta Comercial.
    #
    # 🔴 SO IDENTIFICA, NUNCA CRIA. `find_supplier_by_email` e' consulta pura (RPC da
    # migration 134). Trocar por `resolve_supplier` COMPILA e passa nos testes de
    # caminho feliz, mas faria QUALQUER pessoa que encaminhasse uma guia virar
    # fornecedor no primeiro e-mail, pelo auto-insert.
    #
    # 🔴 POR QUE NAO REGRIDE OS CASOS QUE A REGRA DE IMPOSTO PROTEGE (id 374 e familia,
    # em tests/test_supplier_imposto.py): este bloco exige, CUMULATIVAMENTE, (a) nenhum
    # favorecido extraido, (b) uma linha "De:" cujo dominio NAO seja interno, (c) um
    # endereco de e-mail nessa linha e (d) esse endereco JA CADASTRADO e ATIVO em
    # `supplier`. "PAGAMENTO IMPOSTOS" com pagador OTIMOTEX falha em (b)/(c)/(d) e cai
    # na regra de imposto como antes; encaminhador nao cadastrado, idem.
    #
    # 🔴 E POR QUE NAO REGRIDE A LICAO DA CONTA 401 (assunto vence "De:" de TERCEIRO):
    # rodar antes da regra de imposto significa rodar antes do fallback 2, e sem a guarda
    # abaixo um intermediario CADASTRADO venceria um assunto ja correto ("FATURAMENTO --
    # MOVVI LOGISTICA LTDA"), atribuindo a conta ao fornecedor errado EM SILENCIO — pior
    # que o bug original. A guarda separa os dois mundos:
    #   * GUIA DE TRIBUTO: o assunto NUNCA foi fonte aqui (a regra de imposto o
    #     curto-circuita de proposito, porque assunto de guia produz fornecedor-lixo tipo
    #     "IMPOSTOS"), entao nao ha precedencia a regredir — o 1b so concorre com OTIMOTEX;
    #   * QUALQUER OUTRO documento: o 1b so entra quando o ASSUNTO NAO TEM ancora propria
    #     de sigla de razao social, preservando exatamente a ordem documentada
    #     (assunto ancorado > linha "De:" da cadeia).
    _subject_anchor = _supplier_name_by_legal_suffix(payload.get("subject"))
    _subject_has_anchor = bool(_subject_anchor) and not _is_non_supplier_term(_subject_anchor)
    if not has_real_supplier and (_is_tax_document(payload.get("document_type"))
                                  or not _subject_has_anchor):
        fwd_email = _forwarded_sender_email(body_text or payload.get("email_body_excerpt"))
        # getattr: ctrl de teste/legado pode nao ter o metodo — degrada para o
        # comportamento anterior em vez de estourar AttributeError no meio da gravacao.
        lookup = getattr(ctrl, "find_supplier_by_email", None)
        sk_fwd = lookup(fwd_email) if (fwd_email and lookup) else None
        if sk_fwd:
            for col in ("supplier_name", "supplier_cnpj", "supplier_cpf"):
                payload.pop(col, None)
            payload["sk_supplier"] = sk_fwd
            # 🔴 MARCA O SINAL COMO FRACO — e nao e' cosmetico: e' o que impede
            # apply_forced_classification de fazer WRITE-BACK no cadastro deste
            # fornecedor. Ver SUPPLIER_SIGNAL_WEAK e o bloco em apply_forced_classification.
            payload[SUPPLIER_SIGNAL_KEY] = SUPPLIER_SIGNAL_FORWARDED_EMAIL
            log.info(f"    [FORNECEDOR-ENCAMINHADO-EMAIL] remetente original {fwd_email!r} "
                     f"casa o fornecedor cadastrado sk={sk_fwd}")
            cost_center_id, chart_account_id = ctrl.supplier_defaults(sk_fwd)
            if cost_center_id:
                payload["cost_center_id"] = cost_center_id
            if chart_account_id:
                payload["chart_account_id"] = chart_account_id
            return True
    # Regra de IMPOSTO: guia de tributo (darf/das/gnre/gare/dare/iss/...) SEM favorecido
    # real extraido do documento — o credor e o orgao arrecadador (Fisco), que a
    # extracao nao captura. Lanca sob a OTIMOTEX (a empresa pagadora) em vez de derivar
    # um "fornecedor" generico do assunto (ex.: "IMPOSTOS", "GNRE -PAGAMENTO", "DARE -
    # REF"). Curto-circuita os fallbacks de assunto e pagador. Favorecido real extraido
    # (ex.: "PREFEITURA DE SP") NAO dispara a regra e e preservado.
    if not has_real_supplier and _is_tax_document(payload.get("document_type")):
        for col in ("supplier_name", "supplier_cnpj", "supplier_cpf"):
            payload.pop(col, None)
        payload["sk_supplier"] = OTIMOTEX_SK_SUPPLIER
        log.info("    [FORNECEDOR-IMPOSTO] guia de tributo sem favorecido — "
                 f"gravando OTIMOTEX (sk={OTIMOTEX_SK_SUPPLIER})")
        cost_center_id, chart_account_id = ctrl.supplier_defaults(OTIMOTEX_SK_SUPPLIER)
        if cost_center_id:
            payload["cost_center_id"] = cost_center_id
        if chart_account_id:
            payload["chart_account_id"] = chart_account_id
        return True
    # fallback 2: nome do ASSUNTO ancorado em sigla de razao social — sinal do PROPRIO
    # e-mail, mais confiavel que uma linha "De:" solta no corpo (que pode ser de um
    # TERCEIRO da cadeia). Roda ANTES do fallback pelo corpo para nao regredir casos
    # ja corretos (ex.: id 401, "MOVVI LOGISTICA LTDA" no assunto).
    # `_subject_anchor`/`_subject_has_anchor` ja foram calculados acima, para a guarda do
    # fallback 1b — reusados aqui em vez de recomputados. Sao seguros: derivam so de
    # payload["subject"], que nenhum bloco entre os dois pontos altera.
    if not has_real_supplier:
        if _subject_has_anchor:
            payload["supplier_name"] = _subject_anchor
            has_real_supplier = True
            log.info(f"    [FORNECEDOR-ASSUNTO-SIGLA] nome ancorado em sigla no "
                     f"assunto: {_subject_anchor!r}")
    # fallback 3: remetente ORIGINAL de um bloco ENCAMINHADO no corpo ('De:'/'From:'
    # ancorado em sigla de razao social) — so quando o assunto NAO tem ancora propria
    # (fallback 2 esgotou). So dispara quando o e-mail tem corpo (email_body_excerpt,
    # gravado por extract_from_email_body).
    if not has_real_supplier:
        guessed = _supplier_from_forwarded_sender(payload.get("email_body_excerpt"))
        if guessed:
            payload["supplier_name"] = guessed
            has_real_supplier = True
            log.info(f"    [FORNECEDOR-ENCAMINHADO] nome do remetente original "
                     f"(corpo encaminhado): {guessed!r}")
    # fallback 4: nome do assunto SEM ancora (heuristica generica, tambem filtra tipo
    # de documento/pagamento) — fallback 2 ja tentou a ancora e esgotou.
    if not has_real_supplier:
        guessed = _supplier_name_from_subject(payload.get("subject"))
        if guessed:
            payload["supplier_name"] = guessed
            log.info(f"    [FORNECEDOR-ASSUNTO] nome derivado do assunto: {guessed!r}")
    sk_supplier = ctrl.resolve_supplier(payload)  # fallback 5: e-mail do remetente (na RPC)
    # fallback 6: PAGADOR (ultimo recurso) — esgotaram nome/CNPJ/CPF/e-mail/assunto/corpo.
    if not sk_supplier:
        sk_supplier = _resolve_supplier_by_payer(ctrl, payload)
    for col in ("supplier_name", "supplier_cnpj", "supplier_cpf"):
        payload.pop(col, None)
    if not sk_supplier:
        return False
    payload["sk_supplier"] = sk_supplier
    # Default de classificacao contabil do fornecedor (migration 052): a nova conta
    # herda cost_center_id/chart_account_id do supplier quando > 0. Ausentes => a
    # coluna NOT NULL DEFAULT 0 do banco assume 0 (nao enviamos None, que violaria o
    # NOT NULL). So leitura — o write-back (modal) e responsabilidade da Next API.
    cost_center_id, chart_account_id = ctrl.supplier_defaults(sk_supplier)
    if cost_center_id:
        payload["cost_center_id"] = cost_center_id
    if chart_account_id:
        payload["chart_account_id"] = chart_account_id
    return True


def _to_decimal(value, default):
    """Converte texto/numero em float (2 casas) ou retorna default."""
    if value is None:
        return default
    s = str(value).strip().replace(",", ".")
    if s == "" or s.lower() in ("none", "nan", "null"):
        return default
    try:
        return round(float(s), 2)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Extracao a partir do corpo do e-mail (sem anexo valido)
# ---------------------------------------------------------------------------
# Pedido informal de pagamento a fornecedor — nome, valor e dados bancarios no
# proprio corpo da mensagem (geralmente PIX), sem PDF anexado. O schema ja
# reserva extraction_source='email_body' para esse caso (migration 001).
#
# Rotulo de fornecedor no inicio de uma linha do corpo. Inclui os termos pedidos
# pelo usuario (fornecedor/responsavel/prestador/nome) + variantes ja usadas.
# O separador ":"/"-" e OPCIONAL (pode vir so um espaco, ex.: "Nome MATEUS ...").
# Para NAO capturar continuacoes de frase ("Responsável pela compra", "Empresa de
# transporte"), o valor deve comecar por LETRA MAIUSCULA ou digito (nome proprio):
# o char class `[A-ZÀ-Þ0-9]` e case-SENSITIVE (so o rotulo e case-insensitive via
# `(?i:...)`); `\b` evita casar o rotulo como prefixo ("Nomeação").
_BODY_NAME_RE    = re.compile(
    r"^[ \t]*"
    r"(?i:fornecedor|favorecido|benefici[aá]rio|cedente|raz[aã]o\s+social|"
    r"empresa|nome|respons[aá]vel|prestador)"
    r"\b[ \t]*[:\-]?[ \t]*([A-ZÀ-Þ0-9][^\r\n]*?)[ \t\r]*$",
    re.MULTILINE,
)
# BLOCO rotulado "Dados do emissor/beneficiario/cedente/sacador" — o nome vem na LINHA
# SEGUINTE, nao na mesma (o webmail achata a tabela HTML). _BODY_NAME_RE exige o valor na
# MESMA linha, entao nao alcanca este layout: na conta 694 o fornecedor real
# ("AGENCIA K1 DIGITAL WEBSITES E MARKETING") estava logo abaixo de "Dados do emissor" e
# foi ignorado, e o nome acabou derivado do assunto — virando lixo.
# Aceita as duas formas (mesma linha ou proxima) e exige, como _BODY_NAME_RE, que o valor
# comece por MAIUSCULA/digito (char class case-SENSITIVE) para nao capturar prosa.
#
# So rotulos de BLOCO ("dados do ..."): "emitido por" ficaria de fora de proposito —
# no rodape aparece "Este boleto foi emitido por www.sejaefi.com.br", que e a PLATAFORMA,
# nao o fornecedor.
# O `(?:\r?\n[ \t]*){0,2}` aceita o valor na MESMA linha (0), na linha SEGUINTE (1) ou
# apos UMA linha em branco (2) — limite baixo de proposito: alem disso o texto capturado
# ja nao e o valor do rotulo, e sim uma linha distante.
_BODY_ISSUER_RE = re.compile(
    r"(?im)^[ \t]*dados\s+do\s+(?:emissor|benefici[aá]rio|cedente|sacador)"
    r"[ \t]*:?[ \t]*(?:\r?\n[ \t]*){0,2}([A-ZÀ-Þ0-9][^\r\n]*?)[ \t\r]*$")
# Valor monetario. Tolera separadores entre "R$" e o numero ("R$:", "R$ -")
# porque varios e-mails internos escrevem "R$:  297,08".
_BODY_AMOUNT_RE  = re.compile(r"R\$\s*[:\-]?\s*([\d.,]+)")
# Valor rotulado como "Total" / "Valor Total" — tem PRECEDENCIA: quando o corpo
# lista parcelas somadas/subtraidas, o total e o valor a pagar (nao a 1a parcela).
_BODY_TOTAL_RE   = re.compile(
    r"(?i)(?:valor\s+)?total\s*[:\-]?\s*R\$\s*[:\-]?\s*([\d.,]+)")
# Valor LIQUIDO / A PAGAR — o montante EFETIVO do documento (apos descontos). Tem
# precedencia MAXIMA (antes de "Total" e da soma). Rotulos que denotam o unico
# valor a pagar, jamais uma parcela. Cobre faturas que REPETEM o mesmo valor em
# varios rotulos (ex.: Rodonaves "Valor da fatura" == "Valor liquido"), que a soma
# das parcelas duplicava; e, quando ha desconto, o liquido e o correto (nao o bruto).
# So os rotulos INEQUIVOCOS do valor final: "liquido" e "a pagar". NAO inclui
# "valor da fatura"/"do documento"/"do boleto" (podem ser o BRUTO, antes de juros)
# — se ha varios rotulos com o MESMO valor, a dedup da soma (regra 2) ja resolve;
# se ha desconto real, so o "liquido"/"a pagar" e a fonte de verdade.
_BODY_NET_RE     = re.compile(
    r"(?i)valor\s+(?:l[ií]quido|a\s+pagar)"
    r"\s*[:\-]?\s*R\$\s*[:\-]?\s*([\d.,]+)")
# Valor ROTULADO sem "R$" (ex.: "Valor 50,00", "Valor: 1.250,00", "Total 304,04",
# "o valor de 172,39"). So usado como FALLBACK quando nao ha nenhum valor com "R$".
# Exige o rotulo (valor/total) E o formato monetario BR com EXATAMENTE 2 casas
# decimais — assim nao captura numeros soltos (quantidades, "NF 1087", datas).
# Tolera um conectivo curto (de/da/do) entre o rotulo e o numero ("valor de 172,39")
# sem casar substring ("valor desconto" nao casa: o numero nao segue o conectivo).
# group(1)=rotulo, group(2)=numero. "Total"/"Valor Total" tem precedencia sobre "Valor".
# Aceita 2-3 casas decimais: o 3º digito e digitacao com zero a mais (ex.: "VALOR:
# 1.799,960" → 1799,96, id 186) — o _brl_to_decimal normaliza. Continua exigindo >=2
# casas (nao captura numero solto/quantidade/"NF 1087").
_BODY_LABELED_AMT_RE = re.compile(
    r"(?i)\b(valor\s+total|total|valor)\b(?:\s+(?:de|da|do))?\s*[:\-]?\s*"
    r"(\d{1,3}(?:\.\d{3})*,\d{2,3}|\d+,\d{2,3})(?!\d)")
# Tabela de boletos/parcelas no corpo: cada título é uma sequência de 6 campos —
# documento, parcela, emissão (data), vencimento (data), valor (R$) e dias — que o
# webmail quebra em linhas separadas (\r). Detecta a OBER e similares. A linha
# "Total R$ ..." NÃO casa (não tem documento/parcela/datas antes). Usada para criar
# UMA conta por boleto — nunca uma conta somada com o total (regra de negócio).
_BODY_INSTALLMENTS_RE = re.compile(
    r"(\d{4,})\s+(\d{1,3})\s+(\d{2}/\d{2}/\d{2,4})\s+(\d{2}/\d{2}/\d{2,4})\s+"
    r"R\$\s*([\d.]*\d,\d{1,2})\s+\d{1,4}")
# Tabela de FATURAS no corpo (variante sem parcela/dias): cada titulo e a sequencia
# documento, emissao (data), vencimento (data) e valor (R$) — os demais campos da
# linha (desconto, link "VISUALIZAR", linha digitavel) vem depois e nao entram aqui.
# Caso real (MOVVI, conta 693): o webmail achata a tabela HTML em uma linha por
# CAMPO, entao os rotulos ficam TODOS no cabecalho e os regex ancorados em rotulo
# (_BODY_ISSUE_RE / _BODY_DUE_RE / _BODY_INVOICE_RE) nao alcancam os valores — o
# vencimento caia no fallback (data do e-mail) e o n do documento virava sintetico.
# A propria FORMA da linha (doc + 2 datas + R$ valor, so espacos entre os campos) e
# o que a identifica, sem depender de rotulo.
# Guardas contra falso positivo:
#   (?<![\w/-])       — o documento comeca em inicio de campo (nao no meio de um token)
#   (?=[\w./-]*\d)    — o documento contem ao menos um digito (descarta palavra solta)
#   {3,}              — >= 4 caracteres, para NAO casar a coluna 'Parcela' ('001') da
#                        tabela da OBER, cujo layout tem 6 campos (_BODY_INSTALLMENTS_RE)
# Valor com virgula (BR, '1.234,56') ou ponto ('181.90', como a MOVVI escreve) —
# _brl_to_decimal normaliza os dois.
_BODY_INVOICE_ROW_RE = re.compile(
    r"(?<![\w/-])((?=[A-Za-z0-9._/-]*\d)[A-Za-z0-9][A-Za-z0-9._/-]{3,})\s+"
    r"(\d{2}/\d{2}/\d{2,4})\s+"
    r"(\d{2}/\d{2}/\d{2,4})\s+"
    r"R\$\s*(\d[\d.,]*)")
# Data pura NAO e numero de documento (a classe do documento aceita '/' e digitos,
# entao 'dd/mm/aaaa' casaria a captura) — descartada em _extract_body_invoice_rows.
_BODY_DATE_ONLY_RE = re.compile(r"\d{2}/\d{2}/\d{2,4}")
# Teto do segmento em que se procura a linha digitavel da ULTIMA linha da tabela (as
# demais sao delimitadas pela linha seguinte). Na tabela real (MOVVI) a distancia do
# documento ate a linha digitavel da mesma fatura e ~120 caracteres; 500 e folgado para
# variacoes de layout e ainda impede alcancar o rodape do e-mail.
_INVOICE_ROW_BARCODE_WINDOW = 500
_BODY_PIX_RE     = re.compile(r"\bpix\b", re.IGNORECASE)
_BODY_DUE_RE     = re.compile(r"(?i)venc(?:imento|to)?\D{0,15}?(\d{2}/\d{2}/\d{2,4})")
# "DATA (PARA/DE/DO) PAGAMENTO: DD/MM/AA" — rotulo de vencimento usado nas notas
# internas de pagamento (ex.: "DATA PARA PAGAMENTO: 22/06/26", id 186). Fallback
# apos "vencimento" e antes de usar a data de emissao/extracao.
_BODY_PAYDATE_RE = re.compile(
    r"(?i)data\s+(?:para|de|do)?\s*pagamento\D{0,10}?(\d{2}/\d{2}/\d{2,4})")
_BODY_ISSUE_RE   = re.compile(r"(?i)emiss[aã]o\D{0,10}?(\d{2}/\d{2}/\d{2,4})")
_BODY_INVOICE_RE = re.compile(r"(?i)\b(?:nf(?:[- ]?e)?|nota\s+fiscal|fatura\s+n[º°.]?)\s*[n°.:]?\s*(\d{3,})")
# Nº de documento rotulado EXPLICITAMENTE como "Número do documento" — valor
# ALFANUMÉRICO (ex.: boleto Sabesp "SOR202659903949", CATAGUASES "014696-001").
# Usado como fallback quando _BODY_INVOICE_RE (NF/fatura + dígitos) não casa.
# Conservador de propósito: só o rótulo "número do documento" (varredura confirmou
# 0 falso positivo; rótulos frouxos como "documento nº" capturavam "Banco").
_BODY_DOCNUM_RE  = re.compile(
    r"(?i)n[uú]mero\s+do\s+documento\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9./-]{3,})")
# "Fatura No: 20880" — variante de "fatura Nº" escrita por extenso ("No"), sem o
# sinal º/°. _BODY_INVOICE_RE exige "n" seguido OPCIONALMENTE por º/°/. e depois
# digitos; o "o" literal de "No" nao esta nessa classe e quebra o match (o "o"
# fica sem consumir, e os digitos nao vem logo em seguida). Fallback de
# precedencia MAIS BAIXA (so quando _BODY_INVOICE_RE e _BODY_DOCNUM_RE nao acham
# nada) — caso real: lembrete periodico da Contabil Esquema ("Fatura No: 20880",
# contas 668/669). Exige digitos logo apos (so espaco/':'/'-' no meio) para nao
# casar "fatura no valor de..."/"fatura no total de...".
_BODY_INVOICE_NO_RE = re.compile(r"(?i)\bfatura\s+no?\.?\s*[:\-]?\s*(\d{3,})")
# "Cobranca N 1040983896" — identificador da COBRANCA nas plataformas de assinatura
# (Efi/Gerencianet e afins). O \s* aceita quebra de linha entre o rotulo e o "N", porque
# o webmail achata a tabela HTML ("Cobranca\n N 1040983896" — conta 694).
#
# NAO capturar "Assinatura N" (nao regredir): o numero da ASSINATURA e o mesmo em TODAS
# as cobrancas do contrato, entao usa-lo como invoice_number faria a cobranca do mes
# seguinte DEDUPLICAR contra a anterior (impressao 2: fornecedor+numero+valor) e o titulo
# seria PERDIDO em silencio. O numero da COBRANCA e unico por parcela — e o correto.
_BODY_CHARGE_NUM_RE = re.compile(r"(?i)\bcobran[çc]a\s*n[º°]?\.?\s*[:\-]?\s*(\d{4,})")
# ID estável da fatura no link da SIEG (app.sieg.com/faturas?bill=NNN). Sem nº de
# documento no texto, é o identificador que faz os DOIS lembretes da mesma fatura
# ("Vencimento Próximo" + "Hoje") deduplicarem — antes geravam 2 contas/mês porque
# o corpo só tinha data relativa ("vence hoje") → nº fabricado por data divergia.
_BODY_SIEG_BILL_RE = re.compile(r"app\.sieg\.com/faturas\?bill=(\d+)", re.IGNORECASE)
# CNPJ: o "/NNNN-NN" e altamente distintivo — baixo risco de falso positivo.
_BODY_CNPJ_RE    = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/\d{4}-?\d{2}\b")
# CPF: so quando rotulado, para nao casar outros numeros de 11 digitos.
_BODY_CPF_RE     = re.compile(r"(?i)cpf\D{0,5}(\d{3}\.?\d{3}\.?\d{3}-?\d{2})")
# Linha digitavel / codigo de barras de boleto (47) ou guia de arrecadacao (48).
_BODY_BARCODE_RE = re.compile(
    r"(?i)(?:linha\s+digit[aá]vel|c[oó]digo\s+de\s+barras)\D{0,10}([\d.\s]{47,60})"
)
# Fallback de nome do fornecedor (sem rotulo e sem CNPJ/CPF), em ordem de confianca:
#   1) "pix/pagar/transferir ... p/|para <Nome>" — destinatario do pagamento.
#   2) Assinatura titulada "Prof./Dr./Sr. <Nome>" — quem prestou o servico.
# Caso classico: honorarios de profissional ("pix p/ Wesley" + "Prof. Wesley S. Paixao").
# Tokens capitalizados (preservam acento); o verbo/rotulo e case-insensitive.
_NAME_TOKEN = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ.'-]+"
_NAME_SEQ   = r"(" + _NAME_TOKEN + r"(?:\s+" + _NAME_TOKEN + r"){0,3})"
_BODY_PAYTO_RE = re.compile(
    r"(?i:\b(?:pix|pag\w*|transfer\w*|dep[oó]sit\w*)\b)[^.\n]{0,15}?"
    r"(?i:p/|\bpra\b|\bpara\b)\s+" + _NAME_SEQ
)
_BODY_SIGN_RE  = re.compile(r"(?i:\b(?:prof|dr|dra|sr|sra)\b)\.?\s+" + _NAME_SEQ)
# Palavras que NAO fazem parte de nome — cortam a captura (evita "Wesley Ref...").
_NAME_STOPWORDS = {
    "ref", "refer", "total", "valor", "obs", "att", "atenciosamente", "hoje",
    "amanha", "favor", "gentileza", "conta", "banco", "ag", "cc", "pix", "obg",
    "obrigado", "obrigada", "desde", "ja", "agradeco",
}

def _clean_person_name(raw: str) -> "str | None":
    """Limpa a captura de nome: corta no primeiro stopword e exige >= 3 chars."""
    out = []
    for tok in (raw or "").split():
        if tok.strip(".,").lower() in _NAME_STOPWORDS:
            break
        out.append(tok)
    name = " ".join(out).strip(" .,")
    return name if len(name) >= 3 else None

def _supplier_from_signals(body_text: str) -> "str | None":
    """Tenta extrair o nome do fornecedor sem rotulo: assinatura titulada
    (mais completa) e, depois, o destinatario do pagamento ('p/ <Nome>')."""
    for rx in (_BODY_SIGN_RE, _BODY_PAYTO_RE):
        m = rx.search(body_text or "")
        if m:
            name = _clean_person_name(m.group(1))
            if name:
                return name
    return None
# Comprovante de pagamento ja feito / "pix recebido" / confirmacao — NAO e conta
# a pagar. Inclui o aviso da SIEG "identificamos o pagamento da fatura" e
# "pagamento processado" (confirma pagamento ja realizado, nao cobra).
_BODY_RECEIPT_RE = re.compile(
    r"(?i)comprovante\s+de\s+(?:pix\s+)?recebido|pix\s+recebido\s+de|"
    r"pagamento\s+(?:recebido|confirmado|processado)|"
    r"identificamos\s+o\s+pagamento"
)
# Sinais de que o e-mail PEDE um pagamento (afasta o falso positivo de comprovante).
_BODY_PAYMENT_REQUEST_RE = re.compile(
    r"(?i)fazer\s+o\s+pagamento|favor\s+pagar|por\s+gentileza|efetuar\s+o\s+pagamento"
)

# Tabela de keywords por tipo de documento — verificada contra o corpo do e-mail.
# Ordem importa: termos mais específicos antes dos fallbacks genéricos.
_BODY_DOC_KEYWORDS: list[tuple[str, list[str]]] = [
    # Notas fiscais (NF-e / NFS-e) — NAO geram conta a pagar (ver SKIP_ACCOUNT_TYPES).
    # Tipos em lowercase para casar com o CHECK de financial_account_control e o skip.
    # Mais específico primeiro: NFS-e (serviço) antes de NF-e (mercadoria).
    ("nfse",       ["nota fiscal de servico", "nota fiscal eletronica de servico",
                    "nota fiscal de servicos eletronica", "nfs-e", "nfse"]),
    ("nfe",        ["nota fiscal eletronica", "danfe", "nf-e", "nfe"]),
    # Honorários (serviços profissionais) — tipo próprio; pagamento sempre PIX.
    ("honorários", ["honorario", "honorarios"]),
    # Container (frete/demurrage/movimentação de contêineres).
    ("container",  ["container", "conteiner"]),
    ("DARF",       ["darf"]),
    ("GPS",        ["guia da previdencia social", "guia previdencia social", "gps"]),
    ("DAS",        ["simples nacional", "das-simples", "das simples",
                    "documento de arrecadacao do simples", "simei"]),
    ("GRU",        ["guia de recolhimento da uniao", "gru"]),
    ("DAE",        ["documento de arrecadacao do esocial", "dae"]),
    # DAR / DARE — os dois acronimos do Documento de Arrecadacao ESTADUAL, num tipo so
    # (migration 133).
    # 🔴 SO FRASES para "dar", NUNCA a forma pura: o casamento aqui e' por PALAVRA
    # INTEIRA (_has_word), o que NAO basta — "dar" e' verbo comum e casaria "por
    # gentileza dar baixa neste titulo". Mesmo tratamento de 'das' (so "simples
    # nacional"/"simei") e de 'dam / duam' (so "duam"). "dare" e' inequivoco e entra puro.
    # 🔴 "documento de arrecadacao estadual" FICOU DE FORA: e' o nome por EXTENSO do DAE
    # em PE e no CE ("DAE JUCEPE — Documento de Arrecadacao Estadual"), entao a frase
    # rotularia DAE como DAR / DARE. O nome oficial do DARE ("de RECEITAS estaduais") e'
    # string disjunta e pode ficar.
    ("DAR / DARE", ["documento de arrecadacao de receitas estaduais", "dare",
                    "dar modelo 1", "dar-1", "dar/aut", "dar avulso"]),
    ("GNRE",       ["guia nacional de recolhimento", "gnre"]),
    ("IPVA",       ["guia de ipva", "ipva"]),
    ("IPTU",       ["guia de iptu", "iptu"]),
    ("DAM / DUAM", ["documento de arrecadacao municipal", "duam"]),
    ("ISS",        ["recolhimento de iss", "guia de iss", "guia iss", "iss a recolher"]),
    ("ITBI",       ["imposto de transmissao", "guia de itbi", "itbi"]),
    ("GARE",       ["gare"]),
    ("tributo",    ["guia de recolhimento", "guia de pagamento",
                    "documento de arrecadacao"]),
    # Multa / penalidade avulsa (auto de infracao, juros/multa isolados).
    ("multa",      ["auto de infracao", "multa", "penalidade"]),
    # Fatura generica (ex.: Correios "Valor da fatura") — fallback antes de 'outro'.
    ("fatura",     ["fatura"]),
]


def _ns_body(s: str) -> str:
    """Strip accents + lowercase — para busca de keywords no corpo do e-mail."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


# ── Contatos do fornecedor (telefone / WhatsApp / chave PIX) ────────────────────
# Detecta contato a partir do CORPO/assunto/descrição do e-mail (decisão: NÃO tocar
# no prompt do PDF). Gravado no cadastro `supplier` (migration 082) pela extração
# (apply_contact_writeback) e pelo backfill scripts/backfill_supplier_contacts.py.

# Chave PIX só é capturada com o rótulo "pix" por perto — anti-falso-positivo: um
# e-mail em assinatura ou o CNPJ do pagador NÃO devem virar chave PIX.
_PIX_LABEL_RE = re.compile(r"(?i)\bpix\b")
_PIX_WINDOW = 80  # chars após o rótulo "pix" onde a chave é procurada.
# Candidatos de chave na janela: e-mail > UUID (aleatória) > dígitos (CPF/CNPJ/tel).
_PIX_KEY_CAND_RE = re.compile(
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"                       # e-mail
    r"|(\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b)"  # UUID c/ hífen
    r"|(\b[0-9a-fA-F]{32}\b)"                                                 # UUID sem hífen
    r"|(\+?\d[\d.\-/()\s]{9,17}\d)"                                           # CPF/CNPJ/telefone
)

# Telefone: formato distintivo "(DD) NNNNN-NNNN" OU rótulo fone/tel/celular + número.
_PHONE_PAREN_RE = re.compile(r"\((\d{2})\)\s*(\d{4,5})[-.\s]?(\d{4})")
_PHONE_LABEL_RE = re.compile(
    r"(?i)\b(?:telefone|tel|fone|celular|cel)\b\D{0,4}"
    r"(?:\+?55[\s.-]?)?(\(?\d{2}\)?[\s.-]?)?(\d{4,5})[\s.-]?(\d{4})")
# WhatsApp: só com rótulo whats/zap por perto (precedência sobre o slot de telefone).
_WHATS_LABEL_RE = re.compile(
    r"(?i)\b(?:whats?app|whats|zap)\b\D{0,4}"
    r"(?:\+?55[\s.-]?)?(\(?\d{2}\)?[\s.-]?)?(\d{4,5})[\s.-]?(\d{4})")


def _extract_pix_key(text: str, exclude: "set[str] | None" = None) -> "str | None":
    """Primeira chave PIX plausível numa janela após um rótulo 'pix'. Retorna a chave
    normalizada (e-mail/UUID em minúsculas; dígitos sem máscara p/ CPF/CNPJ/telefone)
    ou None. `exclude` = dígitos que NUNCA viram chave (ex.: CNPJ do pagador/OTIMOTEX,
    que aparece no bloco reencaminhado do corpo) — evita gravar o identificador do
    pagador como chave do fornecedor."""
    if not text:
        return None
    exclude = exclude or set()
    for lab in _PIX_LABEL_RE.finditer(text):
        window = text[lab.end(): lab.end() + _PIX_WINDOW]
        for m in _PIX_KEY_CAND_RE.finditer(window):
            email, uuid_h, uuid_flat, digits = m.groups()
            if email:
                return email.lower()
            if uuid_h or uuid_flat:
                return (uuid_h or uuid_flat).lower()
            d = re.sub(r"\D", "", digits or "")
            if d.startswith("55") and len(d) in (12, 13):  # tira o código do país (+55)
                d = d[2:]
            if 10 <= len(d) <= 14 and d not in exclude:
                return d
            # candidato excluido/invalido: tenta o proximo na janela (ou proximo rotulo).
    return None


def _norm_ddd_fone(ddd_raw: str, local_raw: str) -> "tuple[str, str] | None":
    """Normaliza (DDD, fone). DDD ausente/inválido → '11' (requisito). fone deve ter
    8 ou 9 dígitos; caso contrário retorna None."""
    ddd = re.sub(r"\D", "", ddd_raw or "")
    fone = re.sub(r"\D", "", local_raw or "")
    if len(fone) not in (8, 9):
        return None
    if not re.fullmatch(r"[1-9]\d", ddd):  # DDD válido é 11..99
        ddd = "11"
    return (ddd, fone)


def _extract_whatsapp(text: str) -> "str | None":
    """Número de WhatsApp rotulado → dígitos DDD+fone (10-11). None se ausente."""
    m = _WHATS_LABEL_RE.search(text or "")
    if not m:
        return None
    pair = _norm_ddd_fone(m.group(1), (m.group(2) or "") + (m.group(3) or ""))
    return (pair[0] + pair[1]) if pair else None


def _extract_phone(text: str, exclude_digits: "str | None" = None) -> "tuple[str, str] | None":
    """Primeiro telefone (formato entre parênteses ou rotulado) como (DDD, fone).
    Pula o número igual a `exclude_digits` (o do WhatsApp, que tem precedência)."""
    text = text or ""
    for m in _PHONE_PAREN_RE.finditer(text):
        pair = _norm_ddd_fone(m.group(1), (m.group(2) or "") + (m.group(3) or ""))
        if pair and (pair[0] + pair[1]) != exclude_digits:
            return pair
    for m in _PHONE_LABEL_RE.finditer(text):
        pair = _norm_ddd_fone(m.group(1), (m.group(2) or "") + (m.group(3) or ""))
        if pair and (pair[0] + pair[1]) != exclude_digits:
            return pair
    return None


def parse_supplier_contacts(text: str, exclude_pix: "set[str] | None" = None) -> dict:
    """Extrai contato do fornecedor do texto do e-mail. Retorna
    {'pix': str|None, 'phone': (ddd, fone)|None, 'whatsapp': str|None}.
    `exclude_pix` = dígitos que nunca viram chave PIX (CNPJ do pagador/OTIMOTEX)."""
    text = text or ""
    whats = _extract_whatsapp(text)
    return {
        "pix": _extract_pix_key(text, exclude=exclude_pix),
        "phone": _extract_phone(text, exclude_digits=whats),
        "whatsapp": whats,
    }


def _contact_slot_update(cur1, cur2, key1, key2, value) -> dict:
    """Escolhe o slot (1 ou 2) para gravar `value` sem duplicar: slot1 se vazio;
    slot2 se slot1 já tem OUTRO valor; no-op se já presente ou ambos cheios/difer.
    Comparação sem espaços, case-insensitive."""
    if not value:
        return {}
    def norm(s):
        return (s or "").strip().lower()
    v = norm(value)
    if norm(cur1) == v or norm(cur2) == v:
        return {}
    if not norm(cur1):
        return {key1: value}
    if not norm(cur2):
        return {key2: value}
    return {}


def _phone_slot_update(row: dict, ddd: str, fone: str) -> dict:
    """Slot de telefone (par DDD+fone): grava no 1º par vazio; no-op se já presente
    ou ambos os pares cheios."""
    pairs = (("phone_ddd1", "phone1"), ("phone_ddd2", "phone2"))
    for dk, fk in pairs:
        if (row.get(fk) or "").strip() == fone and (row.get(dk) or "").strip() == ddd:
            return {}
    for dk, fk in pairs:
        if not (row.get(fk) or "").strip():
            return {dk: ddd, fk: fone}
    return {}


def _classify_body_doc_type(body_text: str) -> str:
    """Detecta o tipo de documento tributário a partir do corpo do e-mail.

    Retorna o primeiro tipo cujo algum termo for encontrado (case-insensitive,
    sem acentos). Retorna 'outro' se nenhum termo corresponder.
    PIX sobrescreve este resultado — tratar no chamador.

    Casa por PALAVRA inteira (_has_word), não substring: 'nfe' como substring
    casaria 'co-nfe-ccoes' (CONFECCOES) e classificaria erroneamente uma FATURA
    como NF-e (que é pulada em SKIP_ACCOUNT_TYPES) — bug real do e-mail da RTE,
    endereçado a "TEXTIL E CONFECCOES". Mesma proteção já usada em subjects.
    """
    text = _ns_body(body_text)
    for doc_type, terms in _BODY_DOC_KEYWORDS:
        if any(_has_word(text, term) for term in terms):
            return doc_type
    return "outro"


# Forma de pagamento DECLARADA no corpo do e-mail. Regra de negocio: o pagador escreve
# como pagou (ex.: "PAGAMENTO EM DINHEIRO", "pago deposito", "pagamento via pix"). Termos
# sem acento (casados por _ns_body) e por PALAVRA inteira (_has_word). O valor a gravar e
# o do enum PAYMENT_METHODS (com acento). Ordem = precedencia: credito/debito ANTES de
# cartao (para "cartao de credito" cair em 'crédito', nao 'cartão'); dinheiro primeiro
# (caso do usuario). PIX e boleto tem tratamento proprio antes (has_pix / codigo de
# barras) — aqui servem so de fallback se aquele ramo nao tiver resolvido.
_BODY_PAYMENT_METHOD_KEYWORDS: list[tuple[str, list[str]]] = [
    ("dinheiro",          ["dinheiro", "especie"]),
    ("pix",               ["pix"]),
    ("depósito",          ["deposito"]),
    # 'débito automático' ANTES de 'débito' (mais específico): "Débito Automático" casa aqui,
    # não no 'débito' genérico (cartão-débito). Ambos são valores próprios do enum.
    ("débito automático", ["debito automatico", "debito em conta"]),
    ("crédito",           ["cartao de credito", "credito"]),
    ("débito",            ["cartao de debito", "debito"]),
    ("cartão",            ["cartao"]),
    ("ted",               ["ted"]),
    ("transferência",     ["transferencia"]),
    ("cheque",            ["cheque"]),
    ("vale",              ["vale", "vale refeicao", "vale transporte", "vale alimentacao"]),
    ("boleto",            ["boleto"]),
    ("duplicata",         ["duplicata"]),
]


# Marcadores do RODAPE INSTITUCIONAL das plataformas de cobranca (Efi/Gerencianet,
# Asaas, PagSeguro e afins). Depois deles vem propaganda da plataforma — tipicamente uma
# ENUMERACAO dos produtos dela ("e possivel emitir boletos, carnes, cobrancas via cartao
# de credito e links de pagamento") — que NAO declara nada sobre ESTE titulo. Termos
# normalizados (_ns_body: sem acento, minusculas) e casados por SUBSTRING na linha.
_PLATFORM_FOOTER_MARKERS = (
    "esta cobranca foi gerada",
    "este boleto foi emitido por",
    "pela plataforma e possivel",
    "abra sua conta digital",
)


# Normalizacao que PRESERVA O COMPRIMENTO (1 caractere -> 1 caractere), ao contrario de
# _ns_body: este usa NFD, que DECOMPOE o acento em 2 code points e depois descarta um —
# mudando os indices e impedindo mapear posicao do texto normalizado de volta ao original.
# Aqui a tabela mapeia acento->ASCII e MAIUSCULA->minuscula num unico str.translate, entao
# o offset encontrado no normalizado vale, sempre, no texto original.
_KEEP_LEN_NORM_MAP = str.maketrans(
    "ÁÀÂÃÄáàâãäÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÕÖóòôõöÚÙÛÜúùûüÇçÑñABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "aaaaaaaaaaeeeeeeeeiiiiiiiioooooooooouuuuuuuuccnnabcdefghijklmnopqrstuvwxyz")


def _ns_keep_len(text: str) -> str:
    """Sem acento + minuscula, com comprimento IDENTICO ao da entrada (ver mapa acima).
    Use quando a POSICAO do casamento precisar valer no texto original."""
    return text.translate(_KEEP_LEN_NORM_MAP)


def _strip_platform_boilerplate(text: str | None) -> str:
    """Corta o texto no inicio do RODAPE INSTITUCIONAL da plataforma de cobranca.

    Caso de origem (conta 694, boleto de assinatura via Efi): o rodape dizia
    "...e possivel emitir e enviar boletos, carnes, cobrancas via CARTAO DE CREDITO e
    links de pagamento", e o classificador gravou payment_method='crédito' num titulo
    que o proprio e-mail chama de BOLETO da primeira linha ao assunto. A mencao estava
    numa lista de PRODUTOS DA PLATAFORMA — nao e uma declaracao sobre o documento.

    Corta na POSICAO exata do marcador (nao a linha inteira): quando o marcador divide
    a linha com conteudo util — "Pago em dinheiro. Esta cobranca foi gerada pela Efi." —,
    descartar a linha toda jogaria fora a declaracao que se quer classificar. O corte por
    posicao so e possivel porque _ns_keep_len preserva o comprimento."""
    if not text:
        return ""
    norm = _ns_keep_len(str(text))
    cuts = [pos for pos in (norm.find(m) for m in _PLATFORM_FOOTER_MARKERS) if pos >= 0]
    return str(text) if not cuts else str(text)[:min(cuts)]


def _classify_body_payment_method(*texts: str | None) -> str | None:
    """Detecta a FORMA DE PAGAMENTO declarada no corpo/assunto (ex.: 'PAGAMENTO EM
    DINHEIRO' -> 'dinheiro', 'pago deposito' -> 'depósito'), casando por PALAVRA inteira
    sem acento. Retorna o valor do enum PAYMENT_METHODS (com acento) ou None se nada
    casar. Usado para preencher payment_method quando o ramo principal deixaria 'outro'.

    Precedência POR TEXTO (não por lista): cada texto é avaliado na ordem recebida e o
    primeiro que casar vence — chamando com (body, subject), o CORPO tem precedência sobre
    o ASSUNTO (caso id 325: corpo 'TED AGÊNCIA...' vs assunto 'PAGAMENTO PIX' → 'ted').
    Dentro de um mesmo texto, a ordem de _BODY_PAYMENT_METHOD_KEYWORDS desempata
    (crédito/débito antes de cartão).

    O RODAPÉ da plataforma de cobrança é descartado antes de classificar
    (_strip_platform_boilerplate): a enumeração de produtos dela não declara a forma
    de pagamento deste título. Sem isso, a ordem da lista — que codifica
    ESPECIFICIDADE (débito automático antes de débito), não confiança — fazia um
    'crédito' de propaganda vencer o 'boleto' declarado no assunto e no corpo."""
    for t in texts:
        if not t:
            continue
        norm = _ns_body(_strip_platform_boilerplate(t))
        for method, terms in _BODY_PAYMENT_METHOD_KEYWORDS:
            if any(_has_word(norm, term) for term in terms):
                return method
    return None


# Contas de concessionaria — classificadas por FRASE do assunto/corpo (regra de
# negocio). Tem PRECEDENCIA sobre boleto/fatura/PIX: o assunto "PAGAMENTO CONTA DE
# AGUA" define o tipo mesmo que o corpo pareca uma fatura. Termos sem acento (casados
# por _ns_body) e por PALAVRA inteira (_has_word). "vivo"/"fibra" sao termos do
# usuario para o tipo telefone/internet (a marca Vivo / fibra optica).
_UTILITY_DOC_KEYWORDS: list[tuple[str, list[str]]] = [
    ("conta de água", ["conta de agua", "conta agua"]),
    ("conta de luz",  ["conta de luz", "conta luz"]),
    ("conta de telefone / internet",
        ["conta de telefone", "conta telefone", "conta vivo", "vivo conta",
         "conta de internet", "conta internet", "vivo", "fibra"]),
]


def _classify_utility_doc_type(*texts: str | None) -> str | None:
    """Detecta conta de agua/luz/telefone-internet a partir das frases do assunto e/ou
    corpo. Retorna o tipo (primeiro que casar, na ordem da lista) ou None se nenhum."""
    blob = _ns_body(" ".join(t for t in texts if t))
    for doc_type, terms in _UTILITY_DOC_KEYWORDS:
        if any(_has_word(blob, term) for term in terms):
            return doc_type
    return None


# Marcas de concessionaria → tipo, casadas SOMENTE contra o NOME DO FORNECEDOR
# (regra de negocio). Escopo restrito de proposito: "claro"/"tim"/"vivo" sao
# palavras comuns no corpo ("esta claro", "ao vivo"), entao casar no corpo livre
# geraria falso positivo — por isso so o supplier_name alimenta este match.
_UTILITY_SUPPLIER_BRANDS: list[tuple[str, list[str]]] = [
    ("conta de luz",                  ["enel", "eletropaulo"]),
    ("conta de telefone / internet",  ["vivo", "claro", "tim"]),
    ("conta de água",                 ["sabesp"]),
]


def _classify_utility_by_supplier(supplier_name: str | None) -> str | None:
    """Detecta conta de concessionaria pela MARCA no nome do fornecedor (enel/
    eletropaulo→luz; vivo/claro/tim→telefone-internet; sabesp→água). Palavra inteira,
    sem acento. Retorna o tipo ou None."""
    if not supplier_name:
        return None
    blob = _ns_body(supplier_name)
    for doc_type, brands in _UTILITY_SUPPLIER_BRANDS:
        if any(_has_word(blob, brand) for brand in brands):
            return doc_type
    return None


# ---------------------------------------------------------------------------
# Transporte / CT-e — regra de negocio: o CT-e (documento FISCAL de transporte)
# NAO gera conta a pagar; so o BOLETO de frete gera. Um boleto de transporte fica
# rotulado como document_type='cte' (relatorio). Ver regras 1-5 do plano CTe.
# ---------------------------------------------------------------------------
# Termos de transporte no ASSUNTO (frase/sigla), sem acento (casados via _ns_body).
_TRANSPORT_SUBJECT_TERMS = (
    "cte", "ct-e", "dacte", "conhecimento de transporte", "transporte", "transportadora",
)

# Palavras no NOME DO FORNECEDOR que o identificam como transportadora (palavra
# inteira, sem acento). Escopo restrito ao supplier_name — termos como "frete"/
# "cargas" sao comuns; casar no corpo livre geraria falso positivo (mesmo criterio
# de _UTILITY_SUPPLIER_BRANDS).
_TRANSPORT_SUPPLIER_TERMS = (
    "transporte", "transportes", "transportadora", "logistica", "cargas",
    "encomendas", "frete", "fretes",
)


def _is_transport_supplier(supplier_name: str | None) -> bool:
    """True se o nome do fornecedor indica transportadora (transporte(s)/
    transportadora/logistica/cargas/encomendas/frete(s)). Palavra inteira, sem acento."""
    if not supplier_name:
        return False
    blob = _ns_body(supplier_name)
    return any(_has_word(blob, term) for term in _TRANSPORT_SUPPLIER_TERMS)


def _is_transport_context(subject: str | None, supplier_name: str | None,
                          document_type: str | None) -> bool:
    """True se o e-mail/documento e de TRANSPORTE por qualquer uma das fontes:
    assunto com termo de transporte, fornecedor de transporte (nome) ou
    document_type ja classificado como 'cte'. Base da regra: transporte SEM boleto
    e pulado (nao gera conta); boleto DE transporte vira document_type='cte'."""
    if (document_type or "").strip().lower() == "cte":
        return True
    if _is_transport_supplier(supplier_name):
        return True
    if subject:
        s = _ns_body(subject)
        if any(_has_word(s, t) if " " not in t else (t in s)
               for t in _TRANSPORT_SUBJECT_TERMS):
            return True
    return False


# Tipos "genericos" cujo rotulo pode virar 'cte' quando o documento e um BOLETO de
# transporte. NAO inclui guias/utilities (darf/gnre/conta de luz…): uma transportadora
# que manda um DARF continua DARF — o rotulo transporte so vale para o proprio boleto.
_TRANSPORT_RELABELABLE_TYPES = ("boleto", "cte", "outro", "pix", "")


def _apply_transport_boleto_doc_type(document_type: str | None, subject: str | None,
                                     supplier_name: str | None, barcode: str | None) -> str | None:
    """Regra 4: um BOLETO de transporte (contexto cte/transporte/transportadora ou
    fornecedor de transporte) e rotulado como document_type='cte'. So relabela tipos
    genericos (_TRANSPORT_RELABELABLE_TYPES) — guias/utilities sao preservadas. Exige
    codigo de barras de BOLETO valido (chave NF-e/CT-e nao casa). Idempotente."""
    if not _is_boleto_barcode(barcode):
        return document_type
    if (document_type or "").strip().lower() not in _TRANSPORT_RELABELABLE_TYPES:
        return document_type
    if _is_transport_context(subject, supplier_name, document_type):
        return "cte"
    return document_type


# ---------------------------------------------------------------------------
# Cartorio — pagamento DE/EM cartorio (custas de tabelionato/registro/protesto).
# Um boleto/custa de cartorio e rotulado document_type='cartório'. Sinal: a palavra
# "cartorio" (ou tabelionato/tabeliao) no ASSUNTO ou no NOME DO FORNECEDOR — nao no
# corpo livre (mesma cautela do transporte: casar no corpo geraria falso positivo,
# ex.: "reconhecer firma em cartorio" num e-mail de outra cobranca).
# ---------------------------------------------------------------------------
_CARTORIO_TERMS = ("cartorio", "tabelionato", "tabeliao")

# Tipos "genericos" cujo rotulo pode virar 'cartório'. NAO inclui guias/utilities/cte/
# honorarios: um ITBI pago no cartorio continua ITBI (o rotulo cartorio so vale para a
# custa generica que chega como boleto/outro/pix).
_CARTORIO_RELABELABLE_TYPES = ("boleto", "outro", "pix", "")


def _is_cartorio_context(subject: str | None, supplier_name: str | None) -> bool:
    """True se o e-mail/documento e de CARTORIO: palavra "cartorio" (ou tabelionato/
    tabeliao) no assunto OU no nome do fornecedor. Palavra inteira, sem acento
    (_has_word/_ns_body) — evita casar dentro de outra palavra."""
    for text in (subject, supplier_name):
        if text and any(_has_word(_ns_body(text), t) for t in _CARTORIO_TERMS):
            return True
    return False


def _apply_cartorio_doc_type(document_type: str | None, subject: str | None,
                             supplier_name: str | None) -> str | None:
    """Rotula como document_type='cartório' um pagamento de cartorio (contexto
    "cartorio" no assunto/fornecedor). So relabela tipos genericos
    (_CARTORIO_RELABELABLE_TYPES) — guias/utilities/cte/honorarios sao preservadas.
    Idempotente ('cartório' ja nao esta no set de relabelaveis)."""
    if (document_type or "").strip().lower() not in _CARTORIO_RELABELABLE_TYPES:
        return document_type
    if _is_cartorio_context(subject, supplier_name):
        return "cartório"
    return document_type


# ---------------------------------------------------------------------------
# SEGURADORA — so boleto valido vira conta a pagar
# ---------------------------------------------------------------------------
# Regra de negocio: e-mail de seguradora SO gera conta quando traz um BOLETO com linha
# digitavel valida; sem boleto, o e-mail e 'ignorado' (nao 'falha'). O apolice/kit digital
# costuma vir com DOIS documentos (boleto + "conjunto faturamento"): so o boleto se paga.
#
# O contexto e detectado SO PELO ASSUNTO — deliberado, nao e descuido. Incluir o
# supplier_name ou o dominio do remetente QUEBRARIA contas que hoje funcionam, porque
# "Porto Seguro" e nome de fornecedor legitimo de varios ramos:
#   - conta 348 "BOLETO - PORTO SAUDE", fornecedor "PORTO SEGURO - SEGURO SAUDE S/A",
#     gravada SEM barcode → o gate por supplier_name a teria descartado;
#   - conta  58 "Rastreador - Demonstrativo fatura", remetente @portoseguro.com.br,
#     extraida do CORPO (sem barcode) → o gate por dominio a teria descartado.
# Nenhuma das duas tem "seguro" no ASSUNTO, entao o criterio por assunto as preserva e
# ainda assim pega o alvo ("SEGUROS SURA VID_G_..."). NAO ampliar para fornecedor/remetente.
_INSURANCE_TERMS = ("seguro", "seguros", "seguradora", "seguradoras", "apolice", "apolices")

# Tipos "genericos" cujo rotulo pode virar 'seguro'. Nao inclui guias/utilities/cte/
# cartorio/honorarios: um tributo cobrado por seguradora continua sendo o tributo.
_SEGURO_RELABELABLE_TYPES = ("boleto", "outro", "")


def _is_insurance_context(subject: str | None) -> bool:
    """True se o ASSUNTO indica e-mail de seguradora (seguro/seguros/seguradora/apolice).
    Palavra inteira e sem acento (_has_word/_ns_body): "seguranca" nao casa, e "apolice"
    cobre o assunto escrito com ou sem acento. So o assunto — ver o bloco acima."""
    if not subject:
        return False
    s = _ns_body(subject)
    return any(_has_word(s, t) for t in _INSURANCE_TERMS)


def _apply_seguro_doc_type(document_type: str | None, subject: str | None) -> str | None:
    """Rotula como document_type='seguro' o pagavel de um e-mail de seguradora. So
    relabela tipos genericos (_SEGURO_RELABELABLE_TYPES) — guias/utilities/cte/cartorio/
    honorarios sao preservados. Idempotente ('seguro' nao esta no set de relabelaveis)."""
    if (document_type or "").strip().lower() not in _SEGURO_RELABELABLE_TYPES:
        return document_type
    if _is_insurance_context(subject):
        return "seguro"
    return document_type


# ---------------------------------------------------------------------------
# Classificacao contabil FORCADA por tipo/contexto de documento (regra de negocio,
# so na extracao de e-mail). Alguns documentos vao SEMPRE para uma conta contabil fixa,
# independentemente do default do fornecedor; parte deles tambem propaga a classificacao
# ao supplier (write-back). Ver "Classificacao contabil forcada" no CLAUDE.md.
# ---------------------------------------------------------------------------
# Centro de custo / plano de contas do TRANSPORTE (regra NAO-tributaria — CT-e/frete).
CC_LOGISTICA       = 4    # financial_cost_center: LOG — Logistica
CA_TRANSPORTADORAS = 339  # financial_chart_of_account: 48.2.01 — Servicos de Transportadoras

# document_type canonico da GNRE (usado no gatilho @lebianco de ICMS-ST).
GNRE_DOC_TYPE = "gnre"

# ICMS Substituicao Tributaria — frases EXPLICITAS (sem acento, substring). So GNRE com esse
# sinal vira 4.1.02; GNRE so com codigo de receita/protocolo NAO casa (decisao do usuario).
# "subst.tribut"/"subst tribut" cobrem a forma abreviada dos documentos ("ICMS SUBST.TRIBUT").
_ICMS_ST_PHRASES = ("substituicao tributaria", "subst.tribut", "subst tribut",
                    "subst. tribut", "icms st", "icms-st", "icms substituicao")

# Dominio interno cujo setor envia as guias de ICMS-ST. Uma GNRE vinda deste remetente vira
# 4.1.02 mesmo SEM a frase de ST no texto (gatilho adicional ao _ICMS_ST_PHRASES) — decisao
# do usuario. Casa o dominio (e subdominios); as regras FIXAS (ICMS Importacao) tem precedencia.
LEBIANCO_DOMAIN = "lebianco.com.br"


def _is_lebianco_sender(sender_email: str | None) -> bool:
    """True quando o remetente tem dominio lebianco.com.br (ou subdominio)."""
    domain = (sender_email or "").split("@")[-1].lower().strip()
    return domain == LEBIANCO_DOMAIN or domain.endswith("." + LEBIANCO_DOMAIN)


# Remetente cujo e-mail e SEMPRE da OTIMOTEX FARDOS (endereco EXATO, nao o dominio —
# decisao do usuario: "ester@otimotex.com.br vence o dominio otimotex.com.br").
FARDOS_SENDER = "ester@otimotex.com.br"


def _is_fardos_sender(sender_email: str | None) -> bool:
    """True quando o remetente e EXATAMENTE o endereco da OTIMOTEX FARDOS.

    Endereco completo (nao local-part nem dominio): outro usuario @otimotex.com.br NAO casa —
    e justamente isso que faz esta regra "vencer o dominio". Normalizacao strip+lower, o mesmo
    idiom de resolve_user/is_ignored_sender.
    """
    return (sender_email or "").strip().lower() == FARDOS_SENDER


# ---------------------------------------------------------------------------
# Empresa pagadora (sk_company) — regra por PRECEDENCIA (decisao do usuario, 2026-07-17;
# LE BLANC acrescentada em 2026-09-11).
#
#   1o  mencao a LE BLANC / pagador LE BLANC -> 4 (LE BLANC)        <- VENCE tudo
#   2o  remetente ester@otimotex.com.br      -> 3 (OTIMOTEX FARDOS)
#   3o  referencia a "lebianco"              -> 2 (LEBIANCO)
#   4o  nenhum dos anteriores                -> 1 (OTIMOTEX TECIDOS)
#
# LE BLANC (2026-09-11): QUALQUER mencao ("le blanc", "leblanc", "le_blanc"...) no assunto,
# corpo, anexo (texto e nome do arquivo), remetente, descricao, pagador impresso
# (payer_name/payer_cnpj pela raiz 20584679) — e TAMBEM no FORNECEDOR — marca a conta como da
# LE BLANC. Ela vence inclusive a ester (decisao explicita). ASSIMETRIA DELIBERADA com a
# LEBIANCO: fornecedor LEBIANCO NAO classifica (regra de 2026-07-17, abaixo); fornecedor
# LE BLANC classifica — o usuario decidiu que toda mencao vale, mesmo quando ela e a
# beneficiaria (a correcao nesses casos e manual pela tela; o trigger preserva a curadoria).
#
# A ester vence o DOMINIO otimotex.com.br E a mencao a lebianco (decisao explicita): tudo
# que ela extrai e da FARDOS — salvo mencao a LE BLANC. Nos e-mails/dados externos o termo "OTIMOTEX" continua
# sozinho — o rename para "OTIMOTEX TECIDOS" e so do NOME no cadastro/UI, nada que casa
# texto de fora muda (ver _RECEIVABLE_SUBJECT_TERMS, payer/CNPJ).
#
# E-mail que faz REFERENCIA a "lebianco" (assunto, corpo, ANEXO, remetente ou dominio do
# remetente) e conta da LEBIANCO -> sk_company = 2. SEM mencao -> SEMPRE OTIMOTEX (1).
#
# A REFERENCIA VENCE a busca por CNPJ (nao regredir): a conta pode ser da LEBIANCO com o
# CNPJ da OTIMOTEX impresso no boleto, entao o CNPJ NAO entra nesta regra. Na pratica o
# resolvedor SQL resolve_company_sk deixa de influenciar o pipeline — gravamos sk_company
# SEMPRE (1 ou 2) e o trigger (migration 084) respeita valor explicito.
#
# sk_company (empresa PAGADORA) e INDEPENDENTE de sk_supplier (FORNECEDOR): pode haver conta
# da LEBIANCO cujo fornecedor e a OTIMOTEX. Por isso supplier_name/supplier_cnpj FICAM FORA
# da varredura (nao regredir) — se a LEBIANCO for o FORNECEDOR, quem paga e a OTIMOTEX (=1),
# e varrer o nome do fornecedor inverteria a conta. Reforco: _finalize_supplier ja remove
# essas chaves do payload antes da gravacao.
# ---------------------------------------------------------------------------
# ATENCAO (nao confundir): estes sao sk de EMPRESA (company.sk_company). O
# OTIMOTEX_SK_SUPPLIER (=1) la em cima e sk de FORNECEDOR (supplier.sk_supplier) — mesmo
# valor, mesmo nome, TABELAS DIFERENTES. Nunca tratar um pelo outro.
SK_COMPANY_DEFAULT = 1     # OTIMOTEX TECIDOS — empresa pagadora padrao (sem ester, sem lebianco)
SK_COMPANY_LEBIANCO = 2    # LEBIANCO
SK_COMPANY_FARDOS = 3      # OTIMOTEX FARDOS — tudo que vem do FARDOS_SENDER
SK_COMPANY_LE_BLANC = 4    # LE BLANC ADMINISTRACAO DE BENS PROPRIOS LTDA (CNPJ 20584679000110)
LEBIANCO_TERM = "lebianco"

# Raiz (8 digitos) do CNPJ da LE BLANC — cobre matriz e filiais. Raiz DIFERENTE da do grupo
# OTIMOTEX (47273917), por isso a LE BLANC nao cai na exclusao "a pagadora nunca e
# fornecedor" (company_cnpj() usa so o sk=1) e continua podendo ser FORNECEDORA.
LE_BLANC_CNPJ_ROOT = "20584679"
_CNPJ_LEN = 14

# "le" + separador OPCIONAL (espaco/_/./-) + "blanc": casa "LE BLANC", "LEBLANC", "Le_Blanc",
# "Le-Blanc". Aplicado sobre texto ja normalizado por _ns_body (sem acento, minusculo).
# Fronteiras por lookaround, NAO \b: o \b do Python trata "_" como letra e perderia
# "BOLETO_LEBLANC_-_POR.pdf" (nome de arquivo real). A fronteira a DIREITA impede casar
# "LEBLANCO" — leitura OCR plausivel de "LEBIANCO" (i/l), que e OUTRA empresa.
_LE_BLANC_RE = re.compile(r"(?<![a-z0-9])le[\s_.\-]*blanc(?![a-z0-9])")

# Grafia com ESPACO ("LE BIANCO"), aceita SO NO ASSUNTO (nao regredir — verificado no dado
# real): no assunto e referencia deliberada ("LE BIANCO - PAGAMENTO FORNECEDOR"); no CORPO
# ela aparece na ASSINATURA do grupo ("Departamento Financeiro | Otimotex / Le Bianco") e
# marcaria como LEBIANCO contas que sao da OTIMOTEX (falso positivo comprovado na conta 167,
# assunto "COBRANCA OTIMOTEX TECIDO"). Por isso NAO entra em corpo/anexo/descricao.
LEBIANCO_SUBJECT_TERM = "le bianco"


def _has_lebianco_reference(*texts: "str | None") -> bool:
    r"""True se QUALQUER texto menciona "lebianco" (sem acento, case-insensitive).

    SUBSTRING (e nao _has_word/\b) de proposito: "lebianco" e nome proprio distintivo e
    precisa casar DENTRO de "@lebianco.com.br", "boleto_lebianco.pdf" e "LEBIANCO PLASTICOS".
    O \b do _has_word existe para termos comuns (das/iss/gru) — nao e o caso aqui.
    None/vazio sao ignorados (_ns_body nao e None-safe).

    So a forma JUNTA — a grafia "le bianco" e exclusiva do assunto (_subject_has_lebianco).
    """
    return any(LEBIANCO_TERM in _ns_body(t) for t in texts if t)


def _subject_has_lebianco(subject: "str | None") -> bool:
    """True se o ASSUNTO menciona a LEBIANCO — forma junta OU com espaco ("LE BIANCO")."""
    if not subject:
        return False
    norm = _ns_body(subject)
    return LEBIANCO_TERM in norm or LEBIANCO_SUBJECT_TERM in norm


def _has_le_blanc_reference(*texts: "str | None") -> bool:
    """True se QUALQUER texto menciona a LE BLANC em qualquer grafia (_LE_BLANC_RE).

    Ao contrario da LEBIANCO, a grafia com espaco vale em TODA fonte (decisao do usuario,
    2026-09-11) — e o que aparece no dado real ("Le Blanc - Boleto Porto Saude"). None/vazio
    sao ignorados (_ns_body nao e None-safe).
    """
    return any(_LE_BLANC_RE.search(_ns_body(str(t))) for t in texts if t)


def _is_le_blanc_cnpj(value: "str | None") -> bool:
    """True se o valor e um CNPJ de 14 digitos com a raiz da LE BLANC (matriz ou filial).

    Exige os 14 digitos: um CNPJ com digito deslocado por OCR (caso real, conta 759:
    "02058467900011") NAO casa — aceita-lo exigiria heuristica de substring que casaria
    lixo; nesses casos a mencao ao nome (assunto/anexo) e quem classifica.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    return len(digits) == _CNPJ_LEN and digits.startswith(LE_BLANC_CNPJ_ROOT)


def _le_blanc_supplier_signal(payload: dict) -> bool:
    """True se o FORNECEDOR extraido da linha e a LE BLANC (nome ou CNPJ).

    🔴 Tem de ser avaliado ANTES de _finalize_supplier, que REMOVE supplier_name/
    supplier_cnpj do payload: a fornecedora LE BLANC lida so pelo Vision (sem texto cru do
    anexo) seria perdida em silencio se a checagem ficasse para depois. Os call sites
    capturam o resultado e o repassam a apply_sk_company(le_blanc=...).
    """
    return (_has_le_blanc_reference(payload.get("supplier_name"))
            or _is_le_blanc_cnpj(payload.get("supplier_cnpj")))


def _pdf_text(pdf_path) -> str:
    """Texto CRU do PDF anexado, ou "" — lido UMA vez e servido a dois consumidores.

    O texto do PDF nao chega ao payload (o CSV so traz description/source_file), entao quem
    precisa dele le aqui: a regra LEBIANCO (`_has_lebianco_reference`) e o registro de
    documento fiscal da Onda 3 (`_register_fiscal_documents`). Antes disto a leitura era
    exclusiva da regra LEBIANCO, que abortava na primeira pagina com "lebianco" e descartava
    o resto — a chave de acesso precisa do texto de TODO anexo, entao a leitura passou a ser
    unica e completa.

    BEST-EFFORT: qualquer falha (PDF cifrado sem senha, imagem, pdfplumber indisponivel)
    devolve "" sem levantar — nenhuma destas regras pode bloquear a gravacao da conta.
    """
    try:
        import pdfplumber  # import lazy — so quando ha PDF a inspecionar
        with pdfplumber.open(str(pdf_path)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:  # noqa: BLE001 — best-effort por design
        log.debug(f"Nao foi possivel ler o texto de {pdf_path}: {e}")
    return ""


def _attachment_text(path) -> str:
    """Texto CRU do anexo — PDF via pdfplumber, .docx via `docx_content`. BEST-EFFORT.

    Serve os dois consumidores de sempre (`_pdf_text` explica quais): a regra LEBIANCO e o
    registro de documento fiscal da Onda 3. Sem o ramo de .docx, uma chave de acesso de CT-e ou
    uma mencao a LEBIANCO dentro de um Word seriam perdidas exatamente como o boleto era — o
    `pdfplumber` abre qualquer coisa como PDF e devolve "" no `except`, em nivel DEBUG.
    """
    if str(path).lower().endswith(_DOCX_ATTACHMENT_EXTS):
        mod = _docx_content()
        # Modulo ausente (deploy parcial) ja avisou uma vez; devolver "" e melhor que mandar um
        # ZIP ao pdfplumber, que so produziria ruido no log.
        return mod.docx_text(path) if mod is not None else ""
    return _pdf_text(path)


def _register_fiscal_documents(ctrl, pdf_text: str, storage_key: str,
                               ctx: dict | None = None) -> int:
    """Registra em `fiscal_document` toda chave de acesso valida do texto (Onda 3).

    Devolve quantas foram INSERIDAS (duplicata nao conta) — usado so por log/teste; nenhum
    caller decide nada com isso, porque este registro NAO PODE influenciar o destino da conta.
    A regra de negocio fica intacta: CT-e/NF-e sem boleto continua sem gerar conta a pagar; o
    que muda e que o documento deixa de ser esquecido.

    NAO-FATAL por completo (o try envolve ate a varredura): um PDF fiscal mal formado nao
    pode derrubar a extracao financeira do e-mail.
    """
    if not (pdf_text and storage_key):
        return 0
    mod = _fiscal_key()
    if mod is None:
        return 0
    gravados = 0
    try:
        for chave in mod.extract_access_keys(pdf_text):
            doc = mod.parse_access_key(chave)
            if doc and ctrl.register_fiscal_document(doc, storage_key, ctx):
                gravados += 1
                log.info(f"    [FISCAL] {doc['model_name'].upper()} {doc['doc_number']} "
                         f"(serie {doc['series']}) do CNPJ {doc['emitter_cnpj']} registrado")
    except Exception as e:  # noqa: BLE001 — best-effort por design
        log.warning(f"    [FISCAL] falha ao registrar documento de {storage_key}: {e}")

    # Onda 5 — conteudo do CT-e (peso, rota, NF vinculada, frete) da fatura agregada.
    # DEPOIS do laco acima, e nao dentro dele: o conteudo e um UPDATE na linha, entao ela
    # precisa existir. Rodar antes gravaria em nada, sem erro — e o log diria "registrado".
    _register_cte_content(ctrl, pdf_text, storage_key)
    return gravados


def _register_cte_content(ctrl, pdf_text: str, storage_key: str) -> int:
    """Grava peso/rota/NF/frete dos CT-e da fatura agregada (Onda 5, item 5.3).

    Devolve quantos conhecimentos foram atualizados — so para log/teste; nenhum caller decide
    nada com isso. Igual ao resto do gancho fiscal, e NAO-FATAL e sem efeito colateral: nao
    altera o status do e-mail, nao cria nem muda conta a pagar.

    O parser ja e fail-closed (fatura cujo SUB-TOTAL nao fecha devolve lista vazia), entao aqui
    nao ha decisao de qualidade a tomar — se veio lista, ela fecha.
    """
    if not (pdf_text and storage_key):
        return 0
    mod = _cte_content()
    if mod is None:
        return 0
    try:
        itens = mod.parse_braspress_invoice(pdf_text)
        if not itens:
            return 0
        atualizados = sum(1 for item in itens if ctrl.update_fiscal_content(item))
        if atualizados:
            log.info(f"    [FISCAL] conteudo de {atualizados} CT-e gravado "
                     f"(peso/rota/frete) a partir da fatura")
        return atualizados
    except Exception as e:  # noqa: BLE001 — best-effort por design
        log.warning(f"    [FISCAL] falha ao extrair conteudo de CT-e de {storage_key}: {e}")
        return 0


def resolve_sk_company(subject=None, body_text=None, sender_email=None, description=None,
                       source_file=None, payer_name=None, email_body_excerpt=None,
                       pdf_lebianco: bool = False, payer_cnpj=None,
                       le_blanc: bool = False) -> int:
    """Empresa pagadora da conta, por PRECEDENCIA (nao regredir a ordem):

    1o LE BLANC (4) -> 2o ester (FARDOS=3) -> 3o referencia a lebianco (2) ->
    4o default (OTIMOTEX TECIDOS=1).

    `le_blanc` e o sinal ja apurado pelo caller (texto/nome do anexo, fornecedor capturado
    antes de _finalize_supplier) — fontes que nao chegam aqui como parametro.
    """
    # 🔴 PRIMEIRO de todos: a LE BLANC vence a ester (decisao do usuario, 2026-09-11).
    # Descer esta checagem para baixo da ester devolveria FARDOS para conta da LE BLANC.
    if (le_blanc or _is_le_blanc_cnpj(payer_cnpj)
            or _has_le_blanc_reference(subject, body_text, sender_email, description,
                                       source_file, payer_name, email_body_excerpt)):
        return SK_COMPANY_LE_BLANC
    # Em seguida: o remetente da FARDOS vence o dominio otimotex.com.br E a mencao a
    # lebianco (decisao do usuario). Mover esta checagem para baixo inverteria a regra.
    if _is_fardos_sender(sender_email):
        return SK_COMPANY_FARDOS
    if pdf_lebianco or _is_lebianco_sender(sender_email) or _subject_has_lebianco(subject):
        return SK_COMPANY_LEBIANCO
    if _has_lebianco_reference(body_text, sender_email, description,
                               source_file, payer_name, email_body_excerpt):
        return SK_COMPANY_LEBIANCO
    return SK_COMPANY_DEFAULT


def apply_sk_company(payload: dict, body_text: str = "", pdf_lebianco: bool = False,
                     le_blanc: bool = False) -> None:
    """Grava payload['sk_company'] pela regra de empresa pagadora, RESPEITANDO valor ja
    presente (mesmo idiom de created_by em register_financial).

    O fornecedor LE BLANC tambem e lido do proprio payload quando as colunas ainda estao la
    (rede universal de register_financial / scripts); nos call sites do pipeline elas ja
    foram removidas por _finalize_supplier, e o sinal chega por `le_blanc`."""
    if payload.get("sk_company"):
        return
    payload["sk_company"] = resolve_sk_company(
        subject=payload.get("subject"),
        body_text=body_text,
        sender_email=payload.get("sender_email"),
        description=payload.get("description"),
        source_file=payload.get("source_file"),
        payer_name=payload.get("payer_name"),
        email_body_excerpt=payload.get("email_body_excerpt"),
        pdf_lebianco=pdf_lebianco,
        payer_cnpj=payload.get("payer_cnpj"),
        le_blanc=le_blanc or _le_blanc_supplier_signal(payload),
    )


# ICMS de importacao — frases (sem acento, substring). NUNCA casar "icms" sozinho: ICMS/GNRE
# normal nao deve cair aqui — exige o par icms+importacao.
_ICMS_IMPORT_PHRASES = ("icms importacao", "icms de importacao",
                        "icms na importacao", "importacao icms")


# ---------------------------------------------------------------------------
# Relacao TRIBUTARIA -> plano de contas (financial_chart_of_account). ESTRITA e
# EXCLUSIVAMENTE para _is_tax_document. Determina o account_code do plano a partir do
# TIPO/CONTEXTO do imposto (nunca do supplier) e resolve para (cost_center_id,
# chart_account_id) via classification_for_account_code (segue o cadastro). Precedencia
# MAXIMA (vence o default do fornecedor) e write-back True (o supplier e atualizado com o
# mesmo destino — exceto OTIMOTEX/funcionario, barrados em apply_forced_classification).
# ---------------------------------------------------------------------------
# Nivel 2 — por document_type, quando a guia JA determina o imposto (sem palavra-chave).
# GNRE = veiculo de ICMS interestadual -> conta propria "GNRE a Recolher" (a menos que ST/
# importacao, tratados antes, no nivel 1); GARE = ICMS/SP. Vem ANTES do scan generico para
# nao rebaixar GNRE (que costuma citar "icms") a 4.1.01.
_TAX_DOCTYPE_CHART_CODES = {
    "gnre": "4.4.01",   # GNRE a Recolher
    "gare": "4.1.01",   # ICMS a Recolher (GARE = ICMS/SP)
    "iss":  "4.1.06",   # ISS a Recolher
    "ipva": "6.4.02",   # IPVA
    "iptu": "6.4.01",   # IPTU
    "das":  "4.4.04",   # Taxas Federais a Recolher (Simples Nacional — sem conta dedicada)
}
# Nivel 3 — fallback por ESFERA do document_type, quando o imposto especifico nao foi
# identificado (ex.: "PAGAMENTO IMPOSTOS" tipo darf, "DARE T05"). 'tributo' (sem esfera)
# NAO entra -> nao forca (evita mis-forcar boleto de fornecedor mal-rotulado).
_TAX_SPHERE_CHART_CODES = {
    "darf": "4.4.04", "gru": "4.4.04", "dae": "4.4.04",           # federal
    # estadual — DAR e DARE nomeiam o mesmo instrumento (migration 133). As formas
    # isoladas ficam junto do valor canonico porque este mapa e' consultado com o rotulo
    # normalizado (_norm_term), mas tambem com o rotulo cru em reprocessadores.
    "dar / dare": "4.4.02", "dare": "4.4.02", "dar": "4.4.02",    # estadual
    "dam": "4.4.03", "duam": "4.4.03", "dam / duam": "4.4.03", "itbi": "4.4.03",  # municipal
}
# Nivel 1 — palavra-chave DISTINTIVA do imposto (assunto+descricao), especifico->generico.
# (termo palavra-inteira, account_code). ICMS-import/ICMS-ST/II/PIS-COFINS-CSLL sao tratados
# a parte em _resolve_tax_chart_code (frases/combinacoes). 'das'/'gare'/'gnre'/'dare'/'dam'
# NAO entram aqui (colidem com palavras do PT ou sao ambiguos) — resolvem por doctype/esfera.
_TAX_KEYWORD_CHART_CODES = (
    ("irrf",   "4.2.03"),   # IRRF a Recolher
    ("irpj",   "4.2.01"),   # IRPJ a Recolher
    ("csll",   "4.2.02"),   # CSLL a Recolher
    ("inss",   "4.2.04"),   # INSS Retido a Recolher
    ("iss",    "4.1.06"),   # ISS a Recolher
    ("ipi",    "4.1.03"),   # IPI a Recolher
    ("cofins", "4.1.05"),   # COFINS a Recolher
    ("pis",    "4.1.04"),   # PIS a Recolher
    ("icms",   "4.1.01"),   # ICMS a Recolher
    ("ipva",   "6.4.02"),   # IPVA
    ("iptu",   "6.4.01"),   # IPTU
)


def _resolve_tax_chart_code(document_type: str | None, blob: str,
                            *, sender_email: str | None = None) -> str | None:
    """account_code do plano de contas para uma GUIA TRIBUTARIA, do TIPO/CONTEXTO do imposto
    (nunca do supplier). `blob` = assunto+descricao(+corpo) JA normalizado (`_ns_body`, sem
    acento). Ordem: (1) imposto distintivo por frase/palavra; (2) por document_type especifico;
    (3) palavra-chave distintiva no texto; (4) fallback por esfera. Retorna account_code ou
    None (nao determinavel -> o chamador nao forca)."""
    dt = (document_type or "").strip().lower()
    # (1) frases/combinacoes especificas (maior prioridade)
    if any(p in blob for p in _ICMS_IMPORT_PHRASES):
        return "4.3.05"                                    # ICMS Importacao a Recolher
    if any(p in blob for p in _ICMS_ST_PHRASES) or (
            dt == GNRE_DOC_TYPE and _is_lebianco_sender(sender_email)):
        return "4.1.02"                                    # ICMS-ST a Recolher
    if "imposto de importacao" in blob:
        return "4.3.01"                                    # Imposto de Importacao (II) a Recolher
    if _has_word(blob, "pis") and _has_word(blob, "cofins") and (
            _has_word(blob, "csll") or "retid" in blob):
        return "4.2.05"                                    # PIS/COFINS/CSLL Retidos
    # (2) document_type que JA determina o imposto (GNRE/GARE/ISS/IPVA/IPTU/DAS)
    if dt in _TAX_DOCTYPE_CHART_CODES:
        return _TAX_DOCTYPE_CHART_CODES[dt]
    # (3) palavra-chave distintiva do imposto no texto (refina DARF/DARE/GRU)
    for term, code in _TAX_KEYWORD_CHART_CODES:
        if _has_word(blob, term):
            return code
    # (4) fallback por esfera do document_type
    return _TAX_SPHERE_CHART_CODES.get(dt)


def resolve_forced_classification(ctrl, document_type: str | None, subject: str | None,
                                  *extra_texts: str | None,
                                  sender_email: str | None = None,
                                  sk_supplier: int | None = None) -> tuple[int, int, bool] | None:
    """Classificacao contabil FORCADA (retorna (cost_center_id, chart_account_id, write_back)
    ou None).

    Precedencia MAXIMA para GUIA TRIBUTARIA (`_is_tax_document`): a conta e relacionada ao
    plano de contas pelo TIPO/CONTEXTO do imposto (`_resolve_tax_chart_code` ->
    `classification_for_account_code`), VENCENDO o default do fornecedor. Grava com write-back
    (write_back=True; a exclusao OTIMOTEX/funcionario fica em apply_forced_classification). Se a
    guia nao permite determinar (sem sinal, ou codigo ausente no cadastro), NAO forca (cai no
    comportamento atual — default do fornecedor).

    EXCLUSAO: fornecedores em `TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS` (ex.: Dr. Ricardo,
    despachante — reembolso/honorarios/juridico) NAO recebem classificacao tributaria forcada —
    a regra e pulada e a conta mantem o default do fornecedor / o valor da extracao.

    Abaixo (NAO-tributario): TRANSPORTE — CT-e/frete -> Logistica / Servicos de Transportadoras
    (com write-back). So assunto + document_type (nao a descricao/corpo — evita falso positivo)."""
    if (_is_tax_document(document_type)
            and sk_supplier not in TAX_CLASSIFICATION_EXCLUDED_SK_SUPPLIERS):
        blob = " ".join(_ns_body(t) for t in (subject, *extra_texts) if t)
        code = _resolve_tax_chart_code(document_type, blob, sender_email=sender_email)
        if code:
            cost_center_id, chart_account_id = ctrl.classification_for_account_code(code)
            if cost_center_id or chart_account_id:
                return (cost_center_id, chart_account_id, True)
    if _is_transport_context(subject, None, document_type):
        return (CC_LOGISTICA, CA_TRANSPORTADORAS, True)
    return None


def apply_forced_classification(ctrl, payload: dict, extra_text: str | None = None) -> None:
    """Aplica as regras de classificacao FORCADA (extracao de e-mail): forca
    cost_center_id/chart_account_id na conta por tipo de documento e, quando a regra pede,
    grava a mesma classificacao no supplier (write-back).

    Deve rodar APOS _finalize_supplier (que ja setou sk_supplier + o default do fornecedor)
    e ANTES da gravacao — a classificacao forcada SOBREPOE o default do fornecedor. Textos
    escaneados: assunto + descricao do documento (extraida) + `extra_text` (o corpo do e-mail
    no caminho de corpo). O remetente (`sender_email`) tambem e um sinal: GNRE de @lebianco
    vira ICMS-ST. Write-back nunca ocorre para a OTIMOTEX (sk_supplier=1)."""
    override = resolve_forced_classification(
        ctrl, payload.get("document_type"),
        payload.get("subject"), payload.get("description"), extra_text,
        sender_email=payload.get("sender_email"),
        sk_supplier=payload.get("sk_supplier"),
    )
    if not override:
        return
    cost_center_id, chart_account_id, write_back = override
    payload["cost_center_id"] = cost_center_id
    payload["chart_account_id"] = chart_account_id

    sk_supplier = payload.get("sk_supplier")
    # 🔴 SINAL FRACO NAO ESCREVE NO CADASTRO. Quando o fornecedor foi identificado por um
    # sinal CIRCUNSTANCIAL do e-mail (fallback 1b — o e-mail de quem ENCAMINHOU), a conta
    # ainda recebe a classificacao forcada, mas o cadastro dele NAO e' reescrito: a guia e'
    # do FISCO, nao do encaminhador, e o write-back valeria para todas as contas futuras
    # dele. Antes do 1b esse caso caia na OTIMOTEX, que a linha seguinte ja isentava — a
    # protecao existia por acidente do destino, e esta marca a torna deliberada.
    if _is_weak_supplier_signal(payload):
        log.info(f"    [CLASSIFICACAO-FORCADA] write-back SUPRIMIDO para sk={sk_supplier}: "
                 f"fornecedor veio de sinal fraco "
                 f"({payload.get(SUPPLIER_SIGNAL_KEY)}) — a conta recebe a classificacao, "
                 f"o cadastro nao")
        return
    # Write-back so quando a regra pede E o fornecedor nao e a OTIMOTEX (sk=1). Best-effort.
    if write_back and sk_supplier and sk_supplier != OTIMOTEX_SK_SUPPLIER:
        ctrl.update_supplier_classification(sk_supplier, cost_center_id, chart_account_id)


def apply_contact_writeback(ctrl, payload: dict, extra_text: str | None = None) -> None:
    """Detecta chave PIX / telefone / WhatsApp no texto do e-mail e grava no cadastro
    do fornecedor (write-back, 2 slots). Roda APOS _finalize_supplier (sk_supplier ja
    setado). Best-effort — NUNCA derruba a gravacao da conta. NAO escreve no payload
    (financial_account_control nao tem colunas de contato). Textos escaneados: assunto
    + descricao + corpo (email_body_excerpt/extra_text)."""
    sk_supplier = payload.get("sk_supplier")
    if not sk_supplier or sk_supplier == OTIMOTEX_SK_SUPPLIER:
        return
    try:
        parts = [payload.get("subject"), payload.get("description"),
                 payload.get("email_body_excerpt"), extra_text]
        text = "\n".join(p for p in parts if p)
        if not text.strip():
            return
        # Nunca gravar o CNPJ do PAGADOR (OTIMOTEX) como chave PIX do fornecedor — o
        # bloco do pagador vem no corpo reencaminhado e pode ficar perto de "pix".
        exclude = set()
        pcnpj = re.sub(r"\D", "", str(payload.get("payer_cnpj") or ""))
        if len(pcnpj) == 14:
            exclude.add(pcnpj)
        own = ctrl.company_cnpj() if hasattr(ctrl, "company_cnpj") else None
        if own:
            exclude.add(re.sub(r"\D", "", own))
        contacts = parse_supplier_contacts(text, exclude_pix=exclude)
        if any(contacts.values()):
            ctrl.update_supplier_contact(
                sk_supplier,
                pix=contacts["pix"], phone=contacts["phone"], whatsapp=contacts["whatsapp"],
            )
    except Exception as e:
        log.warning(f"Falha ao detectar/gravar contato do fornecedor {sk_supplier}: {e}")


# Acronimos/frases de GUIA TRIBUTARIA no ASSUNTO -> document_type canonico (lowercase,
# como no CHECK do banco e no enum @sheild/shared). O ASSUNTO e o sinal MAIS confiavel
# do tipo de guia: quem encaminha o pagamento digita o tipo certo ("PAGAMENTO DARE -
# REF. T05S1"), enquanto as guias estaduais sao visualmente quase identicas (DARE x GARE
# x GNRE) e o PDF/Claude troca uma pela outra. Casado por PALAVRA INTEIRA (_has_word),
# sem acento. CONSERVADOR: acronimos que colidem com palavras do portugues ('das' =
# artigo, 'dam', 'dar' = verbo) NAO sao casados pela forma pura — so por frase
# inequivoca ('simples nacional'/'simei', 'dar modelo 1') para nao gerar falso positivo
# em "pagamento DAS contas" nem em "favor dar baixa".
_SUBJECT_TAX_DOC_KEYWORDS: list[tuple[str, list[str]]] = [
    ("darf",       ["darf"]),
    ("gps",        ["gps"]),
    ("das",        ["simples nacional", "simei"]),
    ("gru",        ["gru"]),
    # DAR / DARE — os dois acronimos estaduais num tipo so (migration 133). "dare" e'
    # inequivoco e entra puro; "dar" so por FRASE, com o mesmo tratamento conservador de
    # 'das'. Aqui o risco e' o MAIOR de todas as listas, porque este mapa SOBREPOE a
    # classificacao do PDF (ver o call site em build_financial_payload): um assunto
    # "favor dar baixa" com "dar" solto transformaria um DARF corretamente extraido.
    # 🔴 "documento de arrecadacao estadual" FICOU DE FORA — e' o nome por extenso do
    # DAE em PE/CE, e este par vem ANTES do 'dae'.
    ("dar / dare", ["dare", "dar modelo 1", "dar-1", "dar/aut", "dar avulso"]),
    ("dae",        ["dae"]),
    ("gnre",       ["gnre"]),
    ("gare",       ["gare"]),
    ("ipva",       ["ipva"]),
    ("iptu",       ["iptu"]),
    ("iss",        ["iss"]),
    ("itbi",       ["itbi"]),
    ("dam / duam", ["duam"]),
    ("multa",      ["multa", "penalidade", "auto de infracao"]),
]


def _classify_tax_doc_type_from_subject(subject: str | None) -> str | None:
    """Detecta a GUIA TRIBUTARIA pelo ACRONIMO EXPLICITO no assunto (DARE/GARE/GNRE/
    DARF/...). Precedencia sobre a classificacao do PDF: o remetente declarou o tipo no
    assunto e as guias estaduais confundem o extrator. Casa por PALAVRA INTEIRA, sem
    acento. Retorna o document_type canonico (lowercase) ou None."""
    if not subject:
        return None
    blob = _ns_body(subject)
    for doc_type, terms in _SUBJECT_TAX_DOC_KEYWORDS:
        if any(_has_word(blob, term) for term in terms):
            return doc_type
    return None


def _brl_to_decimal(raw: str | None):
    """Converte valor em formato BR ('8.650,00' ou '8650,00') para float.

    O _to_decimal acima nao trata separador de milhar — necessario aqui pois
    o valor vem direto do texto do e-mail, nao normalizado pelo extract_pdf.
    """
    if not raw:
        return None
    s = re.sub(r"[^\d,.]", "", raw)
    # Vírgula = separador decimal BR. Aceita 1-3 casas: um 3º dígito é digitação com
    # zero a mais (ex.: "1.799,960" → 1799,96) — o round(…,2) normaliza. Sem esse
    # tolerância, "1.799,960" caía no ramo de milhar e virava 1,8 (bug real, id 186).
    if re.search(r",\d{1,3}$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _extract_body_amount(body_text: str) -> "float | None":
    """Determina o valor a pagar a partir do corpo do e-mail.

    Regra (decisao de negocio):
      0. Valor "liquido"/"a pagar" (montante efetivo, apos descontos) tem
         precedencia MAXIMA — Ex.: fatura Rodonaves "Valor liquido R$ 12.985,52".
      1. Valor rotulado como "Total"/"Valor Total" tem precedencia — quando o
         corpo lista parcelas (lado a lado, somando ou subtraindo), o total e o
         valor a pagar. Ex.: "Valor: R$ 297,08 + R$ 6,96 / Total: R$ 304,04".
      2. Sem rotulo de total e com varios valores → soma as parcelas (fallback),
         MAS valores identicos repetidos (mesmo montante sob rotulos diferentes,
         ex.: "Valor da fatura" == "Valor liquido") contam como UM so — nunca
         somar; e R$ 0,00 (ex.: "Decrescimo R$ 0,00") e ignorado.
      3. Um unico valor → o proprio valor.
      4. Sem "R$": valor ROTULADO ("Valor 50,00"/"Total 304,04") — precedencia ao total.
      5. Nenhum valor → None.
    """
    net_match = _BODY_NET_RE.search(body_text)
    if net_match:
        return _brl_to_decimal(net_match.group(1))

    total_match = _BODY_TOTAL_RE.search(body_text)
    if total_match:
        return _brl_to_decimal(total_match.group(1))

    # Ignora R$ 0,00 (linhas "Decrescimo/Desconto R$ 0,00" nao sao valor a pagar).
    valores = [v for v in (_brl_to_decimal(m) for m in _BODY_AMOUNT_RE.findall(body_text))
               if v is not None and v > 0]
    if len(valores) == 1:
        return valores[0]
    if valores:
        # Valores todos IDENTICOS = o mesmo montante repetido (subtotal/liquido/
        # total sob rotulos distintos) → e UM valor, nao parcelas. Somar duplicava
        # (caso Rodonaves). So soma quando ha valores REALMENTE distintos (parcelas).
        if len(set(valores)) == 1:
            return valores[0]
        return round(sum(valores), 2)

    # Fallback sem "R$": numero rotulado por "Valor"/"Total" (precedencia ao total).
    rotulados = _BODY_LABELED_AMT_RE.findall(body_text)  # [(rotulo, numero), ...]
    if rotulados:
        totais = [num for (lbl, num) in rotulados if "total" in lbl.lower()]
        return _brl_to_decimal(totais[0] if totais else rotulados[0][1])
    return None


def _extract_body_installments(body_text: str) -> "list[dict]":
    """Detecta uma TABELA de boletos/parcelas no corpo (documento, parcela, emissão,
    vencimento, valor, dias) e devolve uma lista de parcelas individuais.

    Regra de negócio (NÃO regredir): quando o corpo lista MÚLTIPLOS títulos com
    documentos/parcelas e vencimentos diferentes, NUNCA criar uma conta somada com o
    total — criar UMA conta por boleto. Esta função alimenta esse caminho.

    Retorna [] quando não há tabela aplicável: menos de 2 linhas, OU todas as linhas
    com o MESMO vencimento E a MESMA (documento, parcela) — aí não é um conjunto de
    parcelas distintas e o caminho de valor único/total continua valendo.
    """
    rows: list[dict] = []
    for doc, parcela, emis, venc, valor in _BODY_INSTALLMENTS_RE.findall(body_text or ""):
        amount = _brl_to_decimal(valor)
        if amount is None:
            continue
        rows.append({
            "doc": doc,
            "parcela": parcela,
            "issue_date": _br_date_to_iso(emis),
            "due_date": _br_date_to_iso(venc),
            "amount": amount,
        })
    if len(rows) < 2:
        return []
    distinct_due = {r["due_date"] for r in rows}
    distinct_doc = {(r["doc"], r["parcela"]) for r in rows}
    if len(distinct_due) < 2 and len(distinct_doc) < 2:
        return []
    return rows


def _extract_body_invoice_rows(body_text: str) -> "list[dict]":
    """Linhas de uma TABELA DE FATURAS no corpo (documento, emissão, vencimento,
    valor e a linha digitável DAQUELA linha) — ver _BODY_INVOICE_ROW_RE.

    Complementa `_extract_body_installments` (layout com parcela/dias, OBER) para o
    layout sem parcela (MOVVI): o que identifica a linha é a FORMA (nº do documento
    + duas datas + valor), não um rótulo — que na tabela HTML achatada fica no
    cabeçalho, longe do valor.

    A linha digitável é buscada no SEGMENTO da própria linha (do início dela até o
    início da próxima), nunca no corpo inteiro: numa tabela de N faturas cada linha
    tem o SEU boleto, e herdar o barcode da primeira faria as demais colidirem na
    dedup por código de barras — perdendo títulos em silêncio. A ÚLTIMA linha não tem
    "próxima" que a delimite, então o segmento dela é limitado por
    `_INVOICE_ROW_BARCODE_WINDOW` — sem esse teto ela varreria até o fim do corpo e
    poderia adotar uma linha digitável do rodapé, que não é dela.

    Sem gate: devolve todas as linhas encontradas ([] quando nenhuma). Os
    consumidores decidem (conta única × uma conta por fatura).
    """
    text = body_text or ""
    matches = list(_BODY_INVOICE_ROW_RE.finditer(text))
    rows: list[dict] = []
    for i, m in enumerate(matches):
        doc = (m.group(1) or "").strip(" .,;")
        amount = _brl_to_decimal(m.group(4))
        # Documento inválido (data pura) ou valor ilegível → a linha não é utilizável.
        if not doc or _BODY_DATE_ONLY_RE.fullmatch(doc) or amount is None:
            continue
        end = (matches[i + 1].start() if i + 1 < len(matches)
               else min(len(text), m.start() + _INVOICE_ROW_BARCODE_WINDOW))
        rows.append({
            "doc":        doc,
            "issue_date": _br_date_to_iso(m.group(2)),
            "due_date":   _br_date_to_iso(m.group(3)),
            "amount":     amount,
            "barcode":    _normalize_body_barcode_allow_misread(
                _extract_body_linha_digitavel(text[m.start():end])),
        })
    return rows


def _row_field(row: "dict | None", field: str):
    """Campo da linha da tabela de faturas, tolerante à linha ausente — permite usar
    a tabela como fallback em cadeia (`rotulado or tabela or padrão`) sem um `if`
    extra por campo no `extract_from_email_body`."""
    return row.get(field) if row else None


def _extract_body_invoice_row(body_text: str) -> "dict | None":
    """Primeira linha da tabela de faturas do corpo (ou None). Usada para preencher
    as LACUNAS do payload de conta única — nº do documento, emissão e vencimento que
    os regex ancorados em rótulo não alcançam na tabela achatada."""
    rows = _extract_body_invoice_rows(body_text)
    return rows[0] if rows else None


def _extract_body_invoice_table(body_text: str) -> "list[dict]":
    """Tabela de faturas com MÚLTIPLOS títulos distintos → uma conta por fatura.

    Mesmo gate (e mesma regra de negócio — NUNCA somar) de
    `_extract_body_installments`: [] com menos de 2 linhas ou quando todas têm o
    MESMO vencimento E o MESMO documento — aí o caminho de conta única vale.
    """
    rows = _extract_body_invoice_rows(body_text)
    if len(rows) < 2:
        return []
    if len({r["due_date"] for r in rows}) < 2 and len({r["doc"] for r in rows}) < 2:
        return []
    return rows


def _br_date_to_iso(raw: str | None) -> str | None:
    """Converte data 'dd/mm/aaaa' ou 'dd/mm/aa' do corpo do e-mail para 'aaaa-mm-dd'."""
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _iso_date_to_ddmmyy(iso_date: str | None) -> str | None:
    """Converte data 'aaaa-mm-dd' para 'ddmmaa' — usado para compor invoice_number."""
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%d%m%y")
    except ValueError:
        return None


def _format_brl(value) -> str:
    """Formata um valor numerico como moeda BR: 10999.99 -> 'R$ 10.999,99'."""
    # f-string usa formato US ('10,999.99'); troca os separadores para o padrao BR.
    s = f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _synthetic_invoice_number(document_type, amount, iso_date, payment_method="") -> str | None:
    """N documento sintetico quando o documento nao traz numero proprio.

    Regra de negocio:
    - Pagamento PIX sem tipo claro (payment_method='pix' E document_type='outro'):
      forma de pagamento + '_' + valor BR. Ex.: 'pix_R$ 10.999,99'.
    - Demais tipos: '{tipo}_{ddmmaa(vencimento|emissao)}' quando ha data.
    Retorna None quando nao ha base para gerar (sem regra PIX e sem data).
    """
    pm = (payment_method or "").lower()
    if pm == "pix" and (document_type or "").lower() == "outro" and amount is not None:
        return f"{pm}_{_format_brl(amount)}"
    ddmmyy = _iso_date_to_ddmmyy(iso_date)
    if ddmmyy:
        return f"{document_type or 'outro'}_{ddmmyy}"
    return None


# Numero de documento SINTETICO (gerado por _synthetic_invoice_number quando o
# documento nao traz numero proprio): 'PIX_R$ ...' ou '{tipo}_{ddmmaa}'. NAO e um
# identificador confiavel — dois boletos distintos do mesmo fornecedor, mesmo valor
# e mesmo vencimento (ou vencimento DEFAULTADO p/ a data da extracao) colidem nesse
# numero. Por isso a dedup de conteudo (impressao 2) o IGNORA: o codigo de barras
# (impressao 1) distingue os documentos.
_SYNTHETIC_INVOICE_RE = re.compile(r"_\d{6}(?:\(\d+\))?$")


def _is_synthetic_invoice_number(invoice: str | None) -> bool:
    s = (invoice or "").strip()
    if s.upper().startswith("PIX_"):
        return True
    return bool(_SYNTHETIC_INVOICE_RE.search(s))


def _same_title(novo_invoice, cand_invoice) -> bool:
    """Os dois documentos sao o MESMO TITULO, do ponto de vista do Nº DE DOCUMENTO?

    Usado como guarda da impressao 1b da dedup (fornecedor + nosso numero). Devolve
    True — "pode deduplicar" — em dois casos:
      - os numeros PROPRIOS coincidem (comparados so pelos DIGITOS: '001/00561066674-1'
        e '00561066674' sao o mesmo titulo escrito de formas diferentes); ou
      - um dos lados NAO tem numero proprio (ausente ou SINTETICO tipo 'boleto_150826'),
        e entao ele nao contradiz nada — o nosso numero segue decidindo sozinho, como
        antes desta guarda.

    So devolve False quando AMBOS tem numero proprio e eles DIFEREM: dois titulos
    distintos do mesmo fornecedor. Conservador de proposito — na duvida, mantem o
    comportamento anterior, porque deduplicar a mais PERDE um pagavel em silencio."""
    a = re.sub(r"\D", "", str(novo_invoice or ""))
    b = re.sub(r"\D", "", str(cand_invoice or ""))
    if _is_synthetic_invoice_number(str(novo_invoice or "")) or not a:
        return True
    if _is_synthetic_invoice_number(str(cand_invoice or "")) or not b:
        return True
    # Comparacao por CONTINENCIA, nao por igualdade: o mesmo titulo aparece com prefixo de
    # carteira e/ou DV conforme o campo ('001/00561066674-1' -> '001005610666741' contem
    # '00561066674'). Exige >= 6 digitos no lado menor para que numeros curtos nao casem
    # por acaso — abaixo disso a continencia nao significa nada.
    menor, maior = (a, b) if len(a) <= len(b) else (b, a)
    if len(menor) < 6:
        return a == b
    return menor in maior


def _is_real_nosso_numero(nn: str | None) -> bool:
    """True se `nn` parece um NOSSO NÚMERO real do banco (>= 8 dígitos, não só zeros).
    O nosso número identifica o TÍTULO no banco e é ESTÁVEL entre reemissões (2ª via /
    aviso de vencimento mantêm o mesmo), então é chave de dedup imune a mudança de
    valor (juros) e vencimento. Guarda contra vazio/curto/lixo para não deduplicar
    títulos distintos por engano."""
    d = re.sub(r"\D", "", nn or "")
    return len(d) >= 8 and d.strip("0") != ""


# ── Resolvedores de campo do corpo ─────────────────────────────────────────────
# Cada campo do corpo e uma CADEIA DE PRECEDENCIA (rotulado -> tabela -> padrao).
# Mantidas inline, cada regra nova virava mais um ramo dentro de extract_from_email_body,
# que chegou a complexidade cognitiva 64. Aqui cada cadeia e uma funcao PURA, testavel
# isoladamente; a funcao principal so orquestra. Comportamento inalterado — a ordem de
# precedencia de cada cadeia e exatamente a que estava inline.

def _resolve_body_supplier_identity(body_text: str) -> "tuple[str | None, str | None, str | None]":
    """(nome, CNPJ, CPF) do fornecedor extraidos do corpo por ROTULO.

    Nome: rotulo na MESMA linha (_BODY_NAME_RE) e, sem ele, o bloco "Dados do emissor"
    com o valor na linha seguinte (_BODY_ISSUER_RE) — o rotulo na mesma linha mantem a
    precedencia. CNPJ/CPF so valem com a quantidade exata de digitos."""
    label_match = (_BODY_NAME_RE.search(body_text)
                   or _BODY_ISSUER_RE.search(body_text))
    supplier_name = label_match.group(1).strip() if label_match else None

    cnpj_match    = _BODY_CNPJ_RE.search(body_text)
    supplier_cnpj = re.sub(r"\D", "", cnpj_match.group(0)) if cnpj_match else None
    if supplier_cnpj and len(supplier_cnpj) != 14:
        supplier_cnpj = None

    cpf_match     = _BODY_CPF_RE.search(body_text)
    supplier_cpf  = re.sub(r"\D", "", cpf_match.group(1)) if cpf_match else None
    if supplier_cpf and len(supplier_cpf) != 11:
        supplier_cpf = None

    return supplier_name, supplier_cnpj, supplier_cpf


def _resolve_body_barcode(body_text: str) -> "str | None":
    """Linha digitavel do corpo. Normalizacao canonica: 44/48 mantidos, 47 -> 44.

    PRECEDENCIA — a FORMA vence o ROTULO: `_extract_body_linha_digitavel` valida a
    estrutura dos 5 campos FEBRABAN (5-5 / 5-6 / 5-6 / 1 / 14), enquanto
    `_BODY_BARCODE_RE` aceita quaisquer `[\\d.\\s]{47,60}` depois do rotulo — e `\\s`
    inclui QUEBRA DE LINHA, entao ele pode COLAR digitos de linhas diferentes num codigo
    inventado. O rotulo fica como fallback porque cobre o que a forma estruturada nao
    cobre: a linha digitavel de ARRECADACAO (48 digitos, outro layout).

    Por ser CAPTURA FROUXA, o fallback usa a variante que VALIDA o DV; a extracao
    estruturada nao — ver `extract_pdf.normalize_barcode` para o porque da assimetria."""
    structured = _normalize_body_barcode_allow_misread(
        _extract_body_linha_digitavel(body_text))
    if structured:
        return structured
    m = _BODY_BARCODE_RE.search(body_text)
    return _normalize_body_barcode(m.group(1)) if m else None


# Fontes do Nº DO DOCUMENTO no corpo, em ORDEM DE PRECEDENCIA. O 2o item do par formata
# o valor capturado (o bill da SIEG vira 'sieg_<bill>'). A justificativa de cada fonte
# esta na definicao do respectivo regex.
_BODY_INVOICE_SOURCES = (
    (_BODY_INVOICE_RE,    None),        # NF / NFe / nota fiscal / "fatura nº" + digitos
    (_BODY_DOCNUM_RE,     None),        # rotulo "Numero do documento" (alfanumerico)
    (_BODY_INVOICE_NO_RE, None),        # "Fatura No: NNNN" (sem o sinal º/°)
    (_BODY_CHARGE_NUM_RE, None),        # "Cobranca Nº NNNN" (plataforma de assinatura)
    (_BODY_SIEG_BILL_RE,  "sieg_{}"),   # link app.sieg.com/faturas?bill=NNN
)


def _resolve_body_invoice_number(body_text: str, table_row: "dict | None") -> "str | None":
    """Nº do documento do corpo, na ordem de _BODY_INVOICE_SOURCES; sem nenhuma fonte
    rotulada, cai na linha da tabela de faturas. None quando nada identifica o titulo —
    ai o chamador gera o nº SINTETICO."""
    for regex, template in _BODY_INVOICE_SOURCES:
        m = regex.search(body_text)
        if not m:
            continue
        value = m.group(1).strip()
        if value:
            return template.format(value) if template else value
    return _row_field(table_row, "doc")


def _first_body_date(regex: "re.Pattern", body_text: str) -> "str | None":
    """Primeira data dd/mm/aa(aa) casada por `regex` no corpo, convertida para ISO."""
    m = regex.search(body_text)
    return _br_date_to_iso(m.group(1)) if m else None


def _resolve_body_dates(body_text: str, table_row: "dict | None",
                        received_at: str) -> "tuple[str | None, str]":
    """(emissao, vencimento) do corpo.

    Emissao: rotulo 'Emissao DD/MM/AA' -> linha da tabela -> data de envio do e-mail.
    Vencimento: rotulo 'Vencimento' -> 'DATA PARA PAGAMENTO' (nota interna) -> linha da
    tabela -> emissao -> hoje. A linha da tabela vem ANTES do fallback pela emissao, que
    gravava vencimento ERRADO quando o rotulo esta so no cabecalho (conta 693: 25/07, a
    data do e-mail, no lugar de 01/08) — e ainda fazia a marcacao de vencido / baixa
    automatica agir sobre a data errada."""
    issue_date = (_first_body_date(_BODY_ISSUE_RE, body_text)
                  or _row_field(table_row, "issue_date")
                  or ((received_at or "")[:10] or None))
    due_date = (_first_body_date(_BODY_DUE_RE, body_text)
                or _first_body_date(_BODY_PAYDATE_RE, body_text)
                or _row_field(table_row, "due_date")
                or issue_date
                # Regra de negocio: sem nenhuma data, usa a data da extracao (hoje).
                or datetime.now().strftime("%Y-%m-%d"))
    return issue_date, due_date


def _resolve_body_doc_and_payment(body_text: str, subject: "str | None",
                                  supplier_name: "str | None", barcode: "str | None",
                                  has_pix: bool) -> "tuple[str, str]":
    """(document_type, payment_method) do corpo, na ordem de precedencia:

    concessionaria (agua/luz/telefone-internet, PRECEDENCIA MAXIMA — a frase no assunto/
    corpo define o tipo mesmo parecendo fatura/boleto/PIX) -> guia tributaria pelo
    acronimo do assunto -> honorarios (vence PIX) -> PIX -> keyword do corpo. Depois:
    BOLETO por codigo de barras valido vence TODOS os ramos acima (paga-se como boleto);
    a forma DECLARADA no corpo preenche o que sobrou em 'outro'; e transporte/cartorio/
    seguradora re-rotulam o tipo (mesmas regras do caminho de PDF)."""
    utility = (_classify_utility_doc_type(subject, body_text)
               or _classify_utility_by_supplier(supplier_name))
    tax_subject = _classify_tax_doc_type_from_subject(subject)
    classified = _classify_body_doc_type(body_text)
    if utility:
        document_type, payment_method = utility, ("pix" if has_pix else "outro")
    elif tax_subject:
        document_type, payment_method = tax_subject, ("pix" if has_pix else "outro")
    elif classified == "honorários":
        document_type, payment_method = "honorários", "pix"
    else:
        # PIX e FORMA DE PAGAMENTO, nao tipo de documento: o tipo fica o classificado
        # ('outro' quando nada casou) e o PIX detectado reflete so no payment_method.
        document_type = classified
        payment_method = "pix" if has_pix else "outro"

    # Chave NF-e/CT-e (44 sem moeda '9') nao casa -> segue pix.
    if barcode and _is_boleto_barcode(barcode):
        payment_method = "boleto"
        if (document_type or "outro").lower() in ("pix", "outro", ""):
            document_type = "boleto"

    # Caso de origem: id 442 "PAGAMENTO EM DINHEIRO" gravava 'outro' -> agora 'dinheiro'.
    if payment_method == "outro":
        payment_method = _classify_body_payment_method(body_text, subject) or "outro"

    document_type = _apply_transport_boleto_doc_type(
        document_type, subject, supplier_name, barcode)
    document_type = _apply_cartorio_doc_type(document_type, subject, supplier_name)
    document_type = _apply_seguro_doc_type(document_type, subject)
    return document_type, payment_method


def extract_from_email_body(body_text: str, received_at: str, message_id: str,
                            sender_email: str | None = None,
                            subject: str | None = None) -> dict | None:
    """Monta um payload de financial_account_control a partir do corpo do e-mail.

    Retorna None (sem log de erro) quando nenhum sinal financeiro e encontrado
    (sem valor R$ nem numero de documento). O chamador deve ignorar silenciosamente.

    Regras de extracao:
      - amount      : valor rotulado 'Total'/'Valor Total' (precedencia); sem
                       rotulo, soma as parcelas; valor unico → ele mesmo (_extract_body_amount)
      - invoice_number: 'NF XXXX', 'NFe XXXX', 'nota fiscal XXXX', 'fatura N° XXXX';
                       fallback 'Fatura No: XXXX' (sem sinal º/°)
      - issue_date  : 'emissao DD/MM/AA(AA)'; fallback = data de envio do e-mail
      - due_date    : 'vencimento/vencto DD/MM/AA(AA)'; fallback = issue_date
      - supplier_name: 'Fornecedor:'/'Favorecido:'/'Nome:'/... no corpo; sem rotulo,
                       tenta sinais/assinatura, so entao sender_email — o remetente
                       ORIGINAL de um bloco encaminhado no corpo ('De:'/'From:') e
                       tentado DEPOIS, em _finalize_supplier (fallback 3, so quando o
                       assunto nao tem ancora propria), nao aqui
      - supplier_cnpj/cpf: extraidos do corpo (CNPJ por padrao; CPF so rotulado)
      - barcode     : linha digitavel / codigo de barras (boleto 47 / arrecadacao 48)
      - payment_method: 'pix' no corpo → 'pix'; boleto (barcode) → 'boleto'; senao a
                       FORMA DECLARADA no corpo/assunto (dinheiro/depósito/cheque/cartão/
                       crédito/débito/ted/transferência/duplicata — _classify_body_payment_method);
                       nada casando → 'outro'
      - document_type: 'PIX' quando PIX; senao keywords de tributo; fallback 'outro'
      - invoice_number fallback: PIX -> 'PIX_' + valor BR ('PIX_R$ 10.999,99');
        demais tipos -> '{document_type}_{ddmmyy}' quando nao encontrado

    Guarda: corpos de comprovante de pagamento ja feito / 'pix recebido' (sem
    pedido de pagamento) retornam None — nao sao conta a pagar.
    """
    if not body_text:
        return None

    # Guarda: comprovante de pagamento ja feito / "pix recebido" que NAO pede
    # pagamento — nao e conta a pagar (afasta phishing tipo "Comprovante de Pix
    # Recebido"). Skip silencioso.
    if (_BODY_RECEIPT_RE.search(body_text)
            and not _BODY_PAYMENT_REQUEST_RE.search(body_text)):
        return None

    supplier_name, supplier_cnpj, supplier_cpf = _resolve_body_supplier_identity(body_text)
    barcode = _resolve_body_barcode(body_text)
    amount  = _extract_body_amount(body_text)

    # Linha da TABELA DE FATURAS (documento + emissao + vencimento + valor juntos).
    # Fonte de PREENCHIMENTO DE LACUNA: so entra onde os regex ancorados em rotulo
    # nao acharam nada — nunca sobrescreve um valor explicitamente rotulado.
    table_row      = _extract_body_invoice_row(body_text)
    invoice_number = _resolve_body_invoice_number(body_text, table_row)
    amount         = amount or _row_field(table_row, "amount")

    # Sem sinal financeiro (valor ou numero de documento) — ignorar silenciosamente
    if not amount and not invoice_number:
        return None

    # Fornecedor sem rotulo nem identificador: tenta sinais (destinatario do pix
    # "p/ <Nome>" / assinatura "Prof. <Nome>") e o mapa por remetente
    # (ex.: correios.com.br -> "Correios"). Havendo CNPJ/CPF, deixa o nome vazio.
    #
    # REGRA (robusta — nao regredir): sem NENHUM nome confiavel, o nome fica VAZIO de
    # proposito. O e-mail do remetente (sender_email) NAO vira nome aqui — ele e passado
    # a RPC resolve_supplier_id como chave propria (busca por email/email2/email3/email4).
    # Se o e-mail JA constar em um fornecedor, casa com ele; SO quando o e-mail NAO for
    # encontrado e que o auto-insert da RPC usa o e-mail como nome (ULTIMO RECURSO). Assim
    # o e-mail nunca vira nome ANTES da busca, evitando criar fornecedor DUPLICADO quando o
    # e-mail ja pertence a um cadastrado (ex.: financeiro@... no email2 de um fornecedor).
    # E-mail de dominio interno nunca identifica nem cria fornecedor (migration 046).
    #
    # NAO tentar aqui o remetente ORIGINAL de um bloco encaminhado no corpo
    # (_supplier_from_forwarded_sender) — essa e uma fonte UNICA, centralizada em
    # _finalize_supplier (fallback 3, chamado logo em seguida por try_extract_from_body
    # para TODO payload desta funcao), que a testa SO DEPOIS de esgotar o assunto
    # ancorado em sigla (fallback 2). Duplicar a chamada aqui a executaria ANTES do
    # assunto ter a chance de vencer — regrediria casos ja corretos (ex.: id 401,
    # "MOVVI LOGISTICA LTDA" no assunto) sempre que o corpo tivesse uma linha "De:"
    # de OUTRO correspondente da cadeia (achado em code review, 2026-07-23).
    if not supplier_name and not supplier_cnpj and not supplier_cpf:
        supplier_name = _supplier_from_signals(body_text) or _supplier_from_sender(sender_email)

    has_pix = bool(_BODY_PIX_RE.search(body_text))

    issue_date, due_date = _resolve_body_dates(body_text, table_row, received_at)
    document_type, payment_method = _resolve_body_doc_and_payment(
        body_text, subject, supplier_name, barcode, has_pix)

    # Numero de documento: valor encontrado no corpo ou sintetico (pagamento PIX +
    # tipo 'outro': 'pix_' + valor; demais: tipo+ddmmyy do vencimento/emissao).
    if not invoice_number:
        invoice_number = _synthetic_invoice_number(
            document_type, amount, due_date or issue_date, payment_method) or invoice_number

    payload = {f: None for f in FINANCIAL_FIELDS}
    payload.update({
        "document_type":     document_type,
        "extraction_source": "email_body",
        "supplier_name":     supplier_name,
        "supplier_cnpj":     supplier_cnpj,
        "supplier_cpf":      supplier_cpf,
        "amount":            amount,
        "currency":          "BRL",
        "payment_method":    payment_method,
        "barcode":           barcode,
        "due_date":          due_date,
        "issue_date":        issue_date,
        "invoice_number":    invoice_number,
        # Sempre 'pendente' — a baixa/atualizacao do status e feita pelo usuario.
        "status":            "pendente",
        "extracted_at":      datetime.now(timezone.utc).isoformat(),
    })
    for vf in FINANCIAL_VALUE_FIELDS:
        payload[vf] = 0
    payload["amount_charged"]     = amount or 0
    payload["gmail_message_id"]   = message_id
    payload["email_body_excerpt"] = body_text.strip()
    return payload


# ---------------------------------------------------------------------------
# Download de anexos PDF
# ---------------------------------------------------------------------------
def _is_within_inbox(dest_path: Path) -> bool:
    """True se dest_path resolvido fica DENTRO de PDF_INBOX — invariante explícito contra
    path traversal (safe_filename já remove `..`/separadores; isto é defesa em profundidade)."""
    try:
        inbox = PDF_INBOX.resolve()
        resolved = dest_path.resolve()
        return resolved == inbox or inbox in resolved.parents
    except OSError:
        return False


# Anexos de IMAGEM (foto/scan de recibo, comprovante, "Valor do porte" dos Correios)
# também são lidos — via Claude Vision no extract_pdf. Mapeia extensão→media_type.
_IMAGE_ATTACHMENT_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_IMAGE_ATTACHMENT_CTS  = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Tamanho mínimo (bytes) para uma imagem INLINE ser considerada um documento e não
# um logo/assinatura/ícone embutido. Recibos/comprovantes colados no corpo passam
# bem disso (centenas de KB); logos típicos ficam abaixo. Named constant — sem magia.
_IMAGE_INLINE_MIN_BYTES = 50_000


# Anexos .docx (Word moderno): ZIP+XML, lidos por `docx_content` no extract_pdf. Caso de origem —
# e-mail 1516, boleto judicial anexado como Word e descartado em silencio por esta allowlist.
_DOCX_ATTACHMENT_EXTS = (".docx",)
_DOCX_ATTACHMENT_CTS  = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _attachment_image_ext(content_type: str, filename_lower: str) -> str:
    """Extensão de imagem a usar no arquivo salvo (do nome do anexo ou do MIME)."""
    for ext in _IMAGE_ATTACHMENT_EXTS:
        if filename_lower.endswith(ext):
            return ext
    return {"image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp"}.get(content_type, ".img")


def attachment_kind(content_type: str, filename_lower: str,
                    content_disposition: str) -> "str | None":
    """'pdf' | 'image' | 'docx' | None — REGRA ÚNICA de seleção de anexo-documento.

    Fonte única de `save_attachments`, do `_document_parts` da varredura histórica e do
    `_describe_candidates` do reprocess_message. As três eram cópias que só podiam concordar por
    disciplina — e a guarda cross-layer de `test_varredura_historica` existe justamente porque
    divergir ali significa subir ao bucket o que o pipeline nunca consideraria documento, ou
    perder o documento que a varredura existe para recuperar.

    Por que cada família tem um critério diferente:
      - PDF   — MIME, extensão OU nome com "pdf" em anexo explícito (o mais permissivo: quase todo
                PDF que chega aqui é cobrança).
      - IMAGE — exige `attachment` no Content-Disposition, senão logo/assinatura inline entrariam.
      - DOCX  — NÃO exige `attachment`: Outlook e webmails mandam .docx como
                `application/octet-stream`, às vezes sem disposition. E .docx nunca é inline — a
                razão de a regra de imagem exigir disposition simplesmente não existe aqui.
    """
    cd = (content_disposition or "").lower()
    ct = (content_type or "").lower()
    fl = (filename_lower or "").lower()
    # DOCX vem ANTES do PDF de propósito: `is_pdf` casa "pdf" em qualquer lugar do nome, então
    # um anexo chamado `boleto_pdf.docx` seria salvo com extensão .pdf e quebraria no pdfplumber.
    if fl.endswith(_DOCX_ATTACHMENT_EXTS) or ct in _DOCX_ATTACHMENT_CTS:
        return "docx"
    if ct == "application/pdf" or fl.endswith(".pdf") or ("attachment" in cd and "pdf" in fl):
        return "pdf"
    if "attachment" in cd and (ct in _IMAGE_ATTACHMENT_CTS
                               or fl.endswith(_IMAGE_ATTACHMENT_EXTS)):
        return "image"
    return None


def attachment_ext(kind: str, content_type: str, filename_lower: str) -> str:
    """Extensão IMPOSTA pelo pipeline ao arquivo salvo — nunca copiada do nome do anexo."""
    if kind == "pdf":
        return ".pdf"
    if kind == "docx":
        return _DOCX_ATTACHMENT_EXTS[0]
    return _attachment_image_ext(content_type, filename_lower)


def _unique_inbox_path(base_stem: str, ext: str) -> Path:
    """Caminho único em PDF_INBOX para base_stem+ext, somando sufixo _N se já existir."""
    dest = PDF_INBOX / f"{base_stem}{ext}"
    counter = 1
    while dest.exists():
        dest = PDF_INBOX / f"{base_stem}_{counter}{ext}"
        counter += 1
    return dest


def save_attachments(msg, sender_email: str, subject: str,
                     received_at: str) -> list:
    saved = []
    date_tag    = received_at[:10].replace("-", "")
    sender_tag  = safe_filename(sender_email.split("@")[0], 20)
    subject_tag = safe_filename(subject, 30)

    for part in msg.walk():
        cd    = str(part.get("Content-Disposition", ""))
        ct    = part.get_content_type()
        fname = decode_str(part.get_filename() or "")
        fl    = fname.lower()
        kind  = attachment_kind(ct, fl, cd)
        if kind is None:
            # 🔴 O DESCARTE PRECISA DEIXAR RASTRO. Este `continue` era MUDO, e foi assim que o
            # boleto .docx do e-mail 1516 sumiu: o anexo era jogado fora sem log, o e-mail virava
            # "sem anexo" e terminava em 'falha' culpando o corpo. O banco não registra anexo
            # rejeitado (`attachment_names` fica NULL), então esta linha é a ÚNICA fonte possível
            # de "que formatos estamos perdendo" — vale para .doc/.odt/.xlsx/.msg, que seguem
            # fora do escopo. Só loga anexo NOMEADO: parte sem filename é corpo/multipart.
            if fname:
                log.info(f"    Anexo ignorado (tipo não suportado): {fname} | {ct}")
            continue

        ext       = attachment_ext(kind, ct, fl)
        orig      = safe_filename(Path(fname).stem, 20) if fname else "anexo"
        dest_path = _unique_inbox_path(f"{sender_tag}_{subject_tag}_{date_tag}_{orig}", ext)

        payload = part.get_payload(decode=True)
        if payload:
            if not _is_within_inbox(dest_path):
                log.warning(f"    Anexo ignorado (caminho fora de PDF_INBOX): {dest_path.name}")
                continue
            dest_path.write_bytes(payload)
            saved.append(dest_path)
            log.info(f"    Anexo salvo: {dest_path.name}")
    return saved


def save_inline_images(msg, sender_email: str, subject: str, received_at: str) -> list:
    """Fallback: salva a MAIOR imagem INLINE (>= _IMAGE_INLINE_MIN_BYTES) do corpo
    para leitura via Claude Vision. Usado SÓ quando não houve anexo (PDF/imagem) nem
    PDF por link — imagem inline (Content-ID, sem 'attachment') costuma ser o próprio
    documento colado no corpo (recibo/comprovante). Pega só a MAIOR (o documento é a
    imagem mais proeminente) para evitar logos/2ª imagem e limitar chamadas ao Vision;
    loga quantas imagens inline menores foram ignoradas.
    """
    date_tag    = received_at[:10].replace("-", "")
    sender_tag  = safe_filename(sender_email.split("@")[0], 20)
    subject_tag = safe_filename(subject, 30)

    candidates = []  # (tamanho, payload, filename, content_type)
    for part in msg.walk():
        ct = part.get_content_type()
        if not ct.startswith("image/"):
            continue
        cd = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in cd:
            continue  # anexo explícito já tratado por save_attachments
        payload = part.get_payload(decode=True)
        if not payload or len(payload) < _IMAGE_INLINE_MIN_BYTES:
            continue
        candidates.append((len(payload), payload, decode_str(part.get_filename() or ""), ct))

    if not candidates:
        return []
    candidates.sort(key=lambda c: c[0], reverse=True)  # maior primeiro
    size, payload, fname, ct = candidates[0]
    skipped = len(candidates) - 1

    ext       = _attachment_image_ext(ct, fname.lower())
    orig      = safe_filename(Path(fname).stem, 20) if fname else "imagem"
    dest_path = _unique_inbox_path(f"{sender_tag}_{subject_tag}_{date_tag}_{orig}", ext)
    if not _is_within_inbox(dest_path):
        log.warning(f"    Imagem inline ignorada (caminho fora de PDF_INBOX): {dest_path.name}")
        return []
    dest_path.write_bytes(payload)
    extra = f" — {skipped} imagem(ns) inline menor(es) ignorada(s)" if skipped else ""
    log.info(f"    Imagem inline salva (maior, {size} bytes): {dest_path.name}{extra}")
    return [dest_path]


# ---------------------------------------------------------------------------
# Download de PDFs a partir de links no corpo do e-mail
# ---------------------------------------------------------------------------
# Texto âncora que sugere um documento de cobrança
_LINK_TEXT_RE = re.compile(
    r"boleto|fatura|segunda\s*via|download|baixar|pagar|pagamento|"
    r"clique\s*aqui|acesse|emitir|emiss[aã]o|vencimento|cobran[cç]a|slip",
    re.IGNORECASE,
)
# Segmento de URL que sugere boleto/PDF. Inclui 'protocolo' para reconhecer
# portais de fatura via link (ex.: BRASPRESS /protocoloweb?protocolo=...), em que
# a URL nao traz 'boleto'/'fatura' no caminho.
_LINK_URL_RE = re.compile(
    r"boleto|fatura|invoice|bill|download|pdf|pagamento|documento|cobranca|protocolo",
    re.IGNORECASE,
)
# Portal BRASPRESS: o link do e-mail (/protocoloweb?protocolo=CHAVE) abre uma pagina
# cujo botao "Download" chama faturaPDF(chave), que baixa de
# /fatura/download?protocolo=CHAVE&protocoloWeb=true (exige cookie de sessao).
_BRASPRESS_PROTO_RE = re.compile(r"protocolo=([0-9A-Za-z]+)", re.IGNORECASE)

def _braspress_download_url(page_url: str) -> "str | None":
    """Se page_url for um link de fatura por protocolo BRASPRESS, retorna a URL
    direta de download do PDF; caso contrario None. Funcao pura (testavel)."""
    low = page_url.lower()
    if "braspress" not in low or "protocolo" not in low:
        return None
    m = _BRASPRESS_PROTO_RE.search(page_url)
    if not m:
        return None
    return ("https://www.braspress.com.br/fatura/download"
            f"?protocolo={m.group(1)}&protocoloWeb=true")

# Wrappers de redirecionamento/rastreamento de cliques usados em phishing — a
# Locaweb marca mensagens com esses links como "potencialmente suspeitas".
# Nao seguir (poderiam baixar malware no lugar do boleto). Ex.: redirect do Bing
# (bing.com/ck/a?...&u=a1<base64 do destino>), SafeLinks, Proofpoint URL Defense.
_SUSPICIOUS_LINK_RE = re.compile(
    r"(?i)("
    r"bing\.com/ck/|"                    # Bing click redirect
    r"/ck/a\?|"                          # padrao /ck/a? de redirect ofuscado
    r"safelinks\.protection\.outlook|"   # Microsoft SafeLinks
    r"urldefense\.(?:com|proofpoint)|"   # Proofpoint URL Defense
    r"[?&]u=a1aHR0c"                     # destino em base64 ('aHR0c' = 'http')
    r")"
)

def _is_suspicious_link(url: str) -> bool:
    """True se a URL for um redirect/rastreador ofuscado que a Locaweb classifica
    como suspeito — esses links nao sao seguidos para download. Funcao pura."""
    return bool(_SUSPICIOUS_LINK_RE.search(url or ""))

# Aviso da Locaweb para link suspeito ("Tem certeza que deseja acessar este link?"
# / "identificada como potencialmente suspeita"). NOTA: normalmente esse aviso e um
# modal do webmail exibido APOS o clique — nao costuma estar no corpo bruto (IMAP).
# A defesa primaria e o padrao da URL (_is_suspicious_link); esta verificacao de
# corpo e uma rede secundaria (ex.: aviso citado/encaminhado dentro do corpo).
_SUSPICIOUS_BODY_RE = re.compile(
    r"(?i)tem\s+certeza\s+que\s+deseja\s+acessar\s+este\s+link"
    r"|identificada\s+como\s+potencialmente\s+suspeita"
)

def _body_has_suspicious_warning(text: str, html: str) -> bool:
    """True se o corpo trouxer o aviso de link suspeito da Locaweb. Funcao pura."""
    return bool(_SUSPICIOUS_BODY_RE.search((text or "") + " " + (html or "")))

_LINK_HREF_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']{10,})["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_LINK_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]{15,}")

# SSW (sistema de transportadoras — sswsistemas.com.br). O e-mail "Sua fatura Nº... está
# disponível" traz VÁRIOS links ssw.inf.br/cgi-local/ssw1188?id=<hex>, e o 1º BYTE do `id`
# (hex→ASCII) indica o tipo do documento: 'F' = Fatura (traz o BOLETO no rodapé) · 'D'/'E'/'X'
# = DACTE/CT-e (documento FISCAL, sem boleto). A âncora da fatura é genérica ("AQUI"/número) e
# não casa as heurísticas de boleto/fatura/download; já a do DACTE é "Download do arquivo"
# (casa 'download'). Sem tratamento, extract_pdf_links baixava o DACTE (sem linha digitável) e a
# conta caía em 'ignorado' (regra CT-e/transporte sem boleto). Preferimos a FATURA e descartamos
# os DACTE. Ver "Boleto por link (sem anexo)" no CLAUDE.md.
_SSW_LINK_RE = re.compile(r"ssw\.inf\.br/cgi-local/ssw1188\?id=([0-9a-fA-F]{2,})", re.IGNORECASE)


def _ssw_doc_kind(url: str) -> "str | None":
    """Classifica um link SSW ssw1188?id=<hex> pelo 1º byte decodificado do id:
    'fatura' (id começa com 'F' — traz o boleto), 'dacte' ('D'/'E'/'X' — CT-e/DACTE fiscal,
    sem boleto). None se não for link SSW ou o tipo for desconhecido (deixa a heurística
    normal decidir). Função pura."""
    m = _SSW_LINK_RE.search(url or "")
    if not m:
        return None
    try:
        first = bytes.fromhex(m.group(1)[:2]).decode("ascii", "ignore").upper()
    except ValueError:
        return None
    if first == "F":
        return "fatura"
    if first in ("D", "E", "X"):
        return "dacte"
    return None


def _is_ssw_sender(sender_email: "str | None") -> bool:
    """True para o remetente do sistema de faturas SSW (transportadoras)."""
    return "sswsistemas.com.br" in (sender_email or "").lower()


# Nome do CEDENTE na intro da fatura SSW: "...serviços de transporte realizados por <NOME>."
_SSW_CEDENTE_NAME_RE = re.compile(r"realizad[oa]s?\s+por\s+([^.\r\n]+)", re.IGNORECASE)


def _ssw_cedente_from_body(sender_email: "str | None", body_text: "str | None",
                           own_cnpj: "str | None") -> "tuple[str | None, str | None]":
    """CEDENTE (transportadora que EMITE a fatura e RECEBE o pagamento) de uma fatura
    SSW de transporte, extraído do CORPO do e-mail — sinal DETERMINISTICO que blinda
    contra o erro do extrator de pegar o EMITENTE do CT-e agregado (a transportadora
    SUBCONTRATADA) como fornecedor.

    O corpo da fatura SSW nomeia o cedente em dois pontos estáveis: a intro
    ("...realizados por <NOME>.") e o rodapé ("Atenciosamente / <NOME> LTDA /
    CNPJ: XX.XXX.XXX/XXXX-XX"). O CNPJ do cedente é o CNPJ mascarado do corpo que NÃO
    é o da própria empresa pagadora (OTIMOTEX); com mais de um, prefere o ÚLTIMO (rodapé).

    Retorna (name, cnpj_14_digitos) — cada um pode ser None. Função pura; só dispara para
    remetente SSW. Degrada com segurança para (None, None) quando o corpo não casa."""
    if not _is_ssw_sender(sender_email) or not body_text:
        return (None, None)
    own = re.sub(r"\D", "", str(own_cnpj or "")) or None
    cnpj = None
    for m in _BODY_CNPJ_RE.finditer(body_text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 14 and digits != own:
            cnpj = digits  # mantém o último não-próprio (rodapé do cedente)
    name_m = _SSW_CEDENTE_NAME_RE.search(body_text)
    name = name_m.group(1).strip() if name_m else None
    if not cnpj and not name:
        return (None, None)
    return (name or None, cnpj)


# Distancia MAXIMA (em caracteres) entre o NOME rotulado e o identificador (CNPJ/CPF)
# no corpo. O corpo costuma citar VARIOS CNPJs — o do pagador (bloco do destinatario),
# o da plataforma no rodape, o de um terceiro mencionado — e so vale o que esta JUNTO
# do nome rotulado. Mesmo padrao da janela usada em parse_supplier_contacts para nao
# adotar uma chave PIX distante do rotulo "pix".
_BODY_SUPPLIER_ID_WINDOW = 200


def _payload_has_strong_supplier_id(payload: dict) -> bool:
    """O documento anexado trouxe CNPJ ou CPF do fornecedor? Nome NAO conta.

    Nome sozinho e o que o Vision/LLM erra: ele copia qualquer razao social impressa
    no documento (transportadora, cliente, sacado). CNPJ/CPF sao identificadores
    FORTES — quando presentes, o anexo manda e nenhum override do corpo se aplica."""
    return any(str(payload.get(k) or "").strip()
               for k in ("supplier_cnpj", "supplier_cpf"))


def _body_supplier_identity(body_text: "str | None",
                            own_cnpj: "str | None") -> "tuple[str | None, str | None, str | None]":
    """Fornecedor ROTULADO no corpo do e-mail **com identificador forte junto**.

    Serve para blindar o caminho do ANEXO: quando o documento anexado nao traz CNPJ/CPF,
    o nome que o Vision/LLM leu dele e apenas "alguma razao social impressa na pagina" —
    e num PEDIDO isso costuma ser a TRANSPORTADORA, nao quem recebe o pagamento. Caso de
    origem (conta 822, "Pagamento Bordados"): o anexo era a foto de um pedido e o Vision
    gravou "TRANSFER EXPRESS", enquanto o CORPO nomeava o fornecedor de forma explicita
    ("Razao Social: I S da Silva Camisetas e Malharia" + "CNPJ: 44.427.588/0001-30").
    Como a regra geral e "o anexo vence o corpo", o corpo nem era consultado e a conta
    nasceu sob um fornecedor errado, recem-criado e sem CNPJ.

    Retorna (nome, cnpj_14, cpf_11) — ou (None, None, None) quando nao ha um par
    confiavel. GUARDAS (cada uma existe por um caso real do banco):

    1. EXIGE nome ROTULADO **e** CNPJ/CPF. So o CNPJ nao basta: nos boletos que o
       despachante repassa (contas 423-428, "Dr. Ricardo") o corpo traz o CNPJ do
       CLIENTE solto, sem rotulo de fornecedor — disparar ali trocaria o fornecedor
       correto pelo de um terceiro. Medido: das 8 contas historicas de anexo com
       fornecedor sem CNPJ e CNPJ no corpo, o par rotulado so existe na 822.
    2. O identificador tem de estar DENTRO da janela a partir do nome
       (_BODY_SUPPLIER_ID_WINDOW) — CNPJ do rodape/pagador nao pertence ao rotulo.
    3. Descarta o CNPJ da PROPRIA empresa pagadora pela RAIZ de 8 digitos (bloco do
       destinatario; filiais do grupo compartilham a raiz — mesma regra de
       _finalize_supplier).
    4. Descarta nome que seja um TIPO de documento/pagamento (_is_non_supplier_term).

    Funcao PURA. Reusa _resolve_body_supplier_identity (a extracao canonica do corpo)
    para nao criar uma segunda fonte de verdade do que e "fornecedor rotulado"."""
    if not body_text:
        return (None, None, None)

    label_match = (_BODY_NAME_RE.search(body_text)
                   or _BODY_ISSUER_RE.search(body_text))
    if not label_match:
        return (None, None, None)          # guarda 1: sem rotulo, nao ha par confiavel
    name = label_match.group(1).strip()
    if not name or _is_non_supplier_term(name):
        return (None, None, None)          # guarda 4

    # guarda 2: so o identificador que esta JUNTO do nome rotulado.
    window = body_text[label_match.end():label_match.end() + _BODY_SUPPLIER_ID_WINDOW]
    _, cnpj, cpf = _resolve_body_supplier_identity(window)

    # guarda 3: o CNPJ do proprio pagador nunca e o fornecedor.
    own = re.sub(r"\D", "", str(own_cnpj or ""))
    if cnpj and len(own) >= 8 and cnpj[:8] == own[:8]:
        cnpj = None

    if not cnpj and not cpf:
        return (None, None, None)          # guarda 1 (a outra metade do par)
    return (name, cnpj, cpf)


_MAX_PDF_LINK_BYTES = 50 * 1024 * 1024  # 50 MB
_LINK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def extract_pdf_links(text: str, html: str) -> list[str]:
    """Extrai URLs candidatas a PDF do texto simples e HTML do e-mail.

    Inclui links HTML cujo texto-âncora ou caminho da URL sugira boleto/fatura.
    Limita a 10 candidatas por e-mail para evitar downloads em massa.
    """
    # Aviso de link suspeito da Locaweb no corpo → não seguir nenhum link.
    if _body_has_suspicious_warning(text, html):
        log.info("    Aviso de link suspeito (Locaweb) no corpo — links ignorados")
        return []

    candidates, seen = [], set()
    ssw_faturas: list[str] = []  # links SSW de FATURA (id=F) — têm PRIORIDADE (trazem o boleto)

    def _add(url: str, *, front: bool = False):
        # Desescapa entidades HTML (&amp; → &) — links de boleto vêm escapados no
        # HTML e quebrariam os parâmetros (ex.: SIEG/Vindi ?b=…&m=…&t=…).
        u = html_unescape(url.strip()).rstrip(".,;)>\"'")
        # Ignora links que a Locaweb entende como suspeitos (redirect/ofuscados).
        if u and u not in seen and u.startswith("http") and not _is_suspicious_link(u):
            seen.add(u)
            (ssw_faturas if front else candidates).append(u)

    for m in _LINK_HREF_RE.finditer(html or ""):
        url         = m.group(1).strip()
        anchor_text = _HTML_TAG_RE.sub("", m.group(2)).strip()
        if not url.startswith("http"):
            continue
        # SSW: o link de FATURA (id=F) traz o boleto; o de DACTE (id=D/E/X) é fiscal, sem
        # boleto. Preferimos a fatura (prioridade máxima) e DESCARTAMOS os DACTE — senão o
        # pipeline baixava o DACTE e a conta caía em 'ignorado' (regra CT-e/transporte).
        ssw_kind = _ssw_doc_kind(url)
        if ssw_kind == "fatura":
            _add(url, front=True)
            continue
        if ssw_kind == "dacte":
            continue
        url_path = url.lower().split("?")[0]
        if (_LINK_TEXT_RE.search(anchor_text)
                or url_path.endswith(".pdf")
                or _LINK_URL_RE.search(url)):
            _add(url)

    for url in _LINK_IN_TEXT_RE.findall(text or ""):
        if _ssw_doc_kind(url) == "dacte":
            continue  # nunca seguir o DACTE do SSW pelo texto puro
        if _ssw_doc_kind(url) == "fatura":
            _add(url, front=True)
            continue
        url_path = url.lower().split("?")[0]
        if url_path.endswith(".pdf") or _LINK_URL_RE.search(url):
            _add(url)

    # Faturas SSW primeiro (trazem o boleto), depois as demais candidatas.
    return (ssw_faturas + candidates)[:10]


# ── Guarda anti-SSRF do download por link ───────────────────────────────────
# Conteúdo de remetente DESCONHECIDO controla a URL; sem guarda, o servidor pode ser
# forçado a requisitar alvos internos (metadata cloud 169.254.169.254, localhost, LAN).
# Bloqueamos scheme != http(s) e host que resolve para IP interno — no URL inicial E a
# cada redirect (_SafeRedirectHandler), com o IP validado FIXADO no socket (_Pinned*).
#
# PORTA: NAO ha allowlist de porta. Havia {80,443}, mas ela barrava um caminho legitimo:
# o boleto de seguradora (SEGUROS SURA) chega por link que redireciona para
# `http://mdi.li:7000/api/item/<id>` — host PUBLICO servindo o PDF numa porta alta. O
# redirect era recusado ("destino nao permitido") e o e-mail caia em 'falha'. A protecao
# REAL contra SSRF e o teste de IP interno (_host_is_safe): uma vez provado que o destino
# e um IP EXTERNO, a porta nao muda o risco — nao ha servico interno a alcancar. Manter a
# allowlist so quebrava portais legitimos em porta alta. Trocar isto por uma lista de
# portas/dominios voltaria a quebrar o caso SURA quando a seguradora mudar de encurtador.
_ALLOWED_SCHEMES = ("http", "https")


def _safe_host_ips(host: str) -> "list[str]":
    """Resolve `host` e devolve os IPs SE todos forem externos; [] se a resolução falhar OU
    qualquer IP for interno (privado/loopback/link-local/reservado/multicast/unspecified).
    Base compartilhada por _host_is_safe (compat) e pelo pin de IP (anti-DNS-rebinding): o
    download conecta ao IP JÁ validado aqui, sem re-resolver o nome no socket."""
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    ips: "list[str]" = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return []
        # S4-3: normaliza IPv6 IPv4-mapeado (ex.: ::ffff:169.254.169.254) para o IPv4
        # embutido ANTES das checagens — em runtimes < 3.13 as flags is_private/is_link_local
        # podem não delegar ao IPv4 mapeado. Defesa em profundidade (produção roda 3.14).
        # getattr: só IPv6Address tem `ipv4_mapped`; IPv4Address não → None (sem AttributeError).
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return []
        ips.append(str(ip))
    return ips


def _host_is_safe(host: str) -> bool:
    """False se o host resolve para QUALQUER IP interno — barra SSRF para metadata cloud e
    rede interna. Wrapper de _safe_host_ips (mantido para compat de chamadas/testes)."""
    return bool(_safe_host_ips(host))


def _pin_ip_for_host(host: str) -> "str | None":
    """IP EXTERNO validado para FIXAR no socket (fecha a janela de DNS rebinding entre a
    validação e o connect). None quando o host não resolve ou resolve para algum IP interno."""
    ips = _safe_host_ips(host)
    return ips[0] if ips else None


def _is_safe_download_url(url: str) -> bool:
    """Valida a URL contra SSRF: scheme http(s) e host que NÃO resolve para IP interno.
    Aplicada ao URL inicial e revalidada a cada redirect.

    QUALQUER porta é aceita em host externo (ver o comentário do bloco acima): a porta
    alta é comum em portal de boleto (ex.: mdi.li:7000, das seguradoras) e, com o destino
    já provado externo, não abre acesso a serviço interno. Segue bloqueada a porta
    MALFORMADA (`parts.port` levanta ValueError) e a porta 0 (inválida)."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in _ALLOWED_SCHEMES:
            return False
        host = parts.hostname
        port = parts.port  # porta malformada levanta ValueError → bloqueada no except
    except ValueError:
        return False
    if not host:
        return False
    if port == 0:
        return False
    return _host_is_safe(host)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Revalida o destino de CADA redirect contra SSRF (impede bypass do guard via 302
    para alvo interno). O limite de saltos do urllib (max_redirections) segue ativo."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not _is_safe_download_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "redirect para destino não permitido (SSRF)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection que conecta a um IP JÁ VALIDADO e FIXADO, preservando o Host original
    no request. O socket não re-resolve o nome → fecha a janela de DNS rebinding (S4-1)."""

    def __init__(self, *args, pinned_ip: "str | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):  # noqa: D102
        target = self._pinned_ip or self.host
        self.sock = self._create_connection((target, self.port), self.timeout, self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Como _PinnedHTTPConnection, com TLS: o SNI / validação de certificado usa o HOSTNAME
    ORIGINAL (self.host), não o IP fixado — pin sem quebrar a verificação de certificado."""

    def __init__(self, *args, pinned_ip: "str | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):  # noqa: D102
        target = self._pinned_ip or self.host
        self.sock = self._create_connection((target, self.port), self.timeout, self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


def _pinned_conn_factory(conn_cls, url: str):
    """Fábrica de conexão que RESOLVE + VALIDA o host uma vez e FIXA o IP externo. Levanta
    URLError se o destino resolve para IP interno (ou não resolve) — cobre também cada
    redirect, pois o salto reentra pelo handler com a nova URL."""
    host = urllib.parse.urlsplit(url).hostname
    pinned = _pin_ip_for_host(host or "")
    if pinned is None:
        raise urllib.error.URLError("destino resolve para IP interno ou não resolve (SSRF)")

    def _make(chost, **kwargs):
        return conn_cls(chost, pinned_ip=pinned, **kwargs)

    return _make


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """Substitui o HTTPHandler default no opener: fixa o IP validado no connect (S4-1)."""

    def http_open(self, req):  # noqa: D102
        return self.do_open(_pinned_conn_factory(_PinnedHTTPConnection, req.full_url), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Substitui o HTTPSHandler default: fixa o IP e mantém o context/SNI (verificação de
    certificado preservada) — o server_hostname continua sendo o hostname original."""

    def https_open(self, req):  # noqa: D102
        # `_check_hostname` foi REMOVIDO do HTTPSHandler no Python 3.12+ (a verificação
        # de hostname passou a ser carregada pelo `context`). Referenciá-lo direto
        # levantava AttributeError e quebrava TODO download de link HTTPS sob Python
        # 3.14 (produção) — BRASPRESS e qualquer portal por link. Passamos só `context`
        # (que preserva a validação de certificado/hostname); em Python < 3.12 o
        # atributo ainda existe e é aceito pela HTTPSConnection.
        kwargs = {"context": self._context}
        if hasattr(self, "_check_hostname"):
            kwargs["check_hostname"] = self._check_hostname
        return self.do_open(
            _pinned_conn_factory(_PinnedHTTPSConnection, req.full_url), req, **kwargs)


def _build_safe_opener(*handlers: "urllib.request.BaseHandler") -> "urllib.request.OpenerDirector":
    """Opener com: pin de IP anti-rebinding (_Pinned*Handler substituem os HTTP/HTTPS
    handlers default por serem subclasses deles) + revalidação de CADA redirect
    (_SafeRedirectHandler) + handlers extras (ex.: cookies para BRASPRESS)."""
    return urllib.request.build_opener(
        _SafeRedirectHandler(), _PinnedHTTPHandler(), _PinnedHTTPSHandler(), *handlers)


def _fetch_url(url: str, timeout: int = 30,
               opener: "urllib.request.OpenerDirector | None" = None
               ) -> "tuple[bytes, str, str] | None":
    """GET em url; retorna (conteúdo, content_type, url_final) ou None.

    Quando `opener` é informado (build_opener com HTTPCookieProcessor), a chamada
    reutiliza a mesma sessão/cookies — necessário para portais que exigem cookie
    de sessão entre a página e o download (ex.: BRASPRESS JSESSIONID). O opener deve
    incluir o _SafeRedirectHandler; o caminho sem opener usa um opener seguro próprio."""
    if not _is_safe_download_url(url):
        log.info(f"    Link bloqueado (destino não permitido / SSRF): {url[:70]}")
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _LINK_UA})
        _open = opener.open if opener is not None else _build_safe_opener().open
        with _open(req, timeout=timeout) as resp:
            return (
                resp.read(_MAX_PDF_LINK_BYTES),
                resp.headers.get("Content-Type", "").lower(),
                resp.geturl(),
            )
    except urllib.error.HTTPError as e:
        log.info(f"    HTTP {e.code}: {url[:70]}")
        return None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # Falha de REDE esperada (host inacessivel / timeout / TLS / conexao). Silenciosa.
        # OSError cobre socket.timeout, ssl.SSLError, ConnectionError e TimeoutError.
        log.info(f"    Falha de rede ao acessar link ({type(e).__name__}): {url[:70]}")
        return None
    except Exception:
        # Erro INESPERADO = provavel BUG de codigo (ex.: incompatibilidade de versao
        # do Python que quebrou TODO download HTTPS sob 3.14 — AttributeError
        # '_check_hostname'). NAO pode se disfarcar de "link inacessivel": loga com
        # TRACEBACK (visivel no log/`/erros`) e segue sem derrubar o run inteiro.
        log.exception(f"    Erro inesperado ao acessar link (possivel bug): {url[:70]}")
        return None


def _save_pdf_data(data: bytes, sender_email: str, subject: str,
                   received_at: str) -> "Path | None":
    date_tag    = received_at[:10].replace("-", "")
    sender_tag  = safe_filename((sender_email or "").split("@")[0], 20)
    subject_tag = safe_filename(subject, 30)
    dest_name   = f"{sender_tag}_{subject_tag}_{date_tag}_link.pdf"
    dest_path   = PDF_INBOX / dest_name
    counter = 1
    while dest_path.exists():
        dest_path = PDF_INBOX / f"{dest_name[:-4]}_{counter}.pdf"
        counter += 1
    if not _is_within_inbox(dest_path):
        log.warning(f"    PDF de link ignorado (caminho fora de PDF_INBOX): {dest_name}")
        return None
    dest_path.write_bytes(data)
    log.info(f"    PDF via link salvo: {dest_path.name} ({len(data) // 1024} KB)")
    return dest_path


def download_pdf_from_url(url: str, sender_email: str, subject: str,
                           received_at: str) -> Path | None:
    """Baixa um PDF de uma URL, seguindo até um nível de página HTML intermediária.

    Fluxo:
      1. Faz GET na URL (segue redirects; usa sessão com cookies).
      2. Se a resposta contiver bytes PDF (%PDF), salva e retorna.
      3. Portal conhecido (BRASPRESS protocoloweb): monta a URL direta de
         download da fatura e baixa com o mesmo cookie de sessão.
      4. Se a resposta for HTML (landing page / tracking redirect),
         varre os links da página em busca de um link PDF e tenta baixá-lo.
      5. Qualquer outro conteúdo é ignorado.
    """
    # Sessão com cookies — necessária para portais que exigem JSESSIONID entre a
    # página inicial e o download (ex.: BRASPRESS). O cookiejar do http.cookiejar só
    # envia cookies a domínios correspondentes (sem vazamento cross-domain). O
    # _SafeRedirectHandler revalida cada redirect contra SSRF.
    cj = http.cookiejar.CookieJar()
    opener = _build_safe_opener(urllib.request.HTTPCookieProcessor(cj))

    result = _fetch_url(url, opener=opener)
    if not result:
        return None
    data, content_type, final_url = result

    if final_url != url:
        log.info(f"    Redirecionou para: {final_url[:80]}")

    # Caso 1: PDF direto (checa assinatura %PDF independente do Content-Type)
    if b"%PDF" in data[:32]:
        return _save_pdf_data(data, sender_email, subject, received_at)

    # Caso 2: portal BRASPRESS — a página inicial setou o JSESSIONID; agora baixa
    # a fatura pela URL direta (mesma sessão/cookies).
    bp_url = _braspress_download_url(url)
    if bp_url:
        log.info(f"    Portal BRASPRESS — baixando fatura: {bp_url[:80]}")
        inner = _fetch_url(bp_url, timeout=60, opener=opener)
        if inner and b"%PDF" in inner[0][:32]:
            return _save_pdf_data(inner[0], sender_email, subject, received_at)
        log.info("    Download BRASPRESS não retornou PDF")

    # Caso 3: página HTML intermediária — busca link PDF na página
    is_html = "text/html" in content_type or b"<html" in data[:200].lower()
    if not is_html:
        log.info(f"    Conteúdo não reconhecido (tipo: {content_type[:40]})")
        return None

    log.info("    Página HTML recebida — buscando link PDF interno")
    html_text = data.decode("utf-8", errors="replace")
    for m in _LINK_HREF_RE.finditer(html_text):
        # Desescapa &amp; etc. — ex.: SIEG linka o boleto na Vindi (?b=…&m=…&t=…),
        # cujos parâmetros quebrariam se mantidos como &amp;.
        inner_url   = html_unescape(m.group(1).strip())
        anchor_text = _HTML_TAG_RE.sub("", m.group(2)).strip()
        if not inner_url.startswith("http"):
            continue
        url_path = inner_url.lower().split("?")[0]
        if (url_path.endswith(".pdf")
                or _LINK_URL_RE.search(inner_url)
                or _LINK_TEXT_RE.search(anchor_text)):
            log.info(f"    Candidato na página: {inner_url[:70]}")
            inner = _fetch_url(inner_url, opener=opener)
            if inner and b"%PDF" in inner[0][:32]:
                return _save_pdf_data(inner[0], sender_email, subject, received_at)

    log.info("    Nenhum PDF encontrado na página HTML")
    return None


# ---------------------------------------------------------------------------
# Acionar extract_pdf (IN-PROCESS)
# ---------------------------------------------------------------------------
# A extração roda no MESMO processo do chamador — NÃO mais via subprocesso
# `python extract_pdf.py`. Motivo (busca geral 2026-06-22): o spawn de subprocesso
# partindo do processo do Flask falhava 100% com rc=0xC0000142 (STATUS_DLL_INIT_FAILED),
# o subprocesso nem inicializava (DLLs nativas de pandas/Pillow não carregavam naquele
# contexto). Chamar a função diretamente elimina a criação de processo — funciona
# idêntico no app (Flask), no CLI/terminal e no scheduler. Bônus: sem reimport de
# pdfplumber/pandas/anthropic a cada PDF, e sem o problema de encoding do pipe.
#
# Robustez mantida: falha transitória (I/O, lib nativa) repete com backoff; falha
# definitiva (PDF sem registros extraíveis) não repete. O motivo é propagado para
# gravar em email_processing_errors.
EXTRACTION_MAX_ATTEMPTS = 3
EXTRACTION_RETRY_BACKOFF = (2, 5)  # segundos de espera entre tentativas


# Comprimentos de prefixo do CNPJ do pagador aceitos como senha de boleto, do mais curto
# para o mais longo. `None` = o CNPJ COMPLETO (14 dígitos) — usado por emissores que pedem
# "o CNPJ sem pontuação". Constante nomeada: a regra é de NEGÓCIO e muda por emissor novo,
# não por refactor. Prefixo curto NUNCA abre um PDF cifrado com senha mais longa (a
# comparação do pypdf é exata), então a ordem só afeta o número de tentativas.
PDF_PASSWORD_CNPJ_LENGTHS: "tuple[int | None, ...]" = (3, 4, 5, 6, None)
# Mínimo de dígitos para a fonte valer como CNPJ — DERIVADO dos prefixos acima, nunca
# escrito à mão: prefixo novo maior sem ajustar o mínimo geraria senha truncada em silêncio.
PDF_PASSWORD_MIN_DIGITS = max(t for t in PDF_PASSWORD_CNPJ_LENGTHS if t is not None)


def _payer_cnpjs(ctrl) -> list[str]:
    """CNPJ de todas as empresas pagadoras a partir do controle Supabase, degradando para
    a pagadora principal quando o objeto não expõe a lista (controles falsos de teste e
    scripts antigos) e para vazio quando não expõe nenhuma das duas."""
    if hasattr(ctrl, "company_cnpjs"):
        return ctrl.company_cnpjs() or []
    if hasattr(ctrl, "company_cnpj"):
        um = ctrl.company_cnpj()
        return [um] if isinstance(um, str) else []
    return []


def pdf_password_candidates(cnpj: "str | list[str] | tuple[str, ...] | None") -> list[str]:
    """Senhas candidatas para boletos protegidos, derivadas do CNPJ do pagador.

    Aceita um CNPJ ou uma lista deles (as filiais pagadoras compartilham a raiz, mas o CNPJ
    COMPLETO de cada uma é distinto — e o pagador do e-mail só é resolvido depois da
    extração). Gera, por CNPJ e nesta ordem, os prefixos de `PDF_PASSWORD_CNPJ_LENGTHS` e o
    número completo; duplicatas são removidas PRESERVANDO a ordem, para não repetir a mesma
    tentativa de decrypt. Entrada inválida, vazia ou curta demais → lista vazia (o caller
    simplesmente não tenta abrir o cifrado)."""
    if isinstance(cnpj, str):
        fontes = [cnpj]
    elif isinstance(cnpj, (list, tuple)):
        fontes = [c for c in cnpj if isinstance(c, str)]
    else:
        return []

    candidatas: list[str] = []
    for fonte in fontes:
        digits = re.sub(r"\D", "", fonte)
        # Menos dígitos que o maior prefixo pedido = dado inutilizável como senha (um
        # "CNPJ" de 3 dígitos não produz "os 6 primeiros"). Fonte inteira descartada,
        # em vez de gerar senha curta que não corresponde a regra nenhuma.
        if len(digits) < PDF_PASSWORD_MIN_DIGITS:
            continue
        for tamanho in PDF_PASSWORD_CNPJ_LENGTHS:
            senha = digits if tamanho is None else digits[:tamanho]
            if senha not in candidatas:
                candidatas.append(senha)
    return candidatas


def _run_extraction_once(pdf_path: Path, pdf_passwords: list[str] | None = None) -> tuple[str | None, str | None, bool]:
    """Uma tentativa de extração IN-PROCESS. Retorna (csv_path, motivo_falha, transitorio).

    `transitorio=True` indica que vale repetir (exceção de I/O/runtime); `False` é
    falha definitiva (extração não gerou registros — repetir não muda). Usa diretório
    de saída temporário exclusivo, evitando CSV obsoleto de run anterior.
    """
    try:
        # Import lazy: só carrega pdfplumber/pandas/anthropic quando há PDF a extrair
        # (mantém o import do read_emails leve — mesmo padrão de _normalize_body_barcode).
        if str(EXTRACT_SCRIPT.parent) not in sys.path:
            sys.path.insert(0, str(EXTRACT_SCRIPT.parent))
        import extract_pdf  # noqa: E402

        with tempfile.TemporaryDirectory(dir=CSV_OUTPUT) as tmp_out:
            csv_path = extract_pdf.extract_to_csv(pdf_path, tmp_out, pdf_passwords=pdf_passwords)
            if not csv_path or not Path(csv_path).exists():
                # Sem CSV = nenhum registro válido (PDF ilegível/sem dados). Definitivo.
                return None, "extração não gerou registros (PDF ilegível ou sem dados)", False
            # Move o CSV para o diretório definitivo antes que o tempdir seja removido
            final = CSV_OUTPUT / Path(csv_path).name
            Path(csv_path).replace(final)
            return str(final), None, False
    except Exception as e:
        return None, f"exceção na extração in-process: {e}", True


def run_extraction(pdf_path: Path, pdf_passwords: list[str] | None = None) -> tuple[str | None, str | None]:
    """Executa extract_pdf in-process e retorna (csv_path, motivo_falha).

    Em sucesso: (caminho_do_csv, None). Em falha: (None, motivo). Repete falhas
    transitórias com backoff — um blip de I/O/runtime não perde o PDF. O motivo
    final é gravado em email_processing_errors (observável em /erros), em vez de
    só no console do Flask.
    """
    if not EXTRACT_SCRIPT.exists():
        msg = f"extract_pdf.py não encontrado: {EXTRACT_SCRIPT}"
        log.warning(msg)
        return None, msg

    reason = None
    for attempt in range(1, EXTRACTION_MAX_ATTEMPTS + 1):
        csv_path, reason, transient = _run_extraction_once(pdf_path, pdf_passwords)
        if csv_path:
            return csv_path, None
        if not transient or attempt == EXTRACTION_MAX_ATTEMPTS:
            break
        wait = EXTRACTION_RETRY_BACKOFF[min(attempt - 1, len(EXTRACTION_RETRY_BACKOFF) - 1)]
        log.warning(
            f"    extração falhou (tentativa {attempt}/{EXTRACTION_MAX_ATTEMPTS}): "
            f"{reason} — repetindo em {wait}s"
        )
        time.sleep(wait)

    log.error(f"    extração falhou definitivamente: {reason}")
    return None, reason


def _email_has_real_boleto(rows: list) -> bool:
    """True quando ALGUMA linha extraida do e-mail traz um BOLETO REAL (linha
    digitavel valida). Base da regra fatura+boleto: havendo boleto, as linhas sem
    boleto (fatura/relatorio do mesmo debito) sao ignoradas. Usa o CODIGO DE BARRAS
    (nao o document_type — o extrator rotula relatorio e boleto ambos como 'boleto')."""
    return any(_is_boleto_barcode(r.get("barcode")) for r in rows)


def _amount_key(value) -> float | None:
    """Normaliza um valor monetario para comparacao (float 2 casas) ou None."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _real_boleto_amounts(rows: list) -> set:
    """Conjunto dos VALORES dos boletos REAIS (linha digitavel valida) do e-mail.
    A regra fatura+boleto so descarta uma linha SEM boleto proprio quando ela tem o
    MESMO valor de um boleto real — a fatura/relatorio do MESMO debito. Uma linha de
    valor DISTINTO e outra divida e deve ser mantida mesmo sem barcode (ex.: 2o boleto
    ESCANEADO cujo Vision nao leu a linha digitavel — caso LMED 2937 R$2476,55 +
    1748 R$1166,67). Sem isso, o 2o boleto era descartado silenciosamente."""
    return {
        _amount_key(r.get("amount"))
        for r in rows
        if _is_boleto_barcode(r.get("barcode"))
    } - {None}


# EXTRATO/DEMONSTRATIVO/RELATORIO que acompanha um BOLETO no mesmo e-mail: descreve
# o MESMO debito de forma agregada (nao e um instrumento de pagamento), mas seu valor
# pode DIFERIR do boleto (bruto x liquido), escapando da guarda de valor
# (real_boleto_amounts). Sinal: termo de extrato/relatorio no NOME DO ARQUIVO ou na
# DESCRICAO. Palavra inteira (\b) p/ nao casar substring acidental. NAO inclui 'fatura'
# (comum em boleto legitimo) nem 'boleto' — um 2o boleto ESCANEADO cujo Vision nao leu
# a linha digitavel (caso LMED) e uma VIA DE PAGAMENTO, nunca um 'extrato', entao a
# regra abaixo nao o descarta. Caso de origem: Correios id 605
# (Extrato_sintetico_07.pdf, R$5295,58) coexistindo com o boleto id 606 (R$5158,34).
_STATEMENT_DOC_RE = re.compile(r"\b(extrato|extratos|demonstrativo|relatorio)\b")


def _is_statement_document(row: dict) -> bool:
    """True quando a linha e um EXTRATO/DEMONSTRATIVO/RELATORIO (documento que descreve
    um debito de forma agregada, nao um instrumento de pagamento) SEM codigo de barras
    proprio. Usado na regra fatura+boleto para descartar o extrato que acompanha o
    boleto mesmo quando o valor DIFERE (bruto x liquido) — o que a guarda de valor
    (real_boleto_amounts) nao pega. Comparacao sem acento sobre nome do arquivo +
    descricao. NAO casa boleto real (tem barcode) nem 2o boleto escaneado (nao e
    'extrato'), preservando o caso LMED."""
    if _is_boleto_barcode(row.get("barcode")):
        return False
    blob = _strip_accents_lower(
        f"{row.get('source_file') or ''} {row.get('description') or ''}"
    )
    # Normaliza separadores (inclui '_' do nome de arquivo, que e char de palavra e
    # anularia o \b): "Extrato_sintetico_07.pdf" -> "extrato sintetico 07 pdf".
    blob = re.sub(r"[\W_]+", " ", blob)
    return bool(_STATEMENT_DOC_RE.search(blob))


# Baixa/cancelamento de RECEBIVEL proprio — e-mail sobre titulos que a EMPRESA
# EMITIU (relatorio de baixa/cancelamento), nao um documento que ela deve pagar.
# Sinais: assunto de cobranca propria ("COBRANCA OTIMOTEX") ou o proprio relatorio
# de baixa na descricao. NAO e conta a pagar → ignorado (nao 'sem_fornecedor').
_RECEIVABLE_SUBJECT_TERMS = ("cobranca otimotex", "cobrancas nao enviadas")
_RECEIVABLE_DESC_RE = re.compile(
    r"cancelamento \(baixa\)|titulos? baixad|boletos? baixad"
)


def _is_receivable_notice(subject: str | None, description: str | None) -> bool:
    """True se o e-mail e um AVISO/relatorio de baixa de RECEBIVEL proprio (titulo
    que a empresa emitiu), nunca conta a pagar. Comparacao sem acento."""
    subj = _strip_accents_lower(subject or "")
    if any(t in subj for t in _RECEIVABLE_SUBJECT_TERMS):
        return True
    return bool(_RECEIVABLE_DESC_RE.search(_strip_accents_lower(description or "")))


# 'Documento' visual que NAO e conta a pagar: imagem de assinatura/logo de e-mail
# (image001.png colada no corpo) ou apresentacao/proposta de marketing — lidos via
# Vision, sem valor. So dispara quando amount<=0 E sem codigo de barras: um recibo/
# boleto legitimo (inclusive inline) tem valor e/ou linha digitavel, entao nao cai aqui.
_SIGNATURE_DESC_RE = re.compile(
    r"assinatura de e-?mail|assinatura comercial|assinatura de rodape|"
    r"logotipo|logomarca|rodape de e-?mail|cabecalho de e-?mail"
)
_MARKETING_DESC_RE = re.compile(
    r"apresentac[ao]{2} (institucional|comercial|de servicos|da empresa)|"
    r"proposta comercial|material (de )?marketing|institucional da|catalogo de produtos"
)


# Bloco de CONTATO (assinatura/rodape de e-mail) descrito pelo CONTEUDO. O Vision
# quase sempre descreve a image001.png colada no corpo pelo que ela MOSTRA — "Rua
# do Horto, 940 | CEP: 35681-779 | (37) 3249-4200 | www.peripan.com.br" — em vez de
# chama-la de "assinatura de e-mail". Por isso o _SIGNATURE_DESC_RE nao casava e a
# assinatura virava 'sem_valor', culpando o documento por um valor que ele nunca
# teve e poluindo /erros (5 das 13 linhas 'sem_valor' medidas em 07/08/2026).
_CONTACT_SIGNAL_RES = (
    re.compile(r"\b\d{5}-?\d{3}\b"),                                  # CEP
    re.compile(r"\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b"),                # telefone
    re.compile(r"www\.|\.com\.br|\bsite:"),                           # site
    re.compile(r"\b(rua|avenida|av|alameda|rodovia|distrito industrial)\b"),
)
# Qualquer sinal de documento financeiro DESQUALIFICA o descarte: preferimos uma
# linha a revisar em /erros a perder um recibo em silencio.
_FINANCIAL_TERM_RE = re.compile(
    r"\bvalor\b|r\$|\bvencimento\b|\bboleto\b|\bpagamento\b|\bpagar\b|\bfatura\b|"
    r"\bnota fiscal\b|\brecibo\b|\bpix\b|\bcodigo de barras\b|\blinha digitavel\b|"
    r"\bnosso numero\b|\bbeneficiario\b|\bcedente\b|\btotal\b"
)


def _is_contact_block(description: str | None) -> bool:
    """True se a descricao e um bloco de contato (assinatura/rodape), nao um documento.

    Exige >=2 sinais de contato E nenhum termo financeiro — conservador de proposito.
    """
    desc = _strip_accents_lower(description or "")
    if not desc or _FINANCIAL_TERM_RE.search(desc):
        return False
    return sum(bool(r.search(desc)) for r in _CONTACT_SIGNAL_RES) >= 2


def _nonpayable_visual_amount(value) -> float:
    """Converte o amount (string do CSV, decimal com ponto) em float; 0.0 se invalido."""
    try:
        return float(str(value or "0").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _is_nonpayable_visual(row: dict) -> bool:
    """True para conteudo visual sem valor que nao e conta a pagar (assinatura/logo
    de e-mail ou apresentacao/proposta de marketing). Conservador: exige amount<=0 e
    ausencia de codigo de barras para nao afetar recibo/boleto legitimo."""
    if _nonpayable_visual_amount(row.get("amount")) > 0:
        return False
    if _is_boleto_barcode(row.get("barcode")):
        return False
    desc = _strip_accents_lower(row.get("description") or "")
    if _SIGNATURE_DESC_RE.search(desc) or _MARKETING_DESC_RE.search(desc):
        return True
    return _is_contact_block(row.get("description"))


def extract_and_store_accounts(saved_pdfs: list, message_id: str,
                               ctrl: "SupabaseControl",
                               email_rec: dict = None,
                               body_text: str = "") -> tuple:
    """Extrai cada PDF e grava as contas resultantes em financial_account_control.

    Liga cada conta ao e-mail por gmail_message_id; multiplos PDFs no mesmo
    e-mail recebem sufixo (#1, #2, ...) para nao colidir na chave unica.
    Emails defeituosos sao logados em email_processing_errors e pulados.
    Retorna (lista de CSVs gerados, total de contas gravadas, nonpayable_only,
    attachment_account).

    `nonpayable_only` = True quando TODO registro extraido foi pulado como
    NAO-PAGAVEL (NF-e/NFS-e ou CT-e/transporte sem boleto) e NENHUM registro pagavel
    foi tentado — sinaliza o chamador a marcar o e-mail 'ignorado' (nao 'falha'). Se
    houve um boleto que falhou por sem_valor/sem_fornecedor, NAO e nonpayable_only
    (segue 'falha' para revisao).

    `attachment_account` = True quando um PDF anexado produziu uma conta pagavel que
    foi EFETIVAMENTE tratada — gravada como nova OU casada/atualizada por dedup contra
    uma conta ja existente (mesmo documento chegado por outro e-mail). "O boleto sempre
    vence o corpo": quando isto e True o chamador NAO deve rodar o fallback do corpo,
    pois o anexo ja respondeu pelo(s) pagavel(is) do e-mail. Antes o gate usava so
    accounts_saved (contas NOVAS), entao um boleto deduplicado (accounts_saved==0)
    deixava o corpo criar uma conta espuria com dados divergentes (ex.: vencimento
    lido do texto do corpo, sem o barcode) — id 510 (OBER): corpo gravou venc. 11/07
    duplicando o boleto id 159 (venc. 18/07 pelo fator do codigo de barras).
    """
    csvs_ok, accounts_saved, acc_index = [], 0, 0
    skipped_nonpayable, payable_attempts, dup_matches = 0, 0, 0
    err_ctx = email_rec or {}

    # ROBUSTEZ (cedente do boleto vence o emitente do CT-e): em fatura SSW de transporte,
    # o extrator pode gravar o EMITENTE do CT-e agregado (transportadora SUBCONTRATADA)
    # como fornecedor. O CEDENTE do boleto (quem EMITE a fatura e recebe) é nomeado no
    # corpo de forma determinística — computado uma vez e aplicado por boleto abaixo.
    _own_cnpj = ctrl.company_cnpj() if hasattr(ctrl, "company_cnpj") else None
    ssw_cedente_name, ssw_cedente_cnpj = _ssw_cedente_from_body(
        err_ctx.get("sender_email"), body_text, _own_cnpj)

    # ROBUSTEZ (fornecedor ROTULADO no corpo vence o nome lido do ANEXO sem identificador
    # forte): o nome que o Vision/LLM le de um pedido/recibo e so "alguma razao social
    # impressa na pagina" — tipicamente a TRANSPORTADORA. Quando o corpo nomeia o
    # fornecedor com CNPJ/CPF ao lado, ele e a fonte melhor. Computado uma vez por
    # e-mail; aplicado por linha abaixo, so nas que nao trazem CNPJ/CPF proprios.
    body_sup_name, body_sup_cnpj, body_sup_cpf = _body_supplier_identity(body_text, _own_cnpj)

    # Senhas candidatas (prefixos e CNPJ completo das pagadoras) para boletos protegidos —
    # computadas uma vez por e-mail; vazias quando o CNPJ não está disponível.
    pdf_passwords = pdf_password_candidates(_payer_cnpjs(ctrl))

    # ------------------------------------------------------------------
    # Passo 1 — extrai TODOS os anexos e coleta as linhas. Upload no Storage
    # + extracao acontecem aqui; a decisao de gravar cada conta fica para o
    # passo 2, que precisa enxergar o e-mail INTEIRO (regra fatura+boleto).
    # ------------------------------------------------------------------
    pending = []  # linhas extraidas de todos os PDFs do e-mail, na ordem de chegada
    # Tamanho de cada anexo, colhido AQUI (no passo 2 o arquivo pode nao estar mais em
    # disco) — vai para financial_account_attachment.size_bytes.
    attachment_sizes = {}
    # Regra LEBIANCO — parte "anexo do email": o texto CRU do PDF nao chega ao passo 2 (o CSV
    # so traz description/source_file) e o arquivo pode nao estar mais em disco la, entao a
    # varredura acontece AQUI, uma vez por e-mail (flag no nivel da MENSAGEM: qualquer anexo
    # que mencione lebianco marca o e-mail inteiro). Best-effort — nunca levanta.
    # Otimizacao: se remetente/assunto/corpo JA decidiram que e LEBIANCO, nao ha o que provar —
    # pula a leitura dos PDFs (I/O e superficie de falha a toa).
    pdf_lebianco = (_is_lebianco_sender(err_ctx.get("sender_email"))
                    or _subject_has_lebianco(err_ctx.get("subject"))
                    or _has_lebianco_reference(body_text))
    # Regra LE BLANC — mesma semantica de MENSAGEM da flag acima: mencao no assunto, corpo,
    # remetente ou em QUALQUER anexo (texto cru e nome do arquivo, somados no laco abaixo)
    # marca todas as contas do e-mail. O fornecedor LE BLANC e somado POR LINHA, adiante.
    le_blanc_ref = _has_le_blanc_reference(err_ctx.get("subject"), body_text,
                                           err_ctx.get("sender_email"))
    for pdf_path in saved_pdfs:
        try:
            attachment_sizes[pdf_path.name] = pdf_path.stat().st_size
        except OSError:
            attachment_sizes[pdf_path.name] = 0

        # Texto CRU do anexo, lido UMA vez e servido a dois consumidores independentes: a
        # regra LEBIANCO (abaixo) e o registro de documento fiscal (Onda 3). A leitura antes
        # era exclusiva da LEBIANCO e era PULADA quando remetente/assunto/corpo ja tinham
        # decidido; agora e sempre feita, porque a chave de acesso precisa do texto de TODO
        # anexo. Best-effort: "" quando o PDF e imagem, e cifrado ou o pdfplumber falha.
        # `_attachment_text` (e nao `_pdf_text`) porque o anexo tambem pode ser .docx, que o
        # pdfplumber so faria falhar em silencio.
        pdf_raw_text = _attachment_text(pdf_path)

        if not pdf_lebianco and _has_lebianco_reference(pdf_raw_text):
            pdf_lebianco = True   # curto-circuito da FLAG (a leitura acima ja aconteceu)
        if not le_blanc_ref and _has_le_blanc_reference(pdf_raw_text, pdf_path.name):
            le_blanc_ref = True   # reusa o texto ja lido — sem I/O extra

        # Publica o PDF no Storage SEMPRE (antes da extracao) — assim o anexo fica
        # disponivel para revisao manual mesmo quando a extracao falha por completo.
        # Nao-fatal: se o upload falhar, a extracao segue normalmente.
        ctrl.upload_attachment(pdf_path)

        # ONDA 3 — documento fiscal (CT-e/NF-e/CF-e/NFC-e) pela chave de acesso.
        # AQUI, no Passo 1, e nao nos 7 pontos de `skipped_nonpayable` do Passo 2: e um ponto
        # UNICO, cobre o documento mesmo quando a linha VIRA conta (o boleto de transporte que
        # traz junto a chave do CT-e, hoje perdida) e nao depende da regra de negocio, que pode
        # mudar sem que o registro fiscal deixe de ser desejavel.
        # ANTES do run_extraction de proposito: assim a chave e capturada mesmo quando a
        # extracao falha por indisponibilidade da Claude API.
        _register_fiscal_documents(ctrl, pdf_raw_text, pdf_path.name, err_ctx)

        csv_path, extract_err = run_extraction(pdf_path, pdf_passwords)
        if not csv_path:
            ctrl.register_error(
                {**err_ctx, "source_file": pdf_path.name},
                "extracao_falhou",
                f"extract_pdf nao gerou CSV para {pdf_path.name}",
                raw_payload={"source_file": pdf_path.name, "detalhe": extract_err},
            )
            continue

        csvs_ok.append(csv_path)
        for row in read_extracted_rows(csv_path):
            # Falha de API na extracao: registra erro_api e interrompe o run com
            # seguranca (sem gravar conta, sem fallback regex silencioso).
            if (row.get("extraction_source") or "").strip().lower() == "erro_api":
                ctrl.register_error(
                    {**err_ctx, "source_file": row.get("source_file")},
                    "erro_api",
                    row.get("processing_notes")
                    or "API Anthropic indisponível (crédito/auth/limite)",
                    raw_payload=row,
                )
                raise ApiUnavailableError(
                    row.get("processing_notes") or "API Anthropic indisponível"
                )
            pending.append(row)

    # Regra FATURA + BOLETO (multi-anexo): quando ALGUM anexo do e-mail traz um
    # BOLETO REAL (linha digitavel valida), a fatura/relatorio do MESMO debito e
    # ignorada ("o boleto sempre vence"). Sozinha (sem boleto), a fatura e extraida.
    # O sinal e o CODIGO DE BARRAS + o VALOR: o descarte so vale para a linha SEM
    # boleto proprio cujo valor COINCIDE com um boleto real (mesmo debito). Uma linha
    # de valor DISTINTO e outra divida e e mantida mesmo sem barcode — cobre o 2o
    # boleto ESCANEADO cujo Vision nao leu a linha digitavel (caso LMED, valores
    # 2476,55 x 1166,67). Antes, bastava existir 1 boleto real p/ descartar QUALQUER
    # linha sem barcode, perdendo o 2o boleto silenciosamente.
    has_real_boleto = _email_has_real_boleto(pending)
    real_boleto_amounts = _real_boleto_amounts(pending) if has_real_boleto else set()

    # Seguradora (assunto com seguro/seguradora/apolice): so o boleto com linha digitavel
    # valida vira conta. Calculado uma vez — o assunto e o mesmo para todas as linhas.
    insurance_ctx = _is_insurance_context(email_rec.get("subject") if email_rec else None)

    # ------------------------------------------------------------------
    # Passo 2 — grava as contas
    # ------------------------------------------------------------------
    for row in pending:
        dtype = (row.get("document_type") or "").strip().lower()
        if dtype in SKIP_ACCOUNT_TYPES:
            # NF-e/NFS-e sao documentos FISCAIS que, sozinhos, nao geram conta a pagar
            # (SKIP_ACCOUNT_TYPES). EXCECAO — documento COMBINADO na MESMA linha: alguns
            # prestadores (planos de saude, telecom) emitem NFS-e + BOLETO no MESMO PDF
            # (ex.: Amil "e-Faturamento" — cabecalho NFS-e no topo + ficha de compensacao
            # com linha digitavel abaixo). O extrator ve o cabecalho fiscal e rotula 'nfse',
            # mas a linha traz um BOLETO PAGAVEL (linha digitavel valida) — o pagavel
            # vence. Re-rotula 'boleto' e NAO pula, senao a conta a pagar era descartada
            # silenciosamente e o e-mail virava 'ignorado' (caso real: contas 1045/1046,
            # Amil R$ 7.217,91). NAO afeta a NF-e/NFS-e PURA (sem barcode), que segue
            # pulada; nem o caso multi-anexo com uma NFS-e separada sem boleto proprio.
            if _is_boleto_barcode(row.get("barcode")):
                log.info("    NF-e/NFS-e COM boleto pagavel — re-rotulado 'boleto' "
                         "(documento fiscal + ficha de compensacao no mesmo arquivo)")
                row["document_type"] = "boleto"
                dtype = "boleto"
            else:
                log.info(f"    {dtype.upper()} ignorado — nao gera conta a pagar")
                skipped_nonpayable += 1
                continue

        gmid    = message_id if acc_index == 0 else f"{message_id}#{acc_index}"
        payload = build_financial_payload(row, gmid, received_at=err_ctx.get("received_at"),
                                          subject=err_ctx.get("subject"))
        # Remetente do e-mail → o trigger alinha supplier.email (migration 023).
        payload["sender_email"] = err_ctx.get("sender_email")
        payload["subject"]      = err_ctx.get("subject")  # exibido/buscado em /consulta (migration 025)
        ctx     = {**err_ctx, "source_file": row.get("source_file")}

        # Cedente do boleto (fatura SSW de transporte): sobrepõe o fornecedor extraído
        # SÓ quando a linha É um boleto real (linha digitável válida) — o cedente do corpo
        # é o credor autoritativo; o emitente do CT-e agregado é a transportadora
        # subcontratada. Degrada sozinho quando o corpo não nomeia o cedente (None/None).
        ssw_aplicado = False
        if (ssw_cedente_cnpj or ssw_cedente_name) and _is_boleto_barcode(payload.get("barcode")):
            if ssw_cedente_cnpj:
                payload["supplier_cnpj"] = ssw_cedente_cnpj
            if ssw_cedente_name:
                payload["supplier_name"] = ssw_cedente_name
            payload.pop("supplier_cpf", None)  # cedente PJ — descarta CPF do emitente
            ssw_aplicado = True
            log.info("    [CEDENTE-SSW] fornecedor do boleto pelo cedente do corpo: "
                     f"{ssw_cedente_name!r} / {ssw_cedente_cnpj}")

        # Fornecedor ROTULADO no corpo (nome + CNPJ/CPF juntos) sobrepoe o nome que o
        # anexo trouxe SEM identificador forte. A condicao e o coracao da regra: com
        # CNPJ/CPF proprios, o ANEXO manda (nao regride boleto/nota, que sempre os traz);
        # sem eles, o nome do anexo e um palpite e o par rotulado do corpo e melhor.
        #
        # `not ssw_aplicado` NAO e redundante (achado da autorrevisao): quando o unico CNPJ
        # do corpo SSW e o da PROPRIA empresa, _ssw_cedente_from_body devolve o cedente
        # so com NOME — e a linha fica sem identificador forte, deixando este override
        # sobrepor o cedente que a guarda SSW acabou de gravar. O CEDENTE do boleto e o
        # credor autoritativo daquela fatura; nada no corpo o supera.
        if ((body_sup_cnpj or body_sup_cpf)
                and not ssw_aplicado
                and not _payload_has_strong_supplier_id(payload)):
            payload["supplier_name"] = body_sup_name
            # Exclusivos: gravar os dois faria a RPC casar por CNPJ e deixar um CPF
            # orfao no cadastro. Prioriza o CNPJ (PJ), como no override SSW.
            if body_sup_cnpj:
                payload["supplier_cnpj"] = body_sup_cnpj
                payload.pop("supplier_cpf", None)
            else:
                payload["supplier_cpf"] = body_sup_cpf
                payload.pop("supplier_cnpj", None)
            log.info("    [FORNECEDOR-CORPO] anexo sem CNPJ/CPF — fornecedor pelo rotulo "
                     f"do corpo: {body_sup_name!r} / {body_sup_cnpj or body_sup_cpf}")

        # CT-e / transporte SEM boleto NAO gera conta a pagar (regra 1): o CT-e e
        # documento fiscal; so o boleto de frete e pagavel. supplier_name ainda
        # existe no payload aqui (removido so em _finalize_supplier). Um boleto de
        # transporte ja foi rotulado 'cte' em build_financial_payload e TEM barcode
        # de boleto — logo NAO cai aqui.
        if (_is_transport_context(err_ctx.get("subject"), payload.get("supplier_name"),
                                  payload.get("document_type"))
                and not _is_boleto_barcode(payload.get("barcode"))):
            log.info("    CT-e/transporte sem boleto ignorado — nao gera conta a pagar")
            skipped_nonpayable += 1
            continue

        # Fatura/relatorio acompanhando um BOLETO no mesmo e-mail → ignorado (regra
        # fatura+boleto). So descarta a linha SEM boleto proprio cujo VALOR coincide
        # com um boleto real do e-mail (mesmo debito). Linha de valor DISTINTO e outra
        # divida → mantida mesmo sem barcode (2o boleto escaneado sem linha digitavel).
        if (not _is_boleto_barcode(payload.get("barcode"))
                and _amount_key(payload.get("amount")) in real_boleto_amounts):
            log.info(
                f"    Fatura/relatorio ignorado — mesmo valor de um boleto no e-mail "
                f"({row.get('source_file')})"
            )
            skipped_nonpayable += 1
            continue

        # Extrato/demonstrativo/relatorio acompanhando um BOLETO no mesmo e-mail →
        # ignorado. Complementa a guarda de valor acima: o extrato descreve o MESMO
        # debito de forma agregada e seu valor pode DIFERIR do boleto (bruto x liquido),
        # escapando de real_boleto_amounts. So dispara com boleto real presente e para a
        # linha SEM barcode reconhecida como extrato/relatorio; um 2o boleto ESCANEADO
        # (caso LMED) nao casa esses termos e e mantido. Origem: Correios id 605
        # (Extrato_sintetico) x boleto id 606, valores 5295,58 x 5158,34.
        if has_real_boleto and _is_statement_document(row):
            log.info(
                f"    Extrato/relatorio ignorado — acompanha um boleto no e-mail "
                f"({row.get('source_file')})"
            )
            skipped_nonpayable += 1
            continue

        # SEGURADORA: so pagavel com linha digitavel valida vira conta. Uma condicao
        # cobre as DUAS metades da regra:
        #   - e-mail COM boleto (kit digital SURA = boleto + "conjunto faturamento"):
        #     o boleto grava e a FATURA que vem junto cai aqui — sem conta duplicada.
        #     A guarda de valor acima nao a pegaria: fatura e boleto tem valores
        #     diferentes (premio total x parcela).
        #   - e-mail SEM boleto nenhum: todas as linhas caem aqui, payable_attempts
        #     fica 0 → nonpayable_only=True → status 'ignorado' (nao 'falha').
        # Fica ACIMA de payable_attempts += 1 justamente para nao contar como tentativa
        # de pagavel (e o que diferencia 'ignorado' de 'falha' em status_for_result).
        if insurance_ctx and not _is_boleto_barcode(payload.get("barcode")):
            log.info(
                f"    Documento de seguradora sem boleto valido ignorado "
                f"({row.get('source_file')})"
            )
            skipped_nonpayable += 1
            continue

        # Baixa/cancelamento de RECEBIVEL proprio (relatorio de titulos que a empresa
        # EMITIU, ex.: "COBRANCA OTIMOTEX") NAO e conta a pagar → ignorado, sem logar
        # 'sem_fornecedor'. Roda antes das validacoes (o documento nao tem fornecedor
        # justamente por nao ser uma conta a pagar).
        if _is_receivable_notice(err_ctx.get("subject"), row.get("description")):
            log.info("    Baixa/cobranca de recebivel proprio ignorada — nao gera conta a pagar")
            skipped_nonpayable += 1
            continue

        # Conteudo visual sem valor que nao e conta a pagar: imagem de assinatura/logo
        # de e-mail (image001.png) ou apresentacao/proposta de marketing (Vision) →
        # ignorado, sem logar 'sem_valor'.
        if _is_nonpayable_visual(row):
            log.info(f"    Conteudo nao-pagavel (assinatura/marketing) ignorado — {row.get('source_file')}")
            skipped_nonpayable += 1
            continue

        payable_attempts += 1

        # Validacao 1: valor ausente ou zero
        if not payload.get("amount"):
            ctrl.register_error(
                ctx, "sem_valor",
                f"Valor ausente ou zero — {row.get('source_file')}",
                raw_payload=row
            )
            acc_index += 1
            continue

        # Validacao 2: fornecedor nao identificado por NENHUMA chave.
        # Alem de CNPJ/CPF/nome extraidos do PDF, sao chaves validas: o e-mail
        # do remetente (a RPC casa por email/2/3/4 ou cria o fornecedor), o
        # ASSUNTO (favorecido de e-mail interno de pagamento) e, em ultimo
        # recurso, o PAGADOR (payer_name/payer_cnpj). So rejeita quando NAO ha
        # identificador algum — _finalize_supplier tenta todas essas formas.
        # Guia de IMPOSTO tambem passa: mesmo sem nome/CNPJ/CPF/e-mail/assunto/
        # pagador, _finalize_supplier lanca a conta sob a OTIMOTEX (sk=1).
        if not any([payload.get("supplier_cnpj"),
                    payload.get("supplier_cpf"),
                    payload.get("supplier_name"),
                    payload.get("sender_email"),
                    _supplier_name_from_subject(payload.get("subject")),
                    payload.get("payer_name"),
                    payload.get("payer_cnpj"),
                    _is_tax_document(payload.get("document_type"))]):
            ctrl.register_error(
                ctx, "sem_fornecedor",
                f"CNPJ, CPF, nome e e-mail do fornecedor ausentes — {row.get('source_file')}",
                raw_payload=row
            )
            acc_index += 1
            continue

        # Resolve o fornecedor (RPC) → grava sk_supplier e remove as colunas
        # denormalizadas do payload. Roda APOS a validacao sem_fornecedor (que
        # usa os campos brutos) e ANTES da dedup (que casa por sk_supplier).
        # 🔴 `body_text` E' OBRIGATORIO AQUI — e' o que habilita o fallback 1b
        # (e-mail do remetente original encaminhado) no caminho de ANEXO. Sem ele a
        # chamada COMPILA e o fallback fica MORTO em producao, sem sintoma nenhum.
        # 🔴 Fornecedor LE BLANC capturado AQUI, antes do finalize (que remove
        # supplier_name/supplier_cnpj) e depois dos overrides SSW/corpo acima.
        row_le_blanc = le_blanc_ref or _le_blanc_supplier_signal(payload)
        if not _finalize_supplier(ctrl, payload, body_text):
            ctrl.register_error(
                ctx, "db_erro",
                f"Falha ao resolver fornecedor — {row.get('source_file')}",
                raw_payload=row
            )
            acc_index += 1
            continue

        # Classificacao contabil FORCADA por tipo de documento (IRRF/DUIMP/ICMS Importacao/
        # transporte) — sobrepoe o default do fornecedor e, quando a regra pede, faz write-back
        # no supplier (exceto OTIMOTEX). Roda apos finalize (sk/cc/ca ja setados), antes da dedup.
        apply_forced_classification(ctrl, payload)

        # Empresa pagadora (LE BLANC -> ester -> LEBIANCO -> OTIMOTEX). Aqui (e nao so na
        # rede do register_financial) porque este e o unico ponto com body_text + as flags do
        # anexo em escopo. Roda DEPOIS de _finalize_supplier, que ja removeu supplier_name/cnpj
        # do payload — por isso o fornecedor LE BLANC chega pela flag row_le_blanc; o
        # fornecedor LEBIANCO segue sem classificar (pode haver conta da OTIMOTEX cujo
        # fornecedor e a LEBIANCO).
        apply_sk_company(payload, body_text=body_text, pdf_lebianco=pdf_lebianco,
                         le_blanc=row_le_blanc)

        # Write-back de contato (chave PIX / telefone / WhatsApp) detectado no texto
        # do e-mail para o cadastro do fornecedor. Best-effort, nao toca no payload.
        apply_contact_writeback(ctrl, payload)

        # Dedup de conteudo: o mesmo documento ja gravado por outro e-mail
        # (remetente reenvia o mesmo boleto/guia, com Message-ID diferente).
        # Reemissao com vencimento mais novo (mesma guia) → ATUALIZA a conta
        # existente para o vencimento/boleto atual, em vez de duplicar ou
        # manter dados de pagamento vencidos. Roda ANTES da uniquificacao do
        # invoice_number para nao gravar duplicata.
        dup = ctrl.find_financial_duplicate(payload)
        if dup:
            new_due = payload.get("due_date")
            old_due = dup.get("due_date")
            new_barcode = (payload.get("barcode") or "").strip() or None
            # ISO 'YYYY-MM-DD' compara corretamente como string.
            if new_due and (not old_due or str(new_due) > str(old_due)):
                ctrl.update_financial(dup["id"], {
                    "due_date":       new_due,
                    "barcode":        new_barcode,
                    "amount_charged": payload.get("amount_charged"),
                    "fine_interest":  payload.get("fine_interest"),
                    "other_additions": payload.get("other_additions"),
                })
                log.info(
                    f"    [REEMISSAO] mesma guia — conta atualizada p/ vencimento "
                    f"{new_due} ({row.get('source_file')})"
                )
            elif new_barcode and not (dup.get("barcode") or "").strip():
                # A conta existente (corpo/notificacao) NAO tem linha digitavel e o
                # novo documento e um BOLETO da MESMA divida: grava o barcode/boleto
                # na conta sobrevivente, mesmo com vencimento igual/mais antigo — o
                # boleto sempre vence o corpo. Fecha o gap cross-e-mail sem duplicar.
                ctrl.update_financial(dup["id"], {
                    "barcode":        new_barcode,
                    "amount_charged": payload.get("amount_charged"),
                    "fine_interest":  payload.get("fine_interest"),
                    "other_additions": payload.get("other_additions"),
                })
                log.info(
                    f"    [DUP-DOC] boleto enriquece conta existente com código de "
                    f"barras ({row.get('source_file')})"
                )
            else:
                log.info(
                    f"    [DUP-DOC] reemissão igual/mais antiga — mantido "
                    f"({row.get('source_file')})"
                )
            # Vincula o anexo deduplicado a conta EXISTENTE (migration 079) — sem isto o
            # boleto recem-chegado fica so no Storage (upload do Passo 1), sem linha em
            # financial_account_attachment, e a conta que ele deduplicou (ex.: lancada
            # manualmente antes do e-mail chegar) segue sem nenhum comprovante anexado,
            # em silencio: nenhum erro, nenhum status distinto — so um anexo ausente que
            # ninguem nota ate precisar dele. Mesma chamada do caminho de conta NOVA
            # (idempotente por on_conflict=account_id,storage_key); nao usar
            # payload.get("created_by") sozinho pelo mesmo motivo de la — o dict aqui
            # nunca o recebe, quem resolve de fato e' resolve_user.
            src = row.get("source_file")
            if src:
                ctrl.register_attachment(
                    dup["id"], src,
                    size_bytes=attachment_sizes.get(src, 0),
                    uploaded_by=payload.get("created_by") or ctrl.resolve_user(payload.get("sender_email")),
                )
            # O boleto anexado casou uma conta ja existente: o pagavel foi tratado.
            # Conta como conta do anexo → suprime o fallback do corpo (nao regredir:
            # sem isto o corpo criava conta espuria, ex.: id 510).
            dup_matches += 1
            acc_index += 1
            continue

        if payload.get("invoice_number"):
            payload["invoice_number"] = ctrl.unique_invoice_number(payload["invoice_number"])

        new_id = ctrl.register_financial(payload)
        if new_id:
            accounts_saved += 1
            # Vincula o anexo a conta recem-criada (migration 079) — so aqui ha id. O
            # upload ja ocorreu no passo 1; registrar mesmo se ele falhou mantem a tabela
            # espelhando o source_file (padrao unico com os anexos manuais). Nao-fatal.
            src = row.get("source_file")
            if src:
                # O anexo herda o DONO da conta (igual ao backfill da 079, que usou
                # created_by). NAO usar payload.get("created_by"): register_financial
                # resolve o dono numa COPIA local do payload, entao o dict daqui nunca o
                # recebe e o anexo cairia sempre no sentinela. resolve_user e cacheado,
                # entao esta chamada nao custa uma RPC extra.
                ctrl.register_attachment(
                    new_id, src,
                    size_bytes=attachment_sizes.get(src, 0),
                    uploaded_by=payload.get("created_by") or ctrl.resolve_user(payload.get("sender_email")),
                )
        else:
            ctrl.register_error(
                ctx, "db_erro",
                f"Falha ao gravar em financial_account_control — {row.get('source_file')}",
                raw_payload=row
            )
        acc_index += 1

    nonpayable_only = skipped_nonpayable > 0 and payable_attempts == 0
    # Anexo respondeu por um pagavel (conta nova OU dedup contra conta existente).
    attachment_account = accounts_saved > 0 or dup_matches > 0
    return csvs_ok, accounts_saved, nonpayable_only, attachment_account


# Resultado da extração pelo corpo (try_extract_from_body) — orienta o status do
# e-mail. Distinguir DUPLICATE de NONE é a regra de negócio que evita marcar como
# 'falha' um e-mail cujo pagável já foi registrado por outro e-mail (ex.: a
# mensagem original e seu RES:/encaminhamento).
BODY_CREATED   = "created"    # conta nova gravada            → 'recebido'
BODY_DUPLICATE = "duplicate"  # pagável duplica conta existente → 'ignorado'
BODY_NONE      = "none"       # sem pagável utilizável         → falha/notificação
BODY_IGNORED   = "ignored"    # CT-e/transporte sem boleto     → 'ignorado' (não pagável)


def try_extract_from_body(email_rec: dict, body_text: str, received_at: str,
                          message_id: str, ctrl: "SupabaseControl",
                          sender_email: str | None = None) -> str:
    """Tenta gravar uma conta extraida do corpo do e-mail (sem PDF valido).

    Retorna um de:
      BODY_CREATED   — conta NOVA gravada (chamador marca 'recebido');
      BODY_DUPLICATE — o pagavel do corpo DUPLICA uma conta ja registrada: nao
                       grava de novo, anota a referencia em email_rec['duplicate_of']
                       e o chamador marca 'ignorado' (nao e falha — a conta existe);
      BODY_NONE      — sem pagavel utilizavel (chamador segue p/ falha/notificacao);
      BODY_IGNORED   — CT-e/transporte SEM boleto: nao e conta a pagar (chamador
                       marca 'ignorado', nao 'falha').
    Anota o motivo em email_rec['notes']. O log em email_processing_errors e feito
    de forma centralizada no chamador (process_message), para TODA falha.
    """
    # Reencaminhamento interno de CONFIRMACAO DE PAGAMENTO com assunto REESCRITO: as
    # guardas do run_reader olham so o assunto RECEBIDO, entao um "pagamento Sua Fatura"
    # que encapsula um "Assunto: Confirmacao de pagamento ..." no corpo escapa e gera
    # conta. Reavalia o assunto ORIGINAL encaminhado; sendo confirmacao, nao gera conta
    # (→ 'ignorado', nao 'falha') — um comprovante de algo ja pago nunca e um pagavel.
    # So no caminho do CORPO (este e' o fallback sem anexo pagavel): um boleto real
    # anexado a uma confirmacao encaminhada nunca chega aqui e segue sendo pago.
    # NAO bloqueia 'lembrete' encaminhado (ver docstring de body_forwards_payment_
    # confirmation) — segue para a extracao normal + dedup de conteudo abaixo.
    fwd_reason = body_forwards_payment_confirmation(body_text)
    if fwd_reason:
        log.info(f"    Confirmacao de pagamento encaminhada no corpo — ignorado ({fwd_reason})")
        email_rec["notes"] = fwd_reason
        return BODY_IGNORED

    # SEGURADORA: so boleto com linha digitavel valida vira conta (regra de negocio).
    # Detectado so pelo ASSUNTO — ver o bloco de _INSURANCE_TERMS: casar pelo remetente
    # descartaria a conta 58 (Porto Seguro "Rastreador"), que hoje nasce deste caminho.
    insurance_ctx = _is_insurance_context(email_rec.get("subject"))

    payload = extract_from_email_body(body_text, received_at, message_id, sender_email,
                                      subject=email_rec.get("subject"))
    if payload is None:
        # Seguradora sem sinal financeiro no corpo → 'ignorado', nao 'falha'. E o
        # desfecho do e-mail da SURA quando o boleto por link nao pode ser baixado
        # (rede/portal fora do ar): o corpo so anuncia "Clique aqui para baixar o
        # documento", sem valor nem numero — nao ha o que revisar em /erros.
        if insurance_ctx:
            log.info("    E-mail de seguradora sem boleto valido — ignorado (nao e conta a pagar)")
            email_rec["notes"] = "E-mail de seguradora sem boleto valido em anexo/link"
            return BODY_IGNORED
        # Sem sinal financeiro (sem valor nem documento) no corpo.
        email_rec["notes"] = "Corpo sem sinal financeiro (sem valor nem documento)"
        return BODY_NONE

    # Remetente do e-mail → o trigger alinha supplier.email (migration 023).
    payload["sender_email"] = sender_email
    payload["subject"]      = email_rec.get("subject")  # exibido/buscado em /consulta (migration 025)

    # Mesma trava do caminho de PDF: NF-e/NFS-e nao geram conta a pagar.
    # Sem isso, notificacoes de nota fiscal (ex.: NFe da Editora Globo) vazavam
    # para financial_account_control como document_type='outro'.
    dtype = (payload.get("document_type") or "").strip().lower()
    if dtype in SKIP_ACCOUNT_TYPES:
        log.info(f"    {dtype.upper()} (corpo do e-mail) ignorado — nao gera conta a pagar")
        email_rec["notes"] = f"{dtype.upper()} (corpo do e-mail) — nao gera conta a pagar"
        return BODY_NONE

    # CT-e / transporte SEM boleto (corpo) NAO gera conta a pagar (regra 1). O corpo
    # raramente traz linha digitavel; um boleto de transporte ja teria sido rotulado
    # 'cte' com barcode valido em extract_from_email_body → nao cai aqui.
    if (_is_transport_context(email_rec.get("subject"), payload.get("supplier_name"),
                              payload.get("document_type"))
            and not _is_boleto_barcode(payload.get("barcode"))):
        log.info("    CT-e/transporte sem boleto (corpo) ignorado — nao gera conta a pagar")
        email_rec["notes"] = "CT-e/transporte sem boleto (corpo) — nao gera conta a pagar"
        return BODY_IGNORED

    # SEGURADORA sem boleto valido (mesma forma da guarda de transporte acima): o corpo
    # tem sinal financeiro, mas sem linha digitavel nao e um pagavel pela regra. Fica
    # DEPOIS do payload, e nao no topo da funcao, para nao descartar o caso legitimo em
    # que a propria seguradora escreve a linha digitavel no corpo — ai ha boleto valido
    # e a conta e criada normalmente.
    if insurance_ctx and not _is_boleto_barcode(payload.get("barcode")):
        log.info("    Seguradora sem boleto valido (corpo) ignorado — nao gera conta a pagar")
        email_rec["notes"] = "E-mail de seguradora sem boleto valido em anexo/link"
        return BODY_IGNORED

    # Mesma validacao de valor do caminho de PDF (extract_and_store_accounts):
    # sem valor nao ha conta a pagar.
    if not payload.get("amount"):
        email_rec["notes"] = "Valor ausente ou zero no corpo do e-mail"
        return BODY_NONE

    # Resolve o fornecedor (RPC) → grava sk_supplier e remove as colunas
    # denormalizadas. ANTES da dedup (que casa por sk_supplier). Falha de
    # resolucao → trata como sem pagavel utilizavel (chamador segue p/ falha).
    # 🔴 Fornecedor LE BLANC capturado ANTES do finalize, que remove supplier_name/cnpj.
    body_le_blanc = _le_blanc_supplier_signal(payload)
    if not _finalize_supplier(ctrl, payload, body_text):
        email_rec["notes"] = "Falha ao resolver fornecedor do corpo do e-mail"
        return BODY_NONE

    # Classificacao contabil FORCADA por tipo de documento (IRRF/DUIMP/ICMS Importacao/
    # transporte). Passa o corpo (body_text) como texto extra alem de assunto/descricao.
    # Roda no payload base, ANTES do bloco de parcelas — os clones herdam cost_center_id/
    # chart_account_id.
    apply_forced_classification(ctrl, payload, extra_text=body_text)

    # Empresa pagadora (LE BLANC -> ester -> LEBIANCO -> OTIMOTEX) pelo assunto/corpo/
    # remetente + fornecedor LE BLANC capturado acima. No payload BASE, antes do bloco de
    # parcelas — os clones herdam sk_company via dict(payload). Sem pdf_lebianco: este e o
    # caminho do CORPO (nao ha anexo pagavel).
    apply_sk_company(payload, body_text=body_text, le_blanc=body_le_blanc)

    # Write-back de contato (chave PIX / telefone / WhatsApp) do corpo do e-mail para
    # o cadastro do fornecedor. Best-effort, nao toca no payload. Roda no payload base,
    # antes do bloco de parcelas (o fornecedor ja esta resolvido e e o mesmo dos clones).
    apply_contact_writeback(ctrl, payload, extra_text=body_text)

    # MÚLTIPLOS boletos no corpo (tabela de parcelas com documentos/vencimentos
    # diferentes): cria UMA conta por boleto — NUNCA uma conta somada com o total
    # (regra de negócio). Reusa o fornecedor já resolvido (payload base) e sobrescreve
    # nº do documento (doc/parcela), valor, vencimento e emissão por linha. Cada linha
    # passa pela mesma dedup de conteúdo. Sem barcode (o corpo não traz a linha
    # digitável — só o PDF; por isso o caminho de PDF descriptografado é preferível).
    # Dois layouts de tabela alimentam o MESMO caminho: com parcela/dias (OBER) e sem
    # parcela (MOVVI, tabela de faturas). O de parcelas tem precedencia — o layout de
    # 6 campos e mais especifico e ja e o coberto historicamente.
    installments = _extract_body_installments(body_text) or _extract_body_invoice_table(body_text)
    if len(installments) >= 2:
        created = dups = 0
        for inst in installments:
            row = dict(payload)
            num = f"{inst['doc']}/{inst['parcela']}" if inst.get("parcela") else inst["doc"]
            row["invoice_number"] = num
            row["amount"]   = inst["amount"]
            row["due_date"] = inst["due_date"] or row.get("due_date")
            if inst.get("issue_date"):
                row["issue_date"] = inst["issue_date"]
            # Barcode POR LINHA (so a tabela de faturas o traz — a chave nem existe
            # nas parcelas, que ficam com o do payload base). Sem isto todas as
            # linhas herdariam o boleto da PRIMEIRA e colidiriam na dedup por codigo
            # de barras (impressao 1), perdendo titulos em silencio.
            if "barcode" in inst:
                row["barcode"] = inst["barcode"]
            if ctrl.find_financial_duplicate(row):
                dups += 1
                continue
            row["invoice_number"] = ctrl.unique_invoice_number(num)
            if ctrl.register_financial(row):
                created += 1
        if created:
            email_rec["notes"] = f"{created} conta(s) (parcelas) extraída(s) do corpo do e-mail"
            return BODY_CREATED
        if dups:
            email_rec["notes"] = "Parcelas do corpo já registradas (duplicata)"
            return BODY_DUPLICATE
        # Nenhuma criada nem duplicada → cai para o caminho de conta única abaixo.

    # Dedup de conteudo: o MESMO pagavel ja registrado por outro e-mail (a
    # mensagem original e seu RES:/encaminhamento, p.ex.). NAO e falha — a conta
    # existe; sinaliza para o e-mail virar 'ignorado' (duplicata), nao 'falha'.
    dup = ctrl.find_financial_duplicate(payload)
    if dup:
        dup_id = dup.get("id")
        log.info(f"    [DUP-DOC] conta do corpo já registrada (id {dup_id}) — e-mail será 'ignorado'")
        email_rec["notes"] = f"Duplicata — conta já registrada (id {dup_id})"
        email_rec["duplicate_of"] = dup_id
        return BODY_DUPLICATE

    if payload.get("invoice_number"):
        payload["invoice_number"] = ctrl.unique_invoice_number(payload["invoice_number"])

    if ctrl.register_financial(payload):
        log.info("    Conta extraída do corpo do e-mail e gravada em financial_account_control")
        return BODY_CREATED

    email_rec["notes"] = "Falha ao gravar conta extraida do corpo do e-mail"
    return BODY_NONE

# ---------------------------------------------------------------------------
# Processar um e-mail
# ---------------------------------------------------------------------------
def _parse_internaldate(fetch_meta: bytes | None) -> str | None:
    """Converte o INTERNALDATE do IMAP para ISO-8601 UTC.

    INTERNALDATE e a hora em que a mensagem chegou na caixa postal — definida
    pelo servidor, imune ao relogio (ou a header Date adulterado) do remetente.
    Usa imaplib.Internaldate2tuple (independente de locale) em vez de strptime
    com %b, que falharia sob locale pt-BR. Retorna None se ausente/invalido.
    """
    if not fetch_meta:
        return None
    try:
        tt = imaplib.Internaldate2tuple(fetch_meta)  # struct_time em horario local
        if not tt:
            return None
        epoch = time.mktime(tt)
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except Exception:
        return None


def _received_at_from(meta: bytes | None, date_header: str) -> str:
    """received_at canonico: INTERNALDATE (chegada na caixa postal) e a fonte
    primaria — confiavel mesmo com o header Date do remetente adulterado ou com
    fuso errado. O header Date e fallback; nunca grava data futura. Usada tanto no
    process_message quanto no registro de e-mails 'ignorado', para que /emails
    fique na MESMA ordem da caixa postal (o grid ordena por received_at desc)."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    internal_iso = _parse_internaldate(meta)
    try:
        header_dt = parsedate_to_datetime(date_header).astimezone(timezone.utc)
    except Exception:
        header_dt = None

    if internal_iso:
        received_at = internal_iso
    elif header_dt is not None and header_dt <= now_dt:
        received_at = header_dt.isoformat()
    else:
        received_at = now

    # Rede de seguranca: jamais gravar received_at no futuro.
    try:
        if datetime.fromisoformat(received_at) > now_dt:
            received_at = now
    except Exception:
        received_at = now
    return received_at


def email_sem_conteudo_extraivel(has_attachment: bool, pdf_links, body_text) -> bool:
    """O e-mail nao tinha NADA de onde extrair: sem anexo, sem link e sem corpo util.

    Isso NAO e uma falha do pipeline — e um e-mail que nunca poderia virar conta: thread de
    resposta vazia ("RES: boleto e nf em anexo favor confirmar"), encaminhamento sem corpo,
    aviso cujo conteudo estava so no assunto. Marcar como 'falha' os coloca em /erros, onde
    competem por atencao com extracao que REALMENTE quebrou — e nenhum deles e acionavel.

    🔴 A presenca de LINK importa: com link, o e-mail TINHA de onde extrair e o download
    fracassou — isso e falha de verdade e precisa continuar visivel em /erros. Sem esta
    condicao, a guarda mascararia justamente o caso que mais merece investigacao (portal que
    mudou, SSRF bloqueando destino legitimo, PDF que sumiu).

    Medido nos 21 e-mails em 'falha' de 2026-08-04: 11 tinham corpo VAZIO, sem anexo e sem
    link — nenhum recuperavel por `reprocess_link_emails` nem por `reprocess_body_emails`."""
    if has_attachment or pdf_links:
        return False
    # AUSENCIA de conteudo, NUNCA "corpo curto": corpo curto legitimo e a NORMA aqui —
    # "FORNECEDOR X R$ 250,00 venc 10/08" cabe em 33 chars e E um pagavel. Por isso o
    # criterio e nao sobrar UM caractere alfanumerico depois de remover espaco e
    # pontuacao. Medido: os 11 casos reais tinham o corpo literalmente vazio.
    return not re.sub(r"[\W_]+", "", str(body_text or ""), flags=re.UNICODE)


def _pdf_only_deduplicated(attachment_account: bool, accounts_saved: int) -> bool:
    """O ANEXO respondeu por um pagavel que JA existia — dedup — sem gravar conta nova.

    `attachment_account` e True quando o anexo gerou conta NOVA **ou** casou uma existente
    (`accounts_saved > 0 or dup_matches > 0`). Com `accounts_saved == 0` so resta a segunda
    hipotese, entao a informacao ja estava disponivel: nao foi preciso mudar a assinatura de
    `extract_and_store_accounts`, que 38 testes desempacotam com 4 valores.

    Existe como funcao (e nao inline no call site) para poder ser testada e para que a
    guarda de WIRING em tests/test_status_for_result.py tenha um nome a procurar."""
    return bool(attachment_account) and accounts_saved == 0


def status_for_result(has_attachment: bool, csv_generated: bool,
                       body_created: bool, pure_nfe: bool = False,
                       accounts_saved: int = 0, notification: bool = False,
                       duplicate: bool = False, nonpayable: bool = False) -> str:
    """Deriva email_control.status a partir do resultado real do processamento.

    🔴 INVARIANTE QUE GOVERNA A ORDEM — **conta gravada ⇒ status que DECLARA conta**
    ('extraído' quando veio do PDF, 'recebido' quando veio do corpo). Nenhum sinal que
    descreve o DOCUMENTO ('pure_nfe', 'nonpayable') ou a AUSENCIA de resultado
    ('duplicate', 'has_attachment', 'notification') pode ser avaliado antes dos dois
    sinais de conta, porque nenhum deles refuta uma conta que existe no banco.

    Prioridade: conta do PDF > conta do corpo > NF-e pura sem conta > nao-pagavel >
    CSV sem conta nova > duplicidade > anexo sem conta > notificacao > falha.

      - accounts_saved -> 'extraído'  (conta(s) a pagar gravada(s) do PDF)
      - body_created   -> 'recebido'  (conta extraida do corpo do e-mail)
      - pure_nfe       -> 'ignorado'  (assunto NF-e/NFS-e puro, sem pagavel e sem
                                       conta: notificacao fiscal, nao e conta a pagar)
      - nonpayable     -> 'ignorado'  (CT-e/transporte sem boleto — documento fiscal,
                                       nao e conta a pagar; vem ANTES de csv_generated
                                       porque o PDF do CT-e gera CSV sem conta)
      - csv_generated  -> 'extraído'  (PDF lido — conta nova ou reemissao deduplicada)
      - duplicate      -> 'duplicidade'  (pagavel do corpo duplica conta ja registrada)
      - has_attachment -> 'pendente'  (PDF salvo, aguardando reprocessamento)
      - notification   -> 'ignorado'  (sem anexo/conta: notificacao/aviso, nao pagavel)
      - nenhum         -> 'falha'     (casou keyword, mas nada foi gerado)

    'accounts_saved' vem primeiro para nao esconder conta real de um e-mail cujo
    assunto parece NF-e pura mas que de fato gerou conta (NF-e + boleto). NF-e/
    NFS-e estao em SKIP_ACCOUNT_TYPES (nunca viram conta), entao 'pure_nfe' vem
    antes do 'csv_generated' que o PDF de NF-e sempre produz.
    'notification' fica no lugar do antigo 'falha' (sem anexo, sem CSV, sem conta):
    avisos/confirmacoes/informes sem pagavel viram 'ignorado' em vez de 'falha'.

    🔴 'body_created' SUBIU para o 2o lugar em 2026-08-17 (antes ficava abaixo de
    'pure_nfe'/'nonpayable'/'csv_generated'). Esses tres descrevem o **ANEXO**; o corpo
    e outra metade do e-mail, e barrar um com o outro escondia conta REAL. Caso de
    origem — e-mail 1517 (<000601dd2e4a$9cae5030$d60af090$@lebianco.com.br>): anexo
    'NF_22020.pdf' pulado por SKIP_ACCOUNT_TYPES (nonpayable_only=True) e a conta 1059
    (R$ 8.250,00) gravada pelo CORPO; o e-mail foi para 'ignorado', que em /emails
    significa "nao-financeiro, nada a fazer". Medido: **13 e-mails** no mesmo estado,
    ~R$ 80 mil em contas escondidas atras do card "Ignorados" (backfill: migration 130).
    Corrigir so o ramo 'nonpayable' seria remendo — 'pure_nfe' reproduz o MESMO bug pela
    outra porta (assunto de NF-e + pagavel no corpo). O invariante acima e o que fecha a
    classe inteira, e ele e travado exaustivamente (2^8 combinacoes) em
    tests/test_status_for_result.py::InvarianteContaGravadaTest.

    Efeito colateral deliberado: com o PDF gerando CSV **sem nenhuma conta** e o corpo
    gravando a conta, o status passa de 'extraído' para 'recebido' — mais preciso, porque
    'extraído' significa "o PDF gerou conta" e ali ele nao gerou (o gate
    `if not attachment_account` de process_message garante que o corpo so roda quando o
    anexo nao respondeu por pagavel nenhum). NAO ha perda da precedencia "o boleto sempre
    vence o corpo": ela vive naquele gate, nao nesta ordem.
    """
    if accounts_saved > 0:
        return "extraído"
    if body_created:
        return "recebido"
    if pure_nfe:
        return "ignorado"
    # CT-e/transporte sem boleto (ou NF-e/NFS-e pulada): documento nao-pagavel — vem
    # ANTES de csv_generated, pois o PDF do CT-e gera CSV mas nenhuma conta (seria
    # 'extraído', errado). Ver regra 1 do CTe.
    if nonpayable:
        return "ignorado"
    if csv_generated:
        # O PDF foi lido e gerou CSV, mas nenhuma conta NOVA saiu dele (accounts_saved==0
        # — o primeiro `if` ja retornou quando houve). Se TODAS as linhas foram
        # deduplicadas contra contas existentes, o resultado honesto e 'duplicidade':
        # antes isso caia em 'extraído', indistinguivel de "gravou conta", e foi assim que
        # a perda do boleto T.R.T (conta 847) ficou INVISIVEL — e-mail verde, sem conta e
        # sem erro em /erros. Com o status proprio, 'extraído' volta a significar "gerou
        # conta" e o caso aparece no card "Duplicidades" de /emails.
        # (A guarda `not body_created` que existia aqui virou codigo morto quando
        # 'body_created' subiu para o 2o lugar: a conta nova do corpo ja retornou acima.)
        if duplicate:
            return "duplicidade"
        return "extraído"
    # Pagável do corpo duplica conta já registrada por outro e-mail: a conta
    # existe, então NÃO é falha — vira 'duplicidade' (status próprio). Vem antes
    # de 'has_attachment' porque "já registrada" descreve melhor que "pendente".
    if duplicate:
        return "duplicidade"
    if has_attachment:
        return "pendente"
    if notification:
        return "ignorado"
    return "falha"


def _rfc822_from_fetch(data: list) -> "tuple[bytes | None, bytes]":
    """Extrai (meta, raw) do retorno de um FETCH '(INTERNALDATE RFC822)'.

    O imaplib pode INTERCALAR respostas (ex.: um FLAGS/UID isolado como item
    `bytes`) no meio do resultado. Quando o primeiro item nao e a tupla
    (meta, raw), `data[0][1]` indexa um `bytes` e devolve um INT — e
    `email.message_from_bytes(int)` chama `.decode()` internamente, quebrando com
    "'int' object has no attribute 'decode'". Por isso pegamos o primeiro item
    que seja uma tupla cujo segundo elemento sejam bytes (o conteudo RFC822)."""
    for item in data or []:
        if (isinstance(item, tuple) and len(item) >= 2
                and isinstance(item[1], (bytes, bytearray))):
            return item[0], bytes(item[1])
    raise ValueError("FETCH nao retornou conteudo RFC822")


def process_message(mail, uid: bytes, keywords: list,
                    dry_run: bool, mark_seen: bool,
                    ctrl: SupabaseControl) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    rec = {c: None for c in LOG_COLUMNS}
    rec["processed_at"] = now

    try:
        _, data = mail.uid("fetch", uid, "(INTERNALDATE RFC822)")
        meta, raw = _rfc822_from_fetch(data)
        msg = email.message_from_bytes(raw)

        uid_str      = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
        message_id   = msg.get("Message-ID", f"no-id-{uid_str}").strip()
        subject      = decode_str(msg.get("Subject", "(sem assunto)"))
        from_raw     = msg.get("From", "")
        sender_name, sender_email = parseaddr(from_raw)
        sender_name  = decode_str(sender_name) or sender_email
        date_header  = msg.get("Date", "")

        # received_at canonico (INTERNALDATE -> header Date -> agora; nunca futuro).
        received_at = _received_at_from(meta, date_header)

        body_text    = get_body_text(msg)
        # E-mail SO-HTML (ex.: Correios): sem texto plano, extrai do HTML para que
        # o fallback de corpo (try_extract_from_body) encontre valor/fatura/etc.
        #
        # O 2o caso — texto plano de PLACEHOLDER — foi descoberto em 2026-08-03: a
        # plataforma SSW manda um text/plain de 55 chars dizendo que o conteudo esta em
        # HTML. Como ele NAO e vazio, o fallback antigo (`if not body_text`) nunca
        # disparava: 29 e-mails gravaram esse aviso como se fosse o corpo, a guarda do
        # cedente (_ssw_cedente_from_body) nunca teve texto para ler e o `body_full` da
        # Onda 2 ficou inutil para a busca do chat de IA.
        #
        # So substitui quando o HTML rende algo — senao um HTML vazio/ilegivel apagaria
        # ate o pouco que havia.
        if not body_text or _plain_body_is_placeholder(body_text):
            html_text = _html_to_text(get_body_html(msg))
            if html_text:
                body_text = html_text
        keyword_hit  = match_keyword(subject, keywords)

        rec.update({
            "message_id":     message_id,
            "received_at":    received_at,
            "sender_name":    sender_name,
            "sender_email":   sender_email,
            "subject":        subject,
            # body_preview segue truncado — e o preview que a tela /emails mostra.
            # body_full guarda o texto INTEIRO (migration 105): ate a Onda 2, 53% dos corpos eram
            # perdidos aqui, e o texto so existia no IMAP.
            "body_preview":   body_text[:500].replace("\n", " "),
            "body_full":      _body_full_for_storage(body_text),
            "keyword_matched": keyword_hit,
        })

        if dry_run:
            rec["notes"] = "dry-run"
            return rec

        # Baixar PDFs e acionar extração
        saved_pdfs = save_attachments(msg, sender_email, subject, received_at)

        # Sem anexo direto: tentar baixar PDF de links no corpo do e-mail.
        # `pdf_links` PRECISA nascer aqui, fora do `if`: ele é lido incondicionalmente
        # lá embaixo, por `email_sem_conteudo_extraivel(has_att, pdf_links, body_text)`.
        # Atribuído só dentro do ramo, o e-mail COM ANEXO (o caminho principal do
        # pipeline!) levantava UnboundLocalError depois de a conta já estar gravada —
        # a conta existia, mas o e-mail virava 'falha' e ia para /erros. A guarda de
        # wiring de tests/test_email_sem_pagavel.py não pegava: ela lê o TEXTO de
        # process_message e prova que a chamada existe, não que ela executa.
        pdf_links: list[str] = []
        link_downloaded = False
        if not saved_pdfs:
            body_html = get_body_html(msg)
            pdf_links = extract_pdf_links(body_text, body_html)
            if pdf_links:
                log.info(f"    {len(pdf_links)} link(s) candidato(s) encontrado(s) no corpo")
            else:
                log.info("    Sem links candidatos no corpo do e-mail")
            for url in pdf_links:
                log.info(f"    Tentando link: {url[:80]}")
                pdf_path = download_pdf_from_url(url, sender_email, subject, received_at)
                if pdf_path:
                    saved_pdfs.append(pdf_path)
                    link_downloaded = True

        # Sem PDF (anexo nem link): tenta a MAIOR imagem INLINE do corpo (recibo/
        # comprovante colado), lida via Claude Vision. Prioridade: anexo → link →
        # imagem inline → corpo. Fora desse fallback, imagens inline (logos de
        # assinatura) nunca são processadas — evita chamadas Vision desnecessárias.
        inline_image = False
        if not saved_pdfs:
            inline_imgs = save_inline_images(msg, sender_email, subject, received_at)
            if inline_imgs:
                saved_pdfs.extend(inline_imgs)
                inline_image = True

        att_names = [p.name for p in saved_pdfs]
        has_att   = len(saved_pdfs) > 0

        rec["has_attachment"]   = has_att
        rec["attachment_names"] = " | ".join(att_names) if att_names else None
        rec["attachment_saved"] = has_att
        if link_downloaded:
            rec["notes"] = "PDF baixado de link no corpo do e-mail"
        elif inline_image:
            rec["notes"] = "Imagem inline do corpo lida via Vision"

        csvs_ok, accounts_saved, nonpayable_only, attachment_account = extract_and_store_accounts(
            saved_pdfs, message_id, ctrl, email_rec=rec, body_text=body_text)

        csv_generated = len(csvs_ok) > 0
        rec["pdf_extracted"]  = csv_generated
        rec["extraction_csv"] = " | ".join(csvs_ok) if csvs_ok else None
        if accounts_saved:
            log.info(f"    {accounts_saved} conta(s) gravada(s) em financial_account_control")

        if not has_att:
            rec["notes"] = "Sem anexo PDF — registrado para revisão"

        # Corpo é fallback SOMENTE quando o anexo NÃO respondeu por nenhum pagável.
        # "O boleto sempre vence o corpo": `attachment_account` cobre tanto a conta
        # NOVA gravada quanto o boleto DEDUPLICADO contra uma conta já existente
        # (mesmo documento por outro e-mail). Antes o gate usava só accounts_saved,
        # então um boleto deduplicado (accounts_saved==0) deixava o corpo criar uma
        # conta espúria com vencimento divergente — ex.: id 510 (OBER) duplicou o
        # boleto id 159 com data errada. try_extract_from_body valida fornecedor+valor.
        body_outcome = BODY_NONE
        if not attachment_account:
            body_outcome = try_extract_from_body(rec, body_text, received_at, message_id, ctrl,
                                                 sender_email=sender_email)
            if body_outcome == BODY_CREATED:
                rec["pdf_extracted"] = True
                rec["notes"]         = "Conta extraída do corpo do e-mail"
        body_created = body_outcome == BODY_CREATED

        # Status (CHECK migration 022): conta do PDF > conta do corpo > duplicata >
        # anexo sem conta. A conta vinda do corpo (body_created) prevalece sobre o
        # anexo que nao gerou CSV; uma duplicata do corpo (pagavel ja registrado
        # por outro e-mail) vira 'ignorado' em vez de 'falha'.
        rec["status"] = status_for_result(
            has_att, csv_generated, body_created,
            pure_nfe=subject_is_pure_nfe(subject), accounts_saved=accounts_saved,
            # 'notification' produz 'ignorado' quando nao houve anexo/CSV/conta. Alem do
            # assunto de aviso, entram aqui: remetente de subdominio descartavel
            # (phishing que IMITA cobranca) e e-mail sem nada de onde extrair.
            notification=(subject_is_ignorable_notification(subject)
                          or is_disposable_sender(sender_email)
                          or email_sem_conteudo_extraivel(has_att, pdf_links, body_text)),
            duplicate=(body_outcome == BODY_DUPLICATE
                       or _pdf_only_deduplicated(attachment_account, accounts_saved)),
            nonpayable=(nonpayable_only or body_outcome == BODY_IGNORED))

        # Regra: TODA falha gera log em email_processing_errors para revisão —
        # inclusive o corpo sem sinal financeiro, que antes saía silencioso.
        # try_extract_from_body anota o motivo em rec["notes"]. O caminho de
        # exceção tem seu próprio log abaixo (não passa por aqui).
        if rec["status"] == "falha":
            ctrl.register_error(
                rec, "falha_processamento",
                rec.get("notes") or "E-mail casou keyword mas nenhuma conta foi gerada",
                raw_payload={
                    "subject":         rec.get("subject"),
                    "keyword_matched": rec.get("keyword_matched"),
                    "has_attachment":  rec.get("has_attachment"),
                    "body_preview":    rec.get("body_preview"),
                },
            )

        if mark_seen:
            mail.uid("store", uid, "+FLAGS", "\\Seen")

    except ApiUnavailableError:
        # Erro de API ja registrado em email_processing_errors. Propaga para
        # interromper o run com seguranca: NAO grava em email_control nem no CSV,
        # entao o e-mail sera reprocessado quando a API voltar.
        raise
    except Exception as e:
        rec["notes"] = f"Erro: {str(e)[:200]}"
        log.exception(f"  Erro UID {uid}: {e}")
        ctrl.register_error(rec, "processamento_erro", str(e))

    # O status já foi definido em process_message (extraído/recebido/pendente/falha).
    # No caminho de exceção fica a cargo de _derive_status (→ 'falha').
    ctrl.register(rec)
    append_log_csv(rec)
    return rec


# ---------------------------------------------------------------------------
# Execução reutilizável (CLI + API)
# ---------------------------------------------------------------------------

# Timeout de socket do IMAP (segundos). SEM ele, um fetch que estanca (mensagem
# muito grande, hiccup do servidor) bloqueia o run inteiro indefinidamente — foi
# o que travou um run no e-mail de 3 faturas + XMLs. Com timeout, a operação
# levanta socket.timeout, o e-mail é pulado/erra e o run segue em vez de congelar.
IMAP_TIMEOUT_SECONDS = int(os.getenv("IMAP_TIMEOUT", "120"))

# Retry/backoff para falhas TRANSITÓRIAS do IMAP (timeout de socket, conexão
# derrubada). Sem isso, um hiccup de rede no connect/select/search derrubava o
# run inteiro; com retry, a sequência é refeita com espera crescente.
IMAP_MAX_ATTEMPTS  = int(os.getenv("IMAP_MAX_ATTEMPTS", "3"))
IMAP_RETRY_BACKOFF = float(os.getenv("IMAP_RETRY_BACKOFF", "5"))

# Falhas transitórias: socket.timeout é subclasse de OSError/TimeoutError;
# imaplib.IMAP4.abort sinaliza conexão abortada pelo servidor. NÃO inclui
# imaplib.IMAP4.error puro (login/mailbox inválidos) — retry ali é inútil.
_IMAP_TRANSIENT = (socket.timeout, OSError, imaplib.IMAP4.abort)


def _connect_imap() -> imaplib.IMAP4_SSL:
    """Conecta/autentica no IMAP com timeout de socket e seleciona a caixa."""
    mail = imaplib.IMAP4_SSL(
        os.getenv("IMAP_HOST"),
        int(os.getenv("IMAP_PORT", 993)),
        timeout=IMAP_TIMEOUT_SECONDS,
    )
    mail.login(os.getenv("IMAP_USER"), os.getenv("IMAP_PASS"))
    mail.select(os.getenv("IMAP_MAILBOX", "INBOX"))
    return mail


def _safe_logout(mail) -> None:
    """Fecha a conexão IMAP ignorando erros (usado entre tentativas de retry)."""
    if mail is None:
        return
    try:
        mail.logout()
    except Exception:
        pass


def _connect_and_search(criteria: str):
    """Conecta no IMAP e roda o `search`, com retry/backoff em falha transitória.

    Cobre connect + select + search numa única unidade resiliente: um timeout ou
    queda em qualquer dessas etapas refaz a sequência (nova conexão) até
    IMAP_MAX_ATTEMPTS, com espera de IMAP_RETRY_BACKOFF * tentativa. Erro de
    protocolo/login (imaplib.IMAP4.error) NÃO é repetido. Retorna (mail, uids);
    levanta RuntimeError ao esgotar (o chamador HTTP devolve 502)."""
    last_err = None
    for attempt in range(1, IMAP_MAX_ATTEMPTS + 1):
        mail = None
        try:
            mail = _connect_imap()
            _, uids_data = mail.uid("search", None, criteria)
            uids = uids_data[0].split() if uids_data and uids_data[0] else []
            log.info(f"IMAP conectado (tentativa {attempt}/{IMAP_MAX_ATTEMPTS})")
            return mail, uids
        except _IMAP_TRANSIENT as ex:
            # Transitório (timeout/queda/abort) — refaz a sequência abaixo.
            # _IMAP_TRANSIENT inclui IMAP4.abort, por isso vem ANTES de IMAP4.error.
            last_err = ex
        except imaplib.IMAP4.error as ex:
            # Protocolo/login (credenciais, mailbox inválida): retry é inútil.
            _safe_logout(mail)
            raise RuntimeError(f"Falha na conexão IMAP: {ex}") from ex

        _safe_logout(mail)
        if attempt < IMAP_MAX_ATTEMPTS:
            wait = IMAP_RETRY_BACKOFF * attempt
            log.warning(
                f"  IMAP transitório (tentativa {attempt}/{IMAP_MAX_ATTEMPTS}): "
                f"{last_err} — novo try em {wait:.0f}s"
            )
            time.sleep(wait)

    log.error(f"Falha IMAP após {IMAP_MAX_ATTEMPTS} tentativas: {last_err}")
    raise RuntimeError(
        f"Falha na conexão IMAP após {IMAP_MAX_ATTEMPTS} tentativas: {last_err}"
    ) from last_err


def _register_ignored(ctrl, msg_id, subject, sender_name, sender_email,
                      received_at, notes):
    """Registra um e-mail como 'ignorado' (sem baixar/extrair). Compartilhado pelo
    filtro de remetente de sistema (postmaster@) e pelo filtro de assunto."""
    ctrl.register({
        "message_id":      msg_id,
        "subject":         subject,
        "sender_name":     decode_str(sender_name) or sender_email,
        "sender_email":    sender_email,
        "received_at":     received_at,
        "keyword_matched": None,
        "has_attachment":  None,   # desconhecido — não baixamos o corpo
        "status":          "ignorado",
        "notes":           notes,
        "processed_at":    datetime.now(timezone.utc).isoformat(),
    })


def run_reader(days: int = 0, all_: bool = False,
               dry_run: bool = False, mark_seen: bool = False,
               on_progress=None) -> dict:
    """
    Lê a caixa IMAP, filtra/deduplica e processa os e-mails financeiros.

    Reutilizável tanto pelo CLI (main) quanto pelo backend HTTP (server/app.py).
    Retorna um dicionário-resumo da execução. Lança RuntimeError em falha de IMAP
    (em vez de sys.exit) para que o chamador HTTP possa devolver um erro tratado.

    on_progress: callback opcional `(dict) -> None` chamado a cada e-mail com o
    progresso atual ({phase, total, done, processed, skipped_keyword, skipped_dup}).
    Best-effort — exceções no callback são engolidas e nunca derrubam o run. Usado
    pelo Flask para servir GET /api/emails/progress; o CLI não passa callback.
    """
    kw_env   = os.getenv("EMAIL_KEYWORDS", "")
    keywords = [k.strip() for k in kw_env.split(",")] if kw_env else KEYWORDS_DEFAULT

    # Inicializar controle Supabase
    ctrl = SupabaseControl()
    supabase_ok = ctrl._available

    # Dedup em lote: carrega todos os IDs já processados em um set local.
    # Substitui N chamadas HTTP individuais por uma única consulta — elimina
    # a latência por e-mail no loop de deduplicação.
    known_ids = ctrl.load_known_ids()

    log.info("=" * 58)
    log.info("  email-reader v2.0 — iniciando")
    log.info(f"  Conta    : {os.getenv('IMAP_USER')}")
    log.info(f"  Controle : {'✓ Supabase' if supabase_ok else '✗ Supabase (fallback CSV)'}")
    log.info(f"  Dedup    : {len(known_ids)} IDs em cache")
    log.info(f"  Keywords : {len(keywords)} configuradas")
    log.info(f"  Modo     : {'dry-run' if dry_run else 'processamento completo'}")
    log.info("=" * 58)

    # Critério de busca (montado antes de conectar p/ o retry cobrir o search).
    if all_:
        criteria = "ALL"
    elif days > 0:
        since    = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        criteria = f'SINCE "{since}"'
    else:
        criteria = "UNSEEN"

    # connect + select + search com retry/backoff em falha transitória.
    mail, uids = _connect_and_search(criteria)
    log.info(f"E-mails no servidor ({criteria}): {len(uids)}")

    processed = skipped_kw = skipped_dup = 0
    new_subjects = []
    api_aborted = False
    total = len(uids)

    def _emit(phase: str, done: int) -> None:
        """Reporta progresso ao callback (best-effort — nunca derruba o run)."""
        if not on_progress:
            return
        try:
            on_progress({
                "phase": phase, "total": total, "done": done,
                "processed": processed, "skipped_keyword": skipped_kw,
                "skipped_dup": skipped_dup,
            })
        except Exception:  # noqa: BLE001 — progresso é informativo, não crítico
            pass

    _emit("lendo", 0)
    # try/finally garante mail.logout() mesmo se uma exceção escapar do loop —
    # sem isso, uma falha não-ApiUnavailableError deixaria a conexão IMAP aberta.
    try:
        for idx, uid in enumerate(uids, 1):
            _emit("lendo", idx - 1)  # done = e-mails já concluídos antes deste
            try:
                # Header enxuto para filtrar/registrar rapidamente (inclui remetente e
                # data, necessários para registrar também os e-mails 'ignorado').
                # INTERNALDATE incluido no fetch para registrar 'ignorado' com a data de
                # CHEGADA na caixa (mesma fonte do process_message) — alinha /emails ao
                # webmail. _rfc822_from_fetch tolera respostas IMAP intercaladas.
                _, hdr = mail.uid("fetch", uid,
                                  "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID FROM DATE)])")
                hdr_meta, hdr_raw = _rfc822_from_fetch(hdr)
                hdr_msg = email.message_from_bytes(hdr_raw)
                subject = decode_str(hdr_msg.get("Subject", ""))
                msg_id  = hdr_msg.get("Message-ID", "").strip()
            except Exception as e:
                log.warning(f"  [SKIP] UID {uid} — erro ao ler header: {e}")
                skipped_kw += 1
                continue

            # Deduplicação PRIMEIRO: set em memória (O(1)); fallback para consulta
            # individual se o lote não foi carregado (Supabase indisponível). Vem antes
            # do filtro de keyword para não re-registrar e-mails já conhecidos.
            is_dup = (msg_id in known_ids) if known_ids else (msg_id and ctrl.is_processed(msg_id))
            if is_dup:
                log.info(f"  [DUP] {subject[:65]}")
                skipped_dup += 1
                continue

            # Remetente e data (canonico = INTERNALDATE) — usados pelos filtros abaixo
            # e pelo registro 'ignorado', mantendo /emails na MESMA ordem do webmail.
            sender_name, sender_email = parseaddr(hdr_msg.get("From", ""))
            received_at = _received_at_from(hdr_meta, hdr_msg.get("Date", ""))

            # Remetente de SISTEMA (postmaster@ etc.) → 'ignorado' sem baixar/extrair,
            # MESMO com keyword no assunto (NDR/bounce/aviso nao e conta a pagar).
            if is_ignored_sender(sender_email):
                if not dry_run:
                    _register_ignored(ctrl, msg_id, subject, sender_name, sender_email,
                                      received_at, f"Remetente de sistema ignorado ({sender_email})")
                log.info(f"  [IGN remetente] {subject[:55]}")
                skipped_kw += 1
                continue

            # Confirmacao/comprovante de PAGAMENTO (pagamento ja realizado) → 'ignorado'
            # sem baixar/extrair, MESMO com keyword no assunto: nao e conta a pagar.
            if subject_is_payment_confirmation(subject):
                if not dry_run:
                    _register_ignored(ctrl, msg_id, subject, sender_name, sender_email,
                                      received_at, "Confirmação de pagamento — pagamento já realizado (não é conta a pagar)")
                log.info(f"  [IGN confirmação pgto] {subject[:55]}")
                skipped_kw += 1
                continue

            # Assunto com "lembrete" → 'ignorado' sem baixar/extrair, MESMO com keyword/anexo:
            # e so um lembrete/aviso, nao a conta a pagar (decisao do usuario).
            if subject_is_reminder(subject):
                if not dry_run:
                    _register_ignored(ctrl, msg_id, subject, sender_name, sender_email,
                                      received_at, "Lembrete/aviso — não é conta a pagar")
                log.info(f"  [IGN lembrete] {subject[:55]}")
                skipped_kw += 1
                continue

            # Fora do filtro de assunto → registra como 'ignorado' (sem baixar/extrair),
            # para que /emails reflita a caixa inteira (o app substitui abrir o webmail).
            if not match_keyword(subject, keywords):
                if not dry_run:
                    _register_ignored(ctrl, msg_id, subject, sender_name, sender_email,
                                      received_at, "Fora do filtro de assunto (não-financeiro)")
                log.info(f"  [IGN] {subject[:65]}")
                skipped_kw += 1
                continue

            log.info(f"  [NEW] {subject[:65]}")
            try:
                process_message(mail, uid, keywords, dry_run, mark_seen, ctrl)
            except ApiUnavailableError as e:
                log.error("=" * 58)
                log.error("  PIPELINE INTERROMPIDO — API Anthropic indisponível.")
                log.exception(f"  Motivo: {str(e)[:160]}")
                log.error("  Nenhum dado adicional gravado. Recarregue os créditos "
                          "e rode novamente.")
                log.error("=" * 58)
                api_aborted = True
                break
            processed += 1
            new_subjects.append(subject[:120])

        _emit("concluído", total)
    finally:
        # Fechar a conexão nunca deve mascarar o erro original do run.
        try:
            mail.logout()
        except Exception:  # noqa: BLE001 — logout best-effort
            pass

    log.info("=" * 58)
    log.info(f"  Novos processados : {processed}")
    log.info(f"  Sem palavra-chave : {skipped_kw}")
    log.info(f"  Duplicados (skip) : {skipped_dup}")
    if api_aborted:
        log.info("  Interrompido      : API Anthropic indisponível")
    log.info(f"  Log local         : {EMAILS_LOG}")
    log.info("=" * 58)

    return {
        "imap_user":       os.getenv("IMAP_USER"),
        "supabase_ok":     supabase_ok,
        "criteria":        criteria,
        "found":           len(uids),
        "processed":       processed,
        "skipped_keyword": skipped_kw,
        "skipped_dup":     skipped_dup,
        "new_subjects":    new_subjects,
        "dry_run":         dry_run,
        "api_aborted":     api_aborted,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Lê e-mails financeiros via IMAP — controle Supabase"
    )
    parser.add_argument("--days",      type=int, default=0,
        help="Processar e-mails dos últimos N dias (0 = não lidos)")
    parser.add_argument("--all",       action="store_true",
        help="Processar TODOS os e-mails (ignorando flag UNSEEN)")
    parser.add_argument("--dry-run",   action="store_true",
        help="Listar sem baixar anexos nem registrar")
    parser.add_argument("--mark-seen", action="store_true",
        help="Marcar e-mails como lidos após processar")
    args = parser.parse_args()

    try:
        run_reader(days=args.days, all_=args.all,
                   dry_run=args.dry_run, mark_seen=args.mark_seen)
        # Encerramento normal: remove arquivo de crash vazio para não poluir logs/
        try:
            _crash_file.close()
            if _CRASH_LOG.stat().st_size < 200:
                _CRASH_LOG.unlink(missing_ok=True)
        except Exception:
            pass
    except RuntimeError as e:
        log.exception(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
