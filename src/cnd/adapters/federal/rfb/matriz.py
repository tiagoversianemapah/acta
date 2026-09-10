"""Regra comum da Receita Federal para CNPJ de filial."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from cnd.core.documentos import (
    DocumentoInvalido,
    cnpj_da_matriz,
    formatar,
    validar_cnpj,
)
from cnd.core.modelos import Documento, ResultadoTentativa

# Faixa amarela no topo do formulário quando o CNPJ digitado é de filial:
#
#   "A certidão deve ser emitida para o CNPJ da matriz – 04.401.250/0001-94"
#
# Derivar a matriz trocando o sufixo por 0001 acerta na esmagadora maioria
# dos casos, mas quem manda é o portal: quando ele discorda, o número certo
# está escrito ali. Por isso a frase é reconhecida e o CNPJ, extraído.
FRASE_EMITIR_PELA_MATRIZ = "certidao deve ser emitida para o cnpj da matriz"
RE_CNPJ_NO_TEXTO = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b")


def _sem_acento(texto: str) -> str:
    """Minúsculas, sem acento e com espaços colapsados.

    A faixa quebra linha no meio da frase e usa travessão — comparar o texto
    cru daria falso negativo.
    """
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    ascii_puro = sem_acento.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_puro.lower().split())


def exige_matriz(texto: str) -> bool:
    """O portal recusou o CNPJ digitado e pediu o da matriz?"""
    return FRASE_EMITIR_PELA_MATRIZ in _sem_acento(texto)


def matriz_exigida_no_texto(texto: str) -> str | None:
    """O CNPJ da matriz que o portal escreveu na faixa, limpo, ou None.

    Procura só DEPOIS da frase: a tela também mostra o CNPJ digitado, e
    pegar o primeiro número da página traria justamente o que foi recusado.
    """
    normalizado = _sem_acento(texto)
    inicio = normalizado.find(FRASE_EMITIR_PELA_MATRIZ)
    if inicio < 0:
        return None

    for achado in RE_CNPJ_NO_TEXTO.finditer(normalizado, inicio):
        try:
            return validar_cnpj(achado.group())
        except DocumentoInvalido:
            continue
    return None


def mensagem_de_matriz(documento: str, matriz: str) -> str | None:
    """Nota para o relatório: a consulta não foi feita no CNPJ da carteira."""
    if matriz == documento:
        return None
    return (
        f"filial {formatar(documento)} consultada pela matriz "
        f"{formatar(matriz)}"
    )


def documento_para_consulta(doc: Documento) -> tuple[Documento, str | None]:
    """Na Receita Federal PJ, filial consulta pela matriz."""
    if doc.tipo != "CNPJ":
        return doc, None

    matriz = cnpj_da_matriz(doc.documento)
    mensagem = mensagem_de_matriz(doc.documento, matriz)
    if mensagem is None:
        return doc, None

    return replace(doc, documento=matriz), mensagem


def anotar_matriz(
    resultado: ResultadoTentativa, mensagem: str | None
) -> ResultadoTentativa:
    if not mensagem:
        return resultado

    texto = resultado.mensagem_portal or ""
    combinado = f"{mensagem}; {texto}" if texto else mensagem
    return replace(resultado, mensagem_portal=combinado[:500])
