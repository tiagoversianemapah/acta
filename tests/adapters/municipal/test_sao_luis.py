"""Prefeitura de Sao Luis (MA): JSF/RichFaces por HTTP, com captcha lido local.

Os trechos de HTML e de PDF abaixo sao recortes do portal real, colhidos em
24/09/2026; CNPJ, razao social e codigos foram trocados por ficticios.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cnd.adapters.municipal import sao_luis
from cnd.core.modelos import COM_PDF, Desfecho, Documento
from cnd.infra.config import carregar, nome_do_orgao
from cnd.ingestao import planilha
from tests.conftest import RAIZ

CNPJ = "11222333000181"

PAGINA_INICIAL = """
<form id="j_id10" name="j_id10" method="post" action="/credenciamento/jsp/emissaoCertidao/emissaoPublicaCertidao.jsf;jsessionid=AB12" class="form-horizontal" enctype="application/x-www-form-urlencoded">
<input type="hidden" name="j_id10" value="j_id10" />
<span id="j_id10:dados"><div id="j_id10:msg" class="pf-messages"></div>
<input type="radio" checked="checked" name="j_id10:j_id18" id="j_id10:j_id18:0" value="1" onchange="A4J.AJAX.Submit('j_id10',event,{'similarityGroupingId':'j_id10:j_id22','parameters':{'j_id10:j_id22':'j_id10:j_id22'} } )" />
<input type="radio" name="j_id10:j_id18" id="j_id10:j_id18:1" value="2" onchange="A4J.AJAX.Submit('j_id10',event,{'similarityGroupingId':'j_id10:j_id22','parameters':{'j_id10:j_id22':'j_id10:j_id22'} } )" />
<input type="text" name="j_id10:j_id27" class="span2 cpf" />
<input id="j_id10:captcha" type="text" name="j_id10:captcha" value="" maxlength="5" onblur="this.value=this.value.toUpperCase()" /><img src="/credenciamento/a4j/s/3_3_3.Finalorg.richfaces.renderkit.html.Paint2DResource/DATA/eAGNks9r.jsf;jsessionid=AB12" class="rich-paint2D" id="j_id10:img" />
</span>
<a class="btn btn-primary pull-right" href="#" id="j_id10:j_id58" name="j_id10:j_id58" onclick="A4J.AJAX.Submit('j_id10',event,{'parameters':{'j_id10:j_id58':'j_id10:j_id58'} } );return false;">
<i class="icon-ok"></i> Emitir certid&atilde;o</a>
<input type="hidden" name="javax.faces.ViewState" id="javax.faces.ViewState" value="j_id1" />
</form>
"""

# A resposta do radio "Pessoa Juridica". As tres opcoes dizem "NEGATIVA" no
# rotulo, e so uma e a certidao negativa.
TELA_PJ = """
<span id="j_id10:dados"><div id="j_id10:msg" class="pf-messages"></div>
<div class="input-append"><input type="text" name="j_id10:j_id28" class="span2 cnpj" /> <a class="btn btn-success add-on" href="#" id="j_id10:j_id30" name="j_id10:j_id30" onclick="A4J.AJAX.Submit('j_id10',event,{'parameters':{'j_id10:j_id30':'j_id10:j_id30'} } );return false;"><i class="icon-ok"></i></a></div>
<input type="text" name="j_id10:j_id38" value="" class="span6" disabled="disabled" />
<select id="j_id10:colecaoModelos" name="j_id10:colecaoModelos" class="span8" size="1"><option value=""></option><option value="45">CERTIDÃO POSITIVA COM EFEITO DE NEGATIVA DE DÉBITOS DA PESSOA JURÍDICA</option><option value="47">CERTIDAO NEGATIVA DA PESSOA JURÍDICA</option><option value="50">CERTIDÃO DE BAIXA</option></select>
<input type="text" name="j_id10:j_id47" class="span10" maxlength="280" />
</span>
"""

TELA_CONFERIDA = """
<span id="j_id10:dados"><div id="j_id10:msg" class="pf-messages"></div>
<input type="text" name="j_id10:j_id38" value="EMPRESA FICTICIA LTDA." class="span6" disabled="disabled" />
</span>
"""


def _duas_vezes(texto: str) -> str:
    """Como o portal manda os avisos: UTF-8 codificado duas vezes."""
    return texto.encode("utf-8").decode("latin-1")


def _aviso(*textos: str) -> str:
    itens = "".join(
        '<li><span class="pf-messages-warn-summary"></span>'
        f'<span class="pf-messages-warn-detail">{_duas_vezes(t)}</span></li>'
        for t in textos)
    return ('<div id="j_id10:msg" class="pf-messages"><div class="pf-messages-warn">'
            f'<ul>{itens}</ul></div></div>')


CAPTCHA_ERRADO = "Código de verificação está incorreto"
SEM_RAZAO = ("CNPJ deve ser informado e a razão social deve ser recuperada "
             "pressionando o botão verde")

REDIRECT = ('<?xml version="1.0" encoding="UTF-8"?>\n<html><head>'
            '<meta name="Ajax-Response" content="redirect" />'
            '<meta name="Location" content="emissaoSucesso.jsf" /></head></html>')

TELA_SUCESSO = """
<form id="j_id10" name="j_id10" method="post" action="/credenciamento/jsp/emissaoCertidao/emissaoSucesso.jsf">
<h3>Solicita&ccedil;&atilde;o de certid&atilde;o foi efetuada com sucesso.</h3>
<a id="j_id10:btImprimirCertidao" href="#" onclick="if(typeof jsfcljs == 'function'){jsfcljs(document.forms['j_id10'],'j_id10:btImprimirCertidao,j_id10:btImprimirCertidao','_blank');}return false" class="btn">Imprimir</a>
<input type="hidden" name="javax.faces.ViewState" id="javax.faces.ViewState" value="j_id2" />
</form>
"""

PDF_NEGATIVO = """PREFEITURA DE SAO LUÍS
SECRETARIA MUNICIPAL DA FAZENDA
 CERTIDÃO NEGATIVA
