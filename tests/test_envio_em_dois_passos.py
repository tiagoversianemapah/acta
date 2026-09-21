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
    return up.urlparse(r.headers["location"]).path.rsplit("/", 1)[-1]


def test_a_tela_mostra_todas_as_abas(painel, planilha):
    """Inclusive as que não são automação: sumir com elas faria procurar."""
    cliente, _ = painel
    pagina = cliente.get(f"/planilha/{_enviar(cliente, planilha)}").text
    abas = re.findall(r'<select name="orgao__([^"]+)"', pagina)
    assert set(abas) == {"RFB", "Clientes GO", "Instruções"}


def test_caixa_aparece_no_seletor_mesmo_desligada(painel, tmp_path):
    """FGTS/CAIXA não pode sumir: se não roda, a tela mostra o motivo."""
    livro = Workbook()
    crf = livro.active
    crf.title = "CRF"
    crf.append(["Empresa", "Doc"])
    crf.append(["EMPRESA FGTS", CNPJ_A])
    caminho = tmp_path / "fgts.xlsx"
    livro.save(caminho)

    cliente, _ = painel
    pagina = cliente.get(f"/planilha/{_enviar(cliente, caminho)}").text

    assert "FGTS - CAIXA" in pagina
    assert "desligada no config" in pagina
    assert 'data-orgao-ativo="/api/orgao/CRF/ativo"' in pagina
    trecho = pagina[pagina.index('value="CRF"'):][:180]
    assert "disabled" in trecho


def test_upload_mostra_que_a_planilha_esta_sendo_lida(painel):
    cliente, _ = painel

    pagina = cliente.get("/").text

    assert 'data-enviando="Lendo planilha..."' in pagina
    assert "requestSubmit" in pagina
    assert "data-orgao-ativo" in pagina
    assert "this.form.submit()" not in pagina
    assert "campo.disabled = true" not in pagina


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


def test_o_arquivo_fica_guardado_ao_confirmar(painel, planilha):
    """Enfileirar uma aba não apaga a carteira usada pelas próximas."""
    cliente, modulo = painel
    token = _enviar(cliente, planilha)
    guardado = modulo._pegar_planilha(token)["caminho"]
    assert guardado.exists()

    cliente.post(f"/planilha/{token}/confirmar", data={"orgao__RFB": "RFB_PJ"},
                 follow_redirects=False)
    assert guardado.exists()
    assert modulo._pegar_planilha(token) is not None


def test_voltar_nao_apaga_a_planilha_guardada(painel, planilha):
    cliente, modulo = painel
    token = _enviar(cliente, planilha)
    guardado = modulo._pegar_planilha(token)["caminho"]
    cliente.post(f"/planilha/{token}/cancelar", follow_redirects=False)
    assert guardado.exists()


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


def test_planilha_guardada_aparece_na_operacao(painel, planilha):
    cliente, _ = painel
    _enviar(cliente, planilha)

    pagina = cliente.get("/").text

    assert "Planilhas guardadas" in pagina
    assert "carteira.xlsx" in pagina
    assert "Enfileirar" in pagina


def test_aviso_de_reimportacao_aparece_no_mapeamento(painel, planilha):
    from cnd.core import tempo
    from cnd.infra.db import conectar
    from tests.conftest import criar_job

    cliente, modulo = painel
    with conectar(modulo.cfg.banco) as conn:
        lote = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            ("CND MIA 0826", "CND MIA 0826.xlsx"),
        ).lastrowid
        criar_job(conn, lote, documento=CNPJ_A, orgao="RFB_PJ")
        conn.execute("UPDATE job SET atualizado_em = ?", (tempo.agora_iso(),))

    pagina = cliente.get(f"/planilha/{_enviar(cliente, planilha)}").text

    assert "Já existem 1 item(ns) de RECEITA FEDERAL neste mês" in pagina
    assert "Importar cria um lote novo" in pagina


