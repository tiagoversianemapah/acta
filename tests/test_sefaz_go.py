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

    def test_a_conferencia_nao_escreve_na_pasta_de_entrega(self, ferramenta,
                                                           monkeypatch):
        """O adapter move a negativa para `pasta_certidoes`, e faz certo —
        é de lá que o pacote do cliente é montado. Mas aqui não há lote,
        nem banco, nem entrega: o arquivo ficava em `data/certidoes/0/`
        chamado CONFERENCIA, no meio das certidões de verdade.

        A conferência é no CAMINHO REAL, e não na função isolada: testar só
        o ajudante deixava passar o defeito de verdade, que era não chamá-lo
        — apagar a chamada mantinha o teste verde.
        """
        from cnd.core.modelos import Desfecho, ResultadoTentativa

        cfg = carregar()
        recebido = {}

        class AdapterFalso:
            orgao = "SEFAZ_GO"

            def preparar(self): pass
            def encerrar(self): pass
            def emitir(self, _doc):
                return ResultadoTentativa(Desfecho.ERRO_TECNICO)

        def criar(_orgao, cfg_recebida):
            recebido["cfg"] = cfg_recebida
            return AdapterFalso()

        monkeypatch.setattr(ferramenta.sefaz_go, "criar", criar)
        ferramenta.conferir_cnpj("11222333000181", cfg)

        usada = recebido["cfg"]
        assert usada.pasta_certidoes != cfg.pasta_certidoes, (
            "o adapter recebeu a pasta de ENTREGA")
        assert "conferencia" in str(usada.pasta_certidoes)
        assert cfg.pasta_certidoes not in usada.pasta_certidoes.parents

    def test_o_intervalo_e_da_ferramenta_e_nao_do_config(self, ferramenta,
                                                        monkeypatch):
        """A ferramenta tem intervalo PRÓPRIO. Hoje ele calha de ser igual
        ao do órgão (3s nos dois), mas são decisões separadas: mexer no
        ritmo do robô não pode mudar a conferência por tabela, nem o
        contrário.
        """
        import sys

        recebido = {}
        monkeypatch.setattr(ferramenta, "_rodar",
                            lambda _docs, _cfg, intervalo: (
                                recebido.update(intervalo=intervalo) or ([], False)))
        monkeypatch.setattr(sys, "argv",
                            ["conferir", "--config",
                             str(Path(__file__).resolve().parent.parent
                                 / "config.exemplo.toml"),
                             "11222333000181"])
        ferramenta.main()

        assert recebido["intervalo"] == ferramenta.INTERVALO_PADRAO_S
        assert ferramenta.INTERVALO_PADRAO_S > 0, "sem espaçamento nenhum, não"

    def test_intervalo_da_linha_de_comando_ganha(self, ferramenta, monkeypatch):
        import sys

        recebido = {}
        monkeypatch.setattr(ferramenta, "_rodar",
                            lambda _docs, _cfg, intervalo: (
                                recebido.update(intervalo=intervalo) or ([], False)))
        monkeypatch.setattr(sys, "argv",
                            ["conferir", "--config",
                             str(Path(__file__).resolve().parent.parent
                                 / "config.exemplo.toml"),
                             "--intervalo", "0", "11222333000181"])
        ferramenta.main()

        assert recebido["intervalo"] == 0.0

    def test_a_dica_de_continuar_soma_o_pular_ja_usado(self, ferramenta, capsys):
        """Quem rodou com --pular 12 recebia a sugestão de pular 12 de novo,
        e reconsultaria exatamente os mesmos CNPJs."""
        from cnd.core.modelos import ResultadoTentativa

        vistos = [(f"{i:014d}", ResultadoTentativa(Desfecho.NEGATIVA))
                  for i in range(5)]
        ferramenta._resumir(vistos, ja_pulados=12)

        assert "--pular 17" in capsys.readouterr().out, "12 ja pulados + 5 vistos"

    def test_sem_pular_anterior_a_dica_continua_certa(self, ferramenta, capsys):
        from cnd.core.modelos import ResultadoTentativa

        vistos = [(f"{i:014d}", ResultadoTentativa(Desfecho.NEGATIVA))
                  for i in range(3)]
        ferramenta._resumir(vistos, ja_pulados=0)

        assert "--pular 3" in capsys.readouterr().out

    def _rodar(self, ferramenta, monkeypatch, desfechos, quantos):
        """Roda `_rodar` contra um adapter falso, sem tocar na rede."""
        from cnd.core.modelos import ResultadoTentativa

        chamadas = {"n": 0}

        class Falso:
            orgao = "SEFAZ_GO"

            def preparar(self): pass
            def encerrar(self): pass
            def emitir(self, _doc):
                chamadas["n"] += 1
                return ResultadoTentativa(
                    desfechos.get(chamadas["n"], Desfecho.NEGATIVA))

        monkeypatch.setattr(ferramenta.sefaz_go, "criar", lambda *_a: Falso())
        monkeypatch.setattr(ferramenta, "_esperar", lambda *_a: None)
        documentos = [f"{i:014d}" for i in range(1, quantos + 1)]
        vistos, parou = ferramenta._rodar(documentos, carregar(), 0.0)
        return vistos, parou, chamadas["n"]

    def test_para_quando_o_portal_recusa(self, ferramenta, monkeypatch, capsys):
        """Insistir depois de uma recusa é como se ganha uma recusa maior.
        Numa rodada de centenas, seguir em frente seria o pior caminho."""
        vistos, parou, chamadas = self._rodar(
            ferramenta, monkeypatch,
            {5: Desfecho.BLOQUEIO_TEMPORARIO}, quantos=300)

        assert parou is True
        assert chamadas == 5, f"insistiu depois da recusa ({chamadas} consultas)"
        assert len(vistos) == 5
        assert "PORTAL RECUSOU" in capsys.readouterr().out

    def test_sem_recusa_vai_ate_o_fim(self, ferramenta, monkeypatch):
        vistos, parou, chamadas = self._rodar(ferramenta, monkeypatch, {}, 12)

        assert parou is False
        assert chamadas == 12 and len(vistos) == 12

    def test_rodada_longa_detalha_so_o_que_ensina(self, ferramenta, monkeypatch,
                                                  capsys):
        """Uma parede de blocos esconde justamente a positiva que se procura."""
        self._rodar(ferramenta, monkeypatch,
                    {4: Desfecho.POSITIVA}, quantos=20)

        saida = capsys.readouterr().out
        assert saida.count("marcadores:") == 0 or "primeiro" in saida
        assert "primeiro POSITIVA" in saida, "a positiva tem de aparecer inteira"
        assert saida.count("primeiro NEGATIVA") == 1, "so a primeira negativa"

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


