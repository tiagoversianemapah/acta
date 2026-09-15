"""SEFAZ-ES: adapter e registro da automacao."""
from __future__ import annotations

import base64
import contextlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from cnd.adapters import calibragem
from cnd.adapters.base import AdapterOrgao
from cnd.adapters.estadual import sefaz_es
from cnd.adapters.federal.rfb.cego import Calibragem
from cnd.core.modelos import Desfecho, Documento
from cnd.infra.config import carregar, nome_do_orgao
from cnd.ingestao.planilha import ler
from tests.conftest import RAIZ

CNPJ = "31705832000137"
JANELA = (0, 0, 2560, 1600)
ABSOLUTOS_ES = {
    "menu_cnd": (180, 190),
    "campo_documento": (1280, 520),
    "botao_emitir": (1280, 720),
    "visor_pdf": (1280, 720),
    "fundo_pagina": (2200, 720),
    "faixa_alerta": (1280, 240),
}


def _calibragem_es(absolutos=None) -> Calibragem:
    return Calibragem.de_absolutos(
        JANELA, absolutos or ABSOLUTOS_ES, (255, 255, 255)
    )


def test_mensagem_de_turnstile_vira_captcha():
    texto = "Verificacao de seguranca invalida. Realize nova tentativa."

    assert sefaz_es.classificar_texto(texto) == Desfecho.CAPTCHA


def test_mensagem_de_debito_vira_positiva():
    assert sefaz_es.classificar_texto("Constam debitos para o contribuinte") == (
        Desfecho.POSITIVA
    )


def test_negativa_sem_pdf_nao_vira_certidao():
    assert sefaz_es.classificar_texto("Certidao negativa emitida") == (
        Desfecho.ERRO_TECNICO
    )


def test_ler_pdf_negativa_extrai_validade_e_codigo(tmp_path, monkeypatch):
    texto = (
        "CERTIDAO NEGATIVA DE DEBITOS\n"
        "VALIDA ATE 30/10/2026\n"
        "CODIGO DE CONTROLE: ABC123456\n"
    )
    alvo = tmp_path / "certidao.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(sefaz_es, "_texto_pdf", lambda _c: texto)

    resultado = sefaz_es.ler_pdf(alvo, "ok")

    assert resultado.desfecho == Desfecho.NEGATIVA
    assert resultado.validade == date(2026, 10, 30)
    assert resultado.codigo_controle == "ABC123456"


def test_ler_pdf_cpen_vem_antes_de_positiva(tmp_path, monkeypatch):
    alvo = tmp_path / "certidao.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        sefaz_es,
        "_texto_pdf",
        lambda _c: "CERTIDAO POSITIVA COM EFEITO DE NEGATIVA",
    )

    assert sefaz_es.ler_pdf(alvo, "ok").desfecho == Desfecho.CPEN


def test_pdf_do_data_uri_decodifica_pdf():
    pdf = b"%PDF-1.4\nconteudo"
    data_uri = (
        "data:application/pdf;base64,"
        + base64.b64encode(pdf).decode("ascii")
    )

    assert sefaz_es._pdf_do_data_uri(data_uri) == pdf
    assert sefaz_es._pdf_do_data_uri("data:text/html;base64,PGgxPk88L2gxPg==") is None


def test_pdf_extraido_do_dom_fecha_modal_para_reaproveitar_formulario(
    monkeypatch, tmp_path
):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    pdf = b"%PDF-1.4\nconteudo"
    data_uri = (
        "data:application/pdf;base64,"
        + base64.b64encode(pdf).decode("ascii")
    )
    destino = tmp_path / "certidao.pdf"
    fechados = []

    class Frame:
        def evaluate(self, _script):
            return data_uri

    class Page:
        main_frame = Frame()
        frames = []

    pagina = Page()
    monkeypatch.setattr(
        adapter,
        "_fechar_modal_pdf_por_dom",
        lambda pagina_recebida: fechados.append(pagina_recebida) or True,
    )

    assert adapter._extrair_data_uri_pdf_da_pagina(pagina, destino) is True
    assert destino.read_bytes() == pdf
    assert fechados == [pagina]


