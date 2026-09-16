"""A micro-retentativa: a segunda chance que um bloqueio temporário merece.

Alguns portais respondem "tente novamente em alguns minutos" com um código
no corpo da mensagem. Esperar o backoff cheio nesses casos queima as três
tentativas do item sem nunca tentar com sessão nova — foi o que aconteceu
com os 7 itens de 17/08/2026. Aqui mora só a DECISÃO (é caso de segunda
chance? quanto esperar? o que escrever na mensagem); quem executa é o
worker.
"""
from __future__ import annotations

import random
import re

from cnd.core.modelos import Desfecho, ResultadoTentativa

# Com a data junto: o código vem carimbado como "005 - 17/08/2026 12:03:32".
# Sem exigir isso, os três dígitos do CNPJ passavam por código do portal e o
# item ganhava (ou perdia) a micro-retentativa por acaso do número dele.
RE_CODIGO_PORTAL = re.compile(r"\b(001|005|023|033|106)\s*-\s*\d{2}/\d{2}/\d{4}")


def codigo_portal(resultado: ResultadoTentativa) -> str | None:
    texto = resultado.mensagem_portal or ""
    achado = RE_CODIGO_PORTAL.search(texto)
    return achado.group(1) if achado else None


def deve_retentativa_rapida(resultado: ResultadoTentativa) -> bool:
    # 005 entrou em 17/08/2026: era o código de TODOS os 7 itens que
    # morreram como FAILED no servidor, e a mensagem dele é literalmente
    # "tente novamente em alguns minutos" — exatamente o caso para o qual
    # esta segunda chance foi feita. Sem estar nesta lista ele pulava a
    # micro-retentativa, ia direto ao backoff de 5/15/30 min e queimava as
    # três tentativas sem nunca tentar com sessão nova.
    if resultado.desfecho != Desfecho.BLOQUEIO_TEMPORARIO:
        return False
    return codigo_portal(resultado) in {"005", "023", "106"}


def espera_retentativa_rapida(valores: tuple[float, ...]) -> float:
    if not valores:
        return 0.0
    faixa = [max(0.0, float(valor)) for valor in valores]
    if len(faixa) == 1:
        return faixa[0]
    inicio, fim = sorted((faixa[0], faixa[1]))
    if fim <= inicio:
        return inicio
    return random.uniform(inicio, fim)


def mensagem_com_retentativa(
    primeiro: ResultadoTentativa, segundo: ResultadoTentativa
) -> str | None:
    segunda = " ".join((segundo.mensagem_portal or "").split())
    primeira = " ".join((primeiro.mensagem_portal or "").split())
    codigo = codigo_portal(primeiro)
    detalhe = "micro-retentativa apos bloqueio temporario"
    if codigo:
        detalhe += f" {codigo}"
    if primeira:
        detalhe += f"; primeira resposta: {primeira[:250]}"
    mensagem = "; ".join(parte for parte in (segunda, detalhe) if parte)
    return mensagem[:500] if mensagem else None