class TestDocumentoDeOutroTipo:
    """O formulário aceita CPF e CNPJ; este adapter só monta o de CNPJ.

    Mandar um CPF nos campos de CNPJ não daria erro — o portal consultaria
    OUTRO documento e devolveria a certidão de alguém. Falhar é melhor.
    """

    def _adapter(self, tmp_path):
        cfg = replace(carregar(), pasta_certidoes=tmp_path / "c",
                      pasta_evidencias=tmp_path / "e")
        return sefaz_go.AdapterSEFAZGO(orgao="SEFAZ_GO", cfg=cfg)

    def test_cpf_nao_vira_consulta_de_cnpj(self, tmp_path, monkeypatch):
        def nao_deveria(*_a, **_k):
            raise AssertionError("chegou a consultar o portal com um CPF")

        adapter = self._adapter(tmp_path)
        monkeypatch.setattr(adapter, "_consultar", nao_deveria)
        doc = Documento(empresa_id=1, documento="21360146172", tipo="CPF",
                        nome="FULANO", lote_id=1)

        resultado = adapter.emitir(doc)

        assert resultado.desfecho == Desfecho.PENDENCIA_MANUAL
        assert "CNPJ" in resultado.mensagem_portal

    def test_cnpj_segue_normalmente(self, tmp_path, monkeypatch):
        from cnd.core.modelos import ResultadoTentativa

        adapter = self._adapter(tmp_path)
        monkeypatch.setattr(
            adapter, "_consultar",
            lambda _doc: ResultadoTentativa(Desfecho.NEGATIVA))
        doc = Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                        nome="EMPRESA", lote_id=1)

        assert adapter.emitir(doc).desfecho == Desfecho.NEGATIVA

    def test_sessao_nao_preparada_explica(self, tmp_path):
        """`assert` some com `python -O`, e o erro viraria um AttributeError
        sem explicação lá dentro do urllib."""
        import urllib.request

        adapter = self._adapter(tmp_path)
        with pytest.raises(RuntimeError, match="preparar"):
            adapter._abrir(urllib.request.Request("http://x.invalid"))


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


