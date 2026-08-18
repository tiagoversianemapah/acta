"""As rotas que estacionam, cancelam, retomam e furam a fila."""
from __future__ import annotations

from dataclasses import replace

import pytest

from cnd.core import controle, fila

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CABECALHO = {"X-CND-Senha": "segredo"}


@pytest.fixture
def painel(monkeypatch, tmp_path):
    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import conectar, garantir
    from cnd.web import app as modulo
    from tests.conftest import criar_job

    banco = tmp_path / "cnd.db"
    garantir(banco)
    conn = conectar(banco)
    try:
        lote = conn.execute("INSERT INTO lote (descricao, arquivo_origem) "
                            "VALUES ('t', 't.xlsx')").lastrowid
        criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
        criar_job(conn, lote, documento="00000000000002", orgao="CRF")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(modulo, "cfg", replace(
        carregar(), banco=banco,
        rede=ConfigRede(nome="PC 01", senha="segredo", maquinas=(),
                        papel="robo")))
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente, banco, lote


def _conn(banco):
    from cnd.infra.db import conectar
    return conectar(banco)


def test_estacionar_tira_da_fila(painel):
    cliente, banco, lote = painel
    r = cliente.post(f"/api/fila/{lote}/RFB_PJ", data={"acao": "estacionar"},
                     headers=CABECALHO)
    assert r.status_code == 200, r.text

    conn = _conn(banco)
    try:
        assert fila.reivindicar(conn, "RFB_PJ") is None
        assert fila.reivindicar(conn, "CRF") is not None, "a outra segue"
    finally:
        conn.close()


def test_retomar_devolve(painel):
    cliente, banco, lote = painel
    cliente.post(f"/api/fila/{lote}/RFB_PJ", data={"acao": "estacionar"},
                 headers=CABECALHO)
    cliente.post(f"/api/fila/{lote}/RFB_PJ", data={"acao": "retomar"},
                 headers=CABECALHO)
    conn = _conn(banco)
    try:
        assert fila.reivindicar(conn, "RFB_PJ") is not None
    finally:
        conn.close()


def test_cancelar_nao_apaga(painel):
    cliente, banco, lote = painel
    cliente.post(f"/api/fila/{lote}/CRF", data={"acao": "cancelar"},
                 headers=CABECALHO)
    conn = _conn(banco)
    try:
        assert fila.reivindicar(conn, "CRF") is None
        n = conn.execute("SELECT COUNT(*) AS n FROM job "
                         "WHERE orgao = 'CRF'").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1, "cancelar tira da fila, não do banco"


def test_agora_reativa_e_prioriza(painel):
    cliente, banco, lote = painel
    cliente.post(f"/api/fila/{lote}/CRF", data={"acao": "estacionar"},
                 headers=CABECALHO)
    cliente.post(f"/api/fila/{lote}/CRF", data={"acao": "agora"},
                 headers=CABECALHO)
    conn = _conn(banco)
    try:
        assert controle.situacao(conn, lote, "CRF").ativa
        assert controle.situacao(conn, lote, "CRF").prioridade > 0
    finally:
        conn.close()


def test_acao_desconhecida_e_recusada(painel):
    cliente, _, lote = painel
    r = cliente.post(f"/api/fila/{lote}/CRF", data={"acao": "explodir"},
                     headers=CABECALHO)
    assert r.status_code == 400
    assert "estacionar" in r.json()["detail"], "diz o que vale"


def test_sem_senha_nao_mexe_na_fila(painel):
    cliente, banco, lote = painel
    assert cliente.post(f"/api/fila/{lote}/CRF",
                        data={"acao": "cancelar"}).status_code == 401
    conn = _conn(banco)
    try:
        assert fila.reivindicar(conn, "CRF") is not None, "nada mudou"
    finally:
        conn.close()


def test_estado_expoe_a_situacao_da_fila(painel):
    cliente, _, lote = painel
    cliente.post(f"/api/fila/{lote}/CRF", data={"acao": "estacionar"},
                 headers=CABECALHO)
    orgaos = cliente.get("/api/estado", headers=CABECALHO).json()["orgaos"]
    por_codigo = {o["orgao"]: o for o in orgaos}
    assert por_codigo["CRF"]["situacao_fila"] == controle.ESTACIONADA
    assert por_codigo["RFB_PJ"]["situacao_fila"] == controle.ATIVA
