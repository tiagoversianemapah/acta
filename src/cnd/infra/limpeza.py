"""Zerar a máquina: apagar o trabalho e recomeçar do nada.

Existe porque a alternativa era entrar por AnyDesk e apagar arquivo na mão,
que é justamente onde se apaga o que não devia. Aqui a lista do que morre e
a do que sobrevive estão escritas, e a segunda é a que importa:

  - **a calibragem** (`data/calibragem`), que é o passo mais caro de montar
    uma máquina — perdê-la obriga a medir a tela de novo, ponto a ponto;
  - **o `config.toml`**, que tem o nome da máquina, a senha da rede e os
    parâmetros de ritmo;
  - **os logs**, que são o histórico do PROGRAMA e não o resultado do
    trabalho — são eles que explicam um defeito depois que ele apareceu.

Não há desfazer. Quem chama é responsável por confirmar antes.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cnd.infra.log import obter

log = obter("limpeza")

# As tabelas vêm do PRÓPRIO BANCO, e não de uma lista escrita aqui.
#
# Uma lista fixa foi tentada duas vezes e falhou as duas. Primeiro por
# esquecer `fila_controle`, que referencia `lote`. Depois — numa máquina em
# produção, 31/08/2026 — porque `criar_schema` usa CREATE TABLE IF NOT
# EXISTS e NUNCA remove tabela: um banco antigo carrega tabelas de schemas
# passados, que a lista de hoje não conhece e que ainda apontam para `lote`
# ou `job`. O sintoma era o mesmo nos dois casos, e é dos piores: a
# transação volta atrás inteira e o botão simplesmente não funciona.
#
# `sqlite_master` sabe o que existe naquele banco; este arquivo, não.
def _tabelas_do_banco(conn: sqlite3.Connection) -> list[str]:
    return [linha[0] for linha in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


@dataclass
class Zeragem:
    """O que foi apagado, para quem chamou poder relatar."""

    linhas: dict[str, int]
    arquivos: int

    @property
    def total_linhas(self) -> int:
        return sum(self.linhas.values())

    def como_texto(self) -> str:
        partes = [f"{tabela} {n}" for tabela, n in self.linhas.items() if n]
        corpo = ", ".join(partes) or "nada no banco"
        return f"{corpo}; {self.arquivos} arquivo(s) apagado(s)"


def _apagar_conteudo(pasta: Path) -> int:
    """Esvazia a pasta mas a mantém: o programa espera encontrá-la lá."""
    if not pasta.is_dir():
        return 0
    apagados = 0
    for item in pasta.iterdir():
        try:
            if item.is_dir():
                apagados += sum(1 for _ in item.rglob("*") if _.is_file())
                shutil.rmtree(item)
            else:
                item.unlink()
                apagados += 1
        except OSError:
            log.warning("nao_apaguei", extra={"caminho": str(item)})
    return apagados


def zerar(conn: sqlite3.Connection, pastas: tuple[Path, ...] = ()) -> Zeragem:
    """Apaga o trabalho inteiro: banco e arquivos gerados.

    O banco é limpo numa transação só — pela metade seria pior que não
    limpar, porque sobrariam jobs apontando para lotes que não existem.
    """
    linhas: dict[str, int] = {}

    # Chave estrangeira DESLIGADA durante a limpeza. Ela existe para impedir
    # que sobre filho sem pai — e aqui não sobra nada, porque tudo morre na
    # mesma transação. Com ela ligada, a ordem de DELETE vira um quebra-
    # cabeça que depende de conhecer o schema inteiro, inclusive as tabelas
    # que versões antigas deixaram no banco. Desligar troca esse
    # quebra-cabeça por uma garantia mais simples: ou apaga tudo, ou nada.
    #
    # O PRAGMA não vale dentro de transação, por isso vem antes do BEGIN.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        try:
            for tabela in _tabelas_do_banco(conn):
                cursor = conn.execute(f'DELETE FROM "{tabela}"')
                linhas[tabela] = cursor.rowcount if cursor.rowcount > 0 else 0
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    arquivos = sum(_apagar_conteudo(pasta) for pasta in pastas)
    resultado = Zeragem(linhas=linhas, arquivos=arquivos)
    log.warning("maquina_zerada", extra={"resumo": resultado.como_texto()})
    return resultado
