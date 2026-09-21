"""SEFAZ-MT: fluxo HTTP, reimpressao e classificacao do PDF."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cnd.adapters.estadual import sefaz_mt
from cnd.core.modelos import COM_PDF, Desfecho, Documento, ResultadoTentativa
from cnd.infra.config import carregar
from cnd.ingestao import planilha
from tests.conftest import RAIZ

CNPJ = "11222333000181"

PDF_NEGATIVO = """
ESTADO DE MATO GROSSO
PROCURADORIA GERAL DO ESTADO
SECRETARIA DE ESTADO DE FAZENDA

CERTIDAO NEGATIVA DE DEBITOS RELATIVOS A CREDITOS TRIBUTARIOS E NAO
TRIBUTARIOS ESTADUAIS GERIDOS PELA PROCURADORIA-GERAL DO ESTADO E
PELA SECRETARIA DE ESTADO DE FAZENDA
CND No 0064988861

Data da emissao: 21/09/2026 Hora da emissao: 15:10:40
CNPJ: 31.705.832/0001-37

CERTIFICAMOS que, ate a data e hora em epigrafe, nao consta, nas bases
informatizadas e integradas ao sistema de processamento de dados da CND,
pendencia, em nome do sujeito passivo acima indicado.

Certidao valida ate: 19/11/2026.
Numero de Autenticacao: T7BTLL22TUKKB2K2
"""

LISTA_REIMPRESSAO = """
<form method="POST" action="/cnd/certidao/servlet/ServletRotdAberto">
<input type="hidden" name="origem" value="59">
<tr class="SEFAZ-TD-ExibicaoPar">
  <td><input type="radio" name="ModeloCertidao" onclick="setNumrRelt(64561340)"></td>
  <td>0064561340</td>
  <td>19 - CERTIDAO CONJUNTA</td>
  <td>Certidao Negativa de Debitos</td>
  <td>28/08/2026 10:44:27</td>
  <td>26/10/2026</td>
</tr>
<tr class="SEFAZ-TD-ExibicaoImPar">
  <td><input type="radio" name="ModeloCertidao" onclick="setNumrRelt(64463909)"></td>
  <td>0064463909</td>
  <td>19 - CERTIDAO CONJUNTA</td>
  <td>Certidao Negativa de Debitos</td>
  <td>24/08/2026 17:26:56</td>
  <td>22/10/2026</td>
