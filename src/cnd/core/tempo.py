"""Datas e horas do sistema.

Convenção: tudo é gravado em UTC, no formato ISO-8601 com milissegundos
('2026-08-07T14:03:22.481Z'). Esse formato é ordenável como texto puro,
o que permite ao banco comparar datas sem conversão.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock

FORMATO = "%Y-%m-%dT%H:%M:%S.%f"
_TRAVA_AGORA = Lock()
_ULTIMO_AGORA: datetime | None = None


def agora() -> datetime:
    return datetime.now(UTC)


def agora_iso() -> str:
    global _ULTIMO_AGORA

    with _TRAVA_AGORA:
        momento = _em_milissegundos(agora())
        if _ULTIMO_AGORA is not None and momento <= _ULTIMO_AGORA:
            momento = _ULTIMO_AGORA + timedelta(milliseconds=1)
        _ULTIMO_AGORA = momento
        return para_iso(momento)


def _em_milissegundos(momento: datetime) -> datetime:
    return momento.replace(microsecond=(momento.microsecond // 1000) * 1000)


def para_iso(momento: datetime) -> str:
    return momento.strftime(FORMATO)[:-3] + "Z"


def de_iso(texto: str) -> datetime:
    return datetime.strptime(texto.rstrip("Z"), FORMATO).replace(tzinfo=UTC)


def daqui_a(segundos: float) -> str:
    """Momento futuro, já em texto ISO — usado para agendar retries."""
    return para_iso(agora() + timedelta(seconds=segundos))


def dentro_da_janela(janela: str, momento: datetime | None = None) -> bool:
    """A janela ativa é local, no formato 'HH:MM-HH:MM'.

    '00:00-24:00' significa sempre ativo. Janelas que viram a meia-noite
    (ex.: '22:00-06:00') também funcionam.
    """
    if not janela or janela in ("00:00-24:00", "24h"):
        return True

    inicio_txt, fim_txt = janela.split("-")
    local = (momento or agora()).astimezone()
    minutos = local.hour * 60 + local.minute

    def em_minutos(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    inicio, fim = em_minutos(inicio_txt), em_minutos(fim_txt)
    if inicio <= fim:
        return inicio <= minutos < fim
    return minutos >= inicio or minutos < fim
