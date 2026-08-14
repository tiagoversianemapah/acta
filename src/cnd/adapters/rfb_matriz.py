"""Regra comum da Receita Federal para CNPJ de filial."""
from __future__ import annotations

from dataclasses import replace

from cnd.core.documentos import cnpj_da_matriz, formatar
from cnd.core.modelos import Documento, ResultadoTentativa


def documento_para_consulta(doc: Documento) -> tuple[Documento, str | None]:
    """Na Receita Federal PJ, filial consulta pela matriz."""
    if doc.tipo != "CNPJ":
        return doc, None

    matriz = cnpj_da_matriz(doc.documento)
    if matriz == doc.documento:
        return doc, None

    mensagem = (
        f"filial {formatar(doc.documento)} consultada pela matriz "
        f"{formatar(matriz)}"
    )
    return replace(doc, documento=matriz), mensagem


def anotar_matriz(
    resultado: ResultadoTentativa, mensagem: str | None
) -> ResultadoTentativa:
    if not mensagem:
        return resultado

    texto = resultado.mensagem_portal or ""
    combinado = f"{mensagem}; {texto}" if texto else mensagem
    return replace(resultado, mensagem_portal=combinado[:500])