class TestClassificarPdfSalvo:
    """O roteamento do PDF depois de salvo pelo visualizador.

    Antes isto vivia no _salvar_pdf, que recebia os bytes da resposta AJAX.
    O adapter cego nao ve resposta nenhuma: o PDF chega como arquivo, salvo
    pelo Ctrl+S. O metodo mudou, o comportamento verificado e o mesmo.
    """

    def _adapter(self, tmp_path):
        cfg = SimpleNamespace(
            pasta_certidoes=tmp_path / "certidoes",
            pasta_evidencias=tmp_path / "evidencias",
        )
        return sefaz_es.AdapterSEFAZES(orgao="SEFAZ_ES", cfg=cfg)

    def _pdf(self, tmp_path):
        arquivo = tmp_path / "evidencias" / "baixado.pdf"
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_bytes(b"%PDF-1.4")
        return arquivo

    def test_negativa_vai_para_certidoes(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE", lote_id=9)
        monkeypatch.setattr(
            sefaz_es, "ler_pdf",
            lambda caminho, _msg: sefaz_es.ResultadoTentativa(
                Desfecho.NEGATIVA, caminho_pdf=caminho),
        )

        resultado = adapter._classificar_pdf_salvo(
            self._pdf(tmp_path), doc, "ok")

        assert resultado.caminho_pdf
        assert adapter.cfg.pasta_certidoes in resultado.caminho_pdf.parents

    def test_positiva_fica_em_evidencias(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE", lote_id=9)
        baixado = self._pdf(tmp_path)
        monkeypatch.setattr(
            sefaz_es, "ler_pdf",
            lambda caminho, _msg: sefaz_es.ResultadoTentativa(
                Desfecho.POSITIVA, evidencia=caminho),
        )

        resultado = adapter._classificar_pdf_salvo(baixado, doc, "ok")

        # Positiva nao e entregavel: o PDF fica onde esta, como evidencia.
        assert resultado.evidencia == baixado
        assert adapter.cfg.pasta_evidencias in resultado.evidencia.parents


def test_criar_devolve_adapter_do_contrato():
    cfg = carregar()
    orgao = cfg.orgaos["SEFAZ_ES"]

    adapter = sefaz_es.criar(orgao, cfg)

    assert isinstance(adapter, AdapterOrgao)
    assert adapter.orgao == "SEFAZ_ES"


def test_criar_aceita_urls_do_config(tmp_path):
    cfg = carregar()
    orgao = replace(
        cfg.orgaos["SEFAZ_ES"],
        extras={
            **cfg.orgaos["SEFAZ_ES"].extras,
            "urls": {
                "consulta": "https://portal.exemplo.invalid/certidao/cnd",
            },
        },
    )

    adapter = sefaz_es.criar(orgao, cfg)

    assert adapter.url_consulta == "https://portal.exemplo.invalid/certidao/cnd"


def test_criar_aponta_para_calibragem_do_adapter():
    cfg = carregar()
    orgao = cfg.orgaos["SEFAZ_ES"]

    adapter = sefaz_es.criar(orgao, cfg)

    assert adapter.caminho_calibragem.name == "sefaz_es.json"


def test_preparar_exige_pontos_do_es(tmp_path):
    faltando_visor = dict(ABSOLUTOS_ES)
    del faltando_visor["visor_pdf"]
    caminho = tmp_path / "sefaz_es.json"
    _calibragem_es(faltando_visor).salvar(caminho)
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg, caminho_calibragem=caminho)

    with pytest.raises(sefaz_es.CalibragemAusente, match="visor_pdf"):
        adapter.preparar()


def test_exigir_foco_restaura_janela_minimizada(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    chamadas = []

    monkeypatch.setattr(sefaz_es.entrada_real, "em_primeiro_plano",
                        lambda *_args: True)
    monkeypatch.setattr(sefaz_es.entrada_real, "retangulo_janela",
                        lambda *_args: (-32000, -32000, 237, 39))
    monkeypatch.setattr(sefaz_es.entrada_real, "posicionar_janela",
                        lambda *_args: chamadas.append("posicionar") or True)
    monkeypatch.setattr(sefaz_es.entrada_real, "maximizar",
                        lambda *_args: chamadas.append("maximizar") or True)
    monkeypatch.setattr(sefaz_es.entrada_real, "garantir_em_primeiro_plano",
                        lambda *_args: chamadas.append("foco") or True)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)

    adapter._exigir_foco()

    assert chamadas == ["posicionar", "maximizar", "foco"]


def test_calibragem_es_aceita_visor_pdf_sobre_ponto_do_formulario():
    problemas = calibragem._validar(
        ABSOLUTOS_ES,
        (255, 255, 255),
        JANELA,
        sefaz_es.PONTOS_NECESSARIOS,
        ("visor_pdf",),
    )

    assert problemas == []


def test_abre_formulario_clicando_no_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, True])
    cliques = []
    icone_cnd = (sefaz_es.OFFSET_X_ICONE_MENU_CND, ABSOLUTOS_ES["menu_cnd"][1])

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: cliques.append(ponto))

    assert adapter._abrir_formulario() is True
    assert cliques == [icone_cnd]


def test_abre_formulario_tenta_texto_se_icone_nao_abre(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, False, True])
    cliques = []
    icone_cnd = (sefaz_es.OFFSET_X_ICONE_MENU_CND, ABSOLUTOS_ES["menu_cnd"][1])

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: cliques.append(ponto))

    assert adapter._abrir_formulario() is True
    assert cliques == [icone_cnd, ABSOLUTOS_ES["menu_cnd"]]


def test_abre_formulario_reinicia_site_quando_menu_nao_aparece(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, False, False, False, False, True])
    chamadas = []
    icone_cnd = (sefaz_es.OFFSET_X_ICONE_MENU_CND, ABSOLUTOS_ES["menu_cnd"][1])

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial",
                        lambda: chamadas.append("home") or True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: chamadas.append(("clicar", ponto)))
    monkeypatch.setattr(adapter, "_abrir_navegador",
                        lambda: chamadas.append(("url", sefaz_es.URL_CONSULTA)))

    assert adapter._abrir_formulario() is True
    assert chamadas == [
        "home",
        ("clicar", icone_cnd),
        ("clicar", ABSOLUTOS_ES["menu_cnd"]),
        ("url", sefaz_es.URL_CONSULTA),
        "home",
        ("clicar", icone_cnd),
    ]


