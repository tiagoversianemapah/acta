"""Ritmo adaptativo (AIMD) — ver docs/04, seção 3.

O captcha da Receita é comportamental: a taxa depende de como navegamos.
Como ninguém publica o limite, o sistema o descobre sozinho, no mesmo
padrão do controle de congestão do TCP:

  - a cada N consultas limpas, acelera um pouco  (aumento gradual)
  - ao primeiro captcha, freia forte             (punição multiplicativa)

Assim ele converge para a maior velocidade que o portal tolera, sem
nunca martelar, e sem ninguém precisar ajustar configuração na mão.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from cnd.core import tempo


@dataclass(frozen=True)
class ParametrosRitmo:
    intervalo_inicial_s: float = 10.0
    intervalo_piso_s: float = 1.0       # o mais rápido que pode ficar
    intervalo_teto_s: float = 300.0     # o mais lento que pode ficar
    acelera_apos: int = 25              # consultas limpas seguidas
    fator_aceleracao: float = 0.8       # < 1 => diminui o intervalo
    fator_punicao: float = 4.0          # > 1 => aumenta o intervalo
    fator_punicao_bloqueio: float = 1.4 # bloqueio temporario nao e captcha
    jitter: float = 0.3                 # variação aleatória de cada espera
    janela_ativa: str = "00:00-24:00"

    @classmethod
    def de_config(cls, dados: dict) -> ParametrosRitmo:
        campos = {c: dados[c] for c in cls.__dataclass_fields__ if c in dados}
        return cls(**campos)


@dataclass(frozen=True)
class EstadoRitmo:
    intervalo_s: float
    consultas_limpas: int


def estado(conn: sqlite3.Connection, orgao: str, p: ParametrosRitmo) -> EstadoRitmo:
    """Lê o ritmo atual do órgão, criando-o na primeira vez.

    Fica no banco (e não em memória) para sobreviver a reinício: o robô
    não perde o que aprendeu quando o servidor reinicia.
    """
    linha = conn.execute(
        "SELECT intervalo_s, consultas_limpas FROM ritmo WHERE orgao = ?", (orgao,)
    ).fetchone()

    if linha is None:
        conn.execute(
            "INSERT INTO ritmo (orgao, intervalo_s, consultas_limpas, atualizado_em) "
            "VALUES (?, ?, 0, ?)",
            (orgao, p.intervalo_inicial_s, tempo.agora_iso()),
        )
        return EstadoRitmo(p.intervalo_inicial_s, 0)

    return EstadoRitmo(linha["intervalo_s"], linha["consultas_limpas"])


def _gravar(conn: sqlite3.Connection, orgao: str, intervalo: float, limpas: int) -> EstadoRitmo:
    conn.execute(
        "UPDATE ritmo SET intervalo_s = ?, consultas_limpas = ?, atualizado_em = ? WHERE orgao = ?",
        (intervalo, limpas, tempo.agora_iso(), orgao),
    )
    return EstadoRitmo(intervalo, limpas)


def registrar_sucesso(conn: sqlite3.Connection, orgao: str, p: ParametrosRitmo) -> EstadoRitmo:
    """Uma consulta terminou sem captcha. Acelera quando a sequência limpa
    atinge o limiar; caso contrário, só incrementa o contador."""
    atual = estado(conn, orgao, p)

    if atual.intervalo_s > p.intervalo_inicial_s:
        novo = max(p.intervalo_inicial_s, atual.intervalo_s * p.fator_aceleracao)
        return _gravar(conn, orgao, novo, 0)

    limpas = atual.consultas_limpas + 1

    if limpas >= p.acelera_apos:
        novo = max(p.intervalo_piso_s, atual.intervalo_s * p.fator_aceleracao)
        return _gravar(conn, orgao, novo, 0)

    return _gravar(conn, orgao, atual.intervalo_s, limpas)


def registrar_captcha(conn: sqlite3.Connection, orgao: str, p: ParametrosRitmo) -> EstadoRitmo:
    """Bateu captcha: freia forte e zera a contagem de aceleração."""
    atual = estado(conn, orgao, p)
    novo = min(p.intervalo_teto_s, atual.intervalo_s * p.fator_punicao)
    return _gravar(conn, orgao, novo, 0)


def registrar_bloqueio_temporario(
    conn: sqlite3.Connection, orgao: str, p: ParametrosRitmo
) -> EstadoRitmo:
    """Portal pediu alguns minutos: recua, mas menos que um captcha real."""
    atual = estado(conn, orgao, p)
    novo = min(p.intervalo_teto_s, atual.intervalo_s * p.fator_punicao_bloqueio)
    return _gravar(conn, orgao, novo, 0)


def proxima_espera(intervalo_s: float, jitter: float = 0.3) -> float:
    """Quanto esperar antes da próxima consulta.

    O jitter existe para o intervalo nunca ser metronômico — um robô que
    consulta exatamente a cada 10,000s é mais fácil de detectar do que um
    que varia entre 7 e 13.
    """
    if intervalo_s <= 0:
        return 0.0
    fator = 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, intervalo_s * fator)
