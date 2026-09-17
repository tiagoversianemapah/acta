"""A foto da tela com mais de um monitor.

Com o Edge num monitor à esquerda do principal, as coordenadas são negativas.
A foto era só do principal, e todo ponto dali saía (0, 0, 0): a calibragem
recusava a "página escura" (17/09/2026). A foto passou a cobrir a área de
trabalho inteira, e a leitura desconta onde ela começa.
"""
from __future__ import annotations

from PIL import Image

from cnd.infra import tela

BRANCO = (248, 248, 248)
AZUL = (19, 81, 180)


def _duas_telas():
    """Monitor de 10px à esquerda (x de -10 a -1) e principal de 20px."""
    imagem = Image.new("RGB", (30, 10), (0, 0, 0))
    imagem.paste(BRANCO, (0, 0, 10, 10))      # monitor da esquerda
    imagem.paste(AZUL, (10, 0, 30, 10))       # principal, começa no x=0
    imagem.info["origem"] = (-10, 0)
    return imagem


def test_ponto_no_monitor_da_esquerda_le_a_cor_de_verdade():
    imagem = _duas_telas()

    assert tela.cor_em(imagem, -5, 5) == BRANCO
    assert tela.cor_media(imagem, -5, 5, raio=2) == BRANCO


def test_ponto_no_principal_continua_no_mesmo_lugar():
    imagem = _duas_telas()

    assert tela.cor_em(imagem, 5, 5) == AZUL
    assert tela.cor_media(imagem, 5, 5, raio=2) == AZUL


def test_foto_sem_origem_e_um_monitor_so():
    """Imagem de fora (teste, arquivo) não traz origem: vale o (0, 0)."""
    imagem = Image.new("RGB", (20, 10), AZUL)

    assert tela.cor_media(imagem, 5, 5, raio=2) == AZUL
