"""SEFAZ-ES: adapter e registro da automacao."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from cnd.adapters import calibragem, sefaz_es
from cnd.adapters.base import AdapterOrgao
from cnd.adapters.rfb_cego import Calibragem
from cnd.core.modelos import Desfecho, Documento
from cnd.infra.config import carregar, nome_do_orgao
from cnd.ingestao.planilha import ler

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


def test_pdf_base64_vem_do_json_do_portal():
    bruto = base64.b64encode(b"%PDF-1.4 conteudo").decode("ascii")

    assert sefaz_es._pdf_da_resposta({"success": True, "data": {"blbCertidao": bruto}}) == (
        b"%PDF-1.4 conteudo"
    )


def test_json_sem_pdf_nao_finge_sucesso():
    assert sefaz_es._pdf_da_resposta({"success": False, "message": "x"}) is None
    assert sefaz_es._pdf_da_resposta({"success": True, "data": {"blbCertidao": "abc"}}) is None


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


class TestSalvarPdf:
    def _adapter(self, tmp_path):
        cfg = SimpleNamespace(
            pasta_certidoes=tmp_path / "certidoes",
            pasta_evidencias=tmp_path / "evidencias",
        )
        return sefaz_es.AdapterSEFAZES(orgao="SEFAZ_ES", cfg=cfg)

    def test_negativa_vai_para_certidoes(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE", lote_id=9)
        monkeypatch.setattr(
            sefaz_es,
            "ler_pdf",
            lambda caminho, _msg: sefaz_es.ResultadoTentativa(
                Desfecho.NEGATIVA, caminho_pdf=caminho
            ),
        )

        resultado = adapter._salvar_pdf(b"%PDF-1.4", doc, "ok")

        assert resultado.caminho_pdf
        assert adapter.cfg.pasta_certidoes in resultado.caminho_pdf.parents

    def test_positiva_fica_em_evidencias(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA TESTE", lote_id=9)
        monkeypatch.setattr(
            sefaz_es,
            "ler_pdf",
            lambda caminho, _msg: sefaz_es.ResultadoTentativa(
                Desfecho.POSITIVA, evidencia=caminho
            ),
        )

        resultado = adapter._salvar_pdf(b"%PDF-1.4", doc, "ok")

        assert resultado.evidencia
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
                "emissao": "https://portal.exemplo.invalid/certidao/emitir",
            },
        },
    )

    adapter = sefaz_es.criar(orgao, cfg)

    assert adapter.url_consulta == "https://portal.exemplo.invalid/certidao/cnd"
    assert adapter.url_emissao == "https://portal.exemplo.invalid/certidao/emitir"


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
    monkeypatch.setattr(adapter, "_abrir_formulario_por_javascript",
                        lambda: False)
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
    monkeypatch.setattr(adapter, "_abrir_formulario_por_javascript",
                        lambda: False)
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
    monkeypatch.setattr(adapter, "_abrir_formulario_por_javascript",
                        lambda: chamadas.append("js") or False)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: chamadas.append(("clicar", ponto)))
    monkeypatch.setattr(sefaz_es.entrada_real, "ir_para_url",
                        lambda url: chamadas.append(("url", url)))

    assert adapter._abrir_formulario() is True
    assert chamadas == [
        "home",
        "js",
        ("clicar", icone_cnd),
        ("clicar", ABSOLUTOS_ES["menu_cnd"]),
        ("url", sefaz_es.URL_CONSULTA),
        "home",
        "js",
        ("clicar", icone_cnd),
    ]


def test_abre_formulario_por_javascript_antes_do_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, True])
    chamadas = []

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial",
                        lambda: chamadas.append("home") or True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "ir_para_url",
                        lambda url: chamadas.append(("url", url)))
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *_ponto: pytest.fail("nao deveria clicar no menu"))

    assert adapter._abrir_formulario() is True
    assert chamadas == ["home", ("url", sefaz_es.JS_ABRIR_FORMULARIO)]


def test_abre_formulario_espera_home_antes_de_clicar_no_menu(monkeypatch, tmp_path):
    cfg = SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path)
    adapter = sefaz_es.AdapterSEFAZES("SEFAZ_ES", cfg)
    adapter._calibragem = _calibragem_es()
    pronto = iter([False, False, True])
    chamadas = []

    monkeypatch.setattr(adapter, "_esperar_formulario", lambda segundos: next(pronto))
    monkeypatch.setattr(adapter, "_esperar_pagina_inicial",
                        lambda: chamadas.append("home") or True)
    monkeypatch.setattr(adapter, "_abrir_formulario_por_javascript",
                        lambda: False)
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
    monkeypatch.setattr(adapter, "_esperar_arquivo_pdf", lambda caminho: True)
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: chamadas.append(("clicar", ponto)))
    monkeypatch.setattr(sefaz_es.entrada_real, "atalho",
                        lambda *teclas: chamadas.append(("atalho", teclas)))
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar",
                        lambda texto, **_kw: chamadas.append(("digitar", texto)))
    monkeypatch.setattr(sefaz_es.entrada_real, "tecla",
                        lambda tecla: chamadas.append(("tecla", tecla)))

    caminho = adapter._salvar_pdf_do_modal(doc)

    assert caminho is not None
    assert cfg.pasta_evidencias in caminho.parents
    assert ("clicar", ABSOLUTOS_ES["visor_pdf"]) in chamadas
    assert ("atalho", (sefaz_es.entrada_real.VK_CONTROL, sefaz_es.VK_S)) in chamadas
    assert any(
        tipo == "digitar" and str(cfg.pasta_evidencias) in texto
        for tipo, texto in chamadas
    )


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

    monkeypatch.setattr(adapter, "_focar", lambda: None)
    monkeypatch.setattr(adapter, "_abrir_formulario", lambda: True)
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_ponto", lambda nome: ABSOLUTOS_ES[nome])
    monkeypatch.setattr(adapter, "_ponto_botao_emitir",
                        lambda: ABSOLUTOS_ES["botao_emitir"])
    monkeypatch.setattr(adapter, "_aguardar_reacao",
                        lambda _doc, _segundos: next(respostas))
    monkeypatch.setattr(adapter, "_fechar_aviso", lambda: fechou.append(True))
    monkeypatch.setattr(sefaz_es.time, "sleep", lambda _segundos: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "ir_para_url", lambda _url: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "clicar",
                        lambda *ponto: cliques.append(ponto))
    monkeypatch.setattr(sefaz_es.entrada_real, "limpar_campo", lambda: None)
    monkeypatch.setattr(sefaz_es.entrada_real, "digitar", lambda *_a, **_k: None)

    reacao, caminho = adapter._submeter(doc)

    assert (reacao, caminho) == ("pdf", pdf)
    assert cliques.count(ABSOLUTOS_ES["botao_emitir"]) == 2
    assert fechou == [True]


def test_provider_ainda_nao_e_chamado_sem_token(tmp_path):
    adapter = sefaz_es.AdapterSEFAZES(
        orgao="SEFAZ_ES",
        cfg=SimpleNamespace(pasta_certidoes=tmp_path, pasta_evidencias=tmp_path),
        captcha=sefaz_es.ConfigCaptcha(
            provider="capsolver",
            api_key_env="CAPSOLVER_API_KEY",
            permitir_somente_host="portal.exemplo.invalid",
        ),
    )

    assert adapter._resolver_token_por_provider(object()) is False


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

    web_app.cfg = carregar()

    oferecidas = {a["codigo"]: a for a in web_app.automacoes()}

    assert "SEFAZ_ES" in oferecidas
    assert not oferecidas["SEFAZ_ES"]["disponivel"]
    assert oferecidas["SEFAZ_ES"]["motivo"] == "desligada no config"
    assert oferecidas["SEFAZ_ES"]["aba"] == "ES"


def test_nome_do_orgao_para_pacote():
    assert nome_do_orgao("SEFAZ_ES") == "SEFAZ ESPIRITO SANTO"


def test_o_empacotador_leva_o_adapter():
    from pathlib import Path

    spec = Path("empacotar/acta.spec").read_text(encoding="utf-8")
    assert '"cnd.adapters.sefaz_es"' in spec
