"""A janela nasce proporcional ao monitor, não em tamanho fixo.

Era 1360x820 sempre. Num monitor grande nascia pequena no canto; num
notebook de 1366x768 nascia MAIOR que a área útil, com a barra de tarefas
escondendo os botões — e o mínimo de 1100x660 impedia encolher para caber.
"""
from __future__ import annotations

from cnd.desktop.app import MAXIMO_A, MAXIMO_L, MINIMO_L


def _medir(janela, monkeypatch, largura_tela, altura_tela):
    monkeypatch.setattr(janela, "winfo_screenwidth", lambda: largura_tela)
    monkeypatch.setattr(janela, "winfo_screenheight", lambda: altura_tela)
    visto = {}
    monkeypatch.setattr(janela, "geometry", lambda g: visto.update(g=g))
    monkeypatch.setattr(janela, "minsize",
                        lambda larg, alt: visto.update(minimo=(larg, alt)))
    janela._dimensionar_pela_tela()
    tamanho, _, posicao = visto["g"].partition("+")
    largura, altura = (int(n) for n in tamanho.split("x"))
    x, y = (int(n) for n in posicao.split("+"))
    return largura, altura, x, y, visto["minimo"]


def test_monitor_grande_nao_estica_sem_limite(janela, monkeypatch):
    """Linha larga demais cansa de ler — passar do máximo só afasta as
    colunas uma da outra."""
    largura, altura, _, _, _ = _medir(janela, monkeypatch, 3840, 2160)

    assert largura == MAXIMO_L
    assert altura == MAXIMO_A


def test_notebook_pequeno_cabe_na_area_util(janela, monkeypatch):
    """1366x768: a janela tem de caber, inclusive descontando a barra."""
    largura, altura, x, y, _ = _medir(janela, monkeypatch, 1366, 768)

    assert largura <= 1366
    assert altura <= int(768 * 0.92), "a barra de tarefas comeria os botões"
    assert x >= 0 and y >= 0


def test_em_tela_pequena_o_minimo_cede(janela, monkeypatch):
    """Melhor uma janela apertada que uma que não cabe e não deixa
    encolher."""
    _, altura, _, _, minimo = _medir(janela, monkeypatch, 1280, 720)

    assert minimo[0] <= MINIMO_L
    assert minimo[1] <= altura, "o mínimo não pode ser maior que a janela"


def test_fica_centralizada(janela, monkeypatch):
    largura, _, x, _, _ = _medir(janela, monkeypatch, 1920, 1080)

    assert abs(x - (1920 - largura) // 2) <= 1


def test_monitor_comum_usa_o_espaco_sem_encostar_nas_bordas(janela,
                                                            monkeypatch):
    largura, _, _, _, _ = _medir(janela, monkeypatch, 1920, 1080)

    assert MINIMO_L <= largura < 1920
