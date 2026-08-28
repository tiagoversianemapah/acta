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


class TestClassificacaoDoPdf:
    """O título manda; a prosa só decide quando é inequívoca.

    "consta debito" é substring de "nao consta debito". Enquanto a prosa
    decidia, uma certidão POSITIVA que citasse "não consta débito de ICMS,
    porém consta de IPVA" era classificada como NEGATIVA — e negativa VAI no
    pacote do cliente.
    """

    def _classificar(self, texto_pdf: str, tmp_path, monkeypatch) -> Desfecho:
        monkeypatch.setattr(sefaz_go, "_texto_pdf", lambda _c: texto_pdf)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        return sefaz_go.ler_pdf(alvo, "tela").desfecho

    def test_titulo_negativo(self, tmp_path, monkeypatch):
        assert self._classificar(
            "CERTIDAO DE DEBITO INSCRITO EM DIVIDA ATIVA - NEGATIVA",
            tmp_path, monkeypatch) == Desfecho.NEGATIVA

    def test_titulo_positivo(self, tmp_path, monkeypatch):
        assert self._classificar(
            "CERTIDAO DE DEBITO INSCRITO EM DIVIDA ATIVA - POSITIVA",
            tmp_path, monkeypatch) == Desfecho.POSITIVA

    def test_positiva_com_efeito_de_negativa(self, tmp_path, monkeypatch):
        assert self._classificar("CERTIDAO POSITIVA COM EFEITO DE NEGATIVA",
                                 tmp_path, monkeypatch) == Desfecho.CPEN

    def test_positiva_que_cita_nao_consta_debito_nao_vira_negativa(
        self, tmp_path, monkeypatch
    ):
        """O caso que encontrou o defeito. Entregar isto como negativa é o
        erro que ninguém percebe até o cliente perceber."""
        desfecho = self._classificar(
            "CERTIDAO POSITIVA. Nao consta debito de ICMS, "
            "porem CONSTA DEBITO de IPVA",
            tmp_path, monkeypatch)

        assert desfecho == Desfecho.POSITIVA
        assert desfecho not in COM_PDF, "iria para o pacote do cliente"

    def test_prosa_ambigua_sem_titulo_vai_para_conferencia(
        self, tmp_path, monkeypatch
    ):
        """Diz as duas coisas e não tem título: chutar é que seria o erro."""
        desfecho = self._classificar(
            "Nao consta debito de ICMS. Consta debito de IPVA.",
            tmp_path, monkeypatch)

        assert desfecho == Desfecho.ERRO_TECNICO
        assert desfecho not in COM_PDF

    def test_quebra_de_linha_no_meio_da_frase_nao_engana(
        self, tmp_path, monkeypatch
    ):
        """O texto extraído do PDF vem com quebra no meio das frases."""
        assert self._classificar("NAO\nCONSTA DEBITO inscrito",
                                 tmp_path, monkeypatch) == Desfecho.NEGATIVA

    def test_prosa_negativa_sozinha_vale(self, tmp_path, monkeypatch):
        assert self._classificar("Nao consta debito inscrito em divida ativa",
                                 tmp_path, monkeypatch) == Desfecho.NEGATIVA

    def test_prosa_positiva_sozinha_vale(self, tmp_path, monkeypatch):
        assert self._classificar("Consta debito inscrito em divida ativa",
                                 tmp_path, monkeypatch) == Desfecho.POSITIVA

    def test_pdf_irreconhecivel_nao_vira_certidao(self, tmp_path, monkeypatch):
        assert self._classificar("documento qualquer", tmp_path,
                                 monkeypatch) == Desfecho.ERRO_TECNICO


