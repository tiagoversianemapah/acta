"""Mouse e teclado de verdade, no nível do Windows.

Por que isto existe: o Playwright move o mouse *injetando eventos dentro do
navegador* pela porta de depuração — o cursor físico da tela nunca sai do
lugar. Aqui usamos `SendInput`, a mesma API que o driver do seu mouse usa,
então o cursor anda de verdade e o navegador recebe entrada indistinguível
de uma pessoa, porque é o mesmo caminho no sistema operacional.

Sem dependência externa: só ctypes, que já vem no Python.

Limitação importante: a janela precisa estar visível e em primeiro plano —
o mouse é um só, então enquanto o robô trabalha ninguém mexe na máquina.
"""
from __future__ import annotations

import contextlib
import ctypes
import math
import random
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Sem isto, o Windows mente sobre as coordenadas quando há escala de tela
# (125%, 150%) e o cursor cai no lugar errado.
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # por monitor
except Exception:
    with contextlib.suppress(Exception):
        user32.SetProcessDPIAware()

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002

SPI_GETWORKAREA = 0x0030
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

VK_CONTROL, VK_DELETE, VK_A, VK_C = 0x11, 0x2E, 0x41, 0x43
VK_RETURN, VK_TAB, VK_ESCAPE, VK_L, VK_F5 = 0x0D, 0x09, 0x1B, 0x4C, 0x74
VK_HOME, VK_END, VK_SHIFT, VK_RIGHT = 0x24, 0x23, 0x10, 0x27
VK_V = 0x56

SW_MAXIMIZE = 3
SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _Uniao(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _Uniao)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


user32.MoveWindow.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.BOOL,
]
user32.MoveWindow.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.restype = wintypes.BOOL


def _enviar(*entradas: INPUT) -> None:
    vetor = (INPUT * len(entradas))(*entradas)
    user32.SendInput(len(entradas), vetor, ctypes.sizeof(INPUT))


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
    entrada = INPUT(type=INPUT_MOUSE, u=_Uniao(mi=MOUSEINPUT(
        dx=nx, dy=ny, mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0, dwExtraInfo=None)))
    _enviar(entrada)


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
    _enviar(INPUT(type=INPUT_MOUSE, u=_Uniao(mi=MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN,
        time=0, dwExtraInfo=None))))
    time.sleep(random.uniform(0.045, 0.115))     # tempo de pressão da tecla
    _enviar(INPUT(type=INPUT_MOUSE, u=_Uniao(mi=MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP,
        time=0, dwExtraInfo=None))))


def _tecla_virtual(codigo: int, soltar: bool = False) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=_Uniao(ki=KEYBDINPUT(
        wVk=codigo, wScan=0, dwFlags=KEYEVENTF_KEYUP if soltar else 0,
        time=0, dwExtraInfo=None)))


def _tecla_unicode(caractere: str, soltar: bool = False) -> INPUT:
    """Digita pelo código Unicode, ignorando o layout do teclado."""
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if soltar else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_Uniao(ki=KEYBDINPUT(
        wVk=0, wScan=ord(caractere), dwFlags=flags, time=0, dwExtraInfo=None)))


def digitar(texto: str, minimo: float = 0.06, maximo: float = 0.19) -> None:
    """Digita com ritmo irregular, como gente."""
    for caractere in texto:
        _enviar(_tecla_unicode(caractere))
        time.sleep(random.uniform(0.012, 0.035))
        _enviar(_tecla_unicode(caractere, soltar=True))
        time.sleep(random.uniform(minimo, maximo))


def tecla(codigo: int) -> None:
    """Aperta e solta uma tecla."""
    _enviar(_tecla_virtual(codigo))
    time.sleep(random.uniform(0.03, 0.07))
    _enviar(_tecla_virtual(codigo, soltar=True))
    time.sleep(random.uniform(0.04, 0.10))


def atalho(*codigos: int) -> None:
    """Combinação tipo Ctrl+L: segura todas, solta na ordem inversa."""
    for codigo in codigos:
        _enviar(_tecla_virtual(codigo))
        time.sleep(random.uniform(0.02, 0.05))
    time.sleep(random.uniform(0.03, 0.08))
    for codigo in reversed(codigos):
        _enviar(_tecla_virtual(codigo, soltar=True))
        time.sleep(random.uniform(0.02, 0.04))
    time.sleep(random.uniform(0.05, 0.12))


def limpar_campo() -> None:
    """Ctrl+A e Delete, com teclas reais."""
    atalho(VK_CONTROL, VK_A)
    tecla(VK_DELETE)