def test_api_informa_o_que_ja_esta_na_fila_no_mes(painel):
    from cnd.core import tempo
    from cnd.infra.db import conectar
    from tests.conftest import criar_job

    cliente, modulo = painel
    with conectar(modulo.cfg.banco) as conn:
        lote = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            ("GO", "go.xlsx"),
        ).lastrowid
        criar_job(conn, lote, documento=CNPJ_A, orgao="RFB_PJ")
        conn.execute("UPDATE job SET atualizado_em = ?", (tempo.agora_iso(),))

    resposta = cliente.get("/api/fila/mes?orgao=RFB_PJ")

    assert resposta.status_code == 200
    assert resposta.json()["itens"] == 1
    assert resposta.json()["planilha"] == "GO"


def test_api_lista_automacoes_do_mes_para_exportar(painel):
    from cnd.core import tempo
    from cnd.infra.db import conectar
    from tests.conftest import criar_job

    cliente, modulo = painel
    with conectar(modulo.cfg.banco) as conn:
        lote = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            ("GO", "go.xlsx"),
        ).lastrowid
        criar_job(conn, lote, documento=CNPJ_A, orgao="RFB_PJ")
        criar_job(conn, lote, documento=CNPJ_B, orgao="SEFAZ_GO")
        conn.execute("UPDATE job SET atualizado_em = ?", (tempo.agora_iso(),))

    resposta = cliente.get("/api/orgaos/mes")

    assert resposta.status_code == 200
    assert {item["orgao"] for item in resposta.json()} == {"RFB_PJ", "SEFAZ_GO"}


def test_entrega_deixa_escolher_automacoes_do_mes(painel):
    from cnd.core import tempo
    from cnd.core.modelos import Desfecho, Status
    from cnd.infra.db import conectar
    from tests.conftest import criar_job

    cliente, modulo = painel
    with conectar(modulo.cfg.banco) as conn:
        lote = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            ("CND MIA 0826", "CND MIA 0826.xlsx"),
        ).lastrowid
        atual = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            ("CND RFB", "CND RFB.xlsx"),
        ).lastrowid
        for lote_id, documento, orgao in (
            (atual, CNPJ_A, "RFB_PJ"),
            (lote, CNPJ_B, "SEFAZ_GO"),
        ):
            job = criar_job(conn, lote_id, documento=documento, orgao=orgao)
            conn.execute(
                "UPDATE job SET status = ?, desfecho = ?, atualizado_em = ? "
                "WHERE id = ?",
                (Status.DONE, Desfecho.NEGATIVA, tempo.agora_iso(), job),
            )

    pagina = cliente.get("/").text

    assert 'data-abrir="#exportar-relatorio"' in pagina
    assert 'action="/relatorio/' in pagina
    assert 'value="RFB_PJ"' in pagina
    assert 'value="SEFAZ_GO"' in pagina
    assert "Todas as automações do mês" in pagina


def test_envio_remoto_leva_a_automacao_e_o_nome(monkeypatch, tmp_path):
    from cnd.desktop import remoto
    from cnd.infra.config import Maquina

    arquivo = tmp_path / "guardada.xlsx"
    arquivo.write_bytes(b"x")
    visto = {}

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"criados": 0, "rejeitados": [], "total_rejeitados": 0}'

    def abrir(pedido, timeout):
        visto["timeout"] = timeout
        visto["corpo"] = pedido.data.decode("utf-8", errors="ignore")
        return Resposta()

    monkeypatch.setattr(remoto.urllib.request, "urlopen", abrir)

    remoto.enviar_planilha(
        Maquina("Robo", "http://robo:8000"), arquivo, "segredo",
        aba="Clientes GO", orgao="SEFAZ_GO", nome="CND GO.xlsx",
    )

    assert 'name="aba"' in visto["corpo"] and "Clientes GO" in visto["corpo"]
    assert 'name="orgao"' in visto["corpo"] and "SEFAZ_GO" in visto["corpo"]
    assert 'name="nome"' in visto["corpo"] and "CND GO.xlsx" in visto["corpo"]
    assert visto["timeout"] == 120


