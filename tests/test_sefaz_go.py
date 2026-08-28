"""SEFAZ-GO: classificação da resposta e onde cada PDF é guardado.

Sem rede e sem portal. O que se exercita aqui são as funções puras que
decidem o desfecho — que é onde mora o risco deste adapter: ele fala HTTP,
então a resposta chega como texto ou como bytes, e classificar errado é
mais fácil (e mais caro) do que falhar.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from cnd.adapters import sefaz_go
from cnd.core.modelos import COM_PDF, CONCLUSIVOS, Desfecho, Documento
from cnd.infra.config import carregar

CNPJ = "11222333000181"


def _resposta(corpo: bytes, content_type: str = "text/html", status: int = 200):
    return sefaz_go.RespostaPortal(status=status, content_type=content_type,
                                   corpo=corpo)


class TestLeituraDaPagina:
    def test_pagina_sem_charset_e_lida_como_iso_8859_1(self):
        """As páginas ASP do portal não mandam charset, mas vêm em
        ISO-8859-1. Ler como UTF-8 troca "Certidão" por ruído e quebra
        exatamente os textos que classificam o desfecho."""
        corpo = "<p>Certidão Negativa não consta débito</p>".encode("iso-8859-1")

        texto = sefaz_go.texto_da_resposta(_resposta(corpo, content_type="text/html"))

        assert "Certidão" in texto
        assert "débito" in texto

    def test_charset_declarado_no_header_manda(self):
        corpo = "<p>Certidão</p>".encode()
        texto = sefaz_go.texto_da_resposta(
            _resposta(corpo, content_type="text/html; charset=utf-8"))
        assert "Certidão" in texto

    def test_script_e_tag_somem_e_entidade_vira_texto(self):
        corpo = (b"<script>var x = 'nao consta debito';</script>"
                 b"<b>Consta&nbsp;d&eacute;bito</b>")
        texto = sefaz_go.texto_da_resposta(_resposta(corpo))

        assert "var x" not in texto, "o script entraria na classificação"
        assert "Consta" in texto and "bito" in texto

    def test_pede_confirmacao_reconhece_a_tela_com_acento(self):
        texto = "Confirma o Nome do Contribuinte? FULANO DE TAL LTDA"
        assert sefaz_go.pede_confirmacao(texto) is True
        assert sefaz_go.pede_confirmacao("Certidão emitida") is False


class TestClassificacaoSemPdf:
    """A regra que este adapter não pode quebrar.

    `classificar_texto` só é chamada quando NÃO veio PDF. NEGATIVA e CPEN
    são conclusivas e entram no relatório como certidão em mãos — devolvê-las
    aqui fecharia o item dizendo que existe um documento que não existe, e
    o item nunca mais seria retentado.
    """

    def test_nunca_devolve_desfecho_que_promete_documento(self):
        telas = [
            "Nao consta debito inscrito em divida ativa para o contribuinte",
            "NÃO CONSTA DÉBITO",
            "Consta debito inscrito",
            "Acesso Negado - a requisicao foi bloqueada pela politica de seguranca",
            "CNPJ invalido",
            "Ocorreu o erro ao processar",
            "pagina totalmente inesperada",
            "",
        ]
        for tela in telas:
            assert sefaz_go.classificar_texto(tela) not in COM_PDF, tela

    def test_nao_consta_debito_sem_pdf_vira_erro_retentavel(self):
        """Portal que responde mas não entrega o documento não concluiu o
        trabalho: a próxima tentativa costuma trazer o PDF."""
        desfecho = sefaz_go.classificar_texto(
            "Nao consta debito inscrito em divida ativa")

        assert desfecho == Desfecho.ERRO_TECNICO
        assert desfecho not in CONCLUSIVOS, "fechado assim, nunca seria retentado"

    def test_bloqueio_do_portal_e_reconhecido(self):
        texto = ("Acesso Negado. A requisicao foi bloqueada pela "
                 "politica de seguranca do site")
        assert sefaz_go.classificar_texto(texto) == Desfecho.BLOQUEIO_TEMPORARIO

    def test_acesso_negado_sozinho_nao_basta(self):
        """Sem o motivo, "acesso negado" pode ser qualquer coisa — tratar
        como bloqueio faria o disjuntor pausar o órgão à toa."""
        assert sefaz_go.classificar_texto("Acesso negado") == Desfecho.ERRO_TECNICO

    def test_documento_recusado_vai_para_conferencia(self):
        assert sefaz_go.classificar_texto("CNPJ invalido") == Desfecho.PENDENCIA_MANUAL

    def test_consta_debito_e_positiva(self):
        """Positiva sai daqui porque positiva não tem documento a entregar."""
        assert sefaz_go.classificar_texto("Consta debito") == Desfecho.POSITIVA


class TestExtracaoDoPdf:
    def test_validade_explicita_ganha(self):
        assert sefaz_go._extrair_validade("VALIDA ATE 30/09/2026") == date(2026, 9, 30)

    def test_sem_validade_explicita_soma_o_prazo_a_emissao(self):
        conteudo = ("LOCAL E DATA: GOIANIA, 27 AGOSTO DE 2026 "
                    "VALIDA POR 30 DIAS")
        assert sefaz_go._extrair_validade(conteudo) == date(2026, 9, 26)

    def test_prazo_sem_data_de_emissao_nao_inventa(self):
        assert sefaz_go._extrair_validade("VALIDA POR 30 DIAS") is None

    def test_data_invalida_nao_quebra(self):
        assert sefaz_go._extrair_validade("VALIDA ATE 31/02/2026") is None

    def test_codigo_de_controle_sai_do_validador(self):
        assert sefaz_go._extrair_codigo("VALIDADOR: 1234.5678.") == "1234.5678"

    def test_sem_codigo_devolve_nada(self):
        assert sefaz_go._extrair_codigo("certidao qualquer") is None


class TestOndeOPdfEGuardado:
    """Positiva não pode ficar na pasta de certidões.

    O ZIP não a levaria — ele é montado da tabela `certidao`, e positiva não
    gera linha —, mas quem abre a pasta para distribuir na mão acharia uma
    positiva no meio das negativas.
    """

    @pytest.fixture
    def adapter(self, tmp_path, monkeypatch):
        cfg = replace(carregar(),
                      pasta_certidoes=tmp_path / "certidoes",
                      pasta_evidencias=tmp_path / "evidencias")
        return sefaz_go.AdapterSEFAZGO(orgao="SEFAZ_GO", cfg=cfg)

    @pytest.fixture
    def doc(self):
        return Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                         nome="EMPRESA TESTE LTDA", lote_id=7)

    def _fingir_leitura(self, monkeypatch, desfecho: Desfecho):
        from cnd.core.modelos import ResultadoTentativa

        def ler(caminho: Path, _texto: str) -> ResultadoTentativa:
            if desfecho in COM_PDF:
                return ResultadoTentativa(desfecho, caminho_pdf=caminho,
                                          validade=date(2026, 9, 30))
            return ResultadoTentativa(desfecho, evidencia=caminho)

        monkeypatch.setattr(sefaz_go, "ler_pdf", ler)

    def test_negativa_vai_para_certidoes(self, adapter, doc, monkeypatch):
        self._fingir_leitura(monkeypatch, Desfecho.NEGATIVA)

        resultado = adapter._salvar_pdf(b"%PDF-1.4 negativa", doc, "ok")

        assert resultado.caminho_pdf is not None
        assert adapter.cfg.pasta_certidoes in resultado.caminho_pdf.parents
        assert resultado.caminho_pdf.exists()
        assert resultado.evidencia is None

    def test_positiva_fica_em_evidencias(self, adapter, doc, monkeypatch):
        self._fingir_leitura(monkeypatch, Desfecho.POSITIVA)

        resultado = adapter._salvar_pdf(b"%PDF-1.4 positiva", doc, "ok")

        assert resultado.caminho_pdf is None, "iria para o pacote do cliente"
        assert resultado.evidencia is not None
        assert adapter.cfg.pasta_evidencias in resultado.evidencia.parents

    def test_nenhum_pdf_positivo_sobra_na_pasta_de_certidoes(
        self, adapter, doc, monkeypatch
    ):
        self._fingir_leitura(monkeypatch, Desfecho.POSITIVA)

        adapter._salvar_pdf(b"%PDF-1.4 positiva", doc, "ok")

        sobrou = list(adapter.cfg.pasta_certidoes.rglob("*.pdf"))
        assert sobrou == [], f"positiva ao lado das entregaveis: {sobrou}"

    def test_pdf_ilegivel_nao_vira_certidao(self, adapter, doc, monkeypatch):
        """`ler_pdf` de verdade, com bytes que não são PDF."""
        resultado = adapter._salvar_pdf(b"nao sou pdf", doc, "ok")

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert resultado.caminho_pdf is None
        assert list(adapter.cfg.pasta_certidoes.rglob("*.pdf")) == []


class TestDeteccaoDePdf:
    def test_exige_content_type_e_assinatura(self):
        assert sefaz_go._e_pdf(_resposta(b"%PDF-1.4 x", "application/pdf"))

    def test_html_com_content_type_de_pdf_nao_passa(self):
        """O portal já devolveu página de erro com o header errado; sem
        conferir a assinatura, ela viraria uma certidão vazia."""
        assert not sefaz_go._e_pdf(_resposta(b"<html>erro</html>", "application/pdf"))

    def test_pdf_sem_content_type_nao_passa(self):
        assert not sefaz_go._e_pdf(_resposta(b"%PDF-1.4 x", "text/html"))


class TestContrato:
    def test_criar_devolve_um_adapter_do_contrato(self):
        from cnd.adapters.base import AdapterOrgao

        cfg = carregar()
        orgao = cfg.orgaos.get("SEFAZ_GO")
        assert orgao is not None, "SEFAZ_GO precisa estar no config.exemplo.toml"

        adapter = sefaz_go.criar(orgao, cfg)

        assert isinstance(adapter, AdapterOrgao)
        assert adapter.orgao == "SEFAZ_GO"

    def test_o_empacotador_leva_o_adapter(self):
        """Adapter é importado por NOME, vindo do config: o PyInstaller não
        enxerga isso lendo o código, e sem a linha no .spec o executável
        sobe e morre ao ligar o órgão."""
        raiz = Path(__file__).resolve().parent.parent
        spec = (raiz / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert '"cnd.adapters.sefaz_go"' in spec
