"""Leitura da tela: capturas e amostragem de cor.

É assim que o adapter cego "enxerga". Ele não conversa com o navegador —
só olha os pixels, como uma pessoa olharia. Bem menos informação que ler o
HTML, mas com uma vantagem decisiva: não há nada para o portal detectar.
"""
from __future__ import annotations

from pathlib import Path

from PIL import ImageGrab


def capturar(caminho: Path | None = None):
    """Foto da tela inteira. Se `caminho` vier, salva junto."""
    imagem = ImageGrab.grab(all_screens=False)
    if caminho:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        imagem.save(caminho)
    return imagem


def cor_em(imagem, x: int, y: int) -> tuple[int, int, int]:
    largura, altura = imagem.size
    x = max(0, min(int(x), largura - 1))
    y = max(0, min(int(y), altura - 1))
    pixel = imagem.getpixel((x, y))
    return tuple(pixel[:3])


def cor_media(imagem, x: int, y: int, raio: int = 6) -> tuple[int, int, int]:
    """Média de uma vizinhança — imune a um pixel isolado de borda ou texto."""
    largura, altura = imagem.size
    esquerda = max(0, x - raio)
    topo = max(0, y - raio)
    direita = min(largura, x + raio + 1)
    baixo = min(altura, y + raio + 1)
    recorte = imagem.crop((esquerda, topo, direita, baixo)).convert("RGB")
    pixels = list(recorte.getdata())
    if not pixels:
        return (0, 0, 0)
    n = len(pixels)
    return tuple(sum(canal[i] for canal in pixels) // n for i in range(3))


def brilho(cor: tuple[int, int, int]) -> float:
    """Luminância percebida, 0 (preto) a 255 (branco)."""
    r, g, b = cor
    return 0.299 * r + 0.587 * g + 0.114 * b


def escurecida(cor: tuple[int, int, int], referencia: tuple[int, int, int],
               margem: float = 28.0) -> bool:
    """A região ficou visivelmente mais escura que o normal?

    É assim que o robô percebe uma janela modal aberta: o portal cobre a
    página com um véu escuro por trás dela.
    """
    return brilho(referencia) - brilho(cor) > margem


def parece_alerta(cor: tuple[int, int, int]) -> str | None:
    """Faixa de aviso do portal, pela cor de fundo.

    Amarelo = aviso (código 023), vermelho claro = erro (código 106).
    Sem ler texto nenhum, isso já separa "o portal me barrou" de "algo
    inesperado aconteceu" — que é a distinção que muda o tempo de recuo.
    """
    r, g, b = cor
    if r < 200 or min(r, g, b) < 140:
        return None      # as faixas do portal são sempre claras

    # Amarelo: vermelho E verde altos, azul atrás dos dois.
    if (g - b) > 20 and (r - b) > 35:
        return "aviso"

    # Rosa/vermelho: só o vermelho se destaca; verde e azul andam juntos.
    if (r - g) > 20 and abs(g - b) < 18:
        return "erro"

    return None