def test_abre_formulario_espera_home_antes_de_clicar_no_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, True])
    chamadas = []

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial",
                        lambda: chamadas.append("home") or True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: chamadas.append(("clicar", ponto)))

    assert adapter._abrir_formulario() is True
    assert chamadas == [
        "home",
        ("clicar", (sefaz_es.OFFSET_X_ICONE_MENU_CND, ABSOLUTOS_ES["menu_cnd"][1])),
    ]


def test_home_ja_com_formulario_nao_clica_no_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES(
        "SEFAZ_ES", cfg, tempo_pagina_inicial_s=1.0
    )
    adapter._calibragem = _calibragem_es()

    monkeypatch.setattr(sefaz_es.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(sefaz_es.tela, "capturar", lambda: object())
    monkeypatch.setattr(adapter, "_formulario_visivel",
                        lambda _imagem: (True, (255, 255, 255), (19, 81, 180)))
    monkeypatch.setattr(adapter, "_texto_da_pagina",
                        lambda: pytest.fail("nao deveria ler texto"))

    assert adapter._esperar_pagina_inicial() is True


def test_home_e_detectada_por_texto(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES(
        "SEFAZ_ES", cfg, tempo_pagina_inicial_s=1.0
    )
    adapter._calibragem = _calibragem_es()

    monkeypatch.setattr(sefaz_es.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.tela, "capturar", lambda: object())
    monkeypatch.setattr(adapter, "_formulario_visivel",
                        lambda _imagem: (False, (236, 239, 241), (236, 239, 241)))
    monkeypatch.setattr(adapter, "_texto_da_pagina",
                        lambda: "Portal de Sistemas Certidao Negativa de Debito")

    assert adapter._esperar_pagina_inicial() is True


def test_botao_emitir_e_localizado_mesmo_sem_turnstile(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    imagem = Image.new("RGB", (2560, 1600), (255, 255, 255))
    desenho = ImageDraw.Draw(imagem)
    desenho.rectangle((980, 620, 1160, 675), fill=(54, 183, 244))

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

    x, y = adapter._localizar_botao_emitir(imagem)

    assert 1050 <= x <= 1090
    assert 640 <= y <= 660


def test_botao_emitir_ignora_azul_escuro_da_pagina_inicial(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    imagem = Image.new("RGB", (2560, 1600), (236, 239, 241))
    desenho = ImageDraw.Draw(imagem)
    desenho.rectangle((780, 430, 1400, 650), fill=(48, 69, 93))

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

    assert adapter._localizar_botao_emitir(imagem) is None
    assert sefaz_es._parece_botao((48, 69, 93)) is False


def test_ponto_caixa_turnstile_fica_entre_campo_e_emitir(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    imagem = Image.new("RGB", (2560, 1600), (255, 255, 255))

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(adapter, "_localizar_botao_emitir",
                        lambda _imagem: ABSOLUTOS_ES["botao_emitir"])

    x, y = adapter._ponto_caixa_turnstile(imagem)

    assert x < ABSOLUTOS_ES["campo_documento"][0]
    assert ABSOLUTOS_ES["campo_documento"][1] < y < ABSOLUTOS_ES["botao_emitir"][1]


def test_formulario_visivel_nao_usa_busca_dinamica_para_pular_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    imagem = Image.new("RGB", (2560, 1600), (255, 255, 255))

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(adapter, "_localizar_botao_emitir", lambda _imagem: (1070, 500))
    monkeypatch.setattr(sefaz_es.tela, "cor_media",
                        lambda _img, x, y, raio=5: (
                            (255, 255, 255)
                            if (x, y) == ABSOLUTOS_ES["campo_documento"]
                            else (236, 239, 241)
                        ))

    visivel, _campo, botao = adapter._formulario_visivel(imagem)

    assert visivel is False
    assert not sefaz_es._parece_botao(botao)


def test_esperar_formulario_aceita_botao_deslocado_por_texto(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()

    monkeypatch.setattr(sefaz_es.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.tela, "capturar", lambda: object())
    monkeypatch.setattr(adapter, "_formulario_visivel",
                        lambda _imagem: (False, (255, 255, 255), (236, 239, 241)))
    monkeypatch.setattr(adapter, "_texto_da_pagina",
                        lambda: "CPF / CNPJ Digite o CPF ou o CNPJ Emitir Certidao")

    assert adapter._esperar_formulario(segundos=1.0) is True


def test_diagnostico_por_texto_reconhece_captcha(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter._ultimo_texto_portal = "Complete a verificacao de seguranca antes de prosseguir"

    monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
    monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")

    resultado = adapter._diagnosticar_falha(doc, "texto")

    assert resultado.desfecho == Desfecho.CAPTCHA
    assert resultado.mensagem_portal == sefaz_es.ERRO_CAPTCHA


def test_salvar_pdf_do_modal_usa_ctrl_s_e_destino_de_evidencia(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        pasta_certidoes=tmp_path / "certidoes",
        pasta_evidencias=tmp_path / "evidencias",
    )
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    chamadas = []

    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_salvar_pdf_do_dom", lambda _destino: None)
    monkeypatch.setattr(adapter, "_esperar_arquivo_pdf", lambda caminho: True)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: chamadas.append(("clicar", ponto)))
    monkeypatch.setattr(sefaz_es.entrada_real, "atalho",
                        lambda *teclas: chamadas.append(("atalho", teclas)))
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar",
                        lambda texto, **_kw: chamadas.append(("digitar", texto)))
    monkeypatch.setattr(sefaz_es.entrada_real, "limpar_campo", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "colar",
                        lambda texto: chamadas.append(("colar", texto)) or True)
    monkeypatch.setattr(sefaz_es.entrada_real, "tecla",
                        lambda tecla: chamadas.append(("tecla", tecla)))

    caminho = adapter._salvar_pdf_do_modal(doc)

    assert caminho is not None
    assert cfg.pasta_evidencias in caminho.parents
    assert ("clicar", ABSOLUTOS_ES["visor_pdf"]) in chamadas
    assert ("atalho", (sefaz_es.entrada_real.VK_CONTROL, sefaz_es.VK_S)) in chamadas
    # COLA o caminho, nao digita: caixa "Salvar como" perde caractere em
    # caminho longo e o PDF some com outro nome, sem erro nenhum.
    assert ("colar", str(caminho)) in chamadas
    assert not any(tipo == "digitar" for tipo, _ in chamadas)


def test_salvar_pdf_do_modal_prefere_pdf_extraido_do_dom(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        pasta_certidoes=tmp_path / "certidoes",
        pasta_evidencias=tmp_path / "evidencias",
    )
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    destino = tmp_path / "evidencias" / "certidao.pdf"

    monkeypatch.setattr(adapter, "_novo_pdf_evidencia", lambda _doc: destino)
    monkeypatch.setattr(adapter, "_estado_arquivos_da_pasta", lambda _pasta: {})
    monkeypatch.setattr(adapter, "_salvar_pdf_do_dom", lambda _destino: destino)
    monkeypatch.setattr(adapter, "_clicar",
                        lambda *_args: pytest.fail("nao deveria usar Ctrl+S"))

    assert adapter._salvar_pdf_do_modal(doc) == destino


def test_extrair_data_uri_pdf_da_pagina_salva_pdf(tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    pdf = b"%PDF-1.4\nconteudo"
    data_uri = (
        "data:application/pdf;base64,"
        + base64.b64encode(pdf).decode("ascii")
    )
    destino = tmp_path / "certidao.pdf"

    class Frame:
        def __init__(self, retorno):
            self.retorno = retorno

        def evaluate(self, _script):
            return self.retorno

    pagina = SimpleNamespace(main_frame=Frame(""), frames=[Frame(data_uri)])

    assert adapter._extrair_data_uri_pdf_da_pagina(pagina, destino) is True
    assert destino.read_bytes() == pdf


def test_fechar_caixa_salvar_nao_cancela_download_iniciado(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    pasta = tmp_path / "evidencias"
    pasta.mkdir()
    destino = pasta / "certidao.pdf"
    antes = adapter._estado_arquivos_da_pasta(pasta)
    teclas = []

    (pasta / "certidao.pdf.crdownload").write_bytes(b"baixando")
    monkeypatch.setattr(sefaz_es.entrada_real, "tecla",
                        lambda tecla: teclas.append(tecla))
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)

    adapter._fechar_caixa_salvar(destino, antes)

    assert teclas == []


def test_fechar_caixa_salvar_fecha_dialogo_sem_arquivo_novo(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    pasta = tmp_path / "evidencias"
    pasta.mkdir()
    destino = pasta / "certidao.pdf"
    antes = adapter._estado_arquivos_da_pasta(pasta)
    teclas = []

    monkeypatch.setattr(sefaz_es.entrada_real, "tecla",
                        lambda tecla: teclas.append(tecla))
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)

    adapter._fechar_caixa_salvar(destino, antes)

    assert teclas == [sefaz_es.entrada_real.VK_ESCAPE,
                      sefaz_es.entrada_real.VK_ESCAPE]


def test_pdf_recem_criado_ignora_pagina_web_com_extensao_pdf(tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    pedido = tmp_path / "pedido.pdf"

    (tmp_path / "erro.pdf").write_text("<html>erro do portal</html>", encoding="utf-8")

    assert adapter._pdf_recem_criado(pedido) is None

    real = tmp_path / "certidao-real.pdf"
    real.write_bytes(b"%PDF-1.4\nconteudo")

    assert adapter._pdf_recem_criado(pedido) == real


def test_turnstile_pendente_tenta_emitir_de_novo(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        pasta_certidoes=tmp_path / "certidoes",
        pasta_evidencias=tmp_path / "evidencias",
    )
    adapter = sefaz_es.AdapterSEFAZES(
        "SEFAZ_ES",
        cfg,
        espera_turnstile_s=0,
        tentativas_turnstile=2,
    )
    adapter._calibragem = _calibragem_es()
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    pdf = tmp_path / "certidao.pdf"
    respostas = iter([("turnstile", None), ("pdf", pdf)])
    cliques = []
    fechou = []
    caixas_turnstile = []

    monkeypatch.setattr(adapter, "_focar", lambda: None)
    # Sem isto o teste mata e reabre o Edge de verdade.
    monkeypatch.setattr(adapter, "_abrir_navegador", lambda: None)
    monkeypatch.setattr(adapter, "_abrir_formulario", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_ponto_botao_emitir",
                        lambda: ABSOLUTOS_ES["botao_emitir"])
    monkeypatch.setattr(adapter, "_aguardar_reacao",
                        lambda _doc, _segundos: next(respostas))
    monkeypatch.setattr(adapter, "_fechar_aviso", lambda: fechou.append(True))
    monkeypatch.setattr(adapter, "_clicar_caixa_turnstile",
                        lambda: caixas_turnstile.append(True) or True)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "ir_para_url", lambda _url: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: cliques.append(ponto))
    monkeypatch.setattr(sefaz_es.entrada_real, "limpar_campo", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar", lambda *_a, **_k: None)

    reacao, caminho = adapter._submeter(doc)

    assert (reacao, caminho) == ("pdf", pdf)
    assert cliques.count(ABSOLUTOS_ES["botao_emitir"]) == 2
    assert caixas_turnstile == [True]
    # O Turnstile nao e fechado com Escape: ele recebe clique na caixinha. O
    # unico fechamento aqui e o visor do PDF, para devolver o formulario a tela.
    assert fechou == [True]
    assert adapter._formulario_pronto is True


def test_aba_es_vira_fila_do_orgao(tmp_path):
    from openpyxl import Workbook

    livro = Workbook()
    aba = livro.active
    aba.title = "ES"
    aba.append(["Empresa", "CNPJ"])
    aba.append(["RAFAEL SCARPATI SFALSIN", "31.705.832/0001-37"])
    caminho = tmp_path / "carteira.xlsx"
    livro.save(caminho)

    leitura = ler(caminho)

    assert [i.orgao for i in leitura.itens] == ["SEFAZ_ES"]
    assert [i.documento for i in leitura.itens] == [CNPJ]


def test_o_painel_oferece_a_automacao():
    import cnd.web.app as web_app

    web_app.cfg = carregar(RAIZ / "config.exemplo.toml")

    oferecidas = {a["codigo"]: a for a in web_app.automacoes()}

    assert "SEFAZ_ES" in oferecidas
    assert not oferecidas["SEFAZ_ES"]["disponivel"]
    assert oferecidas["SEFAZ_ES"]["motivo"] == "desligada no config"
    assert oferecidas["SEFAZ_ES"]["aba"] == "ES"


def test_nome_do_orgao_para_pacote():
    assert nome_do_orgao("SEFAZ_ES") == "SEFAZ ESPIRITO SANTO"


def test_o_empacotador_leva_o_adapter():

    spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
    assert '"cnd.adapters.estadual.sefaz_es"' in spec


def test_reiniciar_sessao_limpa_cookie_antes_de_abrir_o_edge(monkeypatch,
                                                             tmp_path):
    """A ordem e o que importa: Edge morto ANTES da limpeza.

    Com o processo vivo o banco de cookies nao abre e a limpeza sai com
    removidos=0, enquanto o portal segue devolvendo 400 sem menu lateral.
    """
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    ordem = []

    monkeypatch.setattr(adapter, "_matar_edge",
                        lambda: ordem.append("matar_edge") or True)
    monkeypatch.setattr(adapter, "_abrir_navegador",
                        lambda: ordem.append("abrir_navegador"))
    monkeypatch.setattr(sefaz_es.perfil_edge, "limpar_cookies",
                        lambda dominio, raiz: ordem.append(
                            ("limpar", dominio, raiz)) or 3)

    adapter.reiniciar_sessao()

    assert ordem == ["matar_edge",
                     ("limpar", "sefaz.es.gov.br",
                      tmp_path.parent / "edge-sefaz-es"),
                     "abrir_navegador"]


def test_abrir_navegador_carrega_estreito_e_so_depois_maximiza(monkeypatch,
                                                               tmp_path):
    """A ORDEM e a correcao: estreito primeiro, largo depois.

    E na largura que este portal decide se carrega. Em janela larga uma folha
    de estilo pendura, os scripts nao executam e `abreTela` nunca e definida;
    estreita, o mesmo portal sobe inteiro (09/09/2026). Uma vez carregado, os
    scripts ja rodaram e a janela pode crescer - e assim a calibragem do
    layout largo, que ja existe em cada maquina, continua valendo.
    """
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg, largura_janela=430,
                                      altura_janela=932)
    comandos = []
    ordem = []

    monkeypatch.setattr(adapter, "encerrar", lambda: None)
    monkeypatch.setattr(adapter, "_esperar_janela", lambda: True)
    monkeypatch.setattr(adapter, "_estreitar_janela",
                        lambda: ordem.append("estreitar"))
    monkeypatch.setattr(adapter, "_posicionar_janela_calibrada", lambda: None)
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es, "_achar_edge", lambda: "msedge.exe")
    # Tela grande: o tamanho pedido cabe inteiro. Fixado para o teste nao
    # depender do monitor de quem roda a suite.
    monkeypatch.setattr(sefaz_es.entrada_real, "area_util",
                        lambda: (0, 0, 2560, 1400))
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "maximizar",
                        lambda _titulo, _exe: ordem.append("maximizar"))
    monkeypatch.setattr(sefaz_es.subprocess, "Popen",
                        lambda comando: comandos.append(comando))

    adapter._abrir_navegador()

    assert "--window-size=430,932" in comandos[0]
    assert f"--user-data-dir={tmp_path.parent / 'edge-sefaz-es'}" in comandos[0]
    assert f"--remote-debugging-port={sefaz_es.PORTA_CDP_EDGE}" in comandos[0]
    assert ordem == ["estreitar", "maximizar"]


def test_abrir_formulario_insiste_ate_o_limite(monkeypatch, tmp_path):
    """O portal e intermitente: as vezes a pagina sobe inerte e o menu nao
    abre formulario nenhum. Nao da para consertar o servidor da SEFAZ - da
    para recarregar ate cair uma carga boa."""
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg,
                                      tentativas_abrir_formulario=4)
    adapter._calibragem = _calibragem_es()
    recargas = []

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: False)
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar", lambda *_p: None)
    monkeypatch.setattr(adapter, "_abrir_navegador",
                        lambda: recargas.append(1))

    assert adapter._abrir_formulario() is False
    # recarrega entre as tentativas, mas nao depois da ultima
    assert len(recargas) == 3


def test_erro_do_portal_faz_reinsistir_em_vez_de_desistir(monkeypatch, tmp_path):
    """"Ocorreu um erro ao processar" nao e resposta sobre o contribuinte.

    Com o Turnstile ja aprovado e o CNPJ ja preenchido, desistir joga fora
    uma emissao que costuma sair no clique seguinte (visto em 08/09/2026).
    """
    assert sefaz_es._erro_transitorio_do_portal(
        "Erro! Ocorreu um erro ao processar a solicitação") is True
    assert sefaz_es._erro_transitorio_do_portal("Certidão emitida") is False

    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg, tentativas_turnstile=4)
    adapter._calibragem = _calibragem_es()
    reacoes = iter(["erro_transitorio", "erro_transitorio", ("texto", None)])
    cliques = []

    def reagir(_doc, _segundos):
        proxima = next(reacoes)
        return proxima if isinstance(proxima, tuple) else (proxima, None)

    monkeypatch.setattr(adapter, "_focar", lambda: None)
    monkeypatch.setattr(adapter, "_abrir_navegador", lambda: None)
    monkeypatch.setattr(adapter, "_abrir_formulario", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_ponto_botao_emitir",
                        lambda: ABSOLUTOS_ES["botao_emitir"])
    monkeypatch.setattr(adapter, "_clicar",
                        lambda alvo, _p: cliques.append(alvo))
    monkeypatch.setattr(adapter, "_aguardar_reacao", reagir)
    monkeypatch.setattr(adapter, "_fechar_aviso", lambda: None)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "limpar_campo", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar", lambda _t: None)

    reacao, _caminho = adapter._submeter(
        Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE"))

    assert reacao == "texto"
    assert cliques.count("botao_emitir") == 3


def test_segundo_cnpj_reaproveita_a_sessao(monkeypatch, tmp_path):
    """Uma sessao serve varios documentos.

    Recarregar o portal a cada CNPJ abria aba nova, refazia a carga estreita
    e gastava meio minuto por item - para chegar no mesmo formulario que ja
    estava na tela. O robo so recarrega quando o formulario nao esta mais la.
    """
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    cargas = []
    pdf = tmp_path / "certidao.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(adapter, "_focar", lambda: None)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_abrir_navegador", lambda: cargas.append(1))
    monkeypatch.setattr(adapter, "_abrir_formulario", lambda: True)
    # O formulario continua na tela depois de fechar o visor do PDF.
    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: True)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_ponto_botao_emitir",
                        lambda: ABSOLUTOS_ES["botao_emitir"])
    monkeypatch.setattr(adapter, "_clicar", lambda _alvo, _p: None)
    monkeypatch.setattr(adapter, "_fechar_aviso", lambda: None)
    monkeypatch.setattr(adapter, "_aguardar_reacao",
                        lambda _doc, _s: ("pdf", pdf))
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "limpar_campo", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar", lambda _t: None)

    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter._submeter(doc)
    adapter._submeter(doc)

    assert cargas == [1]


