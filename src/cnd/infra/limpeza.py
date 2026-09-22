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

import contextlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cnd.core.modelos import Status
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


# Quantos ENVIOS a máquina guarda. Vale para o registro e para os arquivos:
# o disco de uma máquina de escritório não é arquivo morto, e o painel já
# guardava só as três últimas planilhas enviadas (carteiras.QUANTAS_GUARDAR)
# enquanto o banco acumulava tudo desde a primeira rodada — dezenove envios
# em 22/09/2026, com os PDFs de cada um.
QUANTOS_ENVIOS_GUARDAR = 5


@dataclass
class Aposentadoria:
    """Os envios que saíram, para quem chamou poder relatar."""

    lotes: list[int]
    linhas: int
    arquivos: int

    def como_texto(self) -> str:
        if not self.lotes:
            return "nada a aposentar"
        return (f"{len(self.lotes)} envio(s), {self.linhas} linha(s) e "
                f"{self.arquivos} arquivo(s)")


def _colunas(conn: sqlite3.Connection, tabela: str) -> set[str]:
    return {linha[1] for linha in conn.execute(f'PRAGMA table_info("{tabela}")')}


def _apagar_arquivo(caminho: str | None) -> int:
    if not caminho:
        return 0
    alvo = Path(caminho)
    try:
        alvo.unlink()
        return 1
    except OSError:
        return 0


def aposentar_envios(conn: sqlite3.Connection, pasta_certidoes: Path | None = None,
                     manter: int = QUANTOS_ENVIOS_GUARDAR) -> Aposentadoria:
    """Apaga tudo o que passar dos `manter` envios TERMINADOS mais recentes.

    Some o envio, seus itens, tentativas, certidões e os PDFs no disco. O
    que sobrevive é o mesmo do `zerar`: calibragem, config e logs — e as
    empresas, que são cadastro e voltam a ser usadas pelo próximo envio.

    Envio com TRABALHO EM ABERTO nunca sai, por mais antigo que seja. A
    idade não diz que o envio terminou: na máquina do MT havia uma planilha
    de 349 itens com 142 na fila enquanto outras duas entravam por cima
    (22/09/2026). Apagá-la levaria junto trabalho que ninguém mandou parar
    — e o item que um worker estivesse consultando naquele instante viraria
    tentativa órfã, apontando para um job que não existe mais.

    FAILED conta como aberto: o robô nunca desiste de um item sem resposta,
    e a recuperação devolve os falhados à fila quando ela esvazia (ver
    core/recuperacao.py). Apagá-los seria decidir pela máquina que aquelas
    empresas ficam sem certidão.

    As tabelas saem do PRÓPRIO BANCO, pela mesma razão do `zerar`: um banco
    antigo carrega tabelas de schemas passados que ainda apontam para `job`
    ou `lote`, e uma lista escrita aqui não as conhece.
    """
    velhos = [linha[0] for linha in conn.execute(
        """
        SELECT l.id
          FROM lote l
         WHERE NOT EXISTS (
                   SELECT 1 FROM job j
                    WHERE j.lote_id = l.id
                      AND j.status IN (?, ?, ?, ?))
         ORDER BY l.id DESC
         LIMIT -1 OFFSET ?
        """,
        (Status.PENDING, Status.RUNNING, Status.RETRY_WAIT, Status.FAILED,
         manter))]
    if not velhos:
        return Aposentadoria(lotes=[], linhas=0, arquivos=0)

    marcas = ", ".join("?" * len(velhos))
    jobs = [linha[0] for linha in conn.execute(
        f"SELECT id FROM job WHERE lote_id IN ({marcas})", velhos)]

    arquivos = 0
    if jobs:
        marcas_jobs = ", ".join("?" * len(jobs))
        for tabela, coluna in (("certidao", "caminho_pdf"),
                               ("tentativa", "evidencia")):
            if coluna not in _colunas(conn, tabela):
                continue
            arquivos += sum(_apagar_arquivo(linha[0]) for linha in conn.execute(
                f'SELECT "{coluna}" FROM "{tabela}" '
                f"WHERE job_id IN ({marcas_jobs})", jobs))

    linhas = 0
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        try:
            for tabela in _tabelas_do_banco(conn):
                colunas = _colunas(conn, tabela)
                if jobs and "job_id" in colunas:
                    cursor = conn.execute(
                        f'DELETE FROM "{tabela}" WHERE job_id IN ({marcas_jobs})',
                        jobs)
                    linhas += max(cursor.rowcount, 0)
                if "lote_id" in colunas:
                    cursor = conn.execute(
                        f'DELETE FROM "{tabela}" WHERE lote_id IN ({marcas})',
                        velhos)
                    linhas += max(cursor.rowcount, 0)
            cursor = conn.execute(
                f"DELETE FROM lote WHERE id IN ({marcas})", velhos)
            linhas += max(cursor.rowcount, 0)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # A pasta do envio inteiro: os PDFs moram em certidoes/{lote}/{orgao}/,
    # então sobra o esqueleto de pastas depois de apagar arquivo por arquivo.
    if pasta_certidoes is not None:
        for lote_id in velhos:
            with contextlib.suppress(OSError):
                shutil.rmtree(pasta_certidoes / str(lote_id))

    resultado = Aposentadoria(lotes=velhos, linhas=linhas, arquivos=arquivos)
    log.warning("envios_aposentados", extra={"resumo": resultado.como_texto(),
                                             "lotes": velhos})
    return resultado