class TestFerramentaDeConferencia:
    """A ferramenta de subida do adapter (ferramentas/conferir_sefaz_go.py).

    Ela existe para mostrar a PROVA por trás do desfecho, e não só o
    desfecho. Se a tabela de marcadores divergir do que o classificador de
    fato usa, ela passa a mentir justamente na hora em que alguém confia
    nela para consertar um marcador.
    """

    @pytest.fixture
    def ferramenta(self):
        import importlib.util
        import sys

        raiz = Path(__file__).resolve().parent.parent
        caminho = raiz / "ferramentas" / "conferir_sefaz_go.py"
        spec = importlib.util.spec_from_file_location("conferir_sefaz_go", caminho)
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["conferir_sefaz_go"] = modulo
        spec.loader.exec_module(modulo)
        return modulo

    def test_a_prova_bate_com_a_decisao_na_positiva_ambigua(self, ferramenta):
        """O caso que encontrou o defeito: as duas prosas acendem, e quem
        decide é o título."""
        texto = ("CERTIDAO DE DEBITO INSCRITO EM DIVIDA ATIVA - POSITIVA\n"
                 "Nao consta debito de ICMS, porem CONSTA DEBITO de IPVA.")

        marcados = dict(ferramenta._marcadores(texto))

        assert marcados["titulo POSITIVA"] is True
        assert marcados["titulo NEGATIVA"] is False
        assert marcados["prosa 'nao consta debito'"] is True
        assert marcados["prosa 'consta debito'"] is True

    def test_a_prosa_negativa_nao_acende_o_marcador_de_debito(self, ferramenta):
        """`consta debito` é substring de `nao consta debito` — se acendesse
        aqui, a tabela sugeriria um débito que não existe."""
        marcados = dict(ferramenta._marcadores("Nao consta debito inscrito"))

        assert marcados["prosa 'nao consta debito'"] is True
        assert marcados["prosa 'consta debito'"] is False

    def test_mostra_a_linha_do_titulo_como_ela_veio(self, ferramenta):
        texto = "GOVERNO DE GOIAS\nCERTIDAO DE DEBITO ... - NEGATIVA\nrodape"
        assert "NEGATIVA" in ferramenta._trecho_do_titulo(texto)

    def test_pdf_sem_texto_diz_isso_em_vez_de_ficar_mudo(self, ferramenta):
        """PDF que é imagem escaneada não tem texto extraível, e o desfecho
        vira "não reconhecido" sem explicar por quê."""
        assert "CERTID" in ferramenta._trecho_do_titulo("pagina sem titulo")

    def test_o_resumo_diz_quais_tipos_faltam(self, ferramenta, capsys):
        """É o que responde "já consegui um de cada?" sem ler linha a linha."""
        from cnd.core.modelos import ResultadoTentativa

        ferramenta._resumir([
            ("11222333000181", ResultadoTentativa(Desfecho.NEGATIVA)),
            ("22333444000195", ResultadoTentativa(Desfecho.ERRO_TECNICO)),
        ])

        saida = capsys.readouterr().out
        assert "ainda sem exemplo de" in saida
        assert "POSITIVA" in saida and "CPEN" in saida

    def test_o_resumo_confirma_quando_os_tres_apareceram(self, ferramenta, capsys):
        from cnd.core.modelos import ResultadoTentativa

        ferramenta._resumir([
            ("11222333000181", ResultadoTentativa(Desfecho.NEGATIVA)),
            ("22333444000195", ResultadoTentativa(Desfecho.POSITIVA)),
            ("33444555000106", ResultadoTentativa(Desfecho.CPEN)),
        ])

        saida = capsys.readouterr().out
        assert "os tres tipos apareceram" in saida
        assert "ainda sem exemplo" not in saida

    def test_espera_entre_consultas_tem_variacao(self, ferramenta, monkeypatch):
        """Espera igual a cada consulta é padrão de robô. E sem espera
        nenhuma, 15 consultas coladas ganham o "Acesso Negado" do portal —
        quem espaça no sistema é o orquestrador, e a ferramenta não passa
        por ele."""
        dormiu = []
        monkeypatch.setattr(ferramenta.time, "sleep", dormiu.append)

        for _ in range(12):
            ferramenta._esperar(10.0, jitter=0.3)

        assert len(set(dormiu)) > 1, "intervalo fixo vira assinatura"
        assert all(7.0 <= s <= 13.0 for s in dormiu), dormiu

    def test_intervalo_zero_nao_dorme(self, ferramenta, monkeypatch):
        dormiu = []
        monkeypatch.setattr(ferramenta.time, "sleep", dormiu.append)
        ferramenta._esperar(0, jitter=0.3)
        assert dormiu == []

    def test_a_segunda_rodada_nao_repete_a_primeira(self, ferramenta, tmp_path,
                                                    monkeypatch):
        """Sem `--pular`, aumentar o `--limite` reconsultava os primeiros —
        consulta gasta a toa num portal que a gente quer nao incomodar."""
        from cnd.ingestao.planilha import Item, Leitura

        itens = [Item(orgao="SEFAZ_GO", tipo_documento="CNPJ",
                      documento=f"{i:014d}", nome=f"E{i}") for i in range(20)]
        monkeypatch.setattr(ferramenta, "ler",
                            lambda *_a, **_k: Leitura(itens=itens), raising=False)
        import cnd.ingestao.planilha as planilha
        monkeypatch.setattr(planilha, "ler", lambda *_a, **_k: Leitura(itens=itens))

        primeira = ferramenta._documentos_da_planilha(tmp_path, "GO", 5, 0)
        segunda = ferramenta._documentos_da_planilha(tmp_path, "GO", 5, 5)

        assert len(primeira) == len(segunda) == 5
        assert not set(primeira) & set(segunda), "reconsultaria os mesmos CNPJs"

    def test_config_ausente_explica_em_vez_de_estourar(self, ferramenta,
                                                       tmp_path, capsys):
        """Traceback de FileNotFoundError nao ajuda ninguem: config.toml e
        da INSTALACAO e num checkout do repositorio ele legitimamente nao
        existe."""
        assert ferramenta._carregar_config(tmp_path / "nao-existe.toml") is None

        saida = capsys.readouterr().out
        assert "Nao achei o config" in saida
        assert "config.exemplo.toml" in saida, "precisa dizer o que fazer"

    def test_config_sem_a_secao_do_orgao_tambem_explica(self, ferramenta,
                                                        tmp_path, capsys):
        magro = tmp_path / "config.toml"
        magro.write_text('[rede]\nnome = "x"\n', encoding="utf-8")

        assert ferramenta._carregar_config(magro) is None
        assert "[orgaos.SEFAZ_GO]" in capsys.readouterr().out

    def test_config_bom_e_aceito(self, ferramenta):
        raiz = Path(__file__).resolve().parent.parent
        cfg = ferramenta._carregar_config(raiz / "config.exemplo.toml")

        assert cfg is not None
        assert "SEFAZ_GO" in cfg.orgaos


