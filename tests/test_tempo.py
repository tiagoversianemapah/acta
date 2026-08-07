"""Testes do módulo de tempo.

A propriedade importante: o formato de data precisa ser ordenável como
texto, porque a fila compara datas com `<=` direto no SQL.
"""
from __future__ import annotations

from datetime import UTC, datetime

from cnd.core import tempo


def test_ida_e_volta():
    momento = datetime(2026, 8, 7, 14, 3, 22, 481000, tzinfo=UTC)

    texto = tempo.para_iso(momento)

    assert texto == "2026-08-07T14:03:22.481Z"
    assert tempo.de_iso(texto) == momento


def test_ordem_alfabetica_e_ordem_cronologica():
    cedo = tempo.para_iso(datetime(2026, 8, 7, 9, 0, tzinfo=UTC))
    tarde = tempo.para_iso(datetime(2026, 8, 7, 21, 0, tzinfo=UTC))
    outro_dia = tempo.para_iso(datetime(2026, 12, 31, 1, 0, tzinfo=UTC))

    assert cedo < tarde < outro_dia


def test_daqui_a_esta_no_futuro():
    futuro = tempo.de_iso(tempo.daqui_a(3600))

    diferenca = (futuro - tempo.agora()).total_seconds()
    assert 3590 < diferenca <= 3600


def test_daqui_a_negativo_esta_no_passado():
    passado = tempo.de_iso(tempo.daqui_a(-3600))

    assert passado < tempo.agora()


def test_janela_sempre_ativa():
    assert tempo.dentro_da_janela("00:00-24:00") is True
    assert tempo.dentro_da_janela("") is True


def test_janela_comum():
    manha = datetime(2026, 8, 7, 9, 0).astimezone()
    madrugada = datetime(2026, 8, 7, 3, 0).astimezone()

    assert tempo.dentro_da_janela("06:00-23:00", manha) is True
    assert tempo.dentro_da_janela("06:00-23:00", madrugada) is False


def test_janela_que_vira_a_meia_noite():
    madrugada = datetime(2026, 8, 7, 2, 0).astimezone()
    meio_dia = datetime(2026, 8, 7, 12, 0).astimezone()

    assert tempo.dentro_da_janela("22:00-06:00", madrugada) is True
    assert tempo.dentro_da_janela("22:00-06:00", meio_dia) is False
