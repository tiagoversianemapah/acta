"""Leitura do PDF da certidao da Receita Federal.

O adapter cego precisa classificar o PDF sem importar o adapter Playwright,
que fica fora do executavel empacotado. Este modulo tem so a parte comum:
texto do PDF, tipo da certidao, validade e codigo de controle.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from cnd.core.modelos import Desfecho, ResultadoTentativa
from cnd.infra.log import obter

log = obter("adapter.rfb_pdf")

TITULO_CPEN = "certidão positiva com efeitos de negativa"
TITULO_NEGATIVA = "certidão negativa de débitos"
TITULO_POSITIVA = "certidão positiva de débitos"

RE_VALIDADE = re.compile(r"v[áa]lida at[ée]\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_CODIGO = re.compile(
    r"c[óo]digo de controle da certid[ãa]o:?\s*([A-Za-z0-9.]+)", re.IGNORECASE
)


def _normalizar(texto: str | None) -> str:
    """Minusculas com espacos colapsados para comparar textos quebrados."""
    return re.sub(r"\s+", " ", (texto or "")).strip().lower()


def _extrair_validade(conteudo: str) -> date | None:
    achado = RE_VALIDADE.search(re.sub(r"\s+", " ", conteudo))
    if not achado:
        return None
    try:
        return datetime.strptime(achado.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def _extrair_codigo(conteudo: str) -> str | None:
    achado = RE_CODIGO.search(re.sub(r"\s+", " ", conteudo))
    return achado.group(1).rstrip(".") if achado else None


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
    """Classifica pelo titulo do PDF, que e a fonte de verdade."""
    from pypdf import PdfReader

    try:
        conteudo = "\n".join(
            (pagina.extract_text() or "") for pagina in PdfReader(str(caminho)).pages
        )
    except Exception as erro:
        log.warning("pdf_ilegivel", extra={"arquivo": str(caminho), "erro": str(erro)})
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=f"PDF baixado mas ilegivel: {erro}"[:300],
            evidencia=caminho,
        )

    normalizado = _normalizar(conteudo)
    comuns = dict(
        validade=_extrair_validade(conteudo),
        codigo_controle=_extrair_codigo(conteudo),
        mensagem_portal=texto_tela.strip()[:500],
    )

    if TITULO_CPEN in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)

    if TITULO_NEGATIVA in normalizado:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)

    if TITULO_POSITIVA in normalizado:
        log.info("certidao_positiva", extra={"arquivo": str(caminho)})
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    log.warning("titulo_do_pdf_nao_reconhecido",
                extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
    return ResultadoTentativa(
        Desfecho.ERRO_TECNICO,
        mensagem_portal="titulo do PDF nao reconhecido - conferir manualmente",
        evidencia=caminho,
    )
