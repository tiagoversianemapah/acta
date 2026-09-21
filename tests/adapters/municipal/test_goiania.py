"""Prefeitura de Goiania: CND conjunta por CPF/CNPJ."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cnd.adapters.municipal import goiania
from cnd.core.modelos import COM_PDF, Desfecho, Documento
from cnd.infra.config import carregar, nome_do_orgao
from cnd.ingestao import planilha
from tests.conftest import RAIZ

CNPJ = "14757813000640"

HTML_NEGATIVA = """
<html><body>
<b>CERTIDAO CONJUNTA DE REGULARIDADE FISCAL<br>
NEGATIVA DE DEBITOS DE QUALQUER NATUREZA<br>PESSOA JURIDICA<br>
NUMERO DA CERTIDAO: 2.432.686-6</b>
<p>Prazo de Validade: ate 19/12/2026</p>
<p>CNPJ: 14.757.813/0006-40</p>
<p>Certifica-se que ate a presente data NAO CONSTA DEBITO VENCIDO OU A VENCER
referente a debitos de qualquer natureza administrados pela Prefeitura Municipal
de Goiania.</p>
</body></html>
""".encode("iso-8859-1")

HTML_POSITIVA = """
<html><body>
<b>CERTIDAO CONJUNTA DE REGULARIDADE FISCAL<br>
POSITIVA DE DEBITOS DE QUALQUER NATUREZA</b>
<p>CONSTAM DEBITOS VENCIDOS referente ao contribuinte.</p>
</body></html>
""".encode("iso-8859-1")


def _resposta(corpo: bytes, content_type: str = "text/html; Charset=iso-8859-1",
              status: int = 200):
    return goiania.RespostaPortal(status=status, content_type=content_type,
                                  corpo=corpo)


class TestTexto:
    def test_resposta_iso_8859_1_e_lida_com_acento(self):
        corpo = "<b>CERTIDÃO NEGATIVA</b>".encode("iso-8859-1")

        texto = goiania.texto_da_resposta(_resposta(corpo))

        assert "CERTIDÃO NEGATIVA" in texto

    def test_script_style_e_tags_somem(self):
        corpo = (
            b"<style>.x{}</style><script>const x='NAO CONSTA'</script>"
            b"<p>NOME&nbsp;INVALIDO.</p>"
        )

        texto = goiania.texto_da_resposta(_resposta(corpo))

        assert "const x" not in texto
        assert ".x" not in texto
        assert "NOME INVALIDO." in texto


class TestClassificacao:
    def test_negativa_conjunta_e_entregavel(self):
        resultado = goiania.classificar_certidao(
            goiania.texto_da_resposta(_resposta(HTML_NEGATIVA)))

        assert resultado.desfecho == Desfecho.NEGATIVA
        assert resultado.desfecho in COM_PDF
        assert resultado.validade == date(2026, 12, 19)
        assert resultado.codigo_controle == "2.432.686-6"

    def test_cpen_vem_antes_de_positiva(self):
        resultado = goiania.classificar_certidao(
            "CERTIDAO POSITIVA COM EFEITO DE NEGATIVA. CONSTA DEBITO")

        assert resultado.desfecho == Desfecho.CPEN

    def test_positiva_sem_pdf(self):
        resultado = goiania.classificar_certidao(
            goiania.texto_da_resposta(_resposta(HTML_POSITIVA)))

        assert resultado.desfecho == Desfecho.POSITIVA
        assert resultado.desfecho not in COM_PDF

    def test_positiva_com_constam_debitos_plural(self):
        resultado = goiania.classificar_certidao(
            "CERTIDAO POSITIVA DE DEBITOS. CONSTAM DEBITOS VENCIDOS.")

        assert resultado.desfecho == Desfecho.POSITIVA

    def test_documento_invalido_vira_pendencia_manual(self):
        resultado = goiania.classificar_certidao("NUMERO DO CPF/CNPJ INVALIDO.")

        assert resultado.desfecho == Desfecho.PENDENCIA_MANUAL

    def test_captcha_se_um_dia_for_exigido_fica_retentavel(self):
        resultado = goiania.classificar_certidao(
            "Por favor, digite os caracteres mostrados na imagem.")

        assert resultado.desfecho == Desfecho.CAPTCHA


class TestAdapter:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return goiania.AdapterGoiania("GOIANIA", cfg)

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA TESTE", lote_id=7)

    def test_post_clica_emitir_sem_captcha(self, adapter, doc, monkeypatch):
        enviados = []
        monkeypatch.setattr(adapter, "_get",
                            lambda _url: _resposta(b"<form>ok</form>"))

        def post(dados, referer):
            enviados.append((dados, referer))
            return _resposta(HTML_NEGATIVA)

        monkeypatch.setattr(adapter, "_post", post)
        monkeypatch.setattr(adapter, "_salvar_certidao_html",
                            lambda _r, _d, _t: goiania.ResultadoTentativa(
                                Desfecho.NEGATIVA))

        resultado = adapter._consultar(doc)

        assert resultado.desfecho == Desfecho.NEGATIVA
        dados, _referer = enviados[0]
        assert dados["txt_nr_cpfcnpj"] == CNPJ
        assert dados["sel_cpfcnpj"] == "2"
        assert dados["txt_captcha"] == ""

    def test_negativa_html_vira_pdf_em_certidoes(self, adapter, doc, monkeypatch):
        chamados = []

        def gerar_pdf(resposta, evidencia, destino, texto):
            chamados.append((resposta, evidencia, destino, texto))
            destino.write_bytes(b"%PDF-1.4\n% teste\n")

        monkeypatch.setattr(goiania, "_gerar_pdf_certidao_html", gerar_pdf)

        resultado = adapter._salvar_certidao_html(
            _resposta(HTML_NEGATIVA), doc, "ok")

        assert resultado.desfecho == Desfecho.NEGATIVA
        assert resultado.caminho_pdf is not None
        assert resultado.caminho_pdf.exists()
        assert adapter.cfg.pasta_certidoes in resultado.caminho_pdf.parents
        assert resultado.evidencia is None
        assert resultado.caminho_pdf.read_bytes().lstrip().startswith(b"%PDF")
        assert chamados

    def test_positiva_html_fica_em_evidencias(self, adapter, doc):
        resultado = adapter._salvar_certidao_html(
            _resposta(HTML_POSITIVA), doc, "ok")

        assert resultado.desfecho == Desfecho.POSITIVA
        assert resultado.caminho_pdf is None
        assert resultado.evidencia is not None
        assert adapter.cfg.pasta_evidencias in resultado.evidencia.parents
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("GOIANIA")
        assert orgao is not None, "GOIANIA precisa estar no config.exemplo.toml"

        adapter = goiania.criar(orgao, cfg)

        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "GOIANIA"

    def test_o_empacotador_leva_o_adapter(self):
        spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.municipal.goiania"' in spec

    def test_planilha_reconhece_aba_goiania(self):
        assert planilha.ABA_PARA_ORGAO["GOIANIA"] == ("GOIANIA", "CNPJ")
        assert planilha.ORGAO_PARA_TIPO["GOIANIA"] == "CNPJ"
        assert nome_do_orgao("GOIANIA") == "PREFEITURA GOIANIA"
