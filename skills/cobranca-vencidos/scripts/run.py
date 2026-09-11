"""
run.py -- Entry-point da skill cobranca-vencidos
Invocado diretamente pelo Windows Task Scheduler as 10:00.

Uso:
    py -3 skills/cobranca-vencidos/scripts/run.py
    py -3 skills/cobranca-vencidos/scripts/run.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
import traceback
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path -- diretório dos scripts da skill (mesmo padrão de server/app.py com
# read_emails): a pasta tem hífen (cobranca-vencidos), inválido como nome de
# pacote Python, então os módulos irmãos são importados como top-level.
# ---------------------------------------------------------------------------
SCRIPTS_DIR  = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# .env -- antes de qualquer import da skill
# ---------------------------------------------------------------------------
from dotenv import load_dotenv  # noqa: E402
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Imports da skill (módulos irmãos em SCRIPTS_DIR)
# ---------------------------------------------------------------------------
from db_firebird   import fetch_titulos_vencidos   # noqa: E402
from email_sender  import SmtpSession              # noqa: E402
from failure_notify import (                       # noqa: E402
    DEFINITIVE_ERROR_TYPES,
    build_subject,
    group_by_cc,
    render_failure_digest,
)
from send_core     import SendResult, send_and_log, validate_email  # noqa: E402
from supabase_log  import (                        # noqa: E402
    already_sent,
    delete_erro_rows_by_document_id,
    fetch_company_smtp,
    fetch_error_document_ids,
    log_envio_erro,
)

# ---------------------------------------------------------------------------
# Logging -- arquivo rotativo 30 dias + console
# ---------------------------------------------------------------------------
LOG_FILE = PROJECT_ROOT / "logs" / "cobranca_vencidos.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            LOG_FILE, when="D", interval=1, backupCount=30, encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("cobranca-vencidos")

# Tipos de erro de DADO do cliente (cadastro no Firebird) — NÃO reprovam a tarefa
# agendada: são registrados em cobranca_erros_log e notificados ao representante, mas
# o run cumpriu seu papel (não há o que enviar para quem não tem e-mail). Só erros
# OPERACIONAIS (SMTP recusado/bloqueado, Supabase, inesperado) fazem o processo sair
# com código != 0 — o que o wrapper .ps1 marca como "CRASH / ERRO" e o Agendador como 0x1.
DATA_ERROR_TYPES = frozenset({"email_ausente", "email_invalido"})


def compute_exit_code(error_types: Mapping[str, int]) -> int:
    """Decide o exit code do run a partir da contagem de erros por tipo.

    Retorna 0 quando os únicos erros são de DADO (cliente sem e-mail / e-mail inválido)
    — condição de cadastro, não falha do sistema — e 1 quando houve ao menos um erro
    OPERACIONAL (qualquer tipo fora de DATA_ERROR_TYPES). Mantém a tarefa "verde" no
    Agendador quando ela rodou até o fim e só esbarrou em clientes sem e-mail.
    """
    operational = sum(n for t, n in error_types.items() if t not in DATA_ERROR_TYPES)
    return 1 if operational > 0 else 0


def _email_error_type(primary_email) -> str:
    return "email_ausente" if not (primary_email or "").strip() else "email_invalido"


def _log_email_error(titulo, motivo: str | None, *, dry_run: bool) -> None:
    """Registra falha de e-mail (ausente/inválido) em cobranca_erros_log."""
    doc_id = titulo.document_id
    error_type = _email_error_type(titulo.primary_email)
    logger.warning("[ERRO] %s -- %s", doc_id, motivo)
    if not dry_run:
        log_envio_erro(
            error_type=error_type,
            error_message=motivo or "E-mail do cliente indisponível.",
            error_detail=f"primary_email={titulo.primary_email!r}",
            document_id=doc_id, customer_name=titulo.customer_name,
            primary_email=titulo.primary_email, cc_email=titulo.cc_email,
            due_date=titulo.due_date, bill_amount=titulo.bill_amount,
            email_subject=titulo.email_subject,
        )


def _cleanup_resolved_errors(document_id) -> None:
    """Remove as linhas de erro antigas de um título resolvido (enviado agora ou que já
    constava enviado). Best-effort: falha aqui não derruba o run — os e-mails já saíram."""
    try:
        delete_erro_rows_by_document_id(document_id)
    except Exception:  # noqa: BLE001 — limpeza best-effort, não interrompe o lote
        logger.warning("Falha ao limpar erros resolvidos do título %s (envio OK, seguindo).", document_id)


def _send_titulo(titulo, *, dev_mode: bool, dev_override: str, company_row, session) -> SendResult:
    """Adapta um TituloVencido para o núcleo de envio compartilhado (send_core)."""
    return send_and_log(
        document_id=titulo.document_id, customer_name=titulo.customer_name,
        primary_email=titulo.primary_email, cc_email=titulo.cc_email,
        due_date=titulo.due_date, bill_amount=titulo.bill_amount,
        email_subject=titulo.email_subject, company_row=company_row,
        dev_mode=dev_mode, dev_override=dev_override, session=session,
    )


def _process_titulo(titulo, *, dry_run: bool, dev_mode: bool, dev_override: str, company_row, session) -> SendResult:
    """Processa um título do começo ao fim. Retorna SendResult (status sent/skipped/error;
    em erro, com error_type + motivo para a notificação ao representante)."""
    doc_id = titulo.document_id

    if not dry_run and already_sent(doc_id):
        logger.info("[SKIP] %s -- ja consta em cobranca_envios_log.", doc_id)
        return SendResult("skipped")

    email_ok, email_motivo = validate_email(titulo.primary_email)
    if not email_ok:
        _log_email_error(titulo, email_motivo, dry_run=dry_run)
        return SendResult("error", _email_error_type(titulo.primary_email), email_motivo)

    if dry_run:
        logger.info("[DRY-RUN] Enviaria: doc_id=%s to=%s cc=%s subject=%r",
            doc_id, titulo.primary_email, titulo.cc_email, titulo.email_subject)
        return SendResult("sent")

    return _send_titulo(titulo, dev_mode=dev_mode, dev_override=dev_override,
        company_row=company_row, session=session)


def _process_titulo_safe(titulo, *, dry_run: bool, dev_mode: bool, dev_override: str, company_row, session) -> SendResult:
    """Rede de segurança: envolve _process_titulo para que NENHUMA falha de um título
    interrompa os demais. Erros previstos (SMTP/e-mail) já são tratados lá dentro; isto
    captura qualquer exceção inesperada (render, dado ruim), registra como erro_inesperado
    e segue para o próximo título."""
    try:
        return _process_titulo(
            titulo, dry_run=dry_run, dev_mode=dev_mode,
            dev_override=dev_override, company_row=company_row, session=session,
        )
    except Exception:
        doc_id = getattr(titulo, "document_id", None)
        logger.exception("Falha inesperada no título %s — seguindo para o próximo.", doc_id)
        if not dry_run:
            log_envio_erro(
                error_type="erro_inesperado",
                error_message="Ocorreu um erro inesperado ao processar este título.",
                error_detail=traceback.format_exc(),
                document_id=doc_id, customer_name=getattr(titulo, "customer_name", None),
                primary_email=getattr(titulo, "primary_email", None),
                cc_email=getattr(titulo, "cc_email", None),
                due_date=getattr(titulo, "due_date", None),
                bill_amount=getattr(titulo, "bill_amount", None),
                email_subject=getattr(titulo, "email_subject", None),
            )
        # erro_inesperado NÃO é falha definitiva — não notifica o CC (só as previstas).
        return SendResult("error", "erro_inesperado", "Erro inesperado ao processar o título.")


def _notify_failures(session, failures: list[dict], send_delay: float) -> None:
    """Envia ao representante (CC) um resumo das falhas DEFINITIVAS dos seus títulos.
    Best-effort: uma falha no envio da notificação não derruba o run (os e-mails de
    cobrança já saíram). Throttle entre representantes, como nos envios."""
    by_cc = group_by_cc(failures)
    sem_cc = len(failures) - sum(len(v) for v in by_cc.values())
    if sem_cc:
        logger.warning("%d falha(s) definitiva(s) sem CC — sem representante para notificar.", sem_cc)
    enviados = 0
    total = len(by_cc)
    for i, (cc, itens) in enumerate(by_cc.items()):
        try:
            session.send(to_email=cc, cc_email=None,
                subject=build_subject(len(itens)), html_body=render_failure_digest(itens))
            enviados += 1
        except Exception:  # noqa: BLE001 — notificação best-effort
            logger.warning("Falha ao notificar o representante %s sobre %d cobrança(s).", cc, len(itens))
        if send_delay > 0 and i < total - 1:
            time.sleep(send_delay)
    logger.info("Notificações de falha enviadas a %d/%d representante(s).", enviados, total)


def _record_result(
    result, titulo, *, counts: dict[str, int], error_types: Counter[str],
    failures: list[dict], error_doc_ids: set[str],
) -> None:
    """Contabiliza um resultado: counts, error_types, limpeza de erro resolvido e a
    coleta de falhas definitivas (para notificar o CC). Sem efeito de throttle/SMTP."""
    counts[result.status] += 1
    if result.status == "error":
        error_types[result.error_type or "erro_inesperado"] += 1
    # Título resolvido (enviado agora ou já enviado) que antes tinha erro: limpa o log.
    if result.status in ("sent", "skipped") and titulo.document_id in error_doc_ids:
        _cleanup_resolved_errors(titulo.document_id)
    if result.status == "error" and result.error_type in DEFINITIVE_ERROR_TYPES:
        failures.append({
            "cc_email": titulo.cc_email, "customer_name": titulo.customer_name,
            "document_id": titulo.document_id, "due_date": titulo.due_date,
            "bill_amount": titulo.bill_amount, "motivo": result.motivo,
        })


def _run_batch(
    titulos, *, dry_run: bool, dev_mode: bool, dev_override: str,
    company_row, send_delay: float, error_doc_ids: set[str],
) -> tuple[dict[str, int], Counter[str]]:
    """Processa o lote de títulos com UMA conexão SMTP (lazy) e notifica os
    representantes das falhas definitivas ao fim. Retorna (counts, error_types).

    counts: {sent, skipped, error}. error_types: contagem por error_type (base do
    exit code). A sessão SMTP é sempre fechada no finally.
    """
    counts = {"sent": 0, "skipped": 0, "error": 0}
    error_types: Counter[str] = Counter()
    # Falhas DEFINITIVAS (exigem ação humana) acumuladas para notificar o representante (CC)
    # ao fim do run — resumo por CC. Transitórias (smtp_falha) re-tentam e não entram aqui.
    failures: list[dict] = []
    # Uma única conexão SMTP para todo o lote (lazy: só conecta no 1º envio real).
    # Em dry-run não há envio, então não abre sessão.
    session = None if dry_run else SmtpSession(
        company_row, dev_mode=dev_mode, dev_override=dev_override)
    try:
        for titulo in titulos:
            result = _process_titulo_safe(
                titulo, dry_run=dry_run, dev_mode=dev_mode,
                dev_override=dev_override, company_row=company_row, session=session,
            )
            _record_result(
                result, titulo, counts=counts, error_types=error_types,
                failures=failures, error_doc_ids=error_doc_ids,
            )
            if send_delay > 0 and not dry_run and result.status in ("sent", "error"):
                time.sleep(send_delay)

        # Notifica os representantes das falhas definitivas (resumo por CC).
        if failures and not dry_run and session is not None:
            _notify_failures(session, failures, send_delay)
        elif failures and dry_run:
            logger.info("[DRY-RUN] %d falha(s) definitiva(s) gerariam notificação ao CC.", len(failures))
    finally:
        if session is not None:
            session.close()

    return counts, error_types


def main(dry_run: bool = False) -> int:
    logger.info("=" * 60)
    logger.info("Iniciando skill cobranca-vencidos | dry_run=%s", dry_run)
    logger.info("=" * 60)

    dev_mode     = os.environ.get("DEV_MODE", "false").lower() == "true"
    dev_override = os.environ.get("DEV_OVERRIDE_EMAIL", "").strip()

    if dev_mode:
        if not dev_override:
            logger.error("DEV_MODE=true mas DEV_OVERRIDE_EMAIL nao definido. Abortando.")
            return 1
        logger.warning("DEV_MODE ativo -- todos os enviosirao para: %s", dev_override)

    company_row = fetch_company_smtp()
    logger.info(
        "Config SMTP: %s",
        f"tabela company ({company_row.get('email')})" if company_row else "fallback .env",
    )

    try:
        titulos = fetch_titulos_vencidos()
    except Exception as exc:
        logger.exception("Falha critica ao consultar Firebird.")
        log_envio_erro(
            error_type="firebird_falha",
            error_message="Não foi possível consultar os títulos vencidos no sistema financeiro.",
            error_detail=f"{exc}\n\n{traceback.format_exc()}",
        )
        return 1  # falha operacional (não conseguiu nem ler os títulos) → tarefa vermelha

    if not titulos:
        logger.info("Nenhum titulo vencido encontrado. Encerrando.")
        return 0

    # Throttle anti-bloqueio (boa prática Locaweb): pausa de alguns segundos ENTRE envios
    # reais para não saturar a fila de saída (451 "queue file write error" sob rajada).
    # Configurável por COBRANCA_SEND_DELAY_SECONDS (default 10s — alinhado ao reenvio e à
    # produção; 0 desliga). Não pausa em duplicatas puladas nem em dry-run.
    try:
        send_delay = max(0.0, float(os.environ.get("COBRANCA_SEND_DELAY_SECONDS", "10")))
    except ValueError:
        send_delay = 10.0

    # Títulos que JÁ têm erro registrado — usado para limpar o log de erros ao enviá-los
    # com sucesso (e-mail corrigido no Firebird volta ao fluxo e a falha antiga some). Só
    # limpa quem estava neste conjunto, evitando um DELETE por título p/ quem nunca falhou.
    # Best-effort: se a consulta falhar, segue sem limpar (não derruba o run). Vazio em dry-run.
    error_doc_ids: set[str] = set()
    if not dry_run:
        try:
            error_doc_ids = fetch_error_document_ids()
        except Exception:  # noqa: BLE001 — sem isso, só não limpa; o run segue
            logger.warning("Não foi possível listar títulos com erro; limpeza de resolvidos desativada neste run.")

    counts, error_types = _run_batch(
        titulos, dry_run=dry_run, dev_mode=dev_mode, dev_override=dev_override,
        company_row=company_row, send_delay=send_delay, error_doc_ids=error_doc_ids,
    )

    # Erros de DADO (cliente sem e-mail / inválido) são separados dos OPERACIONAIS no
    # resumo: só os operacionais reprovam a tarefa (ver compute_exit_code).
    data_errors = sum(error_types[t] for t in DATA_ERROR_TYPES)
    operational_errors = counts["error"] - data_errors

    logger.info("-" * 60)
    logger.info(
        "Resumo: total=%d | enviados=%d | pulados=%d | erros=%d (sem e-mail/inválido=%d · operacionais=%d)",
        len(titulos), counts["sent"], counts["skipped"], counts["error"], data_errors, operational_errors,
    )
    logger.info("=" * 60)
    return compute_exit_code(error_types)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cobranca de titulos vencidos")
    parser.add_argument("--dry-run", action="store_true",
        help="Simula sem enviar emails nem gravar no Supabase")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