def test_envio_remoto_sem_aba_deixa_servidor_ler_a_planilha(monkeypatch, tmp_path):
    from cnd.desktop import remoto
    from cnd.infra.config import Maquina

    arquivo = tmp_path / "sefaz.xlsx"
    arquivo.write_bytes(b"x")
    visto = {}

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"criados": 300, "rejeitados": [], "total_rejeitados": 0}'

    def abrir(pedido, timeout):
        visto["timeout"] = timeout
        visto["corpo"] = pedido.data.decode("utf-8", errors="ignore")
        return Resposta()

    monkeypatch.setattr(remoto.urllib.request, "urlopen", abrir)

    remoto.enviar_planilha(
        Maquina("Robo", "http://robo:8000"), arquivo, "segredo"
    )

    campo_aba = re.search(
        r'name="aba"\r\n\r\n(.*?)\r\n------acta', visto["corpo"], re.DOTALL
    )
    assert campo_aba and campo_aba.group(1) == ""
    assert "RFB" not in visto["corpo"]
    assert visto["timeout"] == 120


def test_token_desconhecido_nao_quebra(painel):
    """Quem volta num link velho recebe recado, não erro 500."""
    cliente, _ = painel
    r = cliente.get("/planilha/inventado", follow_redirects=False)
    assert r.status_code == 303
    erro = up.parse_qs(up.urlparse(r.headers["location"]).query)["erro"][0]
    assert "não encontrada" in erro


# ---------------------------------------------------------------------------
# Uma planilha, um lote — mesmo com várias abas e várias automações
# ---------------------------------------------------------------------------
def _lotes_e_orgaos(banco):
    from cnd.infra.db import conectar_leitura
    with conectar_leitura(banco) as conn:
        linhas = conn.execute("SELECT lote_id, orgao FROM job").fetchall()
    return ({linha["lote_id"] for linha in linhas},
            sorted({linha["orgao"] for linha in linhas}))


def test_varias_abas_e_automacoes_viram_um_lote_so(painel, planilha):
    """Importada aba por aba, a carteira virava um lote por automação com o
    mesmo nome, e o painel mostrava só um deles (17/09/2026)."""
    cliente, modulo = painel
    token = _enviar(cliente, planilha)

    cliente.post(f"/planilha/{token}/confirmar", follow_redirects=False,
                 data={"orgao__RFB": "RFB_PJ", "orgao__Clientes GO": "SEFAZ_GO"})

    lotes, orgaos = _lotes_e_orgaos(modulo.cfg.banco)
    assert len(lotes) == 1
    assert orgaos == ["RFB_PJ", "SEFAZ_GO"]


def test_mesma_empresa_em_duas_abas_da_mesma_automacao_entra_uma_vez(tmp_path):
    from cnd.ingestao.planilha import ler_pares

    livro = Workbook()
    for titulo in ("RFB", "RFB extra"):
        folha = livro.create_sheet(titulo)
        folha.append(["Empresa", "CNPJ"])
        folha.append(["EMPRESA A", CNPJ_A])
    caminho = tmp_path / "dupla.xlsx"
    livro.save(caminho)

    leitura = ler_pares(caminho, [("RFB", "RFB_PJ"), ("RFB extra", "RFB_PJ"),
                                  ("RFB extra", "CRF")])

    assert [(i.orgao, i.documento) for i in leitura.itens] == [
        ("RFB_PJ", CNPJ_A), ("CRF", CNPJ_A)]
    assert "outra aba" in leitura.rejeitados[0].motivo


def test_api_do_robo_importa_os_pares_num_lote(monkeypatch, tmp_path, planilha):
    import json

    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir
    from cnd.web import app as modulo

    banco = tmp_path / "robo.db"
    garantir(banco)
    monkeypatch.setattr(modulo, "cfg", replace(
        carregar(), banco=banco, rede=ConfigRede(nome="PC 01", senha="s")))
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        r = cliente.post(
            "/api/planilha", headers={"X-CND-Senha": "s"},
            files={"arquivo": (planilha.name, planilha.read_bytes())},
            data={"pares": json.dumps([["RFB", "RFB_PJ"],
                                       ["Clientes GO", "SEFAZ_GO"]])})

    assert r.status_code == 200, r.text
    assert r.json()["pares"] == 2 and r.json()["criados"] == 2
    lotes, orgaos = _lotes_e_orgaos(banco)
    assert len(lotes) == 1 and orgaos == ["RFB_PJ", "SEFAZ_GO"]


