"""Máquina muda mostra o último estado conhecido, não um cartão vazio.

É justamente quando ela cai que se quer saber dela. "Estava em 1.204 de
2.849 às 17h22" orienta quem vai decidir se espera ou vai até lá; nada
nenhum não orienta.
"""
from __future__ import annotations

import json

import pytest

from cnd.desktop import remoto
from cnd.infra.config import Maquina

MAQUINA = Maquina("PC-01", "http://10.0.0.9:8000", "RECEITA FEDERAL")


@pytest.fixture(autouse=True)
def memoria_isolada(tmp_path, monkeypatch):
    """Cada teste com a sua pasta: o cache é um arquivo no disco."""
    monkeypatch.setattr(remoto, "_arquivo_de_memoria",
                        lambda m: tmp_path / "estado.json")


def _dados(concluidos: int) -> dict:
    return {"maquina": "PC-01", "robo_ativo": False, "orgaos": [{
        "orgao": "RFB_PJ", "total": 2849, "concluidos": concluidos,
        "pendentes": 1, "falhados": 0, "por_desfecho": {},
        "disjuntor": "FECHADO"}]}


def test_resposta_boa_fica_guardada(tmp_path, monkeypatch):
    monkeypatch.setattr(remoto, "_pedir", lambda *a, **k: _dados(1204))

    estado = remoto.consultar(MAQUINA)

    assert estado.online
    guardado = json.loads((tmp_path / "estado.json").read_text(encoding="utf-8"))
    assert guardado["orgaos"][0]["concluidos"] == 1204
    assert guardado["lido_em"], "sem a hora, o dado antigo não se identifica"


def test_maquina_muda_devolve_o_que_sabia(monkeypatch):
    monkeypatch.setattr(remoto, "_pedir", lambda *a, **k: _dados(1204))
    remoto.consultar(MAQUINA)

    def cair(*_a, **_k):
        raise OSError("timed out")

    monkeypatch.setattr(remoto, "_pedir", cair)
    estado = remoto.consultar(MAQUINA)

    assert not estado.online
    assert estado.tem_memoria
    assert estado.concluidos == 1204
    assert estado.lido_em


def test_maquina_que_nunca_respondeu_nao_inventa(monkeypatch):
    def cair(*_a, **_k):
        raise OSError("timed out")

    monkeypatch.setattr(remoto, "_pedir", cair)
    estado = remoto.consultar(MAQUINA)

    assert not estado.tem_memoria
    assert estado.total == 0


def test_a_memoria_e_atualizada_a_cada_resposta_boa(monkeypatch):
    monkeypatch.setattr(remoto, "_pedir", lambda *a, **k: _dados(100))
    remoto.consultar(MAQUINA)
    monkeypatch.setattr(remoto, "_pedir", lambda *a, **k: _dados(900))
    remoto.consultar(MAQUINA)

    def cair(*_a, **_k):
        raise OSError("timed out")

    monkeypatch.setattr(remoto, "_pedir", cair)
    assert remoto.consultar(MAQUINA).concluidos == 900


def test_estado_antigo_continua_marcado_como_offline(monkeypatch):
    """A memória não pode fazer a máquina parecer viva — a etiqueta tem de
    continuar dizendo que ela não respondeu."""
    monkeypatch.setattr(remoto, "_pedir", lambda *a, **k: _dados(500))
    remoto.consultar(MAQUINA)

    def cair(*_a, **_k):
        raise OSError("timed out")

    monkeypatch.setattr(remoto, "_pedir", cair)
    assert remoto.consultar(MAQUINA).situacao == ("Sem resposta", "cinza")