class TestCamposDoFormulario:
    """Conferidos contra o formulário real do portal em 28/08/2026.

    O POST é montado à mão, então nenhum `checked` do HTML é herdado: campo
    que não vai explícito não chega ao servidor.
    """

    def _dados(self):
        cfg = carregar()
        adapter = sefaz_go.criar(cfg.orgaos["SEFAZ_GO"], cfg)
        doc = Documento(empresa_id=1, documento=CNPJ, tipo="CNPJ",
                        nome="X", lote_id=1)
        return adapter._dados(doc)

    def test_pede_o_pdf_explicitamente(self):
        """`Certidao.Render` decide o FORMATO: pdf, html ou xml. Sem ele, a
        resposta podia voltar como página — e o adapter só entrega o que vem
        como PDF de verdade, então a certidão simplesmente não sairia."""
        assert self._dados()["Certidao.Render"] == "pdf"

    def test_pede_emissao_e_nao_validacao(self):
        assert self._dados()["Certidao.ValidarEmissao_Emitir"] == "0"

    def test_manda_todos_os_campos_do_formulario(self):
        """A lista veio do HTML do portal. Campo novo que apareça lá e não
        aqui é exatamente o tipo de coisa que falha em silêncio."""
        esperados = {
            "Certidao.Tipo",
            "Certidao.TipoDocumento",
            "Certidao.NumeroDocumento",
            "Certidao.NumeroDocumentoCNPJ",
            "Certidao.Espolio",
            "Certidao.Render",
            "Certidao.ValidarEmissao_Emitir",
        }
        assert set(self._dados()) == esperados

    def test_consulta_como_cnpj_e_nao_como_cpf(self):
        """No formulário, CPF nasce marcado (`value="1"`). Mandar o CNPJ sem
        trocar o tipo consultaria o documento errado."""
        dados = self._dados()
        assert dados["Certidao.TipoDocumento"] == "2"
        assert dados["Certidao.NumeroDocumentoCNPJ"] == CNPJ


