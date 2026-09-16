"""O mouse: onde ele está, para onde vai e como chega lá.

O movimento não é em linha reta de propósito — ver `mover`. Portal que
mede comportamento repara num cursor que salta de um ponto ao outro na
mesma velocidade, sempre.
"""
from __future__ import annotations

import ctypes
import math
import random
import time

from cnd.infra.entrada_real.win32 import (
    INPUT,
    INPUT_MOUSE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEINPUT,
    POINT,
    RECT,
    SM_CXVIRTUALSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
    SPI_GETWORKAREA,
    Uniao,
    enviar,
    user32,
)


def tela_virtual() -> tuple[int, int, int, int]:
    """(x, y, largura, altura) de toda a área de trabalho, somando monitores."""
    g = user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def area_util() -> tuple[int, int, int, int]:
    """(x, y, largura, altura) da tela SEM a barra de tarefas.

    Diferente de `tela_virtual`, que devolve o retângulo bruto. Uma janela
    dimensionada pela tela cheia nasce com o rodapé escondido atrás da barra
    de tarefas — e num robô cego, que mede tudo em fração da janela, o que
    está fora do visível é ponto que nunca vai ser clicado.
    """
    retangulo = RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                        ctypes.byref(retangulo), 0):
        return tela_virtual()
    return (retangulo.left, retangulo.top,
            retangulo.right - retangulo.left,
            retangulo.bottom - retangulo.top)


def posicao() -> tuple[int, int]:
    ponto = POINT()
    user32.GetCursorPos(ctypes.byref(ponto))
    return ponto.x, ponto.y


def _ir_para(x: float, y: float) -> None:
    """Posiciona o cursor. SendInput usa coordenadas normalizadas 0..65535."""
    origem_x, origem_y, largura, altura = tela_virtual()
    nx = round((x - origem_x) * 65535 / max(largura - 1, 1))
    ny = round((y - origem_y) * 65535 / max(altura - 1, 1))
    entrada = INPUT(type=INPUT_MOUSE, u=Uniao(mi=MOUSEINPUT(
        dx=nx, dy=ny, mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0, dwExtraInfo=None)))
    enviar(entrada)


def mover(x: float, y: float, duracao: float | None = None) -> None:
    """Leva o cursor até (x, y) numa curva, com aceleração e desaceleração.

    Movimento em linha reta e velocidade constante é assinatura de robô:
    a mão humana faz um arco, começa devagar, acelera no meio e freia no
    fim. É isso que a curva de Bézier com suavização abaixo reproduz.
    """
    inicio_x, inicio_y = posicao()
    distancia = math.hypot(x - inicio_x, y - inicio_y)
    if distancia < 2:
        _ir_para(x, y)
        return

    duracao = duracao if duracao is not None else min(
        1.1, max(0.18, distancia / random.uniform(1400, 2600))
    )
    passos = max(12, min(70, int(distancia / random.uniform(7, 16))))

    # Ponto de controle deslocado para o lado: dá o arco natural da mão.
    desvio = min(distancia * random.uniform(0.06, 0.20), 130)
    angulo = math.atan2(y - inicio_y, x - inicio_x) + math.pi / 2
    ctrl_x = (inicio_x + x) / 2 + math.cos(angulo) * desvio * random.choice((-1, 1))
    ctrl_y = (inicio_y + y) / 2 + math.sin(angulo) * desvio * random.choice((-1, 1))

    for passo in range(1, passos + 1):
        bruto = passo / passos
        # Suavização: devagar no começo, rápido no meio, devagar no fim.
        t = bruto * bruto * (3 - 2 * bruto)
        um = 1 - t
        px = um * um * inicio_x + 2 * um * t * ctrl_x + t * t * x
        py = um * um * inicio_y + 2 * um * t * ctrl_y + t * t * y
        if passo < passos:            # micro-tremor da mão
            px += random.uniform(-0.7, 0.7)
            py += random.uniform(-0.7, 0.7)
        _ir_para(px, py)
        time.sleep(duracao / passos * random.uniform(0.7, 1.35))


def clicar(x: float | None = None, y: float | None = None) -> None:
    """Clique real, com aproximação e correção de mira.

    O 'quase acertar e ajustar' no fim é o que a mão faz naturalmente ao
    mirar um alvo pequeno — chama-se correção de Fitts.
    """
    if x is not None and y is not None:
        if random.random() < 0.65:
            mover(x + random.uniform(-9, 9), y + random.uniform(-6, 6))
            time.sleep(random.uniform(0.04, 0.13))
        mover(x, y)

    time.sleep(random.uniform(0.05, 0.17))
    enviar(INPUT(type=INPUT_MOUSE, u=Uniao(mi=MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN,
        time=0, dwExtraInfo=None))))
    time.sleep(random.uniform(0.045, 0.115))     # tempo de pressão da tecla
    enviar(INPUT(type=INPUT_MOUSE, u=Uniao(mi=MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP,
        time=0, dwExtraInfo=None))))
