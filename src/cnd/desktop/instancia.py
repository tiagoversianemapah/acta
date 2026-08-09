"""Uma janela só do ACTA por computador.

Duas cópias abertas não são só desarrumação: as duas leem o mesmo banco,
as duas mostram números que divergem por segundos, e as duas podem mandar
a mesma máquina começar a trabalhar. Pior, quem clicou duas vezes no
atalho não percebe que abriu a segunda — só vê a tela que já estava lá
ficar para trás.

A segunda cópia não reclama: ela traz a primeira para frente e sai
calada, que é o que o Windows faz com qualquer aplicativo bem-comportado.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes

# O nome fica no espaço global do Windows para valer entre sessões do
# mesmo usuário. Prefixo próprio para não colidir com outro programa.
NOME_DO_CADEADO = "Local\\Mapah.Acta.JanelaUnica"
_JA_EXISTE = 183           # ERROR_ALREADY_EXISTS
_SW_RESTORE = 9

# O cadeado precisa viver enquanto o processo viver: se o handle for
# coletado, o Windows o libera e a próxima cópia se acha sozinha.
_cadeado = None


def tomar_posse() -> bool:
    """Reserva a vaga desta máquina. Falso se já havia outra cópia."""
    global _cadeado
    try:
        kernel32 = ctypes.windll.kernel32
        _cadeado = kernel32.CreateMutexW(None, False, NOME_DO_CADEADO)
        return kernel32.GetLastError() != _JA_EXISTE
    except Exception:
        # Sem o mutex não dá para saber; deixar abrir é melhor que impedir
        # o programa de rodar por causa da checagem.
        return True


def trazer_para_frente(titulo: str) -> bool:
    """Levanta a janela que já estava aberta e devolve se conseguiu.

    O `AttachThreadInput` é necessário porque o Windows recusa que um
    processo em segundo plano roube o foco — sem ele, a janela só pisca na
    barra de tarefas e quem clicou no atalho acha que nada aconteceu.
    """
    try:
        user32 = ctypes.windll.user32
        janela = user32.FindWindowW(None, titulo)
        if not janela:
            return False

        if user32.IsIconic(janela):
            user32.ShowWindow(janela, _SW_RESTORE)

        alvo = user32.GetWindowThreadProcessId(janela, None)
        nosso = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(nosso, alvo, True)
        try:
            user32.SetForegroundWindow(janela)
            user32.BringWindowToTop(janela)
        finally:
            user32.AttachThreadInput(nosso, alvo, False)
        return True
    except Exception:
        return False


def _tipos() -> None:
    """Assinaturas explícitas, para o ctypes não truncar o handle em 64 bits."""
    with_ = ctypes.windll
    with_.user32.FindWindowW.restype = wintypes.HWND
    with_.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    with_.kernel32.CreateMutexW.restype = wintypes.HANDLE


# Fora do Windows estas bibliotecas não existem; o módulo continua
# importável e as duas funções acima devolvem "pode abrir".
with contextlib.suppress(Exception):
    _tipos()