def test_envio_remoto_leva_os_pares_e_a_primeira_aba(monkeypatch, tmp_path):
    """A primeira aba também vai solta, para um robô antigo importar algo."""
    import json

    from cnd.desktop import remoto
    from cnd.infra.config import Maquina

    arquivo = tmp_path / "carteira.xlsx"
    arquivo.write_bytes(b"x")
    visto = {}

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"pares": 2, "criados": 2, "rejeitados": [], "total_rejeitados": 0}'

    def abrir(pedido, timeout):
        visto["corpo"] = pedido.data.decode("utf-8", errors="ignore")
        return Resposta()

    monkeypatch.setattr(remoto.urllib.request, "urlopen", abrir)

    remoto.enviar_planilha(Maquina("Robo", "http://robo:8000"), arquivo, "s",
                           nome="carteira.xlsx",
                           pares=[("RFB", "RFB_PJ"), ("Clientes GO", "SEFAZ_GO")])

    campo = lambda nome: re.search(  # noqa: E731
        rf'name="{nome}"\r\n\r\n(.*?)\r\n------acta', visto["corpo"], re.DOTALL
    ).group(1)
    assert json.loads(campo("pares")) == [["RFB", "RFB_PJ"], ["Clientes GO", "SEFAZ_GO"]]
    assert campo("aba") == "RFB" and campo("orgao") == "RFB_PJ"


def test_robo_antigo_recebe_as_abas_restantes_uma_por_uma(monkeypatch, tmp_path,
                                                         planilha):
    """Robô sem a versão nova ignora `pares` e importa só a primeira aba.
    Sem a chave "pares" na resposta, o painel manda as outras."""
    from cnd.desktop import remoto
    from cnd.infra.config import ConfigRede, Maquina, carregar
    from cnd.infra.db import garantir
    from cnd.web import app as modulo

    banco = tmp_path / "console.db"
    garantir(banco)
    monkeypatch.setattr(modulo, "cfg", replace(
        carregar(), banco=banco,
        rede=ConfigRede(nome="console", senha="", papel="console",
                        maquinas=(Maquina("PC 01", "http://robo:8000"),))))
    chamadas = []

    def enviar(_maquina, _arquivo, _senha, aba="", orgao="", nome="", pares=None):
        chamadas.append((aba, orgao, bool(pares)))
        return {"lote": len(chamadas), "criados": 1, "rejeitados": [],
                "total_rejeitados": 0}          # sem "pares": robô antigo

    monkeypatch.setattr(remoto, "enviar_planilha", enviar)
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        token = _enviar(cliente, planilha)
        r = cliente.post(f"/planilha/{token}/confirmar?maquina=0",
                         follow_redirects=False,
                         data={"orgao__RFB": "RFB_PJ",
                               "orgao__Clientes GO": "SEFAZ_GO"})

    assert r.status_code == 303
    assert chamadas == [("", "", True), ("Clientes GO", "SEFAZ_GO", False)]
    mensagem = up.parse_qs(up.urlparse(r.headers["location"]).query)["mensagem"][0]
    assert "2 item(ns)" in mensagem


def test_pares_invalidos_sao_recusados_com_motivo(monkeypatch, tmp_path, planilha):
    """Campo vindo torto não pode virar lote pela metade nem traceback."""
    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import garantir
    from cnd.web import app as modulo

    banco = tmp_path / "robo.db"
    garantir(banco)
    monkeypatch.setattr(modulo, "cfg", replace(
        carregar(), banco=banco, rede=ConfigRede(nome="PC 01", senha="s")))

    with fastapi_testclient.TestClient(modulo.app) as cliente:
        def enviar(pares: str):
            return cliente.post(
                "/api/planilha", headers={"X-CND-Senha": "s"},
                files={"arquivo": (planilha.name, planilha.read_bytes())},
                data={"pares": pares})

        quebrado = enviar("isso não é json")
        vazio = enviar('[["RFB", ""]]')

    assert quebrado.status_code == 400 and "pares" in quebrado.json()["detail"]
    assert vazio.status_code == 400
    from cnd.infra.db import conectar_leitura
    with conectar_leitura(banco) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0
