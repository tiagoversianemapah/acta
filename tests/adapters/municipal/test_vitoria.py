"""Prefeitura de Vitoria (ES): fluxo ASP.NET, sem captcha.

Os trechos de HTML e de PDF abaixo sao recortes do portal real, colhidos em
22/09/2026 com CNPJs de verdade.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cnd.adapters.municipal import vitoria
from cnd.core.modelos import COM_PDF, Desfecho, Documento
from cnd.infra.config import carregar, nome_do_orgao
from cnd.ingestao import planilha
from tests.conftest import RAIZ

CNPJ = "28127603000178"

FORMULARIO = """
<html><body><form method="post" action="./CertidaoNegativa.aspx">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="QcI7vnbC8/6F" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="04B519F7" />
<input type="hidden" name="__EVENTVALIDATION" value="VEpOdS5/5hlp+a/b" />
<input id="ctl00_conteudo_rblTipoDocumento_1" type="radio"
       name="ctl00$conteudo$rblTipoDocumento" value="CNPJ" />
<input name="ctl00$conteudo$txtTermoBusca" type="text" maxlength="14" />
<input type="submit" name="ctl00$conteudo$btnEnviar" value="Continuar" />
</form></body></html>
""".encode("utf-8")

TELA_NEGATIVA = """
<html><body>
<span>Certid&atilde;o Negativa de D&eacute;bitos</span>
<p>Documento v&aacute;lido at&eacute; o dia 21/11/2026 e abrange apenas a
pessoa f&iacute;sica ou jur&iacute;dica identificada.</p>
<input type="submit" name="ctl00$conteudo$btnEmitir" value="Emitir"
 onclick="window.open(&#39;ExibirRelatorioRetPDF.aspx?qs=1AtY%2fOT4x8lu&#39;);" />
