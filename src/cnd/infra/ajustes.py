"""Grava um valor no config.toml sem reescrever o arquivo inteiro.

O config é escrito à mão e cheio de comentário — é ele que documenta a
instalação para quem chega depois. Um gravador de TOML genérico devolveria
o arquivo canônico, com os comentários todos perdidos, e a próxima pessoa
não saberia mais por que cada número está ali.

Então mexe-se na linha da chave, e só nela. É deliberadamente burro: dá
conta de `chave = "valor"` dentro de uma seção, que é tudo o que a tela
precisa ajustar.
"""
from __future__ import annotations

import re
from pathlib import Path

from cnd.infra.db import RAIZ_PROJETO


def _caminho() -> Path:
    return RAIZ_PROJETO / "config.toml"


def _linhas() -> list[str]:
    # utf-8-sig tolera o BOM que o Bloco de Notas insere ao salvar.
    return _caminho().read_text(encoding="utf-8-sig").splitlines()


def ler_valor(secao: str, chave: str) -> str:
    """O valor atual, ou vazio se a chave não existir."""
    dentro = False
    for linha in _linhas():
        nua = linha.strip()
        if nua.startswith("["):
            dentro = nua == f"[{secao}]"
            continue
        if dentro and (achado := re.match(rf'\s*{chave}\s*=\s*"(.*)"', linha)):
            return achado.group(1)
    return ""


def gravar_valor(secao: str, chave: str, valor: str) -> None:
    """Escreve a chave na seção, criando a linha (ou a seção) se faltar."""
    _gravar(secao, chave, f'"{valor}"')


def gravar_booleano(secao: str, chave: str, valor: bool) -> None:
    """Escreve um booleano TOML, sem aspas."""
    _gravar(secao, chave, "true" if valor else "false")


def _gravar(secao: str, chave: str, valor_toml: str) -> None:
    linhas = _linhas()
    dentro = False
    fim_da_secao = None

    for indice, linha in enumerate(linhas):
        nua = linha.strip()
        if nua.startswith("["):
            if dentro:
                fim_da_secao = indice
                break
            dentro = nua == f"[{secao}]"
            continue
        if dentro and re.match(rf'\s*{chave}\s*=', linha):
            linhas[indice] = f"{chave} = {valor_toml}"
            _escrever(linhas)
            return
        if dentro and nua:
            fim_da_secao = indice + 1

    if fim_da_secao is not None:
        linhas.insert(fim_da_secao, f"{chave} = {valor_toml}")
    else:
        linhas += ["", f"[{secao}]", f"{chave} = {valor_toml}"]
    _escrever(linhas)


def _escrever(linhas: list[str]) -> None:
    # Sem BOM: é o que o leitor do projeto e as outras ferramentas esperam.
    _caminho().write_text("\n".join(linhas) + "\n", encoding="utf-8")
