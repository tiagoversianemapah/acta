"""Enviar planilha: mandar o arquivo, depois dizer o que fazer com cada aba.

As abas de um arquivo só existem depois de abri-lo, então a escolha não tem
como ser oferecida antes. E planilha de carteira vem com RFB e CRF no mesmo
arquivo — mandá-lo uma vez por automação seria trabalho repetido para um
problema que é de tela, não de dados.
"""
from __future__ import annotations

import re
import urllib.parse as up
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CNPJ_A = "33949051000113"
CNPJ_B = "06031097000186"


@pytest.fixture
def planilha(tmp_path) -> Path:
    livro = Workbook()
    rfb = livro.active
    rfb.title = "RFB"
    rfb.append(["Empresa", "Doc"])
    rfb.append(["EMPRESA A", CNPJ_A])
    livre = livro.create_sheet("Clientes GO")
    livre.append(["Empresa", "Doc"])
    livre.append(["EMPRESA B", CNPJ_B])
    livro.create_sheet("Instruções").append(["leia-me"])
    caminho = tmp_path / "carteira.xlsx"
    livro.save(caminho)
    return caminho


@pytest.fixture
def painel(monkeypatch, tmp_path):
    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir
    from cnd.web import app as modulo

    banco = tmp_path / "cnd.db"
    garantir(banco)
    base = carregar()
    monkeypatch.setattr(modulo, "cfg", replace(
        base, banco=banco,
        rede=ConfigRede(nome="PC 01", senha="", maquinas=(), papel="robo")))
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente, modulo


def _enviar(cliente, planilha: Path) -> str:
    r = cliente.post("/acoes/maquina/0/planilha",
                     files={"arquivo": (planilha.name, planilha.read_bytes())},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


def test_a_tela_mostra_todas_as_abas(painel, planilha):
    """Inclusive as que não são automação: sumir com elas faria procurar."""
    cliente, _ = painel
    pagina = cliente.get(f"/planilha/{_enviar(cliente, planilha)}").text
    abas = re.findall(r'<select name="orgao__([^"]+)"', pagina)
    assert set(abas) == {"RFB", "Clientes GO", "Instruções"}


def test_aba_vazia_nao_pode_ser_escolhida(painel, planilha):
    cliente, _ = painel
    pagina = cliente.get(f"/planilha/{_enviar(cliente, planilha)}").text
    trecho = pagina[pagina.index('orgao__Instruções'):][:120]
    assert "disabled" in trecho, "aba sem linhas não deve ser importável"


def test_uma_confirmacao_resolve_a_planilha_inteira(painel, planilha):
    """O caso que motivou tudo: RFB e CRF no mesmo arquivo."""
    cliente, _ = painel
    token = _enviar(cliente, planilha)
    r = cliente.post(f"/planilha/{token}/confirmar", follow_redirects=False,
                     data={"orgao__RFB": "RFB_PJ",
                           "orgao__Clientes GO": "RFB_PJ",
                           "orgao__Instruções": ""})
    assert r.status_code == 303
    mensagem = up.parse_qs(up.urlparse(r.headers["location"]).query)["mensagem"][0]
    assert "2 item(ns)" in mensagem, "as duas abas entram de uma vez"


def test_aba_de_nome_livre_roda_a_automacao_escolhida(painel, planilha):
    """"Clientes GO" não é código de órgão; quem manda é a escolha."""
    cliente, modulo = painel
    token = _enviar(cliente, planilha)
    cliente.post(f"/planilha/{token}/confirmar",
                 data={"orgao__Clientes GO": "RFB_PJ"}, follow_redirects=False)
    from cnd.infra.db import conectar_leitura
    with conectar_leitura(modulo.cfg.banco) as conn:
        linhas = conn.execute("SELECT orgao FROM job").fetchall()
    assert [linha["orgao"] for linha in linhas] == ["RFB_PJ"]


def test_o_arquivo_sai_do_disco_ao_confirmar(painel, planilha):
    """Planilha de cliente não fica esquecida em pasta temporária (RNF-06)."""
    cliente, modulo = painel
    token = _enviar(cliente, planilha)
    guardado = modulo._pegar_planilha(token)["caminho"]
    assert guardado.exists()

    cliente.post(f"/planilha/{token}/confirmar", data={"orgao__RFB": "RFB_PJ"},
                 follow_redirects=False)
    assert not guardado.exists(), "o arquivo tem de sair do disco"
    assert modulo._pegar_planilha(token) is None


def test_cancelar_tambem_apaga(painel, planilha):
    cliente, modulo = painel
    token = _enviar(cliente, planilha)
    guardado = modulo._pegar_planilha(token)["caminho"]
    cliente.post(f"/planilha/{token}/cancelar", follow_redirects=False)
    assert not guardado.exists()


def test_sem_escolher_nada_nao_importa(painel, planilha):
    cliente, _ = painel
    token = _enviar(cliente, planilha)
    r = cliente.post(f"/planilha/{token}/confirmar",
                     data={"orgao__RFB": ""}, follow_redirects=False)
    assert "Escolha" in r.headers["location"]


def test_planilha_grande_demais_nao_deixa_lixo_no_temp(painel, planilha,
                                                       monkeypatch, tmp_path):
    """Estourar o limite abortava no meio da gravação e ia embora: quem
    chama só recebe o caminho quando dá certo, então redirecionava com a
    mensagem de erro sem ter o que apagar — não sabia nem o nome da pasta.

    Sobrava um `acta_upload_*` com o pedaço já gravado, e planilha de
    cliente esquecida em pasta temporária é exatamente o que o RNF-06
    proíbe (ver test_o_arquivo_sai_do_disco_ao_confirmar).
    """
    import tempfile

    cliente, modulo = painel
    temporarios = tmp_path / "temp"
    temporarios.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporarios))
    monkeypatch.setattr(modulo.comandos, "LIMITE_DA_PLANILHA_MB", 0)

    r = cliente.post("/acoes/maquina/0/planilha",
                     files={"arquivo": (planilha.name, planilha.read_bytes())},
                     follow_redirects=False)

    assert r.status_code == 303
    assert "MB" in up.unquote(r.headers["location"])
    assert list(temporarios.iterdir()) == [], "a pasta temporária ficou para trás"


def test_token_desconhecido_nao_quebra(painel):
    """Quem volta num link velho recebe recado, não erro 500."""
    cliente, _ = painel
    r = cliente.get("/planilha/inventado", follow_redirects=False)
    assert r.status_code == 303
    assert "expirou" in up.unquote(r.headers["location"])
