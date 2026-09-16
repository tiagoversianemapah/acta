"""Testes do adapter cego.

O que dá para testar sem tela: a leitura de cor (que é como ele "enxerga")
e a calibragem em proporções. Cliques e movimento de mouse não têm como ser
verificados aqui — dependem de tela, janela e de ninguém encostar no mouse.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from cnd.adapters.federal.rfb import cego as rfb_cego
from cnd.adapters.federal.rfb.cego import (
    AdapterRFBCego,
    Calibragem,
    CalibragemAusente,
    _classificar_texto_portal,
    _ponto_fracionario,
    _tem_veu_modal,
)
from cnd.core.modelos import (
    RETENTAVEIS,
    Desfecho,
    Documento,
    ResultadoTentativa,
)
from cnd.infra import tela

BRANCO = (255, 255, 255)
CINZA_DO_VEU = (128, 128, 128)      # página coberta pela janela modal
AMARELO_AVISO = (255, 243, 205)     # faixa do código 023
VERMELHO_ERRO = (248, 215, 218)     # faixa do código 106
AZUL_BOTAO = (19, 81, 180)

JANELA = (0, 0, 2560, 1600)
ABSOLUTOS = {
    "campo_cnpj": (640, 800),
    "botao_emitir": (2048, 1440),
    "botao_emitir_nova": (1280, 960),
    "fundo_pagina": (256, 800),
    "faixa_alerta": (1280, 320),
}


def _calibragem(janela=JANELA, absolutos=None, cor=BRANCO) -> Calibragem:
    return Calibragem.de_absolutos(janela, absolutos or ABSOLUTOS, cor)


class TestBrilho:
    def test_extremos(self):
        assert tela.brilho((0, 0, 0)) == 0
        assert tela.brilho(BRANCO) == pytest.approx(255, abs=0.5)

    def test_verde_pesa_mais_que_azul(self):
        """Luminância percebida: o olho enxerga verde muito melhor que azul."""
        assert tela.brilho((0, 255, 0)) > tela.brilho((0, 0, 255))


class TestVeuDaJanelaModal:
    def test_veu_escuro_e_detectado(self):
        """É assim que o robô cego sabe que a janelinha abriu."""
        assert tela.escurecida(CINZA_DO_VEU, BRANCO) is True

    def test_veu_e_detectado_mesmo_com_centro_branco(self):
        """O centro pode cair na janela branca do modal; as laterais
        escurecidas precisam denunciar a janelinha mesmo assim."""
        cores = [BRANCO, CINZA_DO_VEU, CINZA_DO_VEU, BRANCO]

        assert _tem_veu_modal(cores, BRANCO) is True

    def test_um_ponto_escuro_so_nao_dispara_modal(self):
        cores = [BRANCO, CINZA_DO_VEU, BRANCO, BRANCO]

        assert _tem_veu_modal(cores, BRANCO) is False

    def test_pagina_normal_nao_dispara(self):
        assert tela.escurecida(BRANCO, BRANCO) is False

    def test_variacao_pequena_nao_dispara(self):
        """Sombra de borda ou antialiasing não pode virar falso positivo."""
        assert tela.escurecida((245, 245, 245), BRANCO) is False


class TestFaixaDeAlerta:
    def test_amarelo_e_aviso(self):
        assert tela.parece_alerta(AMARELO_AVISO) == "aviso"

    def test_vermelho_e_erro(self):
        assert tela.parece_alerta(VERMELHO_ERRO) == "erro"

    def test_pagina_branca_nao_e_alerta(self):
        assert tela.parece_alerta(BRANCO) is None

    def test_azul_do_botao_nao_e_alerta(self):
        assert tela.parece_alerta(AZUL_BOTAO) is None

    def test_cinza_nao_e_alerta(self):
        assert tela.parece_alerta((200, 200, 200)) is None


class TestProporcoes:
    def test_ponto_fracionario_da_janela(self):
        assert _ponto_fracionario(JANELA, 0.25, 0.5) == (640, 800)

    def test_guarda_como_fracao_da_janela(self):
        cal = _calibragem()

        assert cal.pontos["campo_cnpj"] == pytest.approx((0.25, 0.5))
        assert cal.pontos["botao_emitir"] == pytest.approx((0.8, 0.9))

    def test_volta_para_pixel_na_mesma_janela(self):
        cal = _calibragem()

        assert cal.ponto("campo_cnpj", JANELA) == (640, 800)
        assert cal.ponto("botao_emitir", JANELA) == (2048, 1440)

    def test_adapta_a_outra_resolucao(self):
        """O servidor tem outra tela: as medidas precisam acompanhar."""
        cal = _calibragem()
        menor = (0, 0, 1280, 800)

        assert cal.ponto("campo_cnpj", menor) == (320, 400)
        assert cal.ponto("botao_emitir", menor) == (1024, 720)

    def test_acompanha_janela_deslocada(self):
        """Janela que não começa em (0,0) — monitor secundário, por exemplo."""
        cal = _calibragem()
        deslocada = (100, 50, 2560, 1600)

        assert cal.ponto("campo_cnpj", deslocada) == (740, 850)


class TestValidacaoDaCalibragem:
    def test_ida_e_volta_no_disco(self, tmp_path):
        origem = _calibragem()
        caminho = tmp_path / "cal.json"
        origem.salvar(caminho)

        lida = Calibragem.carregar(caminho)

        assert lida.pontos == pytest.approx(origem.pontos)
        assert lida.cor_fundo == origem.cor_fundo
        assert lida.ponto("campo_cnpj", JANELA) == (640, 800)

    def test_arquivo_ausente_explica_o_que_fazer(self, tmp_path):
        with pytest.raises(CalibragemAusente, match="cnd calibrar"):
            Calibragem.carregar(tmp_path / "nao-existe.json")

    def test_ponto_faltando_e_recusado(self):
        incompleta = _calibragem()
        del incompleta.pontos["botao_emitir"]

        with pytest.raises(CalibragemAusente, match="botao_emitir"):
            incompleta.conferir()

    def test_ponto_fora_da_janela_e_recusado(self):
        """Medido em cima de outra janela: clicar ali seria clicar no vazio."""
        fora = dict(ABSOLUTOS, campo_cnpj=(3000, 800))

        with pytest.raises(CalibragemAusente, match="fora da janela"):
            _calibragem(absolutos=fora).conferir()

    def test_resolucao_diferente_e_aceita(self):
        """Só muda a escala: as proporções continuam válidas."""
        _calibragem().conferir((0, 0, 1920, 1200))

    def test_proporcao_muito_diferente_e_recusada(self):
        """Tela ultralarga reorganiza o layout do site; as frações deixam de
        apontar para os mesmos elementos."""
        with pytest.raises(CalibragemAusente, match="proporção"):
            _calibragem().conferir((0, 0, 3440, 1000))

    def test_json_e_legivel_por_humano(self, tmp_path):
        caminho = tmp_path / "cal.json"
        _calibragem().salvar(caminho)

        dados = json.loads(caminho.read_text(encoding="utf-8"))
        assert dados["pontos"]["campo_cnpj"] == [0.25, 0.5]
        assert dados["janela"] == list(JANELA)


class TestJanelaDoEdge:
    def test_abertura_reposiciona_no_retangulo_calibrado(self, monkeypatch, tmp_path):
        cal = _calibragem()
        adapter = AdapterRFBCego("RFB_PJ", object(), tmp_path,
                                 tmp_path / "rfb_cego.json", cal)
        chamadas = []
        popen_args = []

        monkeypatch.setattr(adapter, "encerrar",
                            lambda: chamadas.append("encerrar"))
        monkeypatch.setattr(rfb_cego, "_achar_edge", lambda: "msedge.exe")
        monkeypatch.setattr(rfb_cego.subprocess, "Popen",
                            lambda args: popen_args.append(args))
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "posicionar_janela",
            lambda _titulo, _exe, retangulo: chamadas.append(
                ("posicionar", retangulo)) or True,
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "maximizar",
            lambda _titulo, _exe: chamadas.append("maximizar") or True,
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "retangulo_janela",
            lambda _titulo, _exe: cal.janela,
        )

        adapter._abrir_navegador()

        assert "--new-window" in popen_args[0]
        assert ("posicionar", cal.janela) in chamadas
        assert chamadas.index(("posicionar", cal.janela)) < chamadas.index("maximizar")

    def test_focar_reposiciona_janela_desalinhada(self, monkeypatch, tmp_path):
        cal = _calibragem()
        adapter = AdapterRFBCego("RFB_PJ", object(), tmp_path,
                                 tmp_path / "rfb_cego.json", cal)
        chamadas = []
        janela_errada = (-1374, 515, 1382, 736)

        monkeypatch.setattr(
            rfb_cego.entrada_real, "achar_janela", lambda _titulo, _exe: 1)
        monkeypatch.setattr(
            rfb_cego.entrada_real, "garantir_em_primeiro_plano",
            lambda _titulo, _exe: True,
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real, "retangulo_janela",
            lambda _titulo, _exe: janela_errada,
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "posicionar_janela",
            lambda _titulo, _exe, retangulo: chamadas.append(
                ("posicionar", retangulo)) or True,
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "maximizar",
            lambda _titulo, _exe: chamadas.append("maximizar") or True,
        )
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        adapter._focar()

        assert chamadas == [("posicionar", cal.janela), "maximizar"]

    def test_pdf_do_cego_usa_leitor_sem_playwright(self, monkeypatch, tmp_path):
        baixado = tmp_path / "baixado.pdf"
        baixado.write_bytes(b"%PDF")
        cfg = SimpleNamespace(pasta_certidoes=tmp_path / "certidoes",
                              pasta_evidencias=tmp_path / "evidencias")
        doc = SimpleNamespace(lote_id=1, documento="12345678000199", nome="EMPRESA")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        chamadas = []

        def leitor(caminho, texto):
            chamadas.append((caminho, texto))
            return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho)

        monkeypatch.setattr(rfb_cego, "ler_pdf", leitor)

        resultado = adapter._ler_pdf(baixado, doc)

        assert resultado.desfecho == Desfecho.NEGATIVA
        assert chamadas[0][1] == "PDF baixado (adapter cego)"
        assert chamadas[0][0].exists()
        assert not baixado.exists()


class TestFaixaDeAlertaDoPortal:
    def test_texto_033_vira_retentar(self):
        texto = (
            "Não foi possível emitir a certidão. Tente novamente em alguns "
            "minutos. 033 - 13/08/2026 15:20:10"
        )

        assert _classificar_texto_portal(texto) == "retentar"

    def test_texto_de_informacoes_insuficientes_continua_conclusivo(self):
        texto = (
            "As informações disponíveis na Receita Federal sobre o contribuinte "
            "são insuficientes para emitir a certidão pela internet."
        )

        assert _classificar_texto_portal(texto) == "insuficiente"

    def test_texto_de_cnpj_inapto_e_reconhecido(self):
        """Tela vista em 17/08/2026. Antes de ser reconhecida, caía no
        diagnóstico final como ERRO_TECNICO e gastava três tentativas."""
        texto = (
            "Inscrição no CNPJ 14.610.909/0001-76 Inapta - Omissão de "
            "declarações, emissão de certidão não permitida."
        )

        assert _classificar_texto_portal(texto) == "inapta"

    def test_a_palavra_inapta_sozinha_nao_classifica(self):
        """Exigir as duas partes evita decidir o destino de uma empresa por
        uma palavra que pode aparecer em qualquer outro texto do portal."""
        assert _classificar_texto_portal("situação cadastral inapta") is None

    def test_texto_que_pede_a_matriz_nao_se_confunde_com_bloqueio(self):
        """A faixa que pede o CNPJ da matriz é amarela igual à do código 023.
        Se virasse 'bloqueio', o robô desaceleraria e retentaria três vezes
        uma consulta que só precisava de outro número."""
        texto = (
            "A certidão deve ser emitida para o CNPJ da matriz – "
            "04.401.250/0001-94"
        )

        assert _classificar_texto_portal(texto) == "matriz"

    def test_texto_de_resultado_pendente_vira_retentar(self):
        texto = (
            "Estamos analisando seu pedido de emissÃ£o de certidÃ£o. "
            "Retorne em alguns minutos para o resultado."
        )

        assert _classificar_texto_portal(texto) == "retentar"

    def test_servico_temporariamente_indisponivel_vira_retentar(self):
        texto = (
            "O servico de emissao de certidao esta temporariamente indisponivel. "
            "Tente novamente em alguns minutos. 001 - 13/08/2026 22:23:04"
        )

        assert _classificar_texto_portal(texto) == "retentar"

    def test_leitura_de_texto_limpa_selecao_antes_do_proximo_clique(
        self, monkeypatch, tmp_path
    ):
        adapter = AdapterRFBCego("RFB_PJ", object(), tmp_path, tmp_path / "cal.json")
        chamadas = []

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "limpar_area_transferencia",
            lambda: chamadas.append(("limpar_area_transferencia",)),
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "atalho",
            lambda *codigos: chamadas.append(("atalho", codigos)),
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "tecla",
            lambda codigo: chamadas.append(("tecla", codigo)),
        )
        monkeypatch.setattr(
            rfb_cego.entrada_real,
            "texto_area_transferencia",
            lambda: "texto copiado",
        )
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        texto = adapter._texto_da_pagina()

        assert texto == "texto copiado"
        assert ("tecla", rfb_cego.entrada_real.VK_ESCAPE) in chamadas
        assert ("tecla", rfb_cego.entrada_real.VK_RIGHT) in chamadas

    def test_varredura_detecta_faixa_vermelha_fora_do_ponto_calibrado(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        imagem = Image.new("RGB", (2560, 1600), BRANCO)
        desenho = ImageDraw.Draw(imagem)
        desenho.rectangle((1500, 330, 2300, 500), fill=VERMELHO_ERRO)

        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)

        tipo, _cor = adapter._alerta_na_imagem(imagem)

        assert tipo == "erro"

    def test_diagnostico_varre_faixa_vermelha_mesmo_fora_do_ponto_calibrado(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        ponto_vermelho = adapter._pontos_da_faixa_alerta()[2]

        def cor_media(_imagem, x, y, raio=10):
            return VERMELHO_ERRO if (x, y) == ponto_vermelho else BRANCO

        monkeypatch.setattr(rfb_cego.tela, "cor_media", cor_media)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.BLOQUEIO_TEMPORARIO

    def test_diagnostico_volta_ao_topo_quando_erro_106_rola_a_pagina(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        chamadas = []
        imagens = iter(["pagina_rolada", "topo"])

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: next(imagens))
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)
        monkeypatch.setattr(
            rfb_cego.entrada_real, "atalho",
            lambda *codigos: chamadas.append(codigos),
        )
        ponto_vermelho = adapter._pontos_da_faixa_alerta()[0]

        def cor_media(imagem, x, y, raio=10):
            if imagem == "topo" and (x, y) == ponto_vermelho:
                return VERMELHO_ERRO
            return BRANCO

        monkeypatch.setattr(rfb_cego.tela, "cor_media", cor_media)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.BLOQUEIO_TEMPORARIO
        assert chamadas == [(rfb_cego.entrada_real.VK_CONTROL,
                             rfb_cego.entrada_real.VK_HOME)]

    def test_bloqueio_visto_na_espera_nao_vira_erro_tecnico(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc, bloqueio_ja_visto=True)

        assert resultado.desfecho == Desfecho.BLOQUEIO_TEMPORARIO
        assert "detectada durante a espera" in resultado.mensagem_portal

    def test_texto_033_vira_resultado_pendente(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        adapter._ultimo_texto_portal = (
            "Não foi possível emitir a certidão. Tente novamente em alguns "
            "minutos. 033 - 13/08/2026 15:20:10"
        )

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.RESULTADO_PENDENTE
        assert "033" in resultado.mensagem_portal

    def test_resultado_pendente_volta_para_fila_sem_virar_bloqueio(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        adapter._ultimo_texto_portal = (
            "Estamos analisando seu pedido de emissÃ£o de certidÃ£o. "
            "Retorne em alguns minutos para o resultado."
        )

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.RESULTADO_PENDENTE
        assert "Retorne em alguns minutos" in resultado.mensagem_portal

    def test_servico_indisponivel_volta_para_fila_sem_virar_bloqueio(
        self, monkeypatch, tmp_path
    ):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        adapter._ultimo_texto_portal = (
            "O servico de emissao de certidao esta temporariamente indisponivel. "
            "Tente novamente em alguns minutos. 001 - 13/08/2026 22:23:04"
        )

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.RESULTADO_PENDENTE
        assert "001" in resultado.mensagem_portal

    def test_informacoes_insuficientes_vira_positiva(self, monkeypatch, tmp_path):
        """A tela de 'informações insuficientes' é como o portal recusa quem
        tem débito. Classificá-la como pendência manual mandaria a empresa
        para a aba errada do relatório — e positiva é justamente o resultado
        que impede a entrega ao cliente."""
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        adapter._ultimo_texto_portal = (
            "As informações disponíveis na Receita Federal sobre o "
            "contribuinte 32.874.104/0001-11 são insuficientes para emitir a "
            "certidão pela internet."
        )

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.POSITIVA
        assert "insuficientes" in resultado.mensagem_portal

    def test_cnpj_inapto_conclui_em_vez_de_virar_erro_tecnico(
        self, monkeypatch, tmp_path
    ):
        """A caixa do CNPJ inapto é branca, sem faixa amarela nem vermelha:
        só o texto a denuncia. Como ERRO_TECNICO, o item gastava as três
        tentativas e ainda pedia reenvio no painel — contra uma tela que não
        muda enquanto a empresa não entregar as declarações."""
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="14610909000176")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        adapter._ultimo_texto_portal = (
            "Inscrição no CNPJ 14.610.909/0001-76 Inapta - Omissão de "
            "declarações, emissão de certidão não permitida."
        )

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.INAPTA
        assert resultado.conclusivo
        assert "Inapta" in resultado.mensagem_portal

    def test_sem_pdf_sem_faixa_e_sem_texto_volta_para_a_fila(
        self, monkeypatch, tmp_path
    ):
        """Sem nada legível na tela, o robô não chuta POSITIVA — mas também
        não encerra o item: tela ilegível quase sempre foi sessão ruim, e
        uma tentativa nova costuma resolver (lote de 15/08/2026)."""
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert resultado.desfecho in RETENTAVEIS
        assert "não foi possível ler a tela" in resultado.mensagem_portal


PAGINA_400 = "400 Bad Request\nRequest Header Or Cookie Too Large\nnginx/1.28.3"


class TestCabecalhoDeCookiesGrande:
    """O portal devolve 400 do nginx quando os cookies acumulados passam do
    limite dele. Não é resposta sobre a empresa: é sessão nossa a limpar.

    Em 15/08/2026 isso derrubou uma sequência inteira de itens, todos
    encerrados como "não consegui ler a tela" — sem nenhuma nova tentativa.
    """

    def _adapter(self, tmp_path) -> AdapterRFBCego:
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        return adapter

    def test_print_novo_apaga_o_anterior(self, tmp_path, monkeypatch):
        """Uma foto de tela por documento, e nao uma por tentativa.

        Este e o adapter de maior volume do robo: a cada item que erra doze
        vezes eram doze imagens de tela cheia paradas na pasta para sempre.
        """
        from PIL import Image

        from cnd.adapters.federal.rfb import cego as modulo

        adapter = self._adapter(tmp_path)
        doc = Documento(1, "11222333000181", "CNPJ", "EMPRESA", lote_id=1)
        monkeypatch.setattr(modulo.tela, "capturar",
                            lambda: Image.new("RGB", (4, 4), "white"))

        primeiro = adapter._print(doc, "sem-pdf")
        segundo = adapter._print(doc, "erro")
        pasta = tmp_path / "RFB_PJ" / doc.documento

        assert primeiro is not None and segundo is not None
        assert not primeiro.exists()
        assert segundo.exists()
        assert len(list(pasta.glob("*.png"))) == 1

    def test_limpeza_nao_leva_pdf_da_pasta(self, tmp_path, monkeypatch):
        from PIL import Image

        from cnd.adapters.federal.rfb import cego as modulo

        adapter = self._adapter(tmp_path)
        doc = Documento(1, "11222333000181", "CNPJ", "EMPRESA", lote_id=1)
        monkeypatch.setattr(modulo.tela, "capturar",
                            lambda: Image.new("RGB", (4, 4), "white"))
        adapter._print(doc, "sem-pdf")
        pdf = tmp_path / "RFB_PJ" / doc.documento / "certidao.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        adapter._apagar_prints(doc)

        assert pdf.exists()
        assert list(pdf.parent.glob("*.png")) == []

    def test_pagina_do_nginx_e_reconhecida(self):
        assert _classificar_texto_portal(PAGINA_400) == "cookies"

    def test_texto_do_portal_continua_valendo_mais_que_o_400(self):
        """A classificação de cookies vem primeiro; não pode engolir uma
        resposta de verdade que por acaso cite outro assunto."""
        texto = (
            "As informações disponíveis na Receita Federal sobre o "
            "contribuinte são insuficientes para emitir a certidão pela "
            "internet."
        )

        assert _classificar_texto_portal(texto) == "insuficiente"

    def test_formulario_que_nao_abre_por_400_nao_digita_o_cnpj(
        self, monkeypatch, tmp_path
    ):
        """Digitar na página de erro custava 45s de espera por um PDF que
        nunca viria — e terminava sem saber o que houve."""
        adapter = self._adapter(tmp_path)
        digitou = []

        monkeypatch.setattr(adapter, "_focar", lambda: None)
        monkeypatch.setattr(adapter, "_esperar_formulario", lambda: False)
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: PAGINA_400)
        monkeypatch.setattr(rfb_cego.entrada_real, "ir_para_url",
                            lambda _url: None)
        monkeypatch.setattr(rfb_cego.entrada_real, "digitar",
                            lambda texto, **_kw: digitou.append(texto))

        reacao, caminho = adapter._submeter("12345678000199")

        assert (reacao, caminho) == ("cookies", None)
        assert digitou == []
        assert adapter._cookies_estourados

    def test_emitir_limpa_os_cookies_e_tenta_de_novo(self, monkeypatch, tmp_path):
        adapter = self._adapter(tmp_path)
        doc = Documento(empresa_id=1, documento="04401250000194",
                        tipo="CNPJ", nome="EMPRESA TESTE")
        pdf = tmp_path / "Certidao.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        passos = []

        def submeter(documento):
            passos.append(("submeter", documento))
            return ("cookies", None) if len(passos) == 1 else ("pdf", pdf)

        monkeypatch.setattr(adapter, "_submeter", submeter)
        monkeypatch.setattr(adapter, "_matar_edge",
                            lambda: passos.append(("matar",)) or True)
        monkeypatch.setattr(adapter, "_abrir_navegador",
                            lambda: passos.append(("abrir",)))
        monkeypatch.setattr(rfb_cego.perfil_edge, "limpar_cookies",
                            lambda dominio: passos.append(("limpar", dominio)) or 42)
        monkeypatch.setattr(
            adapter, "_ler_pdf",
            lambda _caminho, _doc: ResultadoTentativa(Desfecho.NEGATIVA,
                                                     caminho_pdf=pdf),
        )

        resultado = adapter.emitir(doc)

        assert resultado.desfecho == Desfecho.NEGATIVA
        # Matar o Edge ANTES de apagar: com ele vivo o banco de cookies nem
        # abre, e a limpeza sai sem efeito nenhum — foi o que aconteceu em
        # produção em 15/08/2026, com "removidos: 0" e o 400 de pé.
        assert passos == [
            ("submeter", "04401250000194"),
            ("matar",),
            ("limpar", rfb_cego.DOMINIO_PORTAL),
            ("abrir",),
            ("submeter", "04401250000194"),
        ]
        assert not adapter._cookies_estourados

    def test_400_que_insiste_vira_erro_tecnico_e_nao_resposta_do_orgao(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)
        doc = Documento(empresa_id=1, documento="04401250000194",
                        tipo="CNPJ", nome="EMPRESA TESTE")

        monkeypatch.setattr(adapter, "_submeter",
                            lambda _documento: ("cookies", None))
        monkeypatch.setattr(adapter, "_recomecar_sem_cookies", lambda: None)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "p.png")

        resultado = adapter.emitir(doc)

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert resultado.desfecho in RETENTAVEIS
        assert "cookies" in resultado.mensagem_portal
        # A próxima sessão precisa nascer limpa, senão repete o mesmo 400.
        assert adapter._cookies_estourados

    def test_reiniciar_sessao_apaga_cookies_quando_o_cabecalho_estourou(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)
        adapter._cookies_estourados = True
        passos = []

        monkeypatch.setattr(adapter, "_matar_edge",
                            lambda: passos.append("matar") or True)
        monkeypatch.setattr(adapter, "_abrir_navegador",
                            lambda: passos.append("abrir"))
        monkeypatch.setattr(rfb_cego.perfil_edge, "limpar_cookies",
                            lambda _dominio: passos.append("limpar") or 7)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        adapter.reiniciar_sessao()

        assert passos == ["matar", "limpar", "abrir"]

    def test_reiniciar_sessao_normal_nao_mexe_nos_cookies(
        self, monkeypatch, tmp_path
    ):
        """Bloqueio 106 ou captcha não são problema de cookie — apagar a cada
        reinício jogaria fora a sessão que faz o robô parecer recorrente."""
        adapter = self._adapter(tmp_path)
        passos = []

        monkeypatch.setattr(adapter, "_matar_edge",
                            lambda: passos.append("matar") or True)
        monkeypatch.setattr(adapter, "_abrir_navegador",
                            lambda: passos.append("abrir"))
        monkeypatch.setattr(rfb_cego.perfil_edge, "limpar_cookies",
                            lambda _dominio: passos.append("limpar") or 0)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        adapter.reiniciar_sessao()

        # Sem "limpar": bloqueio e captcha não são problema de cookie, e
        # apagar a cada reinício jogaria fora a sessão de usuário recorrente.
        assert passos == ["abrir"]

    def test_limpeza_sem_nenhum_cookie_removido_vira_erro_no_log(
        self, monkeypatch, tmp_path
    ):
        """"Não removi nada" e "não havia nada" são problemas opostos, e o
        log de 15/08/2026 só dizia 'removidos: 0' em nível de aviso."""
        adapter = self._adapter(tmp_path)
        erros = []

        monkeypatch.setattr(adapter, "_matar_edge", lambda: True)
        monkeypatch.setattr(rfb_cego.perfil_edge, "limpar_cookies",
                            lambda _dominio: 0)
        monkeypatch.setattr(rfb_cego.perfil_edge, "marcar_saida_limpa",
                            lambda: 0)
        monkeypatch.setattr(rfb_cego.log, "error",
                            lambda evento, **_kw: erros.append(evento))

        adapter._limpar_cookies_do_portal()

        assert erros == ["nenhum_cookie_removido"]

    def test_400_lido_no_diagnostico_tambem_volta_para_a_fila(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)
        doc = SimpleNamespace(documento="12345678000199")
        adapter._ultimo_texto_portal = PAGINA_400

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "p.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_args: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert adapter._cookies_estourados


class TestEncerrarDeVerdade:
    """Fechar a janela não basta em dois pontos, e os dois custaram caro:
    o processo de rede segura o banco de cookies, e enquanto ele vive uma
    "nova" janela do Edge é só mais uma aba da mesma sessão."""

    def _adapter(self, tmp_path) -> AdapterRFBCego:
        adapter = AdapterRFBCego("RFB_PJ", object(), tmp_path,
                                 tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        return adapter

    def test_espera_o_processo_sumir_da_lista(self, monkeypatch, tmp_path):
        adapter = self._adapter(tmp_path)
        vidas = [True, True, False]
        comandos = []

        monkeypatch.setattr(rfb_cego.subprocess, "run",
                            lambda args, **_kw: comandos.append(args))
        monkeypatch.setattr(rfb_cego.entrada_real, "fechar_janelas",
                            lambda _exe: 1)
        monkeypatch.setattr(rfb_cego.perfil_edge, "marcar_saida_limpa",
                            lambda: 1)
        monkeypatch.setattr(rfb_cego, "_edge_rodando", lambda: vidas.pop(0))
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        assert adapter._matar_edge() is True
        assert vidas == []                     # esperou as três checagens
        assert comandos[0][:4] == ["taskkill", "/F", "/T", "/IM"]

    def test_desfaz_a_bolha_de_restaurar_paginas(self, monkeypatch, tmp_path):
        """Matar à força é o motivo de a bolha existir; deixá-la aparecer
        cobriria a tela que o robô cego mede por coordenada."""
        adapter = self._adapter(tmp_path)
        chamadas = []

        monkeypatch.setattr(rfb_cego.subprocess, "run", lambda *_a, **_k: None)
        monkeypatch.setattr(rfb_cego.entrada_real, "fechar_janelas",
                            lambda _exe: 1)
        monkeypatch.setattr(rfb_cego, "_edge_rodando", lambda: False)
        monkeypatch.setattr(rfb_cego.perfil_edge, "marcar_saida_limpa",
                            lambda: chamadas.append("saida_limpa") or 1)

        adapter.encerrar()

        assert chamadas == ["saida_limpa"]

    def test_desiste_de_esperar_e_avisa_quando_nao_morre(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)
        relogio = iter([0.0, 1.0, 99.0])

        monkeypatch.setattr(rfb_cego.subprocess, "run", lambda *_a, **_k: None)
        monkeypatch.setattr(rfb_cego.entrada_real, "fechar_janelas",
                            lambda _exe: 1)
        monkeypatch.setattr(rfb_cego.perfil_edge, "marcar_saida_limpa",
                            lambda: 1)
        monkeypatch.setattr(rfb_cego, "_edge_rodando", lambda: True)
        monkeypatch.setattr(rfb_cego.time, "monotonic", lambda: next(relogio))
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        assert adapter._matar_edge() is False


class TestSessaoPorEmissao:
    """O portal conta as emissões da sessão: a 3ª sempre tomou 023 no log de
    15/08/2026. Reabrir antes é mais barato do que apanhar e reabrir depois."""

    def _adapter(self, tmp_path, limite=1) -> AdapterRFBCego:
        adapter = AdapterRFBCego("RFB_PJ", object(), tmp_path,
                                 tmp_path / "cal.json",
                                 emissoes_por_sessao=limite)
        adapter._calibragem = _calibragem()
        return adapter

    def test_primeira_consulta_da_sessao_nao_reabre_nada(self, monkeypatch,
                                                          tmp_path):
        adapter = self._adapter(tmp_path)
        reinicios = []

        monkeypatch.setattr(adapter, "reiniciar_sessao",
                            lambda: reinicios.append(1))

        adapter._renovar_sessao_se_gasta()

        assert reinicios == []

    def test_reabre_quando_a_sessao_ja_emitiu_o_que_aguenta(self, monkeypatch,
                                                            tmp_path):
        adapter = self._adapter(tmp_path)
        adapter._emissoes_na_sessao = 1
        reinicios = []

        monkeypatch.setattr(adapter, "reiniciar_sessao",
                            lambda: reinicios.append(1))

        adapter._renovar_sessao_se_gasta()

        assert reinicios == [1]

    def test_limite_maior_deixa_a_sessao_trabalhar_mais(self, monkeypatch,
                                                        tmp_path):
        adapter = self._adapter(tmp_path, limite=2)
        adapter._emissoes_na_sessao = 1
        reinicios = []

        monkeypatch.setattr(adapter, "reiniciar_sessao",
                            lambda: reinicios.append(1))

        adapter._renovar_sessao_se_gasta()

        assert reinicios == []

    def test_janela_nova_zera_a_conta(self, monkeypatch, tmp_path):
        adapter = self._adapter(tmp_path)
        adapter._emissoes_na_sessao = 3

        monkeypatch.setattr(adapter, "encerrar", lambda: None)
        monkeypatch.setattr(adapter, "_esperar_janela", lambda: True)
        monkeypatch.setattr(adapter, "_posicionar_janela_calibrada", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(rfb_cego, "_achar_edge", lambda: "msedge.exe")
        monkeypatch.setattr(rfb_cego.subprocess, "Popen", lambda _args: None)
        monkeypatch.setattr(rfb_cego.entrada_real, "maximizar",
                            lambda _t, _e: True)

        adapter._abrir_navegador()

        assert adapter._emissoes_na_sessao == 0

    def test_abertura_espera_a_janela_em_vez_de_dormir(self, monkeypatch,
                                                       tmp_path):
        """Eram 9 segundos fixos por abertura. Com uma abertura por item,
        isso sozinho valia horas no lote."""
        adapter = self._adapter(tmp_path)
        caixas = [None, None, (0, 0, 300, 200), JANELA]

        monkeypatch.setattr(rfb_cego.entrada_real, "retangulo_janela",
                            lambda _t, _e: caixas.pop(0))
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        assert adapter._esperar_janela() is True
        # A janelinha auxiliar de 300px não conta como "a janela".
        assert caixas == []


BANNER_MATRIZ = (
    "A certidão deve ser emitida para o CNPJ da matriz – 04.401.250/0001-94"
)


class TestPortalPedeAMatriz:
    """O portal recusa CNPJ de filial e diz na faixa qual é a matriz.

    O robô já troca filial por matriz antes de digitar, derivando o sufixo
    0001. Isto aqui é a rede de segurança para quando o portal discorda da
    derivação: quem manda é ele, e o número certo está escrito na tela.
    """

    def _adapter(self, tmp_path) -> AdapterRFBCego:
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = _calibragem()
        return adapter

    def test_faixa_amarela_com_pedido_de_matriz_nao_vira_bloqueio(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)

        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_pdf_pronto", lambda _doc: None)
        monkeypatch.setattr(adapter, "_alerta_na_imagem",
                            lambda _imagem: ("aviso", AMARELO_AVISO))
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: BANNER_MATRIZ)
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_args, **_kw: BRANCO)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _segundos: None)

        reacao, caminho = adapter._aguardar_reacao("04401250000860", segundos=1)

        assert reacao == "matriz"
        assert caminho is None
        assert adapter._ultimo_texto_portal == BANNER_MATRIZ

    def test_emitir_refaz_a_consulta_com_o_cnpj_que_o_portal_indicou(
        self, monkeypatch, tmp_path
    ):
        adapter = self._adapter(tmp_path)
        doc = Documento(empresa_id=1, documento="00082253000232",
                        tipo="CNPJ", nome="EMPRESA TESTE")
        pdf = tmp_path / "Certidao.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        enviados = []

        def submeter(documento):
            enviados.append(documento)
            if len(enviados) == 1:
                adapter._ultimo_texto_portal = BANNER_MATRIZ
                return "matriz", None
            return "pdf", pdf

        monkeypatch.setattr(adapter, "_submeter", submeter)
        monkeypatch.setattr(
            adapter, "_ler_pdf",
            lambda _caminho, _doc: ResultadoTentativa(Desfecho.NEGATIVA,
                                                      caminho_pdf=pdf),
        )

        resultado = adapter.emitir(doc)

        # Primeiro vai a matriz derivada (0001 da própria base); depois, a
        # que o portal escreveu na faixa.
        assert enviados == ["00082253000151", "04401250000194"]
        assert resultado.desfecho == Desfecho.NEGATIVA
        assert "04.401.250/0001-94" in resultado.mensagem_portal

    def test_emitir_nao_insiste_quando_o_portal_repete_o_pedido(
        self, monkeypatch, tmp_path
    ):
        """Se o portal pedir de novo o mesmo número que acabamos de digitar,
        repetir só gastaria tentativa. Vai para conferência manual."""
        adapter = self._adapter(tmp_path)
        doc = Documento(empresa_id=1, documento="04401250000860",
                        tipo="CNPJ", nome="EMPRESA TESTE")
        enviados = []

        def submeter(documento):
            enviados.append(documento)
            adapter._ultimo_texto_portal = BANNER_MATRIZ
            return "matriz", None

        monkeypatch.setattr(adapter, "_submeter", submeter)
        monkeypatch.setattr(adapter, "_print", lambda *_args: tmp_path / "p.png")

        resultado = adapter.emitir(doc)

        assert enviados == ["04401250000194"]
        assert resultado.desfecho == Desfecho.PENDENCIA_MANUAL
        assert "matriz" in resultado.mensagem_portal


class TestCodigoDoPortalNaoSeConfundeComCNPJ:
    """O código de erro vem carimbado com a data ("033 - 17/08/2026").
    Sem exigir esse formato, os três dígitos do CNPJ viravam código."""

    def test_cnpj_com_033_nao_vira_resultado_pendente(self):
        """Caso real: ANGONESE, 17.406.033/0001-39, em 17/08/2026. O portal
        respondeu 'insuficientes' (POSITIVA) e o item foi classificado como
        pendente por causa do próprio número — e, como o robô não desiste,
        entraria em laço eterno."""
        texto = (
            "Resultado da Emissao de Certidao cnpj 17.406.033/0001-39 "
            "As informacoes disponiveis na Receita Federal e na "
            "Procuradoria-Geral da Fazenda Nacional sobre o contribuinte "
            "17.406.033/0001-39 sao insuficientes para emitir a certidao "
            "pela internet."
        )

        assert _classificar_texto_portal(texto) == "insuficiente"

    def test_codigo_033_de_verdade_continua_valendo(self):
        texto = ("Nao foi possivel emitir a certidao. Tente novamente em "
                 "alguns minutos. 033 - 17/08/2026 15:18:08")

        assert _classificar_texto_portal(texto) == "retentar"

    def test_cnpj_com_033_e_codigo_033_juntos(self):
        """O CNPJ não pode anular o código verdadeiro nem o contrário."""
        texto = ("cnpj 17.406.033/0001-39 Nao foi possivel emitir a certidao. "
                 "Tente novamente em alguns minutos. 033 - 17/08/2026 15:18:08")

        assert _classificar_texto_portal(texto) == "retentar"
