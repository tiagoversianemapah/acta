"""As janelas: achar, trazer para a frente, posicionar e fechar.

Conferir que a janela está MESMO na frente é obrigatório (ver
`garantir_em_primeiro_plano`): enquanto alguém usa o computador o Windows
recusa a troca de foco, e sem a conferência o robô digitaria o CNPJ dentro
de outro programa sem perceber.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from cnd.infra.entrada_real.win32 import (
    PROCESS_QUERY_LIMITED_INFORMATION,
    RECT,
    SW_MAXIMIZE,
    SW_RESTORE,
    kernel32,
    user32,
)


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
