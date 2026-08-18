"""Renomear a máquina pela rede, sem entrar nela.

O nome vive no config.toml da instalação, que não viaja com o pacote de
atualização. Enquanto uma máquina rodava uma automação só, "PC Receita
Federal 01" era verdade; com várias, virou mentira — e corrigir exigia
AnyDesk máquina a máquina.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CONFIG = '''# comentário que precisa sobreviver
[rede]
nome  = "PC Receita Federal 01"     # comentário na própria linha
senha = "segredo"

[orgaos.RFB_PJ]
ativo = true
nome  = "RECEITA FEDERAL"
'''


@pytest.fixture
def painel(monkeypatch, tmp_path):
    from cnd.infra import config as modulo_config
    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir

    arquivo = tmp_path / "config.toml"
    arquivo.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(modulo_config, "CAMINHO_PADRAO", arquivo)

    banco = tmp_path / "cnd.db"
    garantir(banco)

    from cnd.web import app as modulo

    monkeypatch.setattr(
        modulo, "cfg",
        replace(carregar(), banco=banco,
                rede=ConfigRede(nome="PC Receita Federal 01", senha="segredo")),
    )
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente, arquivo


def test_renomeia_preservando_os_comentarios(painel):
    cliente, arquivo = painel
    r = cliente.post("/api/identidade", data={"nome": "PC 01"},
                     headers={"X-CND-Senha": "segredo"})
    assert r.status_code == 200, r.text

    texto = arquivo.read_text(encoding="utf-8")
    assert 'nome  = "PC 01"' in texto
    assert "comentário que precisa sobreviver" in texto
    assert "comentário na própria linha" in texto, "o comentário da linha fica"
    assert 'senha = "segredo"' in texto, "nenhuma outra chave se mexe"


def test_nao_renomeia_o_orgao(painel):
    """Cada órgão também tem `nome`; trocar o primeiro renomearia a Receita."""
    cliente, arquivo = painel
    cliente.post("/api/identidade", data={"nome": "PC 01"},
                 headers={"X-CND-Senha": "segredo"})
    assert 'nome  = "RECEITA FEDERAL"' in arquivo.read_text(encoding="utf-8")


@pytest.mark.parametrize("nome", ["", "   ", 'com "aspas"', "a" * 61])
def test_recusa_nome_impossivel(painel, nome):
    cliente, _ = painel
    r = cliente.post("/api/identidade", data={"nome": nome},
                     headers={"X-CND-Senha": "segredo"})
    # 422 quando o próprio FastAPI barra (campo vazio), 400 quando a regra
    # daqui barra. O que importa é não ter renomeado.
    assert r.status_code in (400, 422), r.text


def test_sem_senha_nao_renomeia(painel):
    cliente, arquivo = painel
    assert cliente.post("/api/identidade", data={"nome": "PC 01"}).status_code == 401
    assert "PC Receita Federal 01" in arquivo.read_text(encoding="utf-8")
