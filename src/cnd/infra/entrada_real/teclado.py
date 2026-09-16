"""O teclado: teclas, texto e atalhos.

`digitar` sai caractere a caractere em Unicode, com intervalo sorteado —
texto que aparece inteiro de uma vez é assinatura de robô, e o custo de
não parecer um é alto justamente nos portais que interessam.
"""
from __future__ import annotations

import random
import time

from cnd.infra.entrada_real.win32 import (
    INPUT,
    INPUT_KEYBOARD,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_A,
    VK_CONTROL,
    VK_DELETE,
    VK_L,
    VK_RETURN,
    Uniao,
    enviar,
)


def _tecla_virtual(codigo: int, soltar: bool = False) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=Uniao(ki=KEYBDINPUT(
        wVk=codigo, wScan=0, dwFlags=KEYEVENTF_KEYUP if soltar else 0,
        time=0, dwExtraInfo=None)))


def _tecla_unicode(caractere: str, soltar: bool = False) -> INPUT:
    """Digita pelo código Unicode, ignorando o layout do teclado."""
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if soltar else 0)
    return INPUT(type=INPUT_KEYBOARD, u=Uniao(ki=KEYBDINPUT(
        wVk=0, wScan=ord(caractere), dwFlags=flags, time=0, dwExtraInfo=None)))


def digitar(texto: str, minimo: float = 0.06, maximo: float = 0.19) -> None:
    """Digita com ritmo irregular, como gente."""
    for caractere in texto:
        enviar(_tecla_unicode(caractere))
        time.sleep(random.uniform(0.012, 0.035))
        enviar(_tecla_unicode(caractere, soltar=True))
        time.sleep(random.uniform(minimo, maximo))


def tecla(codigo: int) -> None:
    """Aperta e solta uma tecla."""
    enviar(_tecla_virtual(codigo))
    time.sleep(random.uniform(0.03, 0.07))
    enviar(_tecla_virtual(codigo, soltar=True))
    time.sleep(random.uniform(0.04, 0.10))


def atalho(*codigos: int) -> None:
    """Combinação tipo Ctrl+L: segura todas, solta na ordem inversa."""
    for codigo in codigos:
        enviar(_tecla_virtual(codigo))
        time.sleep(random.uniform(0.02, 0.05))
    time.sleep(random.uniform(0.03, 0.08))
    for codigo in reversed(codigos):
        enviar(_tecla_virtual(codigo, soltar=True))
        time.sleep(random.uniform(0.02, 0.04))
    time.sleep(random.uniform(0.05, 0.12))


def limpar_campo() -> None:
    """Ctrl+A e Delete, com teclas reais."""
    atalho(VK_CONTROL, VK_A)
    tecla(VK_DELETE)


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