def test_janela_estreita_nao_passa_da_tela(monkeypatch, tmp_path):
    """Numa tela pequena a janela encolhe, em vez de nascer maior que ela.

    932 e a altura de um iPhone 16 Pro Max e nao cabe em 1024x768. Uma janela
    mais alta que a area util tem o rodape atras da barra de tarefas - e num
    robo que mede tudo em FRACAO da janela, isso poe pontos calibrados fora
    do visivel.
    """
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg,
                                      largura_janela=430, altura_janela=932)

    monkeypatch.setattr(sefaz_es.entrada_real, "area_util",
                        lambda: (0, 0, 1024, 728))
    assert adapter._tamanho_da_janela() == (430, 728)

    # Tela folgada: o pedido vale como esta.
    monkeypatch.setattr(sefaz_es.entrada_real, "area_util",
                        lambda: (0, 0, 2560, 1400))
    assert adapter._tamanho_da_janela() == (430, 932)

    # Tela absurda: os pisos evitam uma janela inutilizavel.
    monkeypatch.setattr(sefaz_es.entrada_real, "area_util",
                        lambda: (0, 0, 200, 200))
    assert adapter._tamanho_da_janela() == (
        sefaz_es.LARGURA_MINIMA_ESTREITA, sefaz_es.ALTURA_MINIMA_ESTREITA)


