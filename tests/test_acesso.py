"""Acesso remoto e identificação das máquinas."""
from __future__ import annotations

from pathlib import Path

import pytest

from cnd.desktop import acesso
from cnd.desktop.remoto import EstadoRemoto
from cnd.infra.config import Maquina


def test_normaliza_o_numero_como_o_anydesk_mostra():
    # O AnyDesk exibe "123 456 789" e é assim que a pessoa copia.
    assert acesso.normalizar("123 456 789") == "123456789"
    assert acesso.normalizar("  123456789 ") == "123456789"
    assert acesso.normalizar("1681470186") == "1681470186"


def test_apelido_passa_inteiro():
    # Em endereço com apelido os caracteres fazem parte do nome.
    assert acesso.normalizar("mapah-cnd@ad") == "mapah-cnd@ad"


def test_sem_numero_a_mensagem_explica_o_que_fazer():
    with pytest.raises(RuntimeError, match="não tem o número do AnyDesk"):
        acesso.abrir("   ")


def test_abrir_chama_o_executavel_quando_ele_existe(monkeypatch):
    chamadas = []
    caminho = Path(r"C:\Program Files\AnyDesk\AnyDesk.exe")
    monkeypatch.setattr(acesso, "encontrar_anydesk", lambda: caminho)
    monkeypatch.setattr(acesso.subprocess, "Popen", chamadas.append)

    acesso.abrir("1 681 470 186")

    assert chamadas == [[str(caminho), "1681470186"]]


def test_sem_executavel_e_sem_protocolo_mostra_mensagem_clara(monkeypatch):
    monkeypatch.setattr(acesso, "encontrar_anydesk", lambda: None)
    monkeypatch.setattr(acesso, "_protocolo_anydesk_registrado", lambda: False)

    with pytest.raises(RuntimeError, match="AnyDesk não foi encontrado"):
        acesso.abrir("1681470186")


def test_fallback_usa_protocolo_quando_ele_existe(monkeypatch):
    chamadas = []
    monkeypatch.setattr(acesso, "encontrar_anydesk", lambda: None)
    monkeypatch.setattr(acesso, "_protocolo_anydesk_registrado", lambda: True)
    monkeypatch.setattr(acesso.os, "startfile", chamadas.append)

    acesso.abrir("1681470186")

    assert chamadas == ["anydesk:1681470186"]


def test_cartao_mostra_o_orgao_e_o_computador():
    maquina = Maquina(nome="PC-CND-01", url="http://192.168.0.21:8000",
                      orgao="Receita Federal", anydesk="123456789")
    estado = EstadoRemoto(maquina, online=True, dados={"maquina": "PC-CND-01"})

    assert estado.rotulo == "Receita Federal"
    assert "PC-CND-01" in estado.subtitulo
    assert "192.168.0.21:8000" in estado.subtitulo


def test_sem_orgao_o_titulo_e_o_nome_da_maquina():
    estado = EstadoRemoto(Maquina(nome="MEU-PC", url=""), online=True)
    assert estado.rotulo == "MEU-PC"
    # Sem endereço de rede é o próprio computador; a legenda diz isso em
    # vez de repetir o nome que já está no título.
    assert estado.subtitulo == "este computador"


def test_o_nome_vira_link_quando_ha_anydesk():
    com = Maquina("PC-01", "http://x", "ACTA CND FEDERAL", "123456789")
    assert EstadoRemoto(com, online=True).acessavel


def test_a_maquina_muda_continua_acessivel():
    """Máquina fora do ar é justamente quando alguém precisa entrar nela."""
    caida = Maquina("PC-01", "http://x", "ACTA CND FEDERAL", "123456789")
    assert EstadoRemoto(caida, online=False, erro="não respondeu").acessavel


def test_sem_anydesk_o_nome_nao_e_link():
    assert not EstadoRemoto(Maquina("PC-01", "http://x")).acessavel
