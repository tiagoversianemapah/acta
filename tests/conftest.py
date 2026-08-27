"""Peças compartilhadas pelos testes.

Todo teste roda contra um banco temporário, criado do zero e jogado fora
no fim. Nenhum teste toca no banco de produção nem na internet.
"""
from __future__ import annotations

import os
from pathlib import Path

# ANTES de importar `cnd`: o `config.toml` é da INSTALAÇÃO e não vai para o
# controle de versão, então num checkout limpo ele não existe — e o painel
# o lê na IMPORTAÇÃO do módulo. Sem isto, `pytest` falhava já na COLETA,
# com FileNotFoundError, e não em um teste: a suíte inteira ficava vermelha
# por causa de um arquivo que é correto não estar ali. O exemplo versionado
# serve de sobra — senha vazia, papel console, caminhos padrão.
RAIZ = Path(__file__).resolve().parent.parent
if not (RAIZ / "config.toml").exists():
    os.environ.setdefault("CND_CONFIG", str(RAIZ / "config.exemplo.toml"))

import pytest  # noqa: E402

from cnd.infra.db import conectar, criar_schema  # noqa: E402


@pytest.fixture(scope="session")
def janela():
    """UMA raiz do Tk para a sessão inteira de testes.

    Abrir e fechar várias raízes no mesmo processo falha — o interpretador
    Tcl não volta ao estado inicial, e a criação seguinte morre com
    "tk wasn't installed properly", que é a mensagem de instalação
    quebrada e manda procurar no lugar errado.

    Isso já estava escrito em test_link_anydesk.py, mas como comentário de
    UM arquivo: cada um dos três que abrem janela tinha a sua fixture de
    módulo, então a sessão criava e destruía três raízes. O terceiro
    quebrava — nem sempre, porque depende de quando o Tcl é coletado, e foi
    esse "nem sempre" que fez a suíte falhar em 3 de 24 execuções medidas
    (~12%), sempre nos mesmos 5 testes e sempre com a mesma mensagem.

    Escopo de sessão resolve na raiz: nenhuma segunda janela chega a
    existir. Reaproveitar não mistura estado porque cada teste redesenha os
    cartões do zero — é a mesma razão que já valia dentro de um arquivo.
    """
    pytest.importorskip("customtkinter")
    from cnd.desktop.app import Aplicativo

    app = Aplicativo()
    app.withdraw()          # existe, responde, mas não aparece na tela
    yield app
    app.destroy()


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