def _adapter_em_espera(monkeypatch, tmp_path, textos):
    """Adapter com a tela sempre em modal, devolvendo `textos` em sequencia."""
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    salvamentos = []

    fila = iter(textos)
    ultimo = [textos[-1]]

    def proximo_texto(**_kw):
        # Esgotada a sequencia, repete a ultima: o laco le a tela varias
        # vezes por segundo e o teste so descreve as MUDANCAS.
        with contextlib.suppress(StopIteration):
            ultimo[0] = next(fila)
        return ultimo[0]

    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_tem_modal", lambda _img: True)
    monkeypatch.setattr(adapter, "_alerta_na_imagem", lambda _img: (None, None))
    monkeypatch.setattr(adapter, "_texto_da_pagina", proximo_texto)
    monkeypatch.setattr(adapter, "_salvar_pdf_do_modal",
                        lambda _doc: salvamentos.append(1))
    monkeypatch.setattr(sefaz_es.tela, "capturar", lambda: object())
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _s: None)
    return adapter, salvamentos


def test_tela_de_carregamento_nao_e_confundida_com_o_pdf(monkeypatch, tmp_path):
    """Regressao de 09/09/2026.

    O portal mostra "CARREGANDO - POR FAVOR, AGUARDE" no mesmo lugar e com o
    mesmo veu escuro do visualizador do PDF. O robo via o veu 1,4 s depois de
    clicar em Emitir e disparava Ctrl+S em cima do aviso de espera - perdendo
    a certidao que estava a caminho.
    """
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter, salvamentos = _adapter_em_espera(
        monkeypatch, tmp_path, ["CARREGANDO - POR FAVOR, AGUARDE"])

    reacao, caminho = adapter._aguardar_reacao(doc, 0.5)

    assert (reacao, caminho) == ("nada", None)
    assert salvamentos == [], "nao pode tentar salvar a tela de carregamento"


