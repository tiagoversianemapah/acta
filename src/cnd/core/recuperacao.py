"""Recuperação automática: o lote fecha sozinho, sem ninguém clicar.

O sistema encerra um item como FAILED depois de três tentativas. Isso é
certo para não travar a fila — mas os desfechos que levam a isso
(bloqueio, resultado pendente, erro técnico) são todos passageiros por
natureza: nenhum deles é a Receita respondendo sobre a empresa. Deixar o
item parado ali significa que o lote termina em 98,9% e alguém precisa
lembrar de apertar "Reenviar itens com falha".

Foi o que aconteceu no lote 1: 30 itens em aberto, e nenhum deles era
resposta do portal — 19 esperando resultado, 7 bloqueados e 4 numa tela que
o robô não conhecia (17/08/2026).

Então, quando a fila do órgão esvazia e ainda há falhas, o vigia devolve
tudo para a fila e recomeça. A espera entre rodadas CRESCE (30min, 1h, 2h,
4h, teto de 6h) por dois motivos:

  - se o portal está fora do ar, insistir de minuto em minuto não adianta
    e ainda alimenta o disjuntor;
  - se um item estiver quebrado de verdade, o custo dele fica limitado a
    umas quatro consultas por dia, em vez de dezenas.

Não existe rodada final: por decisão de operação (17/08/2026) o robô nunca
desiste, porque item sem resposta é trabalho não entregue. O que existe é
aviso — a partir da Nª rodada o Teams recebe um alerta, para alguém olhar o
que está travando. Ele continua tentando enquanto ninguém olha.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cnd.core import tempo


@dataclass(frozen=True)
class ParametrosRecuperacao:
    ativa: bool = True
    espera_inicial_s: float = 1800.0      # 30 min até a 1ª recuperação
    fator: float = 2.0                    # dobra a cada rodada sem sucesso
    espera_maxima_s: float = 21600.0      # teto de 6h
    avisar_apos: int = 5                  # rodadas antes de chamar gente

    @classmethod
    def de_config(cls, dados: dict) -> ParametrosRecuperacao:
        campos = {c: dados[c] for c in cls.__dataclass_fields__ if c in dados}
        return cls(**campos)


@dataclass(frozen=True)
class EstadoRecuperacao:
    rodadas: int
    proxima_em: str | None

    @property
    def virgem(self) -> bool:
        """Nunca precisou recuperar nada."""
        return self.rodadas == 0 and self.proxima_em is None


def estado(conn: sqlite3.Connection, orgao: str) -> EstadoRecuperacao:
    linha = conn.execute(
        "SELECT rodadas, proxima_em FROM recuperacao WHERE orgao = ?", (orgao,)
    ).fetchone()
    if linha is None:
        return EstadoRecuperacao(0, None)
    return EstadoRecuperacao(int(linha["rodadas"]), linha["proxima_em"])


def espera_da_rodada(rodadas: int, p: ParametrosRecuperacao) -> float:
    """Quanto esperar antes da rodada de número `rodadas + 1`."""
    espera = p.espera_inicial_s * (p.fator ** max(rodadas, 0))
    return float(min(espera, p.espera_maxima_s))


def _gravar(conn: sqlite3.Connection, orgao: str, rodadas: int,
            proxima_em: str | None) -> EstadoRecuperacao:
    conn.execute(
        """
        INSERT INTO recuperacao (orgao, rodadas, proxima_em, atualizado_em)
             VALUES (?, ?, ?, ?)
        ON CONFLICT(orgao) DO UPDATE
                SET rodadas = excluded.rodadas,
                    proxima_em = excluded.proxima_em,
                    atualizado_em = excluded.atualizado_em
        """,
        (orgao, rodadas, proxima_em, tempo.agora_iso()),
    )
    return EstadoRecuperacao(rodadas, proxima_em)


def agendar(conn: sqlite3.Connection, orgao: str,
            p: ParametrosRecuperacao) -> EstadoRecuperacao:
    """Marca a hora da próxima rodada, sem consumir uma.

    Chamado quando as falhas aparecem e ainda não há horário marcado: a
    primeira recuperação não é imediata, para dar tempo de o problema do
    portal passar sozinho.
    """
    atual = estado(conn, orgao)
    if atual.proxima_em:
        return atual
    quando = tempo.daqui_a(espera_da_rodada(atual.rodadas, p))
    return _gravar(conn, orgao, atual.rodadas, quando)


def pode_recuperar(conn: sqlite3.Connection, orgao: str) -> bool:
    atual = estado(conn, orgao)
    if not atual.proxima_em:
        return False
    return tempo.agora_iso() >= atual.proxima_em


def registrar_rodada(conn: sqlite3.Connection, orgao: str,
                     p: ParametrosRecuperacao) -> EstadoRecuperacao:
    """Conta a rodada que acabou de acontecer e agenda a seguinte."""
    atual = estado(conn, orgao)
    rodadas = atual.rodadas + 1
    quando = tempo.daqui_a(espera_da_rodada(rodadas, p))
    return _gravar(conn, orgao, rodadas, quando)


def zerar(conn: sqlite3.Connection, orgao: str) -> None:
    """O lote fechou: nada mais a recuperar, e a contagem recomeça do zero.

    Sem isto, um lote seguinte herdaria a espera de 6h da rodada 5 do lote
    anterior e demoraria um dia para se recuperar de um tropeço banal.
    """
    if estado(conn, orgao).virgem:
        return
    _gravar(conn, orgao, 0, None)