</body></html>
""".encode("utf-8")

TELA_SEM_BOTAO = """
<html><body>
<span>Informe:</span><span>CNPJ:</span>
<input name="ctl00$conteudo$txtTermoBusca" type="text" value="11111111111111" />
<input type="submit" name="ctl00$conteudo$btnEnviar" value="Continuar" />
</body></html>
""".encode("utf-8")

# O cabecalho repete "Certidao Negativa de Debitos" mesmo quando o portal
# acusa pendencia: e o titulo da pagina. Por isso o recorte do resultado.
TELA_COM_PENDENCIA = """
<html><body>
<h1>Certid&atilde;o Negativa de D&eacute;bitos de Tributos Municipais</h1>
<span>Informe:</span><span>CNPJ:</span>
<p>Certid&atilde;o de D&eacute;bitos de Tributos Municipais As informa&ccedil;&otilde;es
dispon&iacute;veis sobre o contribuinte CNPJ: 55.014.213/0001-64 n&atilde;o s&atilde;o
suficientes para que se considere sua situa&ccedil;&atilde;o fiscal regular.</p>
<p>Pend&ecirc;ncias encontradas</p>
<span>&times; Ajuda Documento utilizado para fins de comprova&ccedil;&atilde;o</span>
</body></html>
""".encode("utf-8")

PDF_NEGATIVO = """
Emissao : 22/09/2026 - 16:20h
CNPJ ............................: 07651309000190
CNPJ nao possui registros nos cadastros da PMV
OBSERVACOES
E certificado que nao constam pendencias para a pessoa fisica/juridica acima
identificada perante a Fazenda Publica Municipal.
Documento valido ate o dia 21/11/2026 e abrange apenas a pessoa fisica ou
juridica identificada.
Entre com a chave: ac95ca1f-160f-45b1-81dd-2b9c01294762
Com fundamento no artigo 205 do Codigo Tributario Nacional (Lei 5.172/1966),
certificamos que nao constam em nome do sujeito passivo debitos.
"""

PDF_CPEN = """
Emissao : 22/09/2026 - 16:20h
CNPJ ............................: 28127603000178
RAZAO SOCIAL/NOME: BANESTES SA BANCO DO ESTADO DO ESPIRITO SANTO
Documento valido ate o dia 22/10/2026 e abrange apenas a pessoa fisica ou
juridica identificada.
Entre com a chave: 6357700b-b2ea-4fd2-9b29-fed96a3af5ca
Com fundamento no artigo 206 do CTN, certificamos que constam em nome do
sujeito passivo identificado, nesta data, debitos com a Fazenda Publica
Municipal com exigibilidade suspensa (artigo 151 do CTN) ou penhora efetivada.
Certidao Positiva com Efeito de Negativa
"""

PDF_POSITIVO = """
Emissao : 22/09/2026 - 16:20h
CNPJ ............................: 28127603000178
Certidao Positiva de Debitos
Constam debitos vencidos com a Fazenda Publica Municipal.
"""


def _resposta(corpo: bytes, content_type: str = "text/html; charset=utf-8",
              status: int = 200):
    return vitoria.RespostaPortal(status=status, content_type=content_type,
                                  corpo=corpo)


class TestPaginaDoPortal:
    def test_campos_ocultos_do_webforms_saem_inteiros(self):
        ocultos = vitoria.campos_ocultos(FORMULARIO.decode("utf-8"))

        assert ocultos["__VIEWSTATE"] == "QcI7vnbC8/6F"
        assert ocultos["__EVENTVALIDATION"] == "VEpOdS5/5hlp+a/b"
        assert ocultos["__VIEWSTATEGENERATOR"] == "04B519F7"

    def test_link_do_emitir_sai_do_onclick(self):
        link = vitoria.link_de_emissao(TELA_NEGATIVA.decode("utf-8"))

        assert link == "ExibirRelatorioRetPDF.aspx?qs=1AtY%2fOT4x8lu"

    def test_sem_botao_nao_ha_link(self):
        assert vitoria.link_de_emissao(TELA_SEM_BOTAO.decode("utf-8")) is None


class TestClassificacaoDoPdf:
    def _classificar(self, texto_pdf, tmp_path, monkeypatch):
        monkeypatch.setattr(vitoria, "_texto_pdf", lambda _c: texto_pdf)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.3")
        return vitoria.ler_pdf(alvo, "tela")

    def test_negativa_extrai_validade_e_chave(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_NEGATIVO, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.NEGATIVA
        assert r.caminho_pdf is not None
        assert r.validade == date(2026, 11, 21)
        assert r.codigo_controle == "AC95CA1F-160F-45B1-81DD-2B9C01294762"

    def test_cpen_vem_antes_da_positiva(self, tmp_path, monkeypatch):
        """O texto do CPEN diz "constam debitos" e "positiva": sem a ordem
        certa, a certidao entregavel viraria positiva e ninguem receberia."""
        r = self._classificar(PDF_CPEN, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.CPEN
        assert r.desfecho in COM_PDF
        assert r.validade == date(2026, 10, 22)

    def test_positiva_nao_e_entregavel(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_POSITIVO, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.POSITIVA
        assert r.desfecho not in COM_PDF
        assert r.evidencia is not None

    def test_pdf_sem_marcador_nao_e_chutado(self, tmp_path, monkeypatch):
        r = self._classificar("documento qualquer", tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.ERRO_TECNICO


class TestAdapter:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return vitoria.AdapterVitoria("VITORIA", cfg)

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA TESTE", lote_id=7)

    def test_fluxo_manda_o_documento_e_devolve_os_ocultos(
            self, adapter, doc, monkeypatch):
        """O WebForms recusa o POST que não devolve __VIEWSTATE, e o radio é
        um postback à parte — por isso são dois POSTs, nesta ordem."""
        enviados = []
        monkeypatch.setattr(adapter, "_get", lambda _url: _resposta(FORMULARIO))
        monkeypatch.setattr(adapter, "_post",
                            lambda dados: enviados.append(dados) or
                            _resposta(FORMULARIO if len(enviados) == 1
                                      else TELA_NEGATIVA))
        monkeypatch.setattr(adapter, "_baixar_certidao",
                            lambda _l, _d, _t: vitoria.ResultadoTentativa(
                                Desfecho.NEGATIVA))
        adapter.preparar()

        resultado = adapter._consultar(doc)

        assert resultado.desfecho == Desfecho.NEGATIVA
        escolha, consulta = enviados
        assert escolha["__EVENTTARGET"] == "ctl00$conteudo$rblTipoDocumento$1"
        assert escolha["ctl00$conteudo$rblTipoDocumento"] == "CNPJ"
        assert consulta["ctl00$conteudo$txtTermoBusca"] == CNPJ
        assert consulta["ctl00$conteudo$btnEnviar"] == "Continuar"
        assert consulta["__VIEWSTATE"] == "QcI7vnbC8/6F"

    def test_empresa_com_debito_e_positiva_com_a_tela_guardada(
            self, adapter, doc, monkeypatch):
        """Sem botao Emitir porque nao ha certidao: e POSITIVA, e a tela com as
        pendencias fica de evidencia para quem for tratar (22/09/2026)."""
        monkeypatch.setattr(adapter, "_get", lambda _url: _resposta(FORMULARIO))
        monkeypatch.setattr(adapter, "_post",
                            lambda _dados: _resposta(TELA_COM_PENDENCIA))
        adapter.preparar()

        r = adapter._consultar(doc)

        assert r.desfecho == Desfecho.POSITIVA
        assert r.desfecho not in COM_PDF
        assert r.evidencia is not None and r.evidencia.exists()
        assert "situa" in r.mensagem_portal and "regular" in r.mensagem_portal

    def test_cabecalho_da_pagina_nao_vira_negativa(self):
        """O titulo "Certidao Negativa de Debitos" aparece em toda consulta:
        classificar a pagina inteira daria certidao para quem tem debito."""
        texto = vitoria.texto_da_resposta(_resposta(TELA_COM_PENDENCIA))

        assert vitoria.classificar_texto(texto) == Desfecho.POSITIVA

    def test_documento_recusado_vai_para_conferencia(self, adapter, doc,
                                                     monkeypatch):
        """Digito errado não gera tela de erro: o portal devolve o formulário
        calado, e insistir não muda nada (22/09/2026)."""
        monkeypatch.setattr(adapter, "_get", lambda _url: _resposta(FORMULARIO))
        monkeypatch.setattr(adapter, "_post",
                            lambda _dados: _resposta(TELA_SEM_BOTAO))
        adapter.preparar()

        resultado = adapter._consultar(doc)

        assert resultado.desfecho == Desfecho.PENDENCIA_MANUAL
        assert "sem resposta" in resultado.mensagem_portal

    def test_html_no_lugar_do_pdf_e_erro_retentavel(self, adapter, doc,
                                                    monkeypatch):
        """Veredito na tela sem PDF na mão não é certidão: o pacote promete
        o arquivo, e prometer o que não existe é pior que tentar de novo."""
        monkeypatch.setattr(adapter, "_get",
                            lambda _url: _resposta(TELA_NEGATIVA))
        adapter.preparar()

        resultado = adapter._baixar_certidao("ExibirRelatorioRetPDF.aspx?qs=x",
                                             doc, "tela")

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert resultado.desfecho not in COM_PDF

    def test_negativa_vai_para_certidoes(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(vitoria, "_texto_pdf", lambda _c: PDF_NEGATIVO)

        r = adapter._salvar_pdf(b"%PDF-1.3 negativa", doc, "tela")

        assert r.caminho_pdf is not None
        assert adapter.cfg.pasta_certidoes in r.caminho_pdf.parents
        assert r.evidencia is None
        assert r.mensagem_portal == "Prefeitura de Vitoria emitiu PDF"

    def test_positiva_fica_em_evidencias(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(vitoria, "_texto_pdf", lambda _c: PDF_POSITIVO)

        r = adapter._salvar_pdf(b"%PDF-1.3 positiva", doc, "tela")

        assert r.caminho_pdf is None
        assert r.evidencia is not None
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []

    def test_documento_de_outro_tipo_nao_vai_ao_portal(self, adapter):
        doc = Documento(empresa_id=1, documento="123", tipo="INSCRICAO",
                        nome="X", lote_id=1)

        r = adapter.emitir(doc)

        assert r.desfecho == Desfecho.PENDENCIA_MANUAL


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("VITORIA")
        assert orgao is not None, "VITORIA precisa estar no config.exemplo.toml"

        adapter = vitoria.criar(orgao, cfg)

        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "VITORIA"

    def test_o_empacotador_leva_o_adapter(self):
        spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.municipal.vitoria"' in spec

    def test_planilha_reconhece_aba_vitoria(self):
        assert planilha.ABA_PARA_ORGAO["VITORIA"] == ("VITORIA", "CNPJ")
        assert planilha.ORGAO_PARA_TIPO["VITORIA"] == "CNPJ"

    def test_orgao_tem_nome_para_o_cliente(self):
        assert nome_do_orgao("VITORIA") == "PREFEITURA VITORIA"
