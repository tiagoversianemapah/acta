"""A senha da rede fecha o painel inteiro, não só a API.

Estes testes existem porque a versão anterior protegia `/api/*` e deixava
abertas justamente as rotas que carregam dado de cliente: as páginas e o
download do pacote de certidões.
"""
from __future__ import annotations

import base64

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def painel(monkeypatch, tmp_path):
    """Sobe o painel com um banco vazio e senha configurada."""
    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir

    banco = tmp_path / "cnd.db"
    garantir(banco)

    from dataclasses import replace

    from cnd.web import app as modulo

    monkeypatch.setattr(
        modulo, "cfg",
        replace(carregar(), banco=banco, rede=ConfigRede(nome="teste",
                                                         senha="segredo")),
    )
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente


ROTAS_PROTEGIDAS = [
    "/",                    # painel: razão social e CNPJ da carteira
    "/jobs",                # lista completa dos itens
    "/saude",
    "/api/estado",
    "/relatorio/1.xlsx",    # a planilha do lote
    "/relatorio/1.zip",     # os PDFs das certidões
]


@pytest.mark.parametrize("rota", ROTAS_PROTEGIDAS)
def test_sem_senha_nao_passa(painel, rota):
    assert painel.get(rota).status_code == 401


@pytest.mark.parametrize("rota", ROTAS_PROTEGIDAS)
def test_senha_errada_nao_passa(painel, rota):
    resposta = painel.get(rota, headers={"X-CND-Senha": "chute"})
    assert resposta.status_code == 401


def test_o_aplicativo_de_mesa_entra_pelo_cabecalho(painel):
    resposta = painel.get("/api/estado", headers={"X-CND-Senha": "segredo"})
    assert resposta.status_code == 200
    assert resposta.json()["maquina"] == "teste"


def test_o_navegador_entra_por_basic(painel):
    # Basic porque é o que o navegador sabe fazer sozinho — ele mesmo abre
    # a caixa de login e lembra da resposta durante a sessão.
    credencial = base64.b64encode(b"cnd:segredo").decode()
    resposta = painel.get("/", headers={"Authorization": f"Basic {credencial}"})
    assert resposta.status_code == 200


def test_a_recusa_pede_login_ao_navegador(painel):
    assert painel.get("/").headers["WWW-Authenticate"].startswith("Basic")


def test_ping_continua_livre(painel):
    """O verificador externo não tem como digitar senha — e /ping não
    devolve dado de cliente nenhum, só sinal de vida e tamanho da fila."""
    resposta = painel.get("/ping")
    assert resposta.status_code in (200, 503)
    assert "itens_na_fila" in resposta.json()


def test_sem_senha_configurada_o_painel_fica_aberto(monkeypatch, tmp_path):
    """Instalação de máquina única, ouvindo só em 127.0.0.1: exigir senha
    ali seria atrito sem ganho."""
    from dataclasses import replace

    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir
    from cnd.web import app as modulo

    banco = tmp_path / "cnd.db"
    garantir(banco)
    monkeypatch.setattr(modulo, "cfg",
                        replace(carregar(), banco=banco, rede=ConfigRede()))
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        assert cliente.get("/api/estado").status_code == 200


def test_montar_duas_vezes_nao_duplica_rotas():
    """O roteador nasce dentro de montar(); global, ele acumularia."""
    from cnd.infra.config import carregar
    from cnd.infra.db import conectar_leitura
    from cnd.web import api

    cfg = carregar()
    primeiro = api.montar(lambda: cfg, lambda: conectar_leitura(cfg.banco))
    segundo = api.montar(lambda: cfg, lambda: conectar_leitura(cfg.banco))
    assert len(primeiro.routes) == len(segundo.routes)
