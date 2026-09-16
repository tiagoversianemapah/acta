"""A área de transferência: escrever nela, ler dela e colar.

Colar em vez de digitar é o caminho para texto longo — e para o que o
portal devolve, que é lido daqui em vez de da tela.
"""
from __future__ import annotations

import ctypes

from cnd.infra.entrada_real.teclado import atalho
from cnd.infra.entrada_real.win32 import (
    CF_UNICODETEXT,
    GMEM_MOVEABLE,
    VK_CONTROL,
    VK_V,
    kernel32,
    user32,
)


def limpar_area_transferencia() -> None:
    """Esvazia o clipboard para a leitura seguinte não reaproveitar texto velho."""
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()


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
