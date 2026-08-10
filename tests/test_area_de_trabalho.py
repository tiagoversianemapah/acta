"""Com o painel subindo no boot, quem pergunta é um serviço.

`OpenInputDesktop` falha sempre na sessão 0 — ela não tem área de trabalho
nenhuma. Se essa resposta valesse sozinha, o console recusaria todo
"iniciar robô" com a máquina destravada na frente de quem pediu.
"""
from __future__ import annotations

import sys

import pytest

from cnd.infra import maquina

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="área de trabalho do Windows")


def test_sessao_interativa_responde_na_hora(monkeypatch):
    """Rodando dentro da sessão, a checagem clássica basta e é a mais
    confiável — não se vai perguntar a mais ninguém."""
    monkeypatch.setattr(maquina, "_sessao_do_console_destravada",
                        lambda: pytest.fail("não devia ter sido consultada"))

    assert maquina.area_de_trabalho_disponivel() is True


def test_servico_pergunta_a_sessao_do_console(monkeypatch):
    """Na sessão 0, OpenInputDesktop devolve 0 e a resposta tem de vir da
    sessão do console, não dele."""
    monkeypatch.setattr(maquina.ctypes, "windll",
                        _sem_area_de_trabalho(maquina.ctypes.windll))
    monkeypatch.setattr(maquina, "_sessao_do_console_destravada",
                        lambda: True)

    assert maquina.area_de_trabalho_disponivel() is True


def test_ninguem_logado_e_nao(monkeypatch):
    """Máquina na tela de senha: o robô não clicaria em nada."""
    monkeypatch.setattr(maquina.ctypes, "windll",
                        _sem_area_de_trabalho(maquina.ctypes.windll))
    monkeypatch.setattr(maquina, "_sessao_do_console_destravada",
                        lambda: False)

    assert maquina.area_de_trabalho_disponivel() is False


def test_sem_sessao_no_console_e_nao(monkeypatch):
    """WTSGetActiveConsoleSessionId devolve 0xFFFFFFFF sem ninguém logado."""
    monkeypatch.setattr(maquina.ctypes, "windll",
                        _console_sem_sessao(maquina.ctypes.windll))

    assert maquina._sessao_do_console_destravada() is False


class _Espelho:
    """Repassa tudo ao windll de verdade, menos o que o teste troca."""

    def __init__(self, real, trocas):
        self._real, self._trocas = real, trocas

    def __getattr__(self, nome):
        if nome in self._trocas:
            return self._trocas[nome]
        return getattr(self._real, nome)


def _sem_area_de_trabalho(real):
    falso = type("U", (), {"OpenInputDesktop": staticmethod(lambda *a: 0)})()
    return _Espelho(real, {"user32": falso})


def _console_sem_sessao(real):
    falso = type("K", (), {"WTSGetActiveConsoleSessionId":
                           staticmethod(lambda: 0xFFFFFFFF)})()
    return _Espelho(real, {"kernel32": falso})
