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


AVISO_IE = ("Existe Inscrição Estadual ativa para este CPF/CNPJ. "
            "Favor emitir pela Inscrição Estadual.")
AVISO_IPVA = "Atenção: Existe pendência de IPVA ou de Auto de IPVA."
AVISO_IMAGEM = "Código da imagem inválido."


def _faixa(texto: str) -> str:
    """A faixa de avisos como o portal a escreve — copiada do HTML real.

    O dublê imita a MARCAÇÃO, e não só o texto, porque é a marcação que o
    adapter usa para separar recusa de captcha errado. Dublê que responde
    texto solto passaria mesmo se o adapter estivesse lendo o lugar errado.
    """
    return ('<div id="form1:msgs" class="pf-messages">'
            '<div class="pf-messages-warn">'
            '<span class="pf-messages-warn-icon"></span><ul><li>'
            '<span class="pf-messages-warn-summary"></span>'
            f'<span class="pf-messages-warn-detail">{texto}</span>'
            '</li></ul></div></div>')


class TestTextoDaResposta:
    def test_css_da_pagina_nao_empurra_a_mensagem_para_fora(self):
        """O recado do portal tem que caber no começo do texto limpo.

        A tela de erro do MA abre com um bloco <style>; enquanto ele entrava no
        texto, o `trecho` de 200 caracteres que vai ao Registro era só CSS e o
        "Ocorreu um erro de sistema" ficava invisível (16/09/2026).
        """
        corpo = (
            "<html><head><style>.bg { background-image: url(fundo.png); "
            "background-repeat: repeat; } .rich-panel-body{ background: "
            "transparent; } .coluna{ vertical-align: top; }</style></head>"
            "<body><span>Ocorreu um erro de sistema.</span></body></html>"
        ).encode("iso-8859-1")
        texto = sefaz_ma.texto_da_resposta(
            sefaz_ma.RespostaPortal(200, "text/html;charset=ISO-8859-1", corpo))

        assert "background-image" not in texto
        assert texto[:200].startswith("Ocorreu um erro de sistema.")


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
            return _resposta(_faixa("Este CPF/CNPJ é devedor.").encode(),
                             "text/xml;charset=UTF-8")
        corpo = {
            "armada": sefaz_ma.RE_ARMADA,
            "captcha": sefaz_ma.RE_NAO_ARMADA,
            # O portal manda o aviso JUNTO do if(false) — os três casos reais
            # vistos até 16/09/2026, e o motivo de a regra ser a FAIXA e não
            # a frase.
            "devedor": _faixa("Este CPF/CNPJ é devedor.") + sefaz_ma.RE_NAO_ARMADA,
            "exige_ie": _faixa(AVISO_IE) + sefaz_ma.RE_NAO_ARMADA,
            "ipva": _faixa(AVISO_IPVA) + sefaz_ma.RE_NAO_ARMADA,
            # Mesma faixa, sentido oposto: o portal está falando da LEITURA.
            "imagem_invalida": (_faixa(AVISO_IMAGEM)
                                + sefaz_ma.RE_NAO_ARMADA),
        }[estado]
        return _resposta(corpo.encode("iso-8859-1"), "text/xml")

    @staticmethod
    def _emissao(tipo: str):
        if tipo == "pdf":
            return _resposta(b"%PDF-1.4 negativa", "application/pdf")
        if tipo == "devedor":
            return _resposta(_faixa("Este CPF/CNPJ é devedor.")
                             .encode("iso-8859-1"), "text/html")
        if tipo == "exige_ie":
            return _resposta(_faixa(AVISO_IE).encode("iso-8859-1"), "text/html")
        if tipo == "erro_de_sistema":
            # A tela de erro do portal NÃO usa a faixa de avisos — é isso que
            # a separa de uma recusa com motivo.
            return _resposta(
                "<html><body><span>Alerta</span> Ocorreu um erro de sistema. "
                "java.lang.NullPointerException</body></html>"
                .encode("iso-8859-1"), "text/html")
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
        # Não arma: a resposta de devedor vem com if(false) como qualquer
        # recusa, e emitir logo depois só devolve erro de sistema — conferido
        # no portal em 16/09/2026.
        assert adapter._sessao_armada is False

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
        # A emissão deu certo, mas o portal nunca disse if(true): sem isso a
        # próxima emissão precisa de captcha novo.
        assert adapter._sessao_armada is False

    def test_recusa_na_validacao_nao_vira_captcha(self, adapter, monkeypatch):
        """Aviso do portal na VALIDAÇÃO fecha o item na hora, com o motivo.

        É o caso que travou 33 itens em 16/09/2026: código certo, mas o portal
        responde `if(false)` com o aviso, e o item retentava até acabar a
        paciência do retry.
        """
        chamadas = []
        monkeypatch.setattr(adapter, "_post", self._portal("exige_ie", "erro"))
        monkeypatch.setattr(adapter, "_aprender",
                            lambda *a: chamadas.append(a))
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL
        # O motivo do PORTAL, inteiro — quem abre o painel lê o que ele disse.
        assert r.mensagem_portal == AVISO_IE
        # Captcha estava certo: vale aprender a imagem em vez de descartá-la.
        assert len(chamadas) == 1
        # ...mas o portal recusou com if(false), então a sessão NÃO está armada.
        assert adapter._sessao_armada is False

    def test_recusa_desconhecida_tambem_fecha_o_item(self, adapter, monkeypatch):
        """Mensagem que o código nunca viu não pode virar captcha errado.

        O IPVA apareceu depois da Inscrição Estadual, no mesmo lote, e custou
        mais um item queimando 36 imagens. A regra é a faixa de avisos, não a
        frase — então a próxima mensagem do portal já cai de pé.
        """
        monkeypatch.setattr(adapter, "_post", self._portal("ipva", "erro"))
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL
        assert r.mensagem_portal == AVISO_IPVA

    def test_aviso_de_imagem_invalida_pede_outra_imagem(self, adapter,
                                                       monkeypatch):
        """"Código da imagem inválido." é erro de LEITURA, não pendência.

        Pendência é a empresa dever ou ter auto de IPVA — coisas que uma
        pessoa resolve. Imagem mal lida o robô resolve sozinho, pedindo
        outra: fechar o item aqui cria uma pendência que ninguém tem como
        tratar, porque não existe pendência. Foram 17 itens assim em
        16/09/2026, antes de esta separação existir.
        """
        adapter.tentativas_captcha = 3
        monkeypatch.setattr(adapter, "_post",
                            self._portal("imagem_invalida", "erro"))
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.CAPTCHA, "tem que continuar retentável"
        # E o item mostra o que o PORTAL disse, não o nosso genérico.
        assert r.mensagem_portal == AVISO_IMAGEM

    def test_imagem_invalida_nao_gasta_o_post_do_botao(self, adapter,
                                                       monkeypatch):
        """Quando o portal diz que a leitura não serve, pede-se outra imagem.

        O "pulo do gato" — emitir mesmo com if(false) — existe para quando o
        portal não diz POR QUE recusou. Dizendo, insistir só rende o erro de
        sistema dele, medido em 16/09/2026: é um POST por imagem, doze por
        documento, para descobrir o que ele já tinha falado.
        """
        adapter.tentativas_captcha = 3
        emissoes = []
        portal = self._portal("imagem_invalida", "erro")

        def contando(dados, content_type=None):
            if "form1:btn" in dados:
                emissoes.append(dados)
            return portal(dados, content_type)

        monkeypatch.setattr(adapter, "_post", contando)
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.CAPTCHA
        assert emissoes == [], "não devia ter tentado emitir"

    def test_erro_de_sistema_do_portal_continua_retentavel(self, adapter,
                                                           monkeypatch):
        """Portal quebrado não é recusa: é para tentar de novo.

        A tela de `NullPointerException` não usa a faixa de avisos, e é essa
        diferença que impede um portal instável de fechar itens como se
        tivesse respondido sobre a empresa.
        """
        adapter.tentativas_captcha = 2
        monkeypatch.setattr(adapter, "_post",
                            self._portal("captcha", "erro_de_sistema"))
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.CAPTCHA

    def test_recusa_na_emissao_vira_pendencia_manual(self, adapter, monkeypatch):
        """A recusa também pode chegar no POST do botão, e vale o mesmo."""
        monkeypatch.setattr(adapter, "_post",
                            self._portal("armada", "exige_ie"))
        r = adapter.emitir(self._doc())

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL
        assert r.mensagem_portal == AVISO_IE
        assert adapter._sessao_armada is True

    def test_portal_que_nunca_arma_vira_captcha(self, adapter, monkeypatch):
        adapter.tentativas_captcha = 3
        monkeypatch.setattr(adapter, "_post", self._portal("captcha", "erro"))
        r = adapter.emitir(self._doc())
        assert r.desfecho == Desfecho.CAPTCHA
        assert r.evidencia is None
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
