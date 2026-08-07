"""Acesso remoto e identificação das máquinas."""
from __future__ import annotations

import pytest

from cnd.desktop import acesso
from cnd.desktop.remoto import EstadoRemoto
from cnd.infra.config import Maquina


def test_normaliza_o_numero_como_o_anydesk_mostra():
    # O AnyDesk exibe "123 456 789" e é assim que a pessoa copia.
    assert acesso.normalizar("123 456 789") == "123456789"
    assert acesso.normalizar("  123456789 ") == "123456789"


def test_apelido_passa_inteiro():
    # Em endereço com apelido os caracteres fazem parte do nome.
    assert acesso.normalizar("mapah-cnd@ad") == "mapah-cnd@ad"


def test_sem_numero_a_mensagem_explica_o_que_fazer():
    with pytest.raises(RuntimeError, match="não tem o número do AnyDesk"):
        acesso.abrir("   ")


def test_cartao_mostra_o_orgao_e_o_computador():
    maquina = Maquina(nome="PC-CND-01", url="http://192.168.0.21:8000",
                      orgao="Receita Federal", anydesk="123456789")
    estado = EstadoRemoto(maquina, online=True, dados={"maquina": "PC-CND-01"})

    assert estado.rotulo == "Receita Federal"
    assert "PC-CND-01" in estado.subtitulo
    assert "192.168.0.21:8000" in estado.subtitulo


def test_sem_orgao_o_titulo_e_o_nome_da_maquina():
    estado = EstadoRemoto(Maquina(nome="Esta máquina", url=""), online=True)
    assert estado.rotulo == "Esta máquina"
    assert estado.subtitulo == ""
