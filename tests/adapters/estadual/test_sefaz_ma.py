"""SEFAZ-MA: leitura do formulário, classificação do PDF e o laço do captcha.

Sem rede e sem portal. O que se exercita é onde mora o risco: ler os ids do
formulário JSF (que decidem os POSTs), classificar o PDF sem entregar positiva
como negativa, e orquestrar arma-sessão -> emite sem depender do site.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cnd.adapters.estadual import sefaz_ma
from cnd.adapters.estadual.captcha_ma import BancoCaptcha
from cnd.core.modelos import Desfecho, Documento
from cnd.infra.config import carregar
from tests.conftest import RAIZ

CNPJ = "11222333000181"

# Texto conferido contra a certidão negativa real emitida em 14/09/2026.
PDF_NEGATIVO = (
    "CERTIDÃO NEGATIVA DE DÉBITO GOVERNO DO ESTADO DO MARANHÃO "
    "Certificamos que, após as consultas, não constam débitos relativos aos "
    "tributos estaduais, administrados por esta Secretaria, em nome do sujeito "
    "passivo acima identificado. "
    "Validade da Certidão: 90 (noventa) dias: 13/12/2026. Nº 226555/26"
)


def _resposta(corpo: bytes, content_type: str = "text/html", status: int = 200):
    return sefaz_ma.RespostaPortal(status=status, content_type=content_type,
                                   corpo=corpo)


# Um formulário JSF mínimo, mas com todos os sinais que ler_formulario procura.
FORM_HTML = (
    '<form id="form1">'
    '<input type="radio" name="form1:tipoEmissao" value="1" />'
    "<input type=\"radio\" name=\"form1:tipoEmissao\" value=\"2\" "
    "onclick=\"A4J.AJAX.Submit('form1',event,{'similarityGroupingId':"
    "'form1:j_id15','containerId':'form1:j_id6'})\" />"
    '<input id="form1:cpfCnpj" name="form1:cpfCnpj" maxlength="14" />'
    '<input type="text" name="form1:j_id20" value="" maxlength="4" />'
    '<img src="/certidoes/a4j/s/3_3_3.Final.Paint2DResource/DATA/abc123" />'
    "<input id=\"form1:j_id28\" name=\"form1:j_id28\" "
    "onclick=\"A4J.AJAX.Submit('form1',event,{'oncomplete':function(r,e,d)"
    "{if(false){document.getElementById('form1:btn').click()}}})\" />"
    '<input type="hidden" name="javax.faces.ViewState" '
    'id="javax.faces.ViewState" value="j_id1" />'
    "</form>"
).encode("iso-8859-1")


class TestLeituraDoFormulario:
    def test_extrai_os_ids_e_o_captcha(self):
        f = sefaz_ma.ler_formulario(FORM_HTML)
        assert f is not None
        assert f.viewstate == "j_id1"
        assert "Paint2DResource" in f.captcha_src
        assert f.campo_captcha == "form1:j_id20"
        assert f.container == "form1:j_id6"
        assert f.id_radio_cnpj == "form1:j_id15"
        assert f.id_validar == "form1:j_id28"

    def test_campo_de_cnpj_nao_e_confundido_com_o_captcha(self):
        """O captcha tem maxlength 4; o CNPJ, 14. Trocar os dois mandaria o
        código no campo errado e toda emissão falharia."""
        f = sefaz_ma.ler_formulario(FORM_HTML)
        assert f.campo_captcha != "form1:cpfCnpj"

    def test_pagina_sem_captcha_vira_none(self):
        assert sefaz_ma.ler_formulario(b"<html>fora do ar</html>") is None

    def test_ids_desconhecidos_caem_na_reserva(self):
        """Redeploy do portal pode mudar os j_idNN; sem eles na página, valem
        os valores observados, e não um None que quebraria o POST."""
        magro = ('<img src="x.Paint2DResource/DATA/z" />'
                 '<input name="form1:j_id20" maxlength="4" />').encode("iso-8859-1")
        f = sefaz_ma.ler_formulario(magro)
        assert f.container == sefaz_ma.ID_CONTAINER_PADRAO
        assert f.id_validar == sefaz_ma.ID_VALIDAR_PADRAO
        assert f.viewstate == sefaz_ma.VIEWSTATE_PADRAO


class TestClassificacaoDoPdf:
    def _classificar(self, texto_pdf, tmp_path, monkeypatch):
        monkeypatch.setattr(sefaz_ma, "_texto_pdf", lambda _c: texto_pdf)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        return sefaz_ma.ler_pdf(alvo, "tela")

    def test_certidao_negativa_real(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_NEGATIVO, tmp_path, monkeypatch)
        assert r.desfecho == Desfecho.NEGATIVA
        assert r.caminho_pdf is not None
        assert r.validade == date(2026, 12, 13)
        assert r.codigo_controle == "226555/26"

    def test_positiva_nao_entra_como_negativa(self, tmp_path, monkeypatch):
        r = self._classificar(
            "CERTIDÃO POSITIVA. Constam débitos relativos aos tributos "
            "estaduais em nome do contribuinte.", tmp_path, monkeypatch)
        assert r.desfecho == Desfecho.POSITIVA
        assert r.caminho_pdf is None

    def test_texto_ambiguo_vai_para_conferencia(self, tmp_path, monkeypatch):
        r = self._classificar(
            "não constam débitos de ICMS, porém constam débitos de IPVA",
            tmp_path, monkeypatch)
        assert r.desfecho == Desfecho.ERRO_TECNICO

    def test_pdf_sem_marcador_nao_e_chutado(self, tmp_path, monkeypatch):
        r = self._classificar("documento sem os dizeres esperados",
                              tmp_path, monkeypatch)
        assert r.desfecho == Desfecho.ERRO_TECNICO

    def test_nao_inscrito_ainda_e_negativa_mas_com_aviso(self, tmp_path,
                                                         monkeypatch):
        texto = (PDF_NEGATIVO.replace("Certificamos",
                 "CPF/CNPJ NÃO INSCRITO NO CADASTRO DE CONTRIBUINTES DO ICMS. "
                 "Certificamos"))
        r = self._classificar(texto, tmp_path, monkeypatch)
        assert r.desfecho == Desfecho.NEGATIVA
        assert "não inscrito" in (r.mensagem_portal or "").lower()

    def test_pdf_ilegivel_vira_erro(self, tmp_path, monkeypatch):
        def explode(_c):
            raise ValueError("pdf quebrado")
        monkeypatch.setattr(sefaz_ma, "_texto_pdf", explode)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        assert sefaz_ma.ler_pdf(alvo, "tela").desfecho == Desfecho.ERRO_TECNICO


class TestGuardaDoPdf:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(),
                      pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return sefaz_ma.AdapterSEFAZMA(orgao="SEFAZ_MA", cfg=cfg)

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA TESTE LTDA", lote_id=7)

    def test_negativa_vai_para_certidoes(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(sefaz_ma, "_texto_pdf", lambda _c: PDF_NEGATIVO)
        r = adapter._salvar_pdf(b"%PDF-1.4 negativa", doc)
        assert r.caminho_pdf is not None
        assert adapter.cfg.pasta_certidoes in r.caminho_pdf.parents
        assert r.evidencia is None

    def test_positiva_fica_em_evidencias(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(sefaz_ma, "_texto_pdf",
                            lambda _c: "Constam débitos do contribuinte.")
        r = adapter._salvar_pdf(b"%PDF-1.4 positiva", doc)
        assert r.caminho_pdf is None, "iria para o pacote do cliente"
        assert r.evidencia is not None
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []

    def test_bytes_que_nao_sao_pdf_nao_viram_certidao(self, adapter, doc):
        r = adapter._salvar_pdf(b"nao sou pdf", doc)
        assert r.desfecho == Desfecho.ERRO_TECNICO
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []


class TestDeteccaoDePdf:
    def test_exige_content_type_e_assinatura(self):
        assert sefaz_ma._e_pdf(_resposta(b"%PDF-1.4 x", "application/pdf"))

    def test_html_com_content_type_de_pdf_nao_passa(self):
        assert not sefaz_ma._e_pdf(_resposta(b"<html>erro</html>",
                                             "application/pdf"))

    def test_pdf_sem_content_type_nao_passa(self):
        assert not sefaz_ma._e_pdf(_resposta(b"%PDF-1.4 x", "text/html"))


class TestPortasDeEntrada:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_evidencias=tmp_path / "evidencias",
                      pasta_certidoes=tmp_path / "certidoes")
        a = sefaz_ma.AdapterSEFAZMA(orgao="SEFAZ_MA", cfg=cfg,
                                    caminho_banco=tmp_path / "banco.json")
        return a

    def test_cpf_e_recusado_como_pendencia(self, adapter):
        cpf = Documento(empresa_id=1, documento="12345678909", tipo="CPF",
                        nome="FULANO", lote_id=1)
        r = adapter.emitir(cpf)
        assert r.desfecho == Desfecho.PENDENCIA_MANUAL

    def test_sem_banco_treinado_recusa_com_recado(self, adapter):
        doc = Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                        nome="EMPRESA", lote_id=1)
        r = adapter.emitir(doc)
        assert r.desfecho == Desfecho.ERRO_TECNICO
        assert "treinar_ocr_sefaz_ma" in (r.mensagem_portal or "")

    def test_preparar_usa_contexto_ssl_explicito(self, adapter, monkeypatch):
        contexto = object()
        vistos = {}

        class FakeCookie:
            def __init__(self, jar):
                vistos["jar"] = jar

        class FakeHTTPS:
            def __init__(self, *, context):
                vistos["context"] = context

        def fake_build_opener(*handlers):
            vistos["handlers"] = handlers
            return "opener"

        monkeypatch.setattr(sefaz_ma, "_contexto_ssl", lambda: contexto)
        monkeypatch.setattr(sefaz_ma.urllib.request, "HTTPCookieProcessor",
                            FakeCookie)
        monkeypatch.setattr(sefaz_ma.urllib.request, "HTTPSHandler", FakeHTTPS)
        monkeypatch.setattr(sefaz_ma.urllib.request, "build_opener",
                            fake_build_opener)

        adapter.preparar()

        assert adapter._opener == "opener"
        assert vistos["context"] is contexto
        assert len(vistos["handlers"]) == 2


class TestLacoDoCaptcha:
    """ler captcha -> validar -> emitir, com o HTTP encenado por um duplo.

    Não toca no portal: `_get` devolve o formulário, `_baixar_captcha` um
    marcador, o banco "lê" um código fixo, e `_post` responde como o portal,
    distinguindo troca de tipo, validação (AJAX) e emissão (botão).
    """

    @pytest.fixture
    def adapter(self, tmp_path, monkeypatch):
        cfg = replace(carregar(), pasta_evidencias=tmp_path / "evidencias",
                      pasta_certidoes=tmp_path / "certidoes")
        a = sefaz_ma.AdapterSEFAZMA(orgao="SEFAZ_MA", cfg=cfg,
                                    caminho_banco=tmp_path / "banco.json")
        a._opener = object()
        a._banco = BancoCaptcha(amostras={"w": [1]})  # não-vazio: "treinado"
        monkeypatch.setattr(a, "_get",
                            lambda _u: _resposta(FORM_HTML, "text/html"))
        monkeypatch.setattr(a, "_baixar_captcha", lambda _s: object())
        monkeypatch.setattr(a._banco, "ler", lambda _img: "wzia")
        monkeypatch.setattr(a, "_aprender", lambda *_: None)
        monkeypatch.setattr(sefaz_ma, "_texto_pdf", lambda _c: PDF_NEGATIVO)
        return a

    @staticmethod
    def _validacao(estado: str):
        if estado == "devedor_utf8":
            return _resposta("Este CPF/CNPJ é devedor.".encode(),
                             "text/xml;charset=UTF-8")
        corpo = {
            "armada": sefaz_ma.RE_ARMADA,
            "captcha": sefaz_ma.RE_NAO_ARMADA,
            "devedor": "Este CPF/CNPJ é devedor.",
        }[estado]
        return _resposta(corpo.encode("iso-8859-1"), "text/xml")

    @staticmethod
    def _emissao(tipo: str):
        if tipo == "pdf":
            return _resposta(b"%PDF-1.4 negativa", "application/pdf")
        if tipo == "devedor":
            return _resposta("Este CPF/CNPJ é devedor.".encode("iso-8859-1"),
                             "text/html")
        return _resposta(b"<html>paginaErro</html>", status=302)

    def _portal(self, validacao="armada", emissao="pdf"):
        def post(dados, content_type=None):
            if "form1:btn" in dados:
                return self._emissao(emissao)
            if sefaz_ma.ID_VALIDAR_PADRAO in dados:
                return self._validacao(validacao)
            return _resposta(b"", "text/xml")  # troca de tipo
        return post

    @staticmethod
    def _doc():
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA", lote_id=1)

    def test_captcha_certo_arma_e_emite_negativa(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_post", self._portal("armada", "pdf"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.NEGATIVA
        assert adapter._sessao_armada is True

    def test_devedor_ja_na_validacao_vira_positiva(self, adapter, monkeypatch):
        # Para um devedor o portal responde "é devedor" já na VALIDAÇÃO, com
        # if(false). Não pode ser lido como captcha errado nem exigir emissão.
        monkeypatch.setattr(adapter, "_post", self._portal("devedor", "erro"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.POSITIVA
        assert r.caminho_pdf is None
        assert adapter._sessao_armada is True

    def test_devedor_utf8_na_validacao_vira_positiva(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_post", self._portal("devedor_utf8",
                                                           "erro"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.POSITIVA

    def test_devedor_so_na_emissao_ainda_vira_positiva(self, adapter, monkeypatch):
        # Rede de segurança: se o "é devedor" vier só no POST do botão, pega.
        monkeypatch.setattr(adapter, "_post", self._portal("armada", "devedor"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.POSITIVA

    def test_devedor_com_validacao_if_false_ainda_vira_positiva(
            self, adapter, monkeypatch):
        # O caso do ATLAS: para um devedor a validação responde if(false) SEM o
        # texto "devedor" — igual a captcha errado. Não pode virar CAPTCHA: o
        # botão é que revela "é devedor". Este é o bug que o Tiago pegou.
        monkeypatch.setattr(adapter, "_post", self._portal("captcha", "devedor"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.POSITIVA
        assert adapter._sessao_armada is True

    def test_portal_que_nunca_arma_vira_captcha(self, adapter, monkeypatch):
        adapter.tentativas_captcha = 3
        monkeypatch.setattr(adapter, "_post", self._portal("captcha", "erro"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.CAPTCHA
        assert adapter._sessao_armada is False

    def test_sessao_reutilizada_que_caiu_rearma_sozinha(self, adapter, monkeypatch):
        # Modo reuso: começa armada; o 1º emit cai (sessão expirou) e o adapter
        # rearma e emite na mesma chamada, em vez de reportar erro.
        adapter.sessao_por_documento = False
        adapter._sessao_armada = True
        adapter._ultimo_codigo = "wzia"
        estado = {"emits": 0}

        def post(dados, content_type=None):
            if "form1:btn" in dados:
                estado["emits"] += 1
                return self._emissao("erro" if estado["emits"] == 1 else "pdf")
            if sefaz_ma.ID_VALIDAR_PADRAO in dados:
                return self._validacao("armada")
            return _resposta(b"", "text/xml")

        monkeypatch.setattr(adapter, "_post", post)
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.NEGATIVA
        assert adapter._sessao_armada is True


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("SEFAZ_MA")
        assert orgao is not None, "SEFAZ_MA precisa estar no config.exemplo.toml"
        adapter = sefaz_ma.criar(orgao, cfg)
        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "SEFAZ_MA"

    def test_o_empacotador_leva_o_adapter(self):
        spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.estadual.sefaz_ma"' in spec