def limpar_area_transferencia() -> None:
    """Esvazia o clipboard para a leitura seguinte não reaproveitar texto velho."""
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()


kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE


def definir_area_transferencia(texto: str) -> bool:
    """Põe texto no clipboard. Devolve se conseguiu.

    Existe para o robô cego **colar** em vez de digitar. Digitar caminho
    longo em caixa de diálogo do Windows perde caractere: a caixa "Salvar
    como" recebeu um caminho cortado no meio (08/09/2026), e o arquivo some
    sem erro nenhum, com outro nome. Colar é atômico.
    """
    dados = ctypes.create_unicode_buffer(texto)
    tamanho = ctypes.sizeof(dados)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, tamanho)
    if not handle:
        return False

    ponteiro = kernel32.GlobalLock(handle)
    if not ponteiro:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(ponteiro, dados, tamanho)
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
    finally:
        user32.CloseClipboard()
    # A partir daqui o handle é do sistema: liberar aqui corromperia o
    # clipboard.
    return True


def colar(texto: str) -> bool:
    """Coloca o texto no clipboard e manda Ctrl+V. Devolve se colou."""
    if not definir_area_transferencia(texto):
        return False
    atalho(VK_CONTROL, VK_V)
    return True


def texto_area_transferencia() -> str:
    """Texto Unicode copiado pelo Windows, ou vazio se não houver texto."""
    if not user32.OpenClipboard(None):
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ponteiro = kernel32.GlobalLock(handle)
        if not ponteiro:
            return ""
        try:
            return ctypes.wstring_at(ponteiro)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def ir_para_url(url: str) -> None:
    """Navega pela barra de endereços — Ctrl+L, digita, Enter.

    Navegar pelo teclado dispensa saber onde a barra de endereços está na
    tela, o que torna esta parte imune a mudança de resolução ou de tema.
    """
    atalho(VK_CONTROL, VK_L)
    time.sleep(random.uniform(0.15, 0.35))
    digitar(url, minimo=0.008, maximo=0.028)
    time.sleep(random.uniform(0.20, 0.45))
    tecla(VK_RETURN)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _executavel_do_processo(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        tamanho = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                               ctypes.byref(tamanho)):
            return buffer.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def listar_janelas() -> list[tuple[int, str, str]]:
    """(hwnd, título, executável) de todas as janelas visíveis com título."""
    achadas: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visitar(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        tamanho = user32.GetWindowTextLengthW(hwnd)
        if not tamanho:
            return True
        buffer = ctypes.create_unicode_buffer(tamanho + 1)
        user32.GetWindowTextW(hwnd, buffer, tamanho + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        achadas.append((hwnd, buffer.value, _executavel_do_processo(pid.value)))
        return True

    user32.EnumWindows(visitar, 0)
    return achadas


def _area(hwnd: int) -> int:
    caixa = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(caixa)):
        return 0
    return max(0, caixa.right - caixa.left) * max(0, caixa.bottom - caixa.top)


def achar_janela(titulo_parcial: str | None = None,
                 executavel: str | None = None) -> int | None:
    """Janela pelo programa e/ou por parte do título.

    Duas armadilhas resolvidas aqui:

    - **Título muda.** O título de um navegador acompanha a página visitada
      ("Serviços da Receita Federal" na home, "Certidão de Regularidade
      Fiscal" no formulário), então buscar só por ele quebra no meio do
      fluxo. Por isso o filtro principal é o EXECUTÁVEL.

    - **Nem toda janela do programa é a janela.** O Edge mantém janelinhas
      auxiliares em segundo plano (uma delas mede 516x249). Entre as
      candidatas, ficamos com a de MAIOR área — que é sempre a de verdade.
    """
    candidatas = listar_janelas()

    if executavel:
        alvo = executavel.lower()
        candidatas = [c for c in candidatas if c[2] == alvo]

    if titulo_parcial:
        pedaco = titulo_parcial.lower()
        preferidas = [c for c in candidatas if pedaco in c[1].lower()]
        if preferidas:
            candidatas = preferidas
        elif not executavel:
            return None

    if not candidatas:
        return None

    return max(candidatas, key=lambda c: _area(c[0]))[0]


def _forcar_foreground(hwnd: int) -> bool:
    """Traz a janela para a frente de verdade.

    O Windows PROÍBE um processo em segundo plano de roubar o foco: o
    `SetForegroundWindow` simplesmente falha calado e no máximo pisca o
    botão na barra de tarefas. O contorno oficial é anexar a fila de
    entrada da nossa thread à da janela que está em primeiro plano — aí o
    sistema passa a nos considerar "no mesmo contexto" e permite a troca.

    Sem isto, o robô cego fotografa e clica na janela errada.
    """
    if user32.GetForegroundWindow() == hwnd:
        return True

    janela_atual = user32.GetForegroundWindow()
    thread_alvo = user32.GetWindowThreadProcessId(hwnd, None)
    thread_frente = user32.GetWindowThreadProcessId(janela_atual, None)
    thread_nossa = kernel32.GetCurrentThreadId()

    for thread in (thread_frente, thread_alvo):
        if thread and thread != thread_nossa:
            user32.AttachThreadInput(thread_nossa, thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        for thread in (thread_frente, thread_alvo):
            if thread and thread != thread_nossa:
                user32.AttachThreadInput(thread_nossa, thread, False)

    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def trazer_para_frente(titulo_parcial: str | None = None,
                       executavel: str | None = None) -> bool:
    """Põe a janela em primeiro plano — sem foco, o clique vai para o vazio."""
    hwnd = achar_janela(titulo_parcial, executavel)
    if hwnd is None:
        return False

    # SW_RESTORE só quando a janela está MINIMIZADA. Numa janela maximizada
    # ele faz o oposto do nome: devolve ao tamanho pequeno anterior — e aí
    # todas as coordenadas calibradas passam a cair no lugar errado.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

    return _forcar_foreground(hwnd)


def posicionar_janela(titulo_parcial: str | None = None,
                      executavel: str | None = None,
                      retangulo: tuple[int, int, int, int] | None = None) -> bool:
    """Move a janela para a area calibrada, antes de maximizar.

    O Edge costuma reabrir no ultimo monitor usado. Para o robo cego isso
    importa: a calibragem pertence a um monitor/proporcao especificos.
    """
    hwnd = achar_janela(titulo_parcial, executavel)
    if hwnd is None or retangulo is None:
        return False

    x, y, largura, altura = retangulo
    if largura <= 0 or altura <= 0:
        return False

    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

    if not user32.MoveWindow(hwnd, int(x), int(y), int(largura), int(altura), True):
        return False
    time.sleep(0.3)
    return _forcar_foreground(hwnd)


def maximizar(titulo_parcial: str | None = None,
              executavel: str | None = None) -> bool:
    """Maximiza a janela e a traz para a frente."""
    hwnd = achar_janela(titulo_parcial, executavel)
    if hwnd is None:
        return False
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.6)
    return _forcar_foreground(hwnd)


def retangulo_janela(titulo_parcial: str | None = None,
                     executavel: str | None = None) -> tuple[int, int, int, int] | None:
    """(x, y, largura, altura) da janela na tela.

    É o que permite guardar a calibragem em PROPORÇÕES da janela em vez de
    pixels: assim ela continua valendo num servidor com outra resolução.
    """
    hwnd = achar_janela(titulo_parcial, executavel)
    if hwnd is None:
        return None
    caixa = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(caixa)):
        return None
    return (caixa.left, caixa.top,
            caixa.right - caixa.left, caixa.bottom - caixa.top)


