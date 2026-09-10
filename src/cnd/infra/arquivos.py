"""Guarda de PDFs de certidão no sistema de arquivos.

Organizado para consulta humana direta: quem abrir a pasta no Explorer
consegue achar a certidão de uma empresa sem precisar do sistema.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

RE_INSEGURO = re.compile(r"[^0-9A-Za-z._-]")


def _seguro(texto: str) -> str:
    return RE_INSEGURO.sub("_", texto)[:80]


def caminho_certidao(base: Path, lote_id: int, orgao: str, documento: str,
                     emitida_em: date | None = None, nome: str = "") -> Path:
    """Onde o PDF fica guardado.

        data/certidoes/{lote}/{orgao}/{CNPJ} - {RAZÃO SOCIAL}.pdf

    O nome da empresa no arquivo não é enfeite: quem vai distribuir as
    certidões aos clientes precisa achar a certa sem abrir uma por uma, e
    CNPJ sozinho não diz nada para quem lê. O CNPJ vem primeiro porque é o
    que ordena e o que se busca.
    """
    pasta = base / str(lote_id) / _seguro(orgao)
    pasta.mkdir(parents=True, exist_ok=True)

    rotulo = _seguro(documento)
    if nome:
        # Barra e dois-pontos não podem entrar em nome de arquivo no Windows.
        limpo = re.sub(r"[^\w \-.]", "", nome, flags=re.UNICODE).strip()[:60]
        if limpo:
            rotulo = f"{rotulo} - {limpo}"
    return pasta / f"{rotulo}.pdf"


def hash_arquivo(caminho: Path | str) -> str:
    """SHA-256 do arquivo, para detectar corrupção e duplicata."""
    digest = hashlib.sha256()
    with open(caminho, "rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(65536), b""):
            digest.update(bloco)
    return digest.hexdigest()
