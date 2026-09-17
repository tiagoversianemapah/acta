"""Leitura da tela: capturas e amostragem de cor.

É assim que o adapter cego "enxerga". Ele não conversa com o navegador —
só olha os pixels, como uma pessoa olharia. Bem menos informação que ler o
HTML, mas com uma vantagem decisiva: não há nada para o portal detectar.
"""
from __future__ import annotations

from pathlib import Path

from PIL import ImageGrab, ImageStat


def capturar(caminho: Path | None = None):
    """Foto de TODOS os monitores. Se `caminho` vier, salva junto.

    Era só o principal. Com o Edge num segundo monitor — à esquerda, onde as
    coordenadas são negativas —, todo ponto caía fora da foto e a cor saía
    (0, 0, 0): a calibragem recusava a "página escura" e o robô não veria
    formulário nem faixa (17/09/2026). O mouse já trabalhava na área de
    trabalho inteira; faltava a foto acompanhar.

    A foto começa no canto da área de trabalho, não no (0, 0) da tela
    principal, e esse deslocamento vai junto em `info["origem"]`. Com um
    monitor só, a origem é (0, 0) e nada muda.
    """
    # Importado aqui, e não no topo: é ele que liga a leitura por monitor
    # do DPI, e a foto precisa sair na mesma escala das coordenadas do mouse.
    from cnd.infra.entrada_real import tela_virtual

    origem_x, origem_y, _largura, _altura = tela_virtual()
    imagem = ImageGrab.grab(all_screens=True)
    imagem.info["origem"] = (origem_x, origem_y)
    if caminho:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        imagem.save(caminho)
    return imagem


def _na_imagem(imagem, x: int, y: int) -> tuple[int, int]:
    """Coordenada de tela para coordenada dentro da foto."""
    origem_x, origem_y = getattr(imagem, "info", {}).get("origem", (0, 0))
    return int(x) - origem_x, int(y) - origem_y


def cor_em(imagem, x: int, y: int) -> tuple[int, int, int]:
    x, y = _na_imagem(imagem, x, y)
    largura, altura = imagem.size
    x = max(0, min(int(x), largura - 1))
    y = max(0, min(int(y), altura - 1))
    pixel = imagem.getpixel((x, y))
    return tuple(pixel[:3])


def cor_media(imagem, x: int, y: int, raio: int = 6) -> tuple[int, int, int]:
    """Média de uma vizinhança — imune a um pixel isolado de borda ou texto."""
    x, y = _na_imagem(imagem, x, y)
    largura, altura = imagem.size
    esquerda = max(0, x - raio)
    topo = max(0, y - raio)
    direita = min(largura, x + raio + 1)
    baixo = min(altura, y + raio + 1)
    if direita <= esquerda or baixo <= topo:
        return (0, 0, 0)
    recorte = imagem.crop((esquerda, topo, direita, baixo)).convert("RGB")
    # `ImageStat` e não `list(getdata())`: dá a mesma média por canal sem
    # materializar a lista de pixels, e `Image.getdata` está marcado para
    # sair no Pillow 14 (out/2027). O robô cego chama isto a cada leitura
    # de tela — é o caminho quente, e é o que ele usa para enxergar.
    # Soma inteira dividida por contagem, e não `.mean`: a média do Pillow
    # é float, e `int()` de um 41.999999999999996 devolveria 41 onde a
    # conta exata dá 42. O robô decide o estado da tela comparando cor com
    # margem — um canal a menos por arredondamento é ruído desnecessário
    # justo no sentido dele.
    estatistica = ImageStat.Stat(recorte)
    return tuple(int(soma) // quantos for soma, quantos
                 in zip(estatistica.sum[:3], estatistica.count[:3], strict=True))


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
