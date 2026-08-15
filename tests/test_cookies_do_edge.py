"""Limpeza cirúrgica dos cookies do Edge.

O que importa aqui é o "cirúrgica": a máquina do robô é um computador de
trabalho, e apagar cookie a mais tira alguém de sessões que não têm nada a
ver com a Receita.
"""
from __future__ import annotations

import sqlite3

import pytest

from cnd.infra import cookies

ESQUEMA = """
CREATE TABLE cookies (
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL
)
"""


def _banco(pasta, perfil: str, hosts: list[str]):
    caminho = pasta / perfil / "Network" / "Cookies"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho)
    try:
        conexao.execute(ESQUEMA)
        conexao.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            [(i, host, f"c{i}", "x") for i, host in enumerate(hosts)],
        )
        conexao.commit()
    finally:
        conexao.close()
    return caminho


def _hosts(caminho) -> list[str]:
    conexao = sqlite3.connect(caminho)
    try:
        return [linha[0] for linha in
                conexao.execute("SELECT host_key FROM cookies ORDER BY host_key")]
    finally:
        conexao.close()


class TestLimpezaPorDominio:
    def test_apaga_o_dominio_e_os_subdominios(self, tmp_path):
        banco = _banco(tmp_path, "Default", [
            "servicos.receitafederal.gov.br",
            ".receitafederal.gov.br",
            "receitafederal.gov.br",
            "www.gov.br",
        ])

        removidos = cookies.limpar_dominio("receitafederal.gov.br", tmp_path)

        assert removidos == 3
        assert _hosts(banco) == ["www.gov.br"]

    def test_nao_apaga_dominio_parecido(self, tmp_path):
        """'%dominio' pegaria 'falsareceitafederal.gov.br' junto."""
        banco = _banco(tmp_path, "Default", [
            "falsareceitafederal.gov.br",
            "receitafederal.gov.br.exemplo.com",
            ".receitafederal.gov.br",
        ])

        removidos = cookies.limpar_dominio("receitafederal.gov.br", tmp_path)

        assert removidos == 1
        assert _hosts(banco) == ["falsareceitafederal.gov.br",
                                 "receitafederal.gov.br.exemplo.com"]

    def test_limpa_todos_os_perfis(self, tmp_path):
        padrao = _banco(tmp_path, "Default", [".receitafederal.gov.br"])
        outro = _banco(tmp_path, "Profile 1", [".receitafederal.gov.br",
                                               "intranet.local"])

        removidos = cookies.limpar_dominio("receitafederal.gov.br", tmp_path)

        assert removidos == 2
        assert _hosts(padrao) == []
        assert _hosts(outro) == ["intranet.local"]

    def test_sem_edge_instalado_nao_quebra(self, tmp_path):
        assert cookies.limpar_dominio("receitafederal.gov.br",
                                      tmp_path / "nao-existe") == 0

    def test_banco_ilegivel_nao_derruba_a_limpeza(self, tmp_path):
        """Edge aberto trava o arquivo. Vale registrar e seguir — a próxima
        reabertura tenta de novo."""
        quebrado = tmp_path / "Default" / "Network" / "Cookies"
        quebrado.parent.mkdir(parents=True)
        quebrado.write_bytes(b"isto nao e um banco sqlite")
        bom = _banco(tmp_path, "Profile 1", [".receitafederal.gov.br"])

        removidos = cookies.limpar_dominio("receitafederal.gov.br", tmp_path)

        assert removidos == 1
        assert _hosts(bom) == []


class TestOndeEstaOEdge:
    def test_usa_a_pasta_local_do_windows(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert cookies.raiz_do_edge() == (
            tmp_path / "Microsoft" / "Edge" / "User Data"
        )

    def test_sem_a_variavel_cai_no_perfil_do_usuario(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(cookies.Path, "home", classmethod(lambda _cls: tmp_path))

        assert cookies.raiz_do_edge().is_relative_to(tmp_path)


@pytest.mark.parametrize("perfil", ["Default", "Profile 3"])
def test_encontra_o_banco_de_cada_perfil(tmp_path, perfil):
    _banco(tmp_path, perfil, ["exemplo.com"])

    assert [p.parent.parent.name for p in cookies.bancos_de_cookies(tmp_path)] == [
        perfil
    ]
