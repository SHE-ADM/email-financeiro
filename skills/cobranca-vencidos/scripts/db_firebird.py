"""
db_firebird.py
Conexão com Firebird 5 e execução da query de títulos vencidos.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

logger = logging.getLogger(__name__)

@dataclass
class TituloVencido:
    document_id:    str
    due_date:       date
    bill_amount:    Decimal
    customer_name:  str
    primary_email:  str
    cc_email:       str | None
    email_subject:  str

_QUERY = """
SELECT
    PK.TITULO   AS DOCUMENT_ID,
    PK.DTVC     AS DUE_DATE,
    PK.VDUP     AS BILL_AMOUNT,
    PK.CD_NO    AS CUSTOMER_NAME,
    PK.CD_EMAIL AS PRIMARY_EMAIL,
    PK.CV_EMAIL AS CC_EMAIL,
    'COBRANÇA' || ' ' || PK.EP_NO AS EMAIL_SUBJECT
FROM VW_PSQ_FIN_REC_BAN PK
WHERE PK.STFI = 'VENCIDO'
  AND PK.DTVC >= CURRENT_DATE - 7
  AND PK.CD_GP_NO <> 'INBRANDS'
  AND PK.CD_GP_NO <> 'RESTOQUE'
  AND PK.CD_GP_NO <> 'SHOULDER'
  AND PK.CD_GP_NO <> 'SKAI'
  AND PK.CD_GP_NO <> 'SOMA'
  AND PK.CD_GP_NO <> 'LOJAS MEL'
UNION ALL
SELECT
    PK.TITULO   AS DOCUMENT_ID,
    PK.DTVC     AS DUE_DATE,
    PK.VDUP     AS BILL_AMOUNT,
    PK.CD_NO    AS CUSTOMER_NAME,
    PK.CD_EMAIL AS PRIMARY_EMAIL,
    PK.CV_EMAIL AS CC_EMAIL,
    'COBRANÇA' || ' ' || PK.EP_NO AS EMAIL_SUBJECT
FROM VW_PSQ_FIN_REC_BAN_004 PK
WHERE PK.STFI = 'VENCIDO'
  AND PK.DTVC >= CURRENT_DATE - 7
  AND PK.CD_GP_NO <> 'INBRANDS'
  AND PK.CD_GP_NO <> 'RESTOQUE'
  AND PK.CD_GP_NO <> 'SHOULDER'
  AND PK.CD_GP_NO <> 'SKAI'
  AND PK.CD_GP_NO <> 'SOMA'
  AND PK.CD_GP_NO <> 'LOJAS MEL'
ORDER BY 1
"""

def _get_driver():
    try:
        import fdb; return fdb
    except ImportError: pass
    try:
        import firebirdsql as fdb; return fdb
    except ImportError:
        raise ImportError("Nenhum driver Firebird. Instale: .venv\\Scripts\\pip install fdb")

def fetch_titulos_vencidos() -> list[TituloVencido]:
    fdb = _get_driver()
    dsn = f"{os.environ['FB_HOST']}/{int(os.environ.get('FB_PORT','3050'))}:{os.environ['FB_DATABASE']}"
    logger.info("Conectando Firebird: %s", dsn)
    con = fdb.connect(dsn=dsn, user=os.environ['FB_USER'], password=os.environ['FB_PASSWORD'], charset=os.environ.get('FB_CHARSET','WIN1252'))
    try:
        cur = con.cursor(); cur.execute(_QUERY)
        rows = []
        for row in cur.fetchall():
            d, dt, amt, nm, mail, cc, subj = row
            # Só descarta linha SEM título (document_id) — inutilizável (sem chave de
            # dedup/registro). Linha SEM e-mail SEGUE adiante para virar "email_ausente"
            # em /cobranca/erros: cliente vencido sem e-mail é problema acionável, não some.
            if not d:
                logger.warning("Linha sem título (document_id) ignorada: %s", row)
                continue
            rows.append(TituloVencido(
                document_id=str(d).strip(),
                due_date=dt,
                bill_amount=Decimal(str(amt)) if amt else Decimal('0'),
                customer_name=str(nm).strip() if nm else '',
                primary_email=str(mail).strip() if mail else '',  # nulo/None -> '' (vira email_ausente)
                cc_email=str(cc).strip() if cc else None,
                email_subject=str(subj).strip() if subj else 'COBRANÇA',
            ))
        logger.info("Firebird: %d títulos vencidos", len(rows))
        return rows
    finally: con.close()