def test_depois_de_carregar_o_desfecho_e_lido_normalmente(monkeypatch, tmp_path):
    """Esperar nao pode virar ignorar: passada a espera, o portal e ouvido."""
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter, _salvamentos = _adapter_em_espera(monkeypatch, tmp_path, [
        "CARREGANDO - POR FAVOR, AGUARDE",
        "CARREGANDO - POR FAVOR, AGUARDE",
        "Nao foi possivel emitir certidao negativa",
    ])

    reacao, _caminho = adapter._aguardar_reacao(doc, 5.0)

    assert reacao == "texto"
    assert sefaz_es.classificar_texto(adapter._ultimo_texto_portal) == (
        Desfecho.POSITIVA)


def test_erro_transitorio_so_e_lido_depois_do_carregamento(monkeypatch, tmp_path):
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter, salvamentos = _adapter_em_espera(monkeypatch, tmp_path, [
        "CARREGANDO - POR FAVOR, AGUARDE",
        "Erro! Ocorreu um erro ao processar a solicitacao",
    ])

    reacao, caminho = adapter._aguardar_reacao(doc, 5.0)

    assert (reacao, caminho) == ("erro_transitorio", None)
    assert salvamentos == []


def test_modal_de_aviso_sem_texto_nao_e_tratado_como_pdf(monkeypatch, tmp_path):
    """Se a copia da pagina falhar, o botao OK ainda denuncia o aviso.

    Regressao vista em 09/09/2026: o portal abriu o SweetAlert de erro, mas o
    robo caiu no caso "modal desconhecido" e abriu Salvar como de pagina Web.
    """
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter, salvamentos = _adapter_em_espera(monkeypatch, tmp_path, [""])
    monkeypatch.setattr(adapter, "_modal_parece_aviso", lambda _img: True)

    reacao, caminho = adapter._aguardar_reacao(doc, 5.0)

    assert (reacao, caminho) == ("erro_transitorio", None)
    assert salvamentos == []


