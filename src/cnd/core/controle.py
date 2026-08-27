"""Quem manda na ordem da fila: estacionar, cancelar e furar a fila.

A fila é única e ordenada por id — a planilha que chega depois espera a
anterior terminar. Isso serviu enquanto uma máquina fazia uma automação
só. Com RFB e CRF na mesma planilha e várias planilhas por mês, faltava
poder dizer "para essa e faz aquela agora".

O controle é por PLANILHA E AUTOMAÇÃO, não por lote inteiro: as duas
automações vivem no mesmo arquivo, e estacionar uma não pode parar a
outra.

Três situações:

    ATIVA         o normal — entra na fila na ordem
    ESTACIONADA   sai da vez, e volta de onde parou quando você quiser
    CANCELADA     sai para valer; os itens ficam, mas nunca mais são pegos

Cancelar NÃO apaga nada, e é de propósito. Duas razões. A primeira é que
apagar linha faria o relatório mentir: 2.041 viram 800 e ninguém sabe o
que houve com os outros 1.241 — com o item preservado, a conta fecha e a
planilha mostra "cancelado". A segunda é que a alternativa era criar um
status novo em `job`, e o CHECK da tabela obrigaria a reconstruí-la num
banco de produção com milhares de linhas, para ganhar menos.

Linha ausente vale ATIVA com prioridade zero. É o estado de quase todo
mundo, e assim ele não ocupa espaço nem exige escrever nada na hora de
importar uma planilha.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cnd.core import tempo

ATIVA = "ATIVA"
ESTACIONADA = "ESTACIONADA"
CANCELADA = "CANCELADA"

SITUACOES = (ATIVA, ESTACIONADA, CANCELADA)
# As que a fila pode entregar. Fora daqui, o worker nem enxerga o item.
ENTREGAVEIS = (ATIVA,)


@dataclass(frozen=True)
class Controle:
    lote_id: int
    orgao: str
    situacao: str
    prioridade: int

    @property
    def ativa(self) -> bool:
        return self.situacao == ATIVA

    @property
    def estacionada(self) -> bool:
        return self.situacao == ESTACIONADA

    @property
    def cancelada(self) -> bool:
        return self.situacao == CANCELADA


PADRAO = Controle(lote_id=0, orgao="", situacao=ATIVA, prioridade=0)


def situacao(conn: sqlite3.Connection, lote_id: int, orgao: str) -> Controle:
    """Como está esta automação nesta planilha. NUNCA escreve.

    Só leitura de propósito: o painel abre o banco sem permissão de
    escrita, e uma consulta que insere derruba a tela inteira quando o
    órgão é novo — foi o que aconteceu com o disjuntor em 18/08/2026.
    """
    linha = conn.execute(
        "SELECT situacao, prioridade FROM fila_controle "
        " WHERE lote_id = ? AND orgao = ?",
        (lote_id, orgao),
    ).fetchone()
    if linha is None:
        return Controle(lote_id, orgao, ATIVA, 0)
    return Controle(lote_id, orgao, linha["situacao"], int(linha["prioridade"]))


def listar(conn: sqlite3.Connection,
           lote_id: int | None = None) -> list[Controle]:
    """Só o que foi mexido — o resto está ATIVA e não tem linha."""
    if lote_id is None:
        linhas = conn.execute(
            "SELECT lote_id, orgao, situacao, prioridade FROM fila_controle"
        ).fetchall()
    else:
        linhas = conn.execute(
            "SELECT lote_id, orgao, situacao, prioridade FROM fila_controle"
            " WHERE lote_id = ?", (lote_id,),
        ).fetchall()
    return [Controle(linha["lote_id"], linha["orgao"], linha["situacao"],
                     int(linha["prioridade"])) for linha in linhas]


def _gravar(conn: sqlite3.Connection, lote_id: int, orgao: str,
            situacao_nova: str, prioridade: int) -> Controle:
    conn.execute(
        """
        INSERT INTO fila_controle (lote_id, orgao, situacao, prioridade,
                                   atualizado_em)
             VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(lote_id, orgao) DO UPDATE
                SET situacao = excluded.situacao,
                    prioridade = excluded.prioridade,
                    atualizado_em = excluded.atualizado_em
        """,
        (lote_id, orgao, situacao_nova, prioridade, tempo.agora_iso()),
    )
    return Controle(lote_id, orgao, situacao_nova, prioridade)


def definir(conn: sqlite3.Connection, lote_id: int, orgao: str,
            situacao_nova: str) -> Controle:
    """Estaciona, cancela ou reativa, preservando a prioridade."""
    if situacao_nova not in SITUACOES:
        raise ValueError(f"Situação desconhecida: {situacao_nova!r}")
    atual = situacao(conn, lote_id, orgao)
    return _gravar(conn, lote_id, orgao, situacao_nova, atual.prioridade)


def priorizar(conn: sqlite3.Connection, lote_id: int, orgao: str) -> Controle:
    """"Rodar agora": põe esta na frente de todas, e a reativa.

    A prioridade cresce em vez de reordenar os ids — id é a memória da
    ordem de chegada, e mexer nele apagaria a informação de quem veio
    antes. Reativar junto é o que a pessoa espera: mandar rodar algo
    estacionado sem que ele saia do estacionamento seria um botão que não
    faz o que diz.
    """
    maior = conn.execute(
        "SELECT COALESCE(MAX(prioridade), 0) AS p FROM fila_controle"
    ).fetchone()["p"]
    return _gravar(conn, lote_id, orgao, ATIVA, int(maior) + 1)
