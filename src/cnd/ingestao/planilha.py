"""Lê a planilha Excel e cria os jobs no banco.

Esta é a única parte do sistema que conhece Excel. Se um dia a fonte
virar uma API ou um sistema contábil, troca-se só este arquivo.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

from cnd.core.documentos import DocumentoInvalido, validar

# aba da planilha -> (código do órgão, tipo de documento esperado)
ABA_PARA_ORGAO: dict[str, tuple[str, str]] = {
    "RFB": ("RFB_PJ", "CNPJ"),
    "CRF": ("CRF", "CNPJ"),
    "CPF": ("RFB_PF", "CPF"),
    "GO": ("SEFAZ_GO", "CNPJ"),
    "DF": ("SEFAZ_DF", "CNPJ"),
    "ES": ("SEFAZ_ES", "CNPJ"),
    "SP": ("SEFAZ_SP", "CNPJ"),
}


@dataclass(frozen=True)
class Item:
    orgao: str
    tipo_documento: str
    documento: str
    nome: str


@dataclass(frozen=True)
class Rejeitado:
    aba: str
    linha: int
    valor_original: str
    nome: str
    motivo: str


@dataclass
class Leitura:
    itens: list[Item] = field(default_factory=list)
    rejeitados: list[Rejeitado] = field(default_factory=list)


def ler(caminho: Path, abas: list[str] | None = None) -> Leitura:
    """Lê a planilha e devolve os válidos e os rejeitados.
    Não mexe no banco: só lê e organiza."""
    resultado = Leitura()
    livro = load_workbook(caminho, read_only=True, data_only=True)
    vistos: set[tuple[str, str]] = set()

    for nome_aba in livro.sheetnames:
        chave = nome_aba.strip().upper()
        if chave not in ABA_PARA_ORGAO:
            continue
        if abas and chave not in abas:
            continue

        orgao, tipo = ABA_PARA_ORGAO[chave]
        planilha = livro[nome_aba]

        # linha 1 é o cabeçalho, então começa da 2
        for numero, linha in enumerate(planilha.iter_rows(min_row=2, values_only=True), start=2):
            if not linha or all(c is None for c in linha):
                continue

            nome = str(linha[0] or "").strip()
            bruto = linha[1] if len(linha) > 1 else None

            try:
                documento = validar(bruto, tipo)
            except DocumentoInvalido as erro:
                resultado.rejeitados.append(
                    Rejeitado(chave, numero, str(bruto), nome, str(erro))
                )
                continue

            if (orgao, documento) in vistos:
                resultado.rejeitados.append(
                    Rejeitado(chave, numero, str(bruto), nome,
                              "duplicado na planilha (mantida a primeira ocorrência)")
                )
                continue

            vistos.add((orgao, documento))
            resultado.itens.append(Item(orgao, tipo, documento, nome or documento))

    livro.close()
    return resultado


def importar(conn: sqlite3.Connection, caminho: Path,
             descricao: str, abas: list[str] | None = None) -> tuple[int, Leitura]:
    """Lê a planilha e grava lote + empresas + jobs no banco.

    Tudo de uma vez só: ou o lote inteiro entra, ou nada entra."""
    leitura = ler(caminho, abas)

    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
            (descricao, caminho.name),
        )
        lote_id = cursor.lastrowid

        for item in leitura.itens:
            conn.execute(
                "INSERT INTO empresa (documento, tipo_documento, nome) VALUES (?, ?, ?) "
                "ON CONFLICT (documento) DO UPDATE SET nome = excluded.nome",
                (item.documento, item.tipo_documento, item.nome),
            )
            empresa_id = conn.execute(
                "SELECT id FROM empresa WHERE documento = ?", (item.documento,)
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO job (lote_id, empresa_id, orgao) VALUES (?, ?, ?) "
                "ON CONFLICT (lote_id, empresa_id, orgao) DO NOTHING",
                (lote_id, empresa_id, item.orgao),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return lote_id, leitura


def _main() -> None:
    import argparse

    from cnd.infra.db import conectar, criar_schema

    parser = argparse.ArgumentParser(description="Importa uma planilha e cria um lote.")
    parser.add_argument("planilha", type=Path)
    parser.add_argument("--descricao", default="")
    parser.add_argument("--abas", nargs="*", default=["RFB"])
    args = parser.parse_args()

    conn = conectar()
    criar_schema(conn)

    descricao = args.descricao or f"Importação de {args.planilha.name}"
    abas = [a.upper() for a in args.abas] if args.abas else None
    lote_id, leitura = importar(conn, args.planilha, descricao, abas)

    print(f"\nLote #{lote_id} criado a partir de {args.planilha.name}")
    print(f"  jobs criados : {len(leitura.itens)}")
    print(f"  rejeitados   : {len(leitura.rejeitados)}")

    por_orgao: dict[str, int] = {}
    for item in leitura.itens:
        por_orgao[item.orgao] = por_orgao.get(item.orgao, 0) + 1
    for orgao, total in sorted(por_orgao.items()):
        print(f"    {orgao:10s} {total:5d}")

    if leitura.rejeitados:
        print("\n  Primeiros rejeitados:")
        for r in leitura.rejeitados[:10]:
            print(f"    [{r.aba} linha {r.linha}] {r.valor_original!r}: {r.motivo}")
        if len(leitura.rejeitados) > 10:
            print(f"    ... e mais {len(leitura.rejeitados) - 10}")

    conn.close()


if __name__ == "__main__":
    _main()
