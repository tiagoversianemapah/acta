"""Consultar itens busca em todas as máquinas por padrão.

Quem liga perguntando "o que houve com a Fulana Ltda" não sabe em qual
máquina ela está — obrigar a escolher a máquina antes de procurar é pedir
a resposta como pergunta.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from cnd.desktop import app as tela
from cnd.desktop.app import ESTA_MAQUINA, TODAS_AS_MAQUINAS, TODAS_AS_PLANILHAS
from cnd.infra.config import Maquina

FEDERAL = Maquina(nome="PC-01", url="http://10.0.0.1:8000",
                  orgao="RECEITA FEDERAL")
ESTADUAL = Maquina(nome="PC-02", url="http://10.0.0.2:8000",
                   orgao="SEFAZ GOIÁS")


@pytest.fixture(scope="module")
def janela():
    from cnd.desktop.app import Aplicativo

    app = Aplicativo()
    app.withdraw()
    yield app
    app.destroy()


def _item(job_id: int, nome: str, quando: str = "2026-08-10T10:00:00") -> dict:
    return {"id": job_id, "nome": nome, "documento": "16958497000195",
            "status": "CONCLUIDO", "desfecho": "NEGATIVA",
            "atualizado_em": quando}


def _com_rede(janela, monkeypatch, maquinas, papel="console"):
    """A config é congelada de propósito: troca-se por outra, não se muta."""
    rede = replace(janela.cfg.rede, maquinas=tuple(maquinas), papel=papel)
    monkeypatch.setattr(janela, "cfg", replace(janela.cfg, rede=rede))


@pytest.fixture
def duas_maquinas(janela, monkeypatch):
    """Console sem robô próprio, olhando duas máquinas."""
    _com_rede(janela, monkeypatch, [FEDERAL, ESTADUAL])
    janela.filtro_maquina.configure(values=[TODAS_AS_MAQUINAS])
    janela.filtro_maquina.set(TODAS_AS_MAQUINAS)
    return janela


def test_todas_as_maquinas_e_o_padrao(duas_maquinas):
    duas_maquinas._atualizar_filtros_de_itens()
    assert duas_maquinas.filtro_maquina.get() == TODAS_AS_MAQUINAS


def test_busca_junta_as_duas_e_diz_de_onde_veio(duas_maquinas, monkeypatch):
    def responder(maquina, _senha, **_f):
        return [_item(1, "Fulana Ltda")] if maquina is FEDERAL \
            else [_item(1, "Beltrana ME")]

    monkeypatch.setattr(tela.remoto, "listar_itens", responder)

    itens, mudas = duas_maquinas._buscar_itens({})

    assert mudas == []
    assert {(i["nome"], i["origem"]) for i in itens} == {
        ("Fulana Ltda", "RECEITA FEDERAL"),
        ("Beltrana ME", "SEFAZ GOIÁS"),
    }


def test_maquina_muda_nao_apaga_o_resultado_das_outras(duas_maquinas,
                                                       monkeypatch):
    """O pior desfecho seria dizer "nada encontrado" porque uma caiu."""
    monkeypatch.setattr(
        tela.remoto, "listar_itens",
        lambda m, _s, **_f: [_item(1, "Fulana Ltda")] if m is FEDERAL else None)

    itens, mudas = duas_maquinas._buscar_itens({})

    assert [i["nome"] for i in itens] == ["Fulana Ltda"]
    assert mudas == ["SEFAZ GOIÁS"], "a máquina calada tem de ser nomeada"


def test_maquina_unica_muda_devolve_none_e_nao_lista_vazia(duas_maquinas,
                                                           monkeypatch):
    duas_maquinas.filtro_maquina.configure(
        values=[TODAS_AS_MAQUINAS, "RECEITA FEDERAL"])
    duas_maquinas.filtro_maquina.set("RECEITA FEDERAL")
    monkeypatch.setattr(tela.remoto, "listar_itens", lambda *a, **k: None)

    itens, _ = duas_maquinas._buscar_itens({})

    assert itens is None


def test_o_banco_local_entra_quando_esta_maquina_roda_robo(janela, monkeypatch):
    _com_rede(janela, monkeypatch, [], papel="robo")
    janela.filtro_maquina.configure(values=[TODAS_AS_MAQUINAS])
    janela.filtro_maquina.set(TODAS_AS_MAQUINAS)
    monkeypatch.setattr(tela, "listar_itens",
                        lambda _cfg, **_f: [_item(7, "Daqui SA")])

    itens, mudas = janela._buscar_itens({})

    assert [(i["nome"], i["origem"]) for i in itens] == [("Daqui SA",
                                                          ESTA_MAQUINA)]
    assert mudas == []


def test_mais_recente_primeiro(duas_maquinas, monkeypatch):
    monkeypatch.setattr(
        tela.remoto, "listar_itens",
        lambda m, _s, **_f: [_item(1, "Velha", "2026-01-02T09:00:00")]
        if m is FEDERAL else [_item(2, "Nova", "2026-08-09T09:00:00")])

    itens, _ = duas_maquinas._buscar_itens({})

    assert [i["nome"] for i in itens] == ["Nova", "Velha"]


def test_filtro_de_planilha_desliga_com_todas_as_maquinas(duas_maquinas):
    """Lote é numeração interna de cada máquina — o lote 3 de uma não é o
    da outra, e cruzá-los mostraria planilhas sem relação nenhuma."""
    duas_maquinas._atualizar_filtros_de_itens()

    assert duas_maquinas.filtro_planilha.get() == TODAS_AS_PLANILHAS
    assert duas_maquinas._lotes_da_maquina == {}