class TestAmostraDaPlanilha:
    def _planilha(self, tmp_path, quantas):
        from openpyxl import Workbook

        livro = Workbook()
        aba = livro.active
        aba.title = "GO"
        aba.append(["Empresa", "CNPJ"])
        for i in range(quantas):
            aba.append([f"EMPRESA {i}", f"11.222.333/{i:04d}-81"])
        caminho = tmp_path / "carteira.xlsx"
        livro.save(caminho)
        return caminho

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

    def test_sem_limite_traz_a_aba_inteira(self, ferramenta, tmp_path,
                                           monkeypatch):
        """`--todos` passa limite=None. Antes o jeito de pedir tudo era
        chutar um número grande, e chute errado corta a lista em silêncio."""
        from cnd.ingestao.planilha import Item, Leitura

        itens = [Item(orgao="SEFAZ_GO", tipo_documento="CNPJ",
                      documento=f"{i:014d}", nome=f"E{i}") for i in range(50)]
        import cnd.ingestao.planilha as planilha
        monkeypatch.setattr(planilha, "ler", lambda *_a, **_k: Leitura(itens=itens))

        todos = ferramenta._documentos_da_planilha(tmp_path, "GO", None, 0)
        limitado = ferramenta._documentos_da_planilha(tmp_path, "GO", 10, 0)

        assert len(todos) == 50
        assert len(limitado) == 10

    def _limite_que_main_usa(self, ferramenta, monkeypatch, argv):
        """Roda `main` de verdade e captura o limite que ele repassa.

        Sem rede: o falso devolve lista vazia, e `main` sai pelo caminho de
        "nenhum documento". Testar `_documentos_da_planilha` direto nao
        provava nada sobre a LIGACAO com --todos, que e o que pode quebrar.
        """
        import sys

        visto = {}

        def falso(caminho, aba, limite, pular=0):
            visto["limite"] = limite
            visto["pular"] = pular
            return []

        monkeypatch.setattr(ferramenta, "_documentos_da_planilha", falso)
        monkeypatch.setattr(sys, "argv", ["conferir", *argv])
        ferramenta.main()
        return visto

    def test_todos_desliga_o_limite_de_verdade(self, ferramenta, monkeypatch,
                                               tmp_path):
        raiz = Path(__file__).resolve().parent.parent
        planilha = tmp_path / "c.xlsx"
        planilha.write_bytes(b"x")

        visto = self._limite_que_main_usa(ferramenta, monkeypatch, [
            "--config", str(raiz / "config.exemplo.toml"),
            "--planilha", str(planilha), "--todos"])

        assert visto["limite"] is None, "--todos precisa desligar o corte"

    def test_sem_todos_o_limite_vale(self, ferramenta, monkeypatch, tmp_path):
        raiz = Path(__file__).resolve().parent.parent
        planilha = tmp_path / "c.xlsx"
        planilha.write_bytes(b"x")

        visto = self._limite_que_main_usa(ferramenta, monkeypatch, [
            "--config", str(raiz / "config.exemplo.toml"),
            "--planilha", str(planilha), "--limite", "7"])

        assert visto["limite"] == 7

    def test_sem_limite_respeita_o_pular(self, ferramenta, tmp_path, monkeypatch):
        from cnd.ingestao.planilha import Item, Leitura

        itens = [Item(orgao="SEFAZ_GO", tipo_documento="CNPJ",
                      documento=f"{i:014d}", nome=f"E{i}") for i in range(50)]
        import cnd.ingestao.planilha as planilha
        monkeypatch.setattr(planilha, "ler", lambda *_a, **_k: Leitura(itens=itens))

        resto = ferramenta._documentos_da_planilha(tmp_path, "GO", None, 16)

        assert len(resto) == 34
        assert resto[0] == f"{16:014d}", "tem de continuar do 17o"


# Transcritas de certidoes REAIS de 28/08/2026, com nome e CNPJ trocados.
# Foram elas que mostraram que o portal nao escreve o que eu supunha.
CERTIDAO_REAL_POSITIVA = """ESTADO DE GOIAS
SECRETARIA DE ESTADO DA ECONOMIA
CERTIDAO DE DEBITO EM DIVIDA ATIVA - POSITIVA
NR. CERTIDAO: N 74798818
NOME: CNPJ
EMPRESA DE TESTE LTDA                     11.222.333/0001-81
DESPACHO (Certidao valida para a matriz e suas filiais):
POSSUI DEBITO INSCRITO NA DIVIDA ATIVA, RELATIVO A
30 PROCESSO(S).
PROCESSOS:
2013359911156 2007530811160 4012300917703
FUNDAMENTO LEGAL:
Esta certidao e expedida nos termos da alinea 'a' do inciso II do artigo 2.
SEGURANCA:
Certidao VALIDA POR 120 DIAS.
Fica ressalvado o direito de a Fazenda Publica Estadual inscrever na divida
ativa e COBRAR EVENTUAIS DEBITOS QUE VIEREM A SER APURADOS.
VALIDADOR: 5.555.581.482.162                    EMITIDA VIA INTERNET
SGTI-SEFAZ:  LOCAL E DATA: GOIANIA, 28 AGOSTO DE 2026     HORA: 18:43:5:8
"""

