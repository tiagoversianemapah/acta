"""Uma janela só do ACTA por computador.

Duas cópias leem o mesmo banco, mostram números que divergem por segundos e
podem mandar a mesma máquina começar a trabalhar. E quem clicou duas vezes
no atalho não percebe que abriu a segunda — só vê a janela que já estava
aberta ficar para trás.
"""
from __future__ import annotations

import sys

import pytest

from cnd.desktop import instancia

windows = pytest.mark.skipif(sys.platform != "win32",
                             reason="o cadeado é uma API do Windows")


@pytest.fixture
def cadeado():
    """Um nome só deste teste.

    Usar o cadeado real faria o teste depender de não haver ACTA aberto no
    computador — e teste que falha por causa do estado da máquina não diz
    nada sobre o código.
    """
    import uuid

    return f"Local\\Acta.Teste.{uuid.uuid4().hex}"


@windows
def test_a_primeira_copia_toma_posse(cadeado):
    assert instancia.tomar_posse(cadeado) is True


@windows
def test_a_segunda_copia_nao_toma(cadeado):
    """Segunda chamada no mesmo processo é o mesmo caso de dois processos:
    o Windows responde ERROR_ALREADY_EXISTS para o nome já reservado."""
    instancia.tomar_posse(cadeado)
    assert instancia.tomar_posse(cadeado) is False


@windows
def test_o_cadeado_fica_guardado(cadeado):
    """Sem alguém guardando o handle, o Windows libera o nome e a cópia
    seguinte se acha sozinha."""
    instancia.tomar_posse(cadeado)
    assert instancia._cadeado


@windows
def test_janela_inexistente_nao_quebra():
    """Se a outra cópia fechou entre a checagem e a busca, não há o que
    levantar — e isso não pode virar erro na cara de quem abriu."""
    assert instancia.trazer_para_frente("janela que não existe 8f3a") is False


def test_o_nome_do_cadeado_e_proprio():
    """Nome genérico colidiria com outro programa e um impediria o outro
    de abrir, sem nenhuma pista do motivo."""
    assert "Acta" in instancia.NOME_DO_CADEADO
