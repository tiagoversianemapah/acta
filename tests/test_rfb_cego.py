"""Testes do adapter cego.

O que dá para testar sem tela: a leitura de cor (que é como ele "enxerga")
e a calibragem em proporções. Cliques e movimento de mouse não têm como ser
verificados aqui — dependem de tela, janela e de ninguém encostar no mouse.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cnd.adapters import rfb_cego
from cnd.adapters.rfb_cego import (
    AdapterRFBCego,
    Calibragem,
    CalibragemAusente,
    _ponto_fracionario,
    _tem_veu_modal,
)
from cnd.core.modelos import Desfecho, ResultadoTentativa
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
        cfg = SimpleNamespace(pasta_certidoes=tmp_path / "certidoes")
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
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        ponto_vermelho = adapter._pontos_da_faixa_alerta()[2]

        def cor_media(_imagem, x, y, raio=10):
            return VERMELHO_ERRO if (x, y) == ponto_vermelho else BRANCO

        monkeypatch.setattr(rfb_cego.tela, "cor_media", cor_media)

        resultado = adapter._diagnosticar_falha(doc)

        assert resultado.desfecho == Desfecho.BLOQUEIO_TEMPORARIO