</tr>
</form>
""".encode()


def _resposta(corpo: bytes, content_type: str = "text/html;charset=UTF-8",
              status: int = 200):
    return sefaz_mt.RespostaPortal(status=status, content_type=content_type,
                                   corpo=corpo)


class TestTextoDaResposta:
    def test_script_style_e_tags_somem(self):
        corpo = (
            b"<style>.x{color:red}</style>"
            b"<script>var x='nao consta pendencia';</script>"
            b"<b>CNPJ&nbsp;invalido</b>"
        )

        texto = sefaz_mt.texto_da_resposta(_resposta(corpo))

        assert "var x" not in texto
        assert ".x" not in texto
        assert "CNPJ invalido" in texto


class TestClassificacaoSemPdf:
    def test_negativa_sem_pdf_nao_vira_certidao(self):
        desfecho = sefaz_mt.classificar_texto(
            "nao consta pendencia para o contribuinte")

        assert desfecho == Desfecho.ERRO_TECNICO
        assert desfecho not in COM_PDF

    def test_pendencia_vira_positiva(self):
        assert sefaz_mt.classificar_texto(
            "Nao foi possivel emitir a Certidao Negativa: existem pendencias"
        ) == Desfecho.POSITIVA

    def test_ocorrencia_sem_regularidade_vira_positiva(self):
        texto = (
            "OCORRENCIAS NO AMBITO DA SECRETARIA DE ESTADO DE FAZENDA. "
            "As informacoes disponiveis sobre o contribuinte nao sao "
            "suficientes para que se considere sua situacao regular."
        )

        assert sefaz_mt.classificar_texto(texto) == Desfecho.POSITIVA

    def test_documento_invalido_vai_para_conferencia(self):
        assert sefaz_mt.classificar_texto("CNPJ invalido") == (
            Desfecho.PENDENCIA_MANUAL
        )

    def test_sessao_encerrada_e_erro_retentavel(self):
        assert sefaz_mt.classificar_texto("Sessao encerrada") == (
            Desfecho.ERRO_TECNICO
        )


class TestClassificacaoDoPdf:
    def _classificar(self, texto_pdf: str, tmp_path, monkeypatch):
        monkeypatch.setattr(sefaz_mt, "_texto_pdf", lambda _c: texto_pdf)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        return sefaz_mt.ler_pdf(alvo, "tela")

    def test_negativa_real_extrai_validade_e_codigo(self, tmp_path, monkeypatch):
        r = self._classificar(PDF_NEGATIVO, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.NEGATIVA
        assert r.caminho_pdf is not None
        assert r.validade == date(2026, 11, 19)
        assert r.codigo_controle == "T7BTLL22TUKKB2K2"

    def test_cpen_vem_antes_da_positiva(self, tmp_path, monkeypatch):
        r = self._classificar("CERTIDAO POSITIVA COM EFEITOS DE NEGATIVA",
                              tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.CPEN
        assert r.desfecho in COM_PDF

    def test_positiva_nao_e_entregavel(self, tmp_path, monkeypatch):
        r = self._classificar(
            "CERTIDAO POSITIVA DE DEBITOS. Consta pendencia tributaria.",
            tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.POSITIVA
        assert r.desfecho not in COM_PDF
        assert r.evidencia is not None

    def test_pdf_sem_marcador_nao_e_chutado(self, tmp_path, monkeypatch):
        r = self._classificar("documento qualquer", tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.ERRO_TECNICO


class TestReimpressao:
    def test_lista_certidoes_e_escolhe_a_mais_recente(self):
        certidoes = sefaz_mt.certidoes_da_lista(LISTA_REIMPRESSAO)

        assert [c.sequencial for c in certidoes] == ["64561340", "64463909"]
        assert sefaz_mt.certidao_mais_recente(certidoes).sequencial == "64561340"

    def test_tela_de_vigente_dispara_reimpressao(self, tmp_path, monkeypatch):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "c",
                      pasta_evidencias=tmp_path / "e")
        adapter = sefaz_mt.AdapterSEFAZMT("SEFAZ_MT", cfg)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA", lote_id=1)
        chamadas = []

        monkeypatch.setattr(
            adapter, "_reimprimir",
            lambda recebido: chamadas.append(recebido) or
            ResultadoTentativa(Desfecho.NEGATIVA),
        )

        r = adapter._interpretar(
            _resposta(b"Reimprimir Certidao Vigente"), doc)

        assert r.desfecho == Desfecho.NEGATIVA
        assert chamadas == [doc]


class TestProcessamento:
    def test_requerimento_polla_ate_o_pdf(self, tmp_path, monkeypatch):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "c",
                      pasta_evidencias=tmp_path / "e")
        adapter = sefaz_mt.AdapterSEFAZMT(
            "SEFAZ_MT", cfg, espera_processamento_s=0.0,
            tempo_processamento_s=1.0)
        doc = Documento(1, CNPJ, "CNPJ", "EMPRESA", lote_id=1)
        respostas = iter([
            _resposta(b"<b>REQUERIMENTO</b>"),
            _resposta(b"%PDF-1.4 negativa", "application/pdf"),
        ])

        monkeypatch.setattr(adapter, "_get", lambda *_args: next(respostas))
        monkeypatch.setattr(adapter, "_salvar_pdf",
                            lambda _bytes, _doc, _msg: ResultadoTentativa(
                                Desfecho.NEGATIVA))

        r = adapter._interpretar(_resposta(b"<b>REQUERIMENTO</b>"), doc)

        assert r.desfecho == Desfecho.NEGATIVA


class TestGuardaDoPdf:
    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return sefaz_mt.AdapterSEFAZMT(orgao="SEFAZ_MT", cfg=cfg)

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA TESTE LTDA", lote_id=7)

    def test_negativa_vai_para_certidoes(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(sefaz_mt, "_texto_pdf", lambda _c: PDF_NEGATIVO)

        r = adapter._salvar_pdf(b"%PDF-1.4 negativa", doc, "ok")

        assert r.caminho_pdf is not None
        assert adapter.cfg.pasta_certidoes in r.caminho_pdf.parents
        assert r.evidencia is None

    def test_positiva_fica_em_evidencias(self, adapter, doc, monkeypatch):
        monkeypatch.setattr(
            sefaz_mt, "_texto_pdf",
            lambda _c: "CERTIDAO POSITIVA DE DEBITOS. Consta pendencia.",
        )

        r = adapter._salvar_pdf(b"%PDF-1.4 positiva", doc, "ok")

        assert r.caminho_pdf is None
        assert r.evidencia is not None
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("SEFAZ_MT")
        assert orgao is not None, "SEFAZ_MT precisa estar no config.exemplo.toml"

        adapter = sefaz_mt.criar(orgao, cfg)

        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "SEFAZ_MT"

    def test_o_empacotador_leva_o_adapter(self):
        spec = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.estadual.sefaz_mt"' in spec

    def test_planilha_reconhece_aba_mt(self):
        assert planilha.ABA_PARA_ORGAO["MT"] == ("SEFAZ_MT", "CNPJ")
        assert planilha.ORGAO_PARA_TIPO["SEFAZ_MT"] == "CNPJ"
