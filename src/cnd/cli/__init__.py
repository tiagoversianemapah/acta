"""Linha de comando do ACTA.

    cnd importar CND_MIA_0726.xlsx --abas RFB
    cnd rodar
    cnd painel
    cnd relatorio --mes 2026-08
    cnd simular 200          # lote falso, para exercitar o sistema offline

Aqui mora só a montagem: cada arquivo ao lado agrupa os comandos de um
assunto e registra os próprios argumentos. Comando novo é arquivo novo mais
uma linha em `MODULOS` — e não mais uma emenda no meio de uma função de
setenta linhas, que era o que fazia dois comandos sem relação nenhuma
brigarem pelo mesmo espaço.
"""
from __future__ import annotations

import argparse
import sys

from cnd.cli import lotes, manutencao, robo, telas

MODULOS = (lotes, robo, telas, manutencao)


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cnd",
                                     description="RPA de certidões fiscais")
    sub = parser.add_subparsers(dest="comando", required=True)
    for modulo in MODULOS:
        modulo.registrar(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