def test_modal_de_aviso_e_lido_sem_escape_antes_de_salvar(monkeypatch, tmp_path):
    doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE")
    adapter, salvamentos = _adapter_em_espera(monkeypatch, tmp_path, [
        "Erro! Ocorreu um erro ao processar a solicitacao",
    ])
    leituras = []

    monkeypatch.setattr(adapter, "_modal_parece_aviso", lambda _img: True)

    def ler_texto(**kwargs):
        leituras.append(kwargs)
        return "Erro! Ocorreu um erro ao processar a solicitacao"

    monkeypatch.setattr(adapter, "_texto_da_pagina", ler_texto)

    reacao, caminho = adapter._aguardar_reacao(doc, 5.0)

    assert (reacao, caminho) == ("erro_transitorio", None)
    assert leituras == [{"limpar": False}]
    assert salvamentos == []


def test_modal_parece_aviso_pelo_botao_ok_azul(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

    def cor_media(_imagem, x, y, **_kw):
        fx = (x - JANELA[0]) / JANELA[2]
        fy = (y - JANELA[1]) / JANELA[3]
        if 0.46 <= fx <= 0.54 and 0.68 <= fy <= 0.78:
            return (50, 140, 220)
        return (255, 255, 255)

    monkeypatch.setattr(sefaz_es.tela, "cor_media", cor_media)

    assert adapter._modal_parece_aviso(object()) is True


def test_modal_parece_aviso_pelo_icone_de_erro_vermelho(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

    def cor_media(_imagem, x, y, **_kw):
        fx = (x - JANELA[0]) / JANELA[2]
        fy = (y - JANELA[1]) / JANELA[3]
        if 0.44 <= fx <= 0.56 and 0.24 <= fy <= 0.40:
            return (240, 110, 110)
        return (255, 255, 255)

    monkeypatch.setattr(sefaz_es.tela, "cor_media", cor_media)

    assert adapter._modal_parece_aviso(object()) is True


def test_modal_parece_aviso_com_icone_de_erro_fino(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    imagem = Image.new("RGB", (2560, 1600), (255, 255, 255))
    desenho = ImageDraw.Draw(imagem)

    desenho.ellipse((1220, 470, 1340, 590), outline=(245, 112, 112), width=8)
    desenho.line((1248, 505, 1312, 565), fill=(245, 112, 112), width=8)
    desenho.line((1312, 505, 1248, 565), fill=(245, 112, 112), width=8)

    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

    assert adapter._modal_parece_aviso(imagem) is True


class TestModalDeCertidaoNegativaImpossivel:
    """O aviso que o portal mostra quando a empresa nao tem direito a negativa.

    Texto real, capturado do portal em 10/09/2026. O padrao antigo era a
    substring "nao foi possivel emitir certidao negativa" e o portal escreve
    "emitir A certidao negativa" - com artigo. Nao casava, entao o modal nem
    era reconhecido como resposta: virava erro_transitorio, o robo reapertava
    Emitir quatro vezes e o job ainda era reagendado tres, numa empresa que
    nunca teria negativa.
    """

    TEXTO = (
        "Atencao! Nao foi possivel emitir a Certidao Negativa para o CNPJ "
        "61.841.428/0001-51. Se tiver cadastro na Agencia Virtual, clique aqui "
        "para acessar o site e tentar emitir uma Certidao Positiva com Efeito "
        "de Negativa. Caso contrario, procure a Agencia da Receita Estadual "
        "de sua preferencia."
    )

    def test_o_modal_e_reconhecido_como_resposta_do_portal(self):
        assert sefaz_es._texto_tem_resposta(self.TEXTO)

    def test_o_modal_vira_positiva(self):
        assert sefaz_es.classificar_texto(self.TEXTO) is Desfecho.POSITIVA

    def test_positiva_e_conclusivo_e_nao_volta_para_a_fila(self):
        """POSITIVA e conclusivo: e isso que impede as retentativas."""
        from cnd.core.modelos import CONCLUSIVOS

        assert sefaz_es.classificar_texto(self.TEXTO) in CONCLUSIVOS

    def test_casa_com_e_sem_o_artigo(self):
        """A regex existe para o artigo nao poder quebrar de novo."""
        for frase in ("nao foi possivel emitir a certidao negativa",
                      "nao foi possivel emitir certidao negativa",
                      "Nao foi possivel emitir  a  Certidao  Negativa de debitos"):
            assert sefaz_es.classificar_texto(frase) is Desfecho.POSITIVA, frase

    def test_positiva_preserva_a_sessao_para_o_proximo_cnpj(self):
        """Depois deste aviso o formulario continua atras do modal.

        ESC devolve a tela e o proximo documento entra direto no campo, sem
        pagar o minuto de recarga do portal.
        """
        assert (sefaz_es.classificar_texto(self.TEXTO)
                in sefaz_es.DESFECHOS_QUE_PRESERVAM_A_SESSAO)

    def test_captcha_nao_preserva_a_sessao(self):
        """Captcha e avaria da sessao: reaproveitar repetiria o problema."""
        captcha = sefaz_es.classificar_texto("Verificacao de seguranca invalida")
        assert captcha is Desfecho.CAPTCHA
        assert captcha not in sefaz_es.DESFECHOS_QUE_PRESERVAM_A_SESSAO

    def test_cookie_estourado_nao_preserva_a_sessao(self):
        estourado = sefaz_es.classificar_texto(
            "400 Bad Request Request Header Or Cookie Too Large")
        assert estourado is Desfecho.ERRO_TECNICO
        assert estourado not in sefaz_es.DESFECHOS_QUE_PRESERVAM_A_SESSAO
