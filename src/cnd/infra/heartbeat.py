"""Sinal de vida dos processos — é o que detecta "o robô parou".

O orquestrador bate o ponto a cada minuto. O processo web confere: se o
último sinal for mais velho que o timeout, dispara alerta. Como são dois
processos independentes, um vigia o outro (docs/05, seção 4).
"""
from __future__ import annotations

import sqlite3

from cnd.core import tempo


def bater(conn: sqlite3.Connection, processo: str = "orquestrador") -> None:
    conn.execute(
        "INSERT INTO heartbeat (processo, atualizado_em) VALUES (?, ?) "
        "ON CONFLICT (processo) DO UPDATE SET atualizado_em = excluded.atualizado_em",
        (processo, tempo.agora_iso()),
    )


def apagar(conn: sqlite3.Connection, processo: str = "orquestrador") -> None:
    """Esquece o sinal deste processo — ele encerrou de propósito.

    Sem isto, o painel seguia mostrando "Parar robô" por até cinco minutos
    depois de o robô terminar o lote: o último sinal continuava lá, e a
    regra é "vivo enquanto o sinal for recente" (18/09/2026).
    """
    conn.execute("DELETE FROM heartbeat WHERE processo = ?", (processo,))


def ultimo(conn: sqlite3.Connection, processo: str = "orquestrador") -> str | None:
    linha = conn.execute(
        "SELECT atualizado_em FROM heartbeat WHERE processo = ?", (processo,)
    ).fetchone()
    return linha["atualizado_em"] if linha else None


def segundos_desde(conn: sqlite3.Connection, processo: str = "orquestrador") -> float | None:
    """Há quantos segundos o processo não dá sinal. None = nunca deu."""
    marca = ultimo(conn, processo)
    if marca is None:
        return None
    return (tempo.agora() - tempo.de_iso(marca)).total_seconds()


def vivo(conn: sqlite3.Connection, timeout_s: int,
         processo: str = "orquestrador") -> bool:
    idade = segundos_desde(conn, processo)
    return idade is not None and idade <= timeout_s