WM_CLOSE = 0x0010


def fechar_janelas(executavel: str) -> int:
    """Fecha as janelas do programa com educação (WM_CLOSE).

    Diferente de `taskkill /F`: matar o navegador à força marca o perfil
    como "travado", e na volta ele exibe a bolha "Restaurar páginas" — que
    rouba o foco, cobre parte da tela e deixa a sessão diferente da de um
    usuário comum.
    """
    fechadas = 0
    for hwnd, _, exe in listar_janelas():
        if exe == executavel.lower():
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            fechadas += 1
    if fechadas:
        time.sleep(2.5)
    return fechadas


def garantir_em_primeiro_plano(titulo_parcial: str | None = None,
                               executavel: str | None = None,
                               tentativas: int = 6) -> bool:
    """Insiste até a janela estar realmente na frente. Devolve se conseguiu.

    Conferir é obrigatório: enquanto alguém usa o computador, o Windows
    recusa a troca de foco. Sem esta verificação, o robô digitaria o CNPJ e
    clicaria dentro de outro programa, sem perceber.
    """
    for _ in range(tentativas):
        if em_primeiro_plano(titulo_parcial, executavel):
            return True
        trazer_para_frente(titulo_parcial, executavel)
        time.sleep(0.6)
    return em_primeiro_plano(titulo_parcial, executavel)


def em_primeiro_plano(titulo_parcial: str | None = None,
                      executavel: str | None = None) -> bool:
    hwnd = achar_janela(titulo_parcial, executavel)
    return hwnd is not None and user32.GetForegroundWindow() == hwnd
