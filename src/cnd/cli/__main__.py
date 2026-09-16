"""Ponto de entrada de `python -m cnd.cli`.

O painel e o robô são iniciados assim (ver desktop/estado e web/comandos),
então este arquivo é o que mantém aquela forma de chamar funcionando agora
que `cli` é um pacote.
"""
from __future__ import annotations

import sys

from cnd.cli import main

if __name__ == "__main__":
    sys.exit(main())
