"""SEFAZ-MT: fluxo HTTP, certidao vigente e classificacao do PDF."""
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


class TestCertidaoVigente:
    """Com certidao vigente, o robo pede NOVA - nunca reimprime a antiga."""

    TELA_VIGENTE = (b"Voce possui uma Certidao Negativa de Debitos (CND) que "
                    b"ainda esta no prazo de validade. "
                    b"<a>Reimprimir Certidao Vigente</a> "
                    b"<a>Emitir nova Certidao</a>")

    @pytest.fixture
    def adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "c",
                      pasta_evidencias=tmp_path / "e")
        return sefaz_mt.AdapterSEFAZMT(
            "SEFAZ_MT", cfg, espera_processamento_s=0.0,
            tempo_processamento_s=1.0)

    @pytest.fixture
    def doc(self):
        return Documento(1, CNPJ, "CNPJ", "EMPRESA", lote_id=1)

    def test_pede_nova_com_os_campos_do_navegador(self, adapter, doc,
                                                   monkeypatch):
        enviados = []
        monkeypatch.setattr(
            adapter, "_post",
            lambda dados, _ref: enviados.append(dados) or
            _resposta(b"%PDF-1.4 nova", "application/pdf"))
        monkeypatch.setattr(adapter, "_salvar_pdf",
                            lambda _b, _d, _m: ResultadoTentativa(
                                Desfecho.NEGATIVA))

        r = adapter._interpretar(_resposta(self.TELA_VIGENTE), doc)

        assert r.desfecho == Desfecho.NEGATIVA
        assert len(enviados) == 1
        assert enviados[0]["origem"] == "62"
        assert enviados[0]["numrDoctFinal"] == CNPJ
        assert enviados[0]["indiceModlCertSelecionado"] == "19"
        assert enviados[0]["tipoDoctSele"] == "2"

    def test_nova_passa_pelo_requerimento_ate_o_pdf(self, adapter, doc,
                                                     monkeypatch):
        monkeypatch.setattr(adapter, "_post",
                            lambda *_a: _resposta(b"<b>REQUERIMENTO</b>"))
        monkeypatch.setattr(adapter, "_get", lambda *_a: _resposta(
            b"%PDF-1.4 nova", "application/pdf"))
        monkeypatch.setattr(adapter, "_salvar_pdf",
                            lambda _b, _d, _m: ResultadoTentativa(
                                Desfecho.NEGATIVA))

        r = adapter._interpretar(_resposta(self.TELA_VIGENTE), doc)

        assert r.desfecho == Desfecho.NEGATIVA

    def test_vigente_de_novo_e_erro_e_nao_laco(self, adapter, doc,
                                               monkeypatch):
        chamadas = []
        monkeypatch.setattr(
            adapter, "_post",
            lambda *_a: chamadas.append(1) or _resposta(self.TELA_VIGENTE))

        r = adapter._interpretar(_resposta(self.TELA_VIGENTE), doc)

        assert r.desfecho == Desfecho.ERRO_TECNICO
        assert r.desfecho not in COM_PDF
        assert len(chamadas) == 1


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
