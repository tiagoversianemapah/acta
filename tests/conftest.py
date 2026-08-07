"""Peças compartilhadas pelos testes.

Todo teste roda contra um banco temporário, criado do zero e jogado fora
no fim. Nenhum teste toca no banco de produção nem na internet.
"""
from __future__ import annotations

import pytest

from cnd.infra.db import conectar, criar_schema


@pytest.fixture
def conn(tmp_path):
    conexao = conectar(tmp_path / "teste.db")
    criar_schema(conexao)
    yield conexao
    conexao.close()


@pytest.fixture
def lote(conn):
    cursor = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('teste', 'teste.xlsx')"
    )
    return cursor.lastrowid


def criar_job(conn, lote_id: int, documento: str = "11222333000181",
              orgao: str = "FAKE", nome: str = "EMPRESA TESTE") -> int:
    conn.execute(
        "INSERT INTO empresa (documento, tipo_documento, nome) VALUES (?, 'CNPJ', ?) "
        "ON CONFLICT (documento) DO NOTHING",
        (documento, nome),
    )
    empresa_id = conn.execute(
        "SELECT id FROM empresa WHERE documento = ?", (documento,)
    ).fetchone()["id"]
    cursor = conn.execute(
        "INSERT INTO job (lote_id, empresa_id, orgao) VALUES (?, ?, ?)",
        (lote_id, empresa_id, orgao),
    )
    return cursor.lastrowid
