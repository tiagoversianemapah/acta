"""Mexidas no perfil do Edge: limpeza de cookies e marca de saída limpa.

O que importa aqui é o "cirúrgica": a máquina do robô é um computador de
trabalho, e apagar cookie a mais tira alguém de sessões que não têm nada a
ver com a Receita.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from cnd.infra import perfil_edge

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

        removidos = perfil_edge.limpar_cookies("receitafederal.gov.br", tmp_path)

        assert removidos == 3
        assert _hosts(banco) == ["www.gov.br"]

    def test_nao_apaga_dominio_parecido(self, tmp_path):
        """'%dominio' pegaria 'falsareceitafederal.gov.br' junto."""
        banco = _banco(tmp_path, "Default", [
            "falsareceitafederal.gov.br",
            "receitafederal.gov.br.exemplo.com",
            ".receitafederal.gov.br",
        ])

        removidos = perfil_edge.limpar_cookies("receitafederal.gov.br", tmp_path)

        assert removidos == 1
        assert _hosts(banco) == ["falsareceitafederal.gov.br",
                                 "receitafederal.gov.br.exemplo.com"]

    def test_limpa_todos_os_perfis(self, tmp_path):
        padrao = _banco(tmp_path, "Default", [".receitafederal.gov.br"])
        outro = _banco(tmp_path, "Profile 1", [".receitafederal.gov.br",
                                               "intranet.local"])

        removidos = perfil_edge.limpar_cookies("receitafederal.gov.br", tmp_path)

        assert removidos == 2
        assert _hosts(padrao) == []
        assert _hosts(outro) == ["intranet.local"]

    def test_sem_edge_instalado_nao_quebra(self, tmp_path):
        assert perfil_edge.limpar_cookies("receitafederal.gov.br",
                                      tmp_path / "nao-existe") == 0

    def test_banco_ilegivel_nao_derruba_a_limpeza(self, tmp_path):
        """Edge aberto trava o arquivo. Vale registrar e seguir — a próxima
        reabertura tenta de novo."""
        quebrado = tmp_path / "Default" / "Network" / "Cookies"
        quebrado.parent.mkdir(parents=True)
        quebrado.write_bytes(b"isto nao e um banco sqlite")
        bom = _banco(tmp_path, "Profile 1", [".receitafederal.gov.br"])

        removidos = perfil_edge.limpar_cookies("receitafederal.gov.br", tmp_path)

        assert removidos == 1
        assert _hosts(bom) == []


class TestSaidaLimpa:
    """Matar o Edge à força faz ele voltar com "Restaurar páginas" — bolha
    que cobre a tela e rouba o foco de quem trabalha por coordenada."""

    def _preferencias(self, pasta, perfil: str, conteudo: dict):
        caminho = pasta / perfil / "Preferences"
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(conteudo), encoding="utf-8")
        return caminho

    def test_desfaz_a_marca_de_fechamento_anormal(self, tmp_path):
        caminho = self._preferencias(tmp_path, "Default", {
            "profile": {"exit_type": "Crashed", "exited_cleanly": False,
                        "name": "Pessoa 1"},
            "browser": {"window_placement": {"maximized": True}},
        })

        assert perfil_edge.marcar_saida_limpa(tmp_path) == 1

        dados = json.loads(caminho.read_text(encoding="utf-8"))
        assert dados["profile"]["exit_type"] == "Normal"
        assert dados["profile"]["exited_cleanly"] is True
        # O resto das preferências é do usuário e não pode ser perdido.
        assert dados["profile"]["name"] == "Pessoa 1"
        assert dados["browser"]["window_placement"]["maximized"] is True

    def test_perfil_ja_limpo_nao_e_reescrito(self, tmp_path):
        self._preferencias(tmp_path, "Default", {
            "profile": {"exit_type": "Normal", "exited_cleanly": True},
        })

        assert perfil_edge.marcar_saida_limpa(tmp_path) == 0

    def test_preferencias_corrompidas_nao_derrubam(self, tmp_path):
        ruim = tmp_path / "Default" / "Preferences"
        ruim.parent.mkdir(parents=True)
        ruim.write_text("{isto não é json", encoding="utf-8")
        self._preferencias(tmp_path, "Profile 1", {
            "profile": {"exit_type": "Crashed", "exited_cleanly": False},
        })

        assert perfil_edge.marcar_saida_limpa(tmp_path) == 1


class TestOndeEstaOEdge:
    def test_usa_a_pasta_local_do_windows(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert perfil_edge.raiz_do_edge() == (
            tmp_path / "Microsoft" / "Edge" / "User Data"
        )

    def test_sem_a_variavel_cai_no_perfil_do_usuario(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(perfil_edge.Path, "home", classmethod(lambda _cls: tmp_path))

        assert perfil_edge.raiz_do_edge().is_relative_to(tmp_path)


@pytest.mark.parametrize("perfil", ["Default", "Profile 3"])
def test_encontra_o_banco_de_cada_perfil(tmp_path, perfil):
    _banco(tmp_path, perfil, ["exemplo.com"])

    assert [p.parent.parent.name for p in perfil_edge.bancos_de_cookies(tmp_path)] == [
        perfil
    ]