Número da Certidão: 00014911042026
Validade: 23/03/2027
Certificamos que até a presente data não consta débito fiscal relativo a pessoa jurídica, descrita
abaixo, reserva-se o direito de a fazenda municipal cobrar dívidas posteriormente comprovadas,
hipótese prevista no artigo 147, da lei 6.289, de 28/12/2017 do código tributário municipal.
DADOS DA PESSOA JURÍDICA
CNPJ: 11.222.333/0001-81 Inscrição Municipal: 1234567890
Razão Social: EMPRESA FICTICIA LTDA.
A presente certidão, sem conter rasuras, tem sua eficácia até a data de validade acima informada,
tendo sido lavrada em São Luís (MA), em 24 de setembro de 2026 as 14:51, sob o código de
autenticidade nº 677DB09664D9C5FE554382347A276D55.
"""

PDF_CPEN = """PREFEITURA DE SAO LUÍS
 CERTIDÃO POSITIVA COM EFEITO DE NEGATIVA
Validade: 23/03/2027
Certificamos que constam débitos com exigibilidade suspensa.
"""

PDF_POSITIVO = """PREFEITURA DE SAO LUÍS
 CERTIDÃO POSITIVA
Certificamos que constam débitos fiscais.
"""


def _resposta(corpo: str, content_type: str = "text/xml;charset=UTF-8",
              status: int = 200) -> sao_luis.RespostaPortal:
    return sao_luis.RespostaPortal(status, content_type, corpo.encode("utf-8"))


class TestPaginas:
    def test_formulario_le_os_ids_da_pagina(self):
        f = sao_luis.ler_formulario(PAGINA_INICIAL)

        assert f is not None
        assert f.form == "j_id10"
        assert f.viewstate == "j_id1"
        assert f.campo_tipo == "j_id10:j_id18"
        assert f.id_tipo == "j_id10:j_id22"
        assert f.campo_captcha == "j_id10:captcha"
        assert f.id_emitir == "j_id10:j_id58"
        assert "Paint2DResource" in f.captcha_src

    def test_pagina_sem_captcha_nao_e_formulario(self):
        assert sao_luis.ler_formulario("<html>Erro 503</html>") is None

    def test_radio_pj_revela_o_campo_de_cnpj_e_a_negativa(self):
        """A positiva com efeito de negativa tambem diz "NEGATIVA": pegar a
        primeira opcao com a palavra pediria a certidao errada."""
        f = sao_luis.completar_com_pj(
            sao_luis.ler_formulario(PAGINA_INICIAL), TELA_PJ)

        assert f.campo_cnpj == "j_id10:j_id28"
        assert f.id_conferir == "j_id10:j_id30"
        assert f.campo_modelo == "j_id10:colecaoModelos"
        assert f.modelo == "47"
        assert f.campo_finalidade == "j_id10:j_id47"

    def test_razao_social_volta_no_campo_desabilitado(self):
        assert sao_luis.razao_social(TELA_CONFERIDA) == "EMPRESA FICTICIA LTDA."
        assert sao_luis.razao_social(TELA_PJ) == ""

    def test_redirect_do_emitir(self):
        assert sao_luis.destino_do_redirect(REDIRECT) == "emissaoSucesso.jsf"
        assert sao_luis.destino_do_redirect(_aviso(CAPTCHA_ERRADO)) is None


class TestAvisos:
    def test_desfaz_a_dupla_codificacao_do_portal(self):
        """Sem isso "Código de verificação" vira "CÃ³digo de verificaÃ§Ã£o",
        e o aviso de captcha errado passa por recusa do portal."""
        assert sao_luis.avisos_do_portal(_aviso(CAPTCHA_ERRADO)) == [CAPTCHA_ERRADO]

    def test_texto_ja_certo_fica_como_veio(self):
        pagina = ('<span class="pf-messages-warn-detail">'
                  'Código de verificação está incorreto</span>')

        assert sao_luis.avisos_do_portal(pagina) == [CAPTCHA_ERRADO]

    def test_avisos_se_acumulam_e_so_um_e_de_captcha(self):
        avisos = sao_luis.avisos_do_portal(_aviso(SEM_RAZAO, CAPTCHA_ERRADO))

        assert avisos == [SEM_RAZAO, CAPTCHA_ERRADO]
        assert [sao_luis.e_aviso_de_captcha(a) for a in avisos] == [False, True]


class TestConferirCaptcha:
    """O oraculo da ferramenta de treino: "Emitir" sem CNPJ, que nao emite."""

    def _portal(self, monkeypatch, corpo):
        portal = sao_luis.Portal()
        monkeypatch.setattr(portal, "emitir",
                            lambda *_a: _resposta(corpo))
        return portal

    def test_so_o_aviso_do_cnpj_e_codigo_certo(self, monkeypatch):
        portal = self._portal(monkeypatch, _aviso(SEM_RAZAO))

        assert portal.conferir_captcha(sao_luis.Formulario(), "ABCD")

    def test_aviso_de_captcha_e_codigo_errado(self, monkeypatch):
        portal = self._portal(monkeypatch, _aviso(SEM_RAZAO, CAPTCHA_ERRADO))

        assert not portal.conferir_captcha(sao_luis.Formulario(), "ABCD")

    def test_resposta_sem_aviso_nao_confirma_nada(self, monkeypatch):
        portal = self._portal(monkeypatch, "<html>500</html>")

        assert not portal.conferir_captcha(sao_luis.Formulario(), "ABCD")

    def test_emissao_sem_cnpj_para_a_ferramenta(self, monkeypatch):
        portal = self._portal(monkeypatch, REDIRECT)

        with pytest.raises(RuntimeError):
            portal.conferir_captcha(sao_luis.Formulario(), "ABCD")


class TestClassificacaoDoPdf:
    def _classificar(self, texto, tmp_path, monkeypatch):
        monkeypatch.setattr(sao_luis, "_texto_pdf", lambda _c: texto)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.5")
        return sao_luis.ler_pdf(alvo)

    def test_negativa_extrai_validade_codigo_e_lavratura(self, tmp_path,
                                                         monkeypatch):
        r = self._classificar(PDF_NEGATIVO, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.NEGATIVA
        assert r.caminho_pdf is not None
        assert r.validade == date(2027, 3, 23)
        assert r.codigo_controle == "677DB09664D9C5FE554382347A276D55"
        # O portal reaproveita certidao recente: a lavratura diz de quando
        # ela e, para quem confere.
        assert "24 de setembro de 2026 as 14:51" in r.mensagem_portal

    def test_cpen_vem_antes_da_negativa(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_CPEN, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.CPEN
        assert r.desfecho in COM_PDF

    def test_positiva_nao_e_entregavel(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_POSITIVO, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.POSITIVA
        assert r.evidencia is not None

    def test_pdf_sem_marcador_nao_e_chutado(self, tmp_path, monkeypatch):
        r = self._classificar("documento qualquer", tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.ERRO_TECNICO


class BancoFalso:
    """Le os palpites em ordem e registra o que aprendeu."""

    def __init__(self, *palpites):
        self.palpites = list(palpites)
        self.aprendidos = []
        self.vazio = False
        self.total = 1

    def ler(self, _imagem):
        return self.palpites.pop(0)

    def aprender(self, _imagem, codigo):
        self.aprendidos.append(codigo)
        return 0


class PortalFalso:
    def __init__(self, conferido=TELA_CONFERIDA, emissoes=()):
        self.conferido = conferido
        self.emissoes = list(emissoes)
        self.enviados = []
        self.url = sao_luis.URL_CONSULTA

    def abrir(self):
        return sao_luis.completar_com_pj(
            sao_luis.ler_formulario(PAGINA_INICIAL), TELA_PJ)

    def conferir_cnpj(self, _f, _cnpj):
        return _resposta(self.conferido)

    def captcha(self, _f):
        return object()

    def emitir(self, _f, cnpj, codigo, finalidade):
        self.enviados.append((cnpj, codigo, finalidade))
        return self.emissoes.pop(0)


class TestAdapter:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return sao_luis.AdapterSaoLuis("SAO_LUIS", cfg,
                                       caminho_banco=tmp_path / "ocr.json")

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA FICTICIA", lote_id=7)

    def _rodar(self, adapter, doc, monkeypatch, portal, *palpites):
        adapter._banco = BancoFalso(*palpites)
        monkeypatch.setattr(sao_luis, "Portal", lambda *_a: portal)
        return adapter.emitir(doc)

    def test_cnpj_sem_cadastro_vai_para_conferencia_com_o_texto_do_portal(
            self, adapter, doc, monkeypatch):
        portal = PortalFalso(conferido=_aviso("CNPJ não encontrado"))

        r = self._rodar(adapter, doc, monkeypatch, portal)

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL
        assert r.mensagem_portal == "CNPJ não encontrado"
        assert portal.enviados == []

    def test_captcha_errado_pede_outra_imagem_e_aprende_o_certo(
            self, adapter, doc, monkeypatch):
        portal = PortalFalso(emissoes=[_resposta(_aviso(CAPTCHA_ERRADO)),
                                       _resposta(REDIRECT)])
        baixados = []
        monkeypatch.setattr(adapter, "_baixar_certidao",
                            lambda _p, destino, _d: baixados.append(destino)
                            or sao_luis.ResultadoTentativa(Desfecho.NEGATIVA))

        r = self._rodar(adapter, doc, monkeypatch, portal, "abcd", "efgh")

        assert r.desfecho == Desfecho.NEGATIVA
        assert baixados == ["emissaoSucesso.jsf"]
        assert [c for _, c, _ in portal.enviados] == ["abcd", "efgh"]
        assert adapter._banco.aprendidos == ["efgh"]
        cnpj, _, finalidade = portal.enviados[-1]
        assert cnpj == CNPJ
        assert finalidade == sao_luis.FINALIDADE_PADRAO

    def test_leitura_sem_palpite_nao_vai_ao_portal(self, adapter, doc,
                                                   monkeypatch):
        portal = PortalFalso(emissoes=[_resposta(REDIRECT)])
        monkeypatch.setattr(adapter, "_baixar_certidao",
                            lambda *_a: sao_luis.ResultadoTentativa(
                                Desfecho.NEGATIVA))

        self._rodar(adapter, doc, monkeypatch, portal, None, "abcd")

        assert [c for _, c, _ in portal.enviados] == ["abcd"]

    def test_recusa_com_debito_e_positiva_com_a_frase_do_portal(
            self, adapter, doc, monkeypatch):
        """Nao vimos ainda um devedor em Sao Luis: o que vale e a faixa de
        avisos, e a frase do portal vai inteira para o item."""
        recusa = "Contribuinte possui débitos em aberto"
        portal = PortalFalso(emissoes=[_resposta(_aviso(recusa))])

        r = self._rodar(adapter, doc, monkeypatch, portal, "abcd")

        assert r.desfecho == Desfecho.POSITIVA
        assert r.mensagem_portal == recusa

    def test_recusa_sem_debito_vai_para_conferencia(self, adapter, doc,
                                                    monkeypatch):
        portal = PortalFalso(emissoes=[_resposta(_aviso(SEM_RAZAO,
                                                        CAPTCHA_ERRADO))])

        r = self._rodar(adapter, doc, monkeypatch, portal, "abcd")

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL
        assert r.mensagem_portal == SEM_RAZAO
        # Com o captcha errado na mesma resposta, o palpite nao e aprendido.
        assert adapter._banco.aprendidos == []

    def test_captcha_esgotado_leva_a_frase_do_portal(self, adapter, doc,
                                                     monkeypatch):
        adapter.tentativas_captcha = 2
        portal = PortalFalso(emissoes=[_resposta(_aviso(CAPTCHA_ERRADO))] * 2)

        r = self._rodar(adapter, doc, monkeypatch, portal, "abcd", "efgh")

        assert r.desfecho == Desfecho.CAPTCHA
        assert r.mensagem_portal == CAPTCHA_ERRADO

    def test_erro_do_jboss_e_retentavel(self, adapter, doc, monkeypatch):
        portal = PortalFalso(emissoes=[_resposta(
            "<html>HTTP Status 500 NullPointerException</html>",
            "text/html;charset=utf-8", 500)])

        r = self._rodar(adapter, doc, monkeypatch, portal, "abcd")

        assert r.desfecho == Desfecho.ERRO_TECNICO

    def test_impressao_pega_o_botao_e_o_viewstate_da_tela_de_sucesso(
            self, monkeypatch):
        portal = sao_luis.Portal()
        enviados = []
        monkeypatch.setattr(portal, "get", lambda _url: _resposta(
            TELA_SUCESSO, "text/html;charset=ISO-8859-1"))
        monkeypatch.setattr(portal, "post", lambda url, dados: enviados.append(
            (url, dados)) or _resposta("%PDF-1.5", "application/pdf"))

        portal.imprimir("https://x/emissaoSucesso.jsf")

        url, dados = enviados[0]
        assert url == "https://x/emissaoSucesso.jsf"
        assert dados == {"j_id10": "j_id10", "javax.faces.ViewState": "j_id2",
                         "j_id10:btImprimirCertidao":
                             "j_id10:btImprimirCertidao"}

    def test_negativa_vai_para_certidoes(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(sao_luis, "_texto_pdf", lambda _c: PDF_NEGATIVO)

        r = adapter._salvar_pdf(b"%PDF-1.5 negativa", doc)

        assert r.caminho_pdf is not None
        assert adapter.cfg.pasta_certidoes in r.caminho_pdf.parents
        assert r.evidencia is None

    def test_positiva_fica_em_evidencias(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(sao_luis, "_texto_pdf", lambda _c: PDF_POSITIVO)

        r = adapter._salvar_pdf(b"%PDF-1.5 positiva", doc)

        assert r.caminho_pdf is None
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []

    def test_cpf_nao_vai_ao_portal(self, adapter):
        doc = Documento(empresa_id=1, documento="12345678909", tipo="CPF",
                        nome="X", lote_id=1)

        assert adapter.emitir(doc).desfecho == Desfecho.PENDENCIA_MANUAL


class TestBancoDoCaptcha:
    def test_a_semente_vem_no_pacote_e_cobre_o_alfabeto(self):
        """Sem a semente o orgao liga e nao le captcha nenhum."""
        banco = sao_luis.BancoCaptcha.carregar(sao_luis.SEMENTE)

        # O portal usa A-Z e 1-9; o zero nao aparece (24/09/2026).
        assert set(banco.amostras) == set("abcdefghijklmnopqrstuvwxyz123456789")

    def test_maquina_sem_banco_usa_a_semente(self, tmp_path):
        adapter = sao_luis.AdapterSaoLuis(
            "SAO_LUIS", carregar(), caminho_banco=tmp_path / "nao_existe.json")

        adapter.preparar()

        assert not adapter._banco.vazio

    def test_banco_da_maquina_vale_no_lugar_da_semente(self, tmp_path):
        proprio = sao_luis.BancoCaptcha()
        proprio.registrar("a", 1)
        caminho = tmp_path / "sao_luis.json"
        proprio.salvar(caminho)
        adapter = sao_luis.AdapterSaoLuis("SAO_LUIS", carregar(),
                                          caminho_banco=caminho)

        adapter.preparar()

        assert adapter._banco.total == 1


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("SAO_LUIS")
        assert orgao is not None, "SAO_LUIS precisa estar no config.exemplo.toml"

        adapter = sao_luis.criar(orgao, cfg)

        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "SAO_LUIS"
        assert adapter.finalidade == "Comprovação de regularidade fiscal"

    def test_o_empacotador_leva_o_adapter_e_a_semente(self):
        spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.municipal.sao_luis"' in spec
        assert '"captcha_sao_luis.json"' in spec

    def test_planilha_reconhece_aba_sao_luis(self):
        assert planilha.ABA_PARA_ORGAO["SAO LUIS"] == ("SAO_LUIS", "CNPJ")
        assert planilha.ORGAO_PARA_TIPO["SAO_LUIS"] == "CNPJ"

    def test_orgao_tem_nome_para_o_cliente(self):
        assert nome_do_orgao("SAO_LUIS") == "PREFEITURA SAO LUIS"
