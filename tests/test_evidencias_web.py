from __future__ import annotations

from dataclasses import replace

import pytest

from cnd.core import fila
from cnd.core.modelos import Desfecho, ResultadoTentativa
from cnd.infra.db import conectar, garantir
from tests.conftest import criar_job

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def painel(monkeypatch, tmp_path):
    from cnd.infra.config import ConfigRede, carregar
    from cnd.web import app as modulo

    banco = tmp_path / "cnd.db"
    garantir(banco)
    cfg = replace(
        carregar(),
        banco=banco,
        pasta_evidencias=tmp_path / "evidencias",
        pasta_certidoes=tmp_path / "certidoes",
        rede=ConfigRede(nome="teste", papel="robo"),
    )
    monkeypatch.setattr(modulo, "cfg", cfg)

    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente, cfg


def _criar_lote(conn) -> int:
    cursor = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('teste', 'teste.xlsx')"
    )
    return int(cursor.lastrowid)


def test_detalhe_mostra_e_abre_evidencia_de_tentativa(painel):
    cliente, cfg = painel
    evidencia = cfg.pasta_evidencias / "SEFAZ_MA" / "11222333000181" / "portal.png"
    evidencia.parent.mkdir(parents=True, exist_ok=True)
    evidencia.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    with conectar(cfg.banco) as conn:
        lote = _criar_lote(conn)
        job_id = criar_job(conn, lote, orgao="SEFAZ_MA")
        job = fila.reivindicar(conn, "SEFAZ_MA")
        tentativa_id = fila.abrir_tentativa(conn, job, worker=0)
        fila.fechar_tentativa(
            conn,
            tentativa_id,
            ResultadoTentativa(Desfecho.CAPTCHA, evidencia=evidencia),
        )

    resposta = cliente.get(f"/job/{job_id}")
    assert resposta.status_code == 200
    assert f"/evidencias/tentativa/{tentativa_id}" in resposta.text
    assert "mini-evidencia" in resposta.text

    imagem = cliente.get(f"/evidencias/tentativa/{tentativa_id}")
    assert imagem.status_code == 200
    assert imagem.content.startswith(b"\x89PNG")


def test_evidencia_fora_da_pasta_configurada_nao_abre(painel, tmp_path):
    cliente, cfg = painel
    fora = tmp_path / "fora.png"
    fora.write_bytes(b"nao deveria abrir")

    with conectar(cfg.banco) as conn:
        lote = _criar_lote(conn)
        job_id = criar_job(conn, lote, orgao="SEFAZ_MA")
        job = fila.reivindicar(conn, "SEFAZ_MA")
        tentativa_id = fila.abrir_tentativa(conn, job, worker=0)
        fila.fechar_tentativa(
            conn,
            tentativa_id,
            ResultadoTentativa(Desfecho.CAPTCHA, evidencia=fora),
        )

    pagina = cliente.get(f"/job/{job_id}")
    assert pagina.status_code == 200
    assert f"/evidencias/tentativa/{tentativa_id}" not in pagina.text
    assert cliente.get(f"/evidencias/tentativa/{tentativa_id}").status_code == 404


def test_evidencia_apagada_do_disco_vira_so_caminho(painel):
    cliente, cfg = painel
    sumida = cfg.pasta_evidencias / "SEFAZ_MA" / "11222333000181" / "sumiu.png"

    with conectar(cfg.banco) as conn:
        lote = _criar_lote(conn)
        job_id = criar_job(conn, lote, orgao="SEFAZ_MA")
        job = fila.reivindicar(conn, "SEFAZ_MA")
        tentativa_id = fila.abrir_tentativa(conn, job, worker=0)
        fila.fechar_tentativa(
            conn,
            tentativa_id,
            ResultadoTentativa(Desfecho.CAPTCHA, evidencia=sumida),
        )

    pagina = cliente.get(f"/job/{job_id}")
    assert pagina.status_code == 200
    assert f"/evidencias/tentativa/{tentativa_id}" not in pagina.text
    assert "sumiu.png" in pagina.text
