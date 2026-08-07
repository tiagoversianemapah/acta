"""Ponto de entrada do ACTA.exe.

Um executável só, com dois comportamentos: clicado no atalho, abre a
janela; chamado com argumentos, vira a linha de comando. É o mesmo binário
porque o aplicativo precisa lançar o robô, e num programa empacotado o
`sys.executable` é o próprio .exe — sem este desvio, mandar o robô rodar
abriria outra janela do aplicativo.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] not in ("app", "--app"):
        from cnd.cli import main as linha_de_comando

        return linha_de_comando(argv)

    from cnd.desktop.app import main as abrir_janela

    return abrir_janela()


if __name__ == "__main__":
    sys.exit(main())
