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

# Ordem importa: filho antes de pai, para não esbarrar em chave estrangeira.
#
# A lista é a do schema INTEIRO, e não a das tabelas lembradas na hora de
# escrevê-la. `fila_controle` referencia `lote`, então esquecê-la não
# deixava sobra: derrubava a zeragem com FOREIGN KEY constraint failed em
# qualquer máquina que já tivesse estacionado uma automação — e a transação
# volta atrás inteira, de forma que o botão simplesmente não funcionava.
# `recuperacao` não trava nada, mas guarda contagem de rodadas do lote que
# acabou de ser apagado, e ficaria mentindo para o robô seguinte.
TABELAS = ("tentativa", "certidao", "job", "fila_controle", "empresa",
           "lote", "ritmo", "breaker", "recuperacao", "heartbeat")


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
    conn.execute("BEGIN")
    try:
        for tabela in TABELAS:
            cursor = conn.execute(f"DELETE FROM {tabela}")
            linhas[tabela] = cursor.rowcount if cursor.rowcount > 0 else 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    arquivos = sum(_apagar_conteudo(pasta) for pasta in pastas)
    resultado = Zeragem(linhas=linhas, arquivos=arquivos)
    log.warning("maquina_zerada", extra={"resumo": resultado.como_texto()})
    return resultado