# Transcrito de uma certidao REAL emitida em 28/08/2026, conferida contra o
# PDF. Vale mais que qualquer exemplo inventado: foi ela que derrubou a
# suposicao de que existia um titulo "... DIVIDA ATIVA - NEGATIVA". Nao
# existe — o que o documento tem e um bloco DESPACHO.
CERTIDAO_REAL_NEGATIVA = """NOME:                                     CNPJ
EMPRESA DE TESTE LTDA                     11.222.333/0001-81

DESPACHO (Certidao valida para a matriz e suas filiais):

                          NAO CONSTA DEBITO

FUNDAMENTO LEGAL:
Esta certidao e expedida nos termos do Paragrafo 2 do artigo 1, combinado com a
alinea 'b' do inciso II do artigo 2, ambos da IN nr. 405/1999-GSF, de 16 de de
dezembro de 1999, alterada pela IN nr. 828/2006-GSF, de 13 de novembro de 2006 e
constitui documento habil para comprovar a regularidade fiscal perante a Fazenda
Publica Estadual, nos termos do inciso III do art. 68 da Leinr. 14.133, de 2021.

SEGURANCA:
Certidao VALIDA POR 120 DIAS.
A autenticidade pode ser verificada pela INTERNET, no endereco:
https://goias.gov.br/economia/
Fica ressalvado o direito de a Fazenda Publica Estadual inscrever na divida
ativa e COBRAR EVENTUAIS DEBITOS QUE VIEREM A SER APURADOS.

VALIDADOR:  5.555.451.586.243                      EMITIDA VIA INTERNET

SGTI-SEFAZ:   LOCAL E DATA: GOIANIA, 28 AGOSTO DE 2026      HORA: 17:1:40:2
"""


class TestCertidaoReal:
    """Contra o documento de verdade, e nao contra o que eu supunha.

    O rodape desta certidao NEGATIVA fala em "inscrever na divida ativa" e
    em "COBRAR EVENTUAIS DEBITOS" — texto que aparece em toda certidao,
    inclusive nesta. Classificar pelo documento inteiro era conviver com
    isso; classificar pelo DESPACHO resolve na origem.
    """

    def _resultado(self, texto, tmp_path, monkeypatch):
        monkeypatch.setattr(sefaz_go, "_texto_pdf", lambda _c: texto)
        alvo = tmp_path / "real.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        return sefaz_go.ler_pdf(alvo, "conferencia")

    def test_o_despacho_e_isolado_do_rodape(self):
        normalizado = " ".join(
            sefaz_go._sem_acento(CERTIDAO_REAL_NEGATIVA).split())
        achado = sefaz_go.RE_DESPACHO.search(normalizado)

        assert achado is not None, "o bloco DESPACHO precisa ser encontrado"
        assert achado.group(1) == "nao consta debito"
        assert "divida ativa" not in achado.group(1), "rodape entrou no escopo"

    def test_a_certidao_real_e_negativa_e_entregavel(self, tmp_path, monkeypatch):
        r = self._resultado(CERTIDAO_REAL_NEGATIVA, tmp_path, monkeypatch)

        assert r.desfecho == Desfecho.NEGATIVA
        assert r.desfecho in COM_PDF
        assert r.caminho_pdf is not None, "o PDF tem de ser guardado"

    def test_extrai_validade_de_120_dias_e_o_validador(self, tmp_path,
                                                       monkeypatch):
        r = self._resultado(CERTIDAO_REAL_NEGATIVA, tmp_path, monkeypatch)

        assert r.validade == date(2026, 12, 26), "28/08 + 120 dias"
        assert r.codigo_controle == "5.555.451.586.243"

    def test_nao_ha_titulo_de_negativa_neste_documento(self):
        """Registra a suposicao derrubada: o marcador de titulo nao casa
        com o documento real, e quem decide e o DESPACHO."""
        normalizado = " ".join(
            sefaz_go._sem_acento(CERTIDAO_REAL_NEGATIVA).split())

        assert not sefaz_go.RE_TITULO_NEGATIVA.search(normalizado)
        assert not sefaz_go.RE_TITULO_POSITIVA.search(normalizado)
