"""Fila que acabou não pede decisão nenhuma na tela.

Em 22/09/2026 a Prefeitura de Vitória apareceu no painel da máquina como
"Cancelada", com 0 na fila e 100% concluído, e ainda com o botão "Rodar
agora" ao lado. Os dois enganam: o rótulo é história — a planilha foi
cancelada quando ainda tinha item —, e o botão reativa a planilha sem rodar
coisa alguma, porque `fila.reivindicar` só entrega PENDING e RETRY_WAIT.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import replace as _replace
from pathlib import Path

import pytest

from cnd.core import controle, fila
from cnd.core.modelos import Desfecho, ResultadoTentativa
from cnd.infra.config import ConfigRede, carregar
from cnd.infra.db import conectar, criar_schema
from tests.conftest import RAIZ, criar_job


@pytest.fixture
def painel(tmp_path, monkeypatch):
    """O painel desta máquina, com banco próprio."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    # O app lê o config da raiz ao ser importado, e um checkout limpo não tem
    # config.toml: o exemplo serve, e some junto com o teste.
    raiz_config = RAIZ / "config.toml"
    criado = not raiz_config.exists()
    if criado:
        shutil.copy(RAIZ / "config.exemplo.toml", raiz_config)
    try:
        from cnd.web import app as modulo

        banco = tmp_path / "cnd.db"
        conn = conectar(banco)
        criar_schema(conn)
        monkeypatch.setattr(modulo, "cfg", _replace(
            carregar(RAIZ / "config.exemplo.toml"), banco=banco,
            rede=ConfigRede(nome="PC 01", senha="")))
        yield conn, fastapi_testclient.TestClient(modulo.app)
        conn.close()
    finally:
        if criado:
            raiz_config.unlink(missing_ok=True)


def _linha_do_orgao(pagina: str, orgao: str) -> str:
    for linha in re.findall(r"<tr[^>]*>(?:(?!</tr>).)*</tr>", pagina, re.S):
        if f">{orgao}<" in linha:
            return linha
    return ""


def _fila_cancelada_e_terminada(conn, *, sobra: int = 0) -> int:
    lote = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('t', 'c.xlsx')"
    ).lastrowid
    criar_job(conn, lote, documento="11222333000181", orgao="GOIANIA")
    for n in range(sobra):
        criar_job(conn, lote, documento=f"1144477700016{n}", orgao="GOIANIA")
    job = fila.reivindicar(conn, "GOIANIA")
    fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.POSITIVA))
    controle.definir(conn, lote, "GOIANIA", controle.CANCELADA)
    conn.commit()
    return lote


def test_fila_terminada_aparece_como_concluida(painel):
    conn, cliente = painel
    _fila_cancelada_e_terminada(conn)

    with cliente as c:
        linha = _linha_do_orgao(c.get("/").text, "GOIANIA")

    assert "Concluída" in linha
    assert "Cancelada" not in linha, "o rótulo antigo vira história, não estado"


def test_fila_terminada_nao_oferece_rodar_agora(painel):
    """O botão reativaria a planilha e não rodaria nada."""
    conn, cliente = painel
    _fila_cancelada_e_terminada(conn)

    with cliente as c:
        linha = _linha_do_orgao(c.get("/").text, "GOIANIA")

    assert "Rodar agora" not in linha
    assert "Estacionar" not in linha and "Cancelar" not in linha


def test_fila_cancelada_com_item_de_sobra_ainda_oferece_rodar_agora(painel):
    """Aí o botão faz o que diz: devolve a planilha à fila e ela anda."""
    conn, cliente = painel
    _fila_cancelada_e_terminada(conn, sobra=2)

    with cliente as c:
        linha = _linha_do_orgao(c.get("/").text, "GOIANIA")

    assert "Cancelada" in linha
    assert "Rodar agora" in linha