CERTIDAO_REAL_CPEN = """ESTADO DE GOIAS
SECRETARIA DE ESTADO DA ECONOMIA
CERTIDAO DE DEBITO EM DIVIDA ATIVA - POSITIVA
COM EFEITO NEGATIVO(PARCELAMENTO)
NR. CERTIDAO: N 74798740
NOME: CNPJ
EMPRESA DE TESTE LTDA                     11.222.333/0001-81
DESPACHO (Certidao valida para a matriz e suas filiais):
POR FORCA DO PARAG. UNICO, ART.195, LEI 11651/91, DE
26 DE DEZEMBRO DE 1991, ESTA CERTIDAO NAO DA DIREITO
A ALIENACAO DE QUALQUER BEM PATRIMONIAL DO SUJEITO
PASSIVO, ESPECIALMENTE BEM IMOVEL.
PROCESSOS:
2564428922235
FUNDAMENTO LEGAL:
Esta certidao e expedida nos termos do inciso IV do artigo 3.
SEGURANCA:
Certidao VALIDA POR 120 DIAS.
VALIDADOR: 5.555.386.714.165                    EMITIDA VIA INTERNET
SGTI-SEFAZ:  LOCAL E DATA: GOIANIA, 28 AGOSTO DE 2026     HORA: 18:40:25:9
"""


class TestPositivaECpenReais:
    """Os dois documentos que o portal so mostrou depois de 299 consultas.

    Eles derrubaram duas suposicoes: a positiva nao diz "consta debito", diz
    "POSSUI DEBITO INSCRITO"; e a CPEN nao diz "positiva com efeito de
    negativa", diz "- POSITIVA" numa linha e "COM EFEITO NEGATIVO
    (PARCELAMENTO)" na seguinte.
    """

    def _classificar(self, texto, tmp_path, monkeypatch) -> Desfecho:
        monkeypatch.setattr(sefaz_go, "_texto_pdf", lambda _c: texto)
        alvo = tmp_path / "c.pdf"
        alvo.write_bytes(b"%PDF-1.4")
        return sefaz_go.ler_pdf(alvo, "conferencia").desfecho

    def test_a_positiva_real(self, tmp_path, monkeypatch):
        assert self._classificar(CERTIDAO_REAL_POSITIVA, tmp_path,
                                 monkeypatch) == Desfecho.POSITIVA

    def test_a_cpen_real(self, tmp_path, monkeypatch):
        assert self._classificar(CERTIDAO_REAL_CPEN, tmp_path,
                                 monkeypatch) == Desfecho.CPEN

    def test_a_cpen_nao_depende_da_quebra_de_linha(self, tmp_path, monkeypatch):
        """O marcador antigo era "positiva com efeito", e ele só casava
        porque o colapso de espaços juntava "POSITIVA" de uma linha com "COM
        EFEITO NEGATIVO" da seguinte. Quebrar em outro ponto transformava a
        CPEN em POSITIVA — e CPEN VALE como regularidade, então o cliente
        perderia uma certidão a que tem direito."""
        embaralhado = CERTIDAO_REAL_CPEN.replace(
            "- POSITIVA\nCOM EFEITO NEGATIVO(PARCELAMENTO)",
            "- POSITIVA\nNR. CERTIDAO: N 1\nCOM EFEITO NEGATIVO(PARCELAMENTO)")

        assert self._classificar(embaralhado, tmp_path,
                                 monkeypatch) == Desfecho.CPEN

    def test_a_cpen_e_entregavel_e_a_positiva_nao(self, tmp_path, monkeypatch):
        cpen = self._classificar(CERTIDAO_REAL_CPEN, tmp_path, monkeypatch)
        positiva = self._classificar(CERTIDAO_REAL_POSITIVA, tmp_path,
                                     monkeypatch)

        assert cpen in COM_PDF, "CPEN vale como regularidade"
        assert positiva not in COM_PDF, "positiva nao vai para o cliente"

    def test_o_titulo_da_positiva_nao_tem_inscrito(self):
        """A negativa diz "DEBITO INSCRITO EM DIVIDA ATIVA"; a positiva diz
        "DEBITO EM DIVIDA ATIVA". Marcador que exigisse o "inscrito" nas duas
        deixaria a positiva sem titulo reconhecido."""
        for texto in (CERTIDAO_REAL_POSITIVA, CERTIDAO_REAL_CPEN):
            normalizado = " ".join(sefaz_go._sem_acento(texto).split())
            assert sefaz_go.RE_TITULO_POSITIVA.search(normalizado)
            assert not sefaz_go.RE_TITULO_NEGATIVA.search(normalizado)
