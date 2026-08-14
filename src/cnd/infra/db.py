"""Conexão com o banco de dados."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def _raiz() -> Path:
    """Onde ficam config.toml e a pasta data/.

    Rodando do código-fonte, é a raiz do projeto — três níveis acima deste
    arquivo (src/cnd/infra/db.py). Rodando do executável empacotado, o
    código vive dentro do pacote e não há "projeto" nenhum acima dele: o
    que interessa é a pasta onde o ACTA.exe foi instalado, porque é lá que
    o operador enxerga o config e as certidões.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


RAIZ_PROJETO = _raiz()
CAMINHO_SCHEMA = Path(__file__).with_name("schema.sql")
CAMINHO_BANCO_PADRAO = RAIZ_PROJETO / "data" / "cnd.db"


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    """Abre o banco para leitura e escrita. Cria o arquivo se não existir."""
    caminho = Path(caminho or CAMINHO_BANCO_PADRAO)
    caminho.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(caminho, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def conectar_leitura(caminho: Path | None = None) -> sqlite3.Connection:
    """Abre o banco só para ler. Usado pelo painel, para não atrapalhar o robô."""
    caminho = Path(caminho or CAMINHO_BANCO_PADRAO)
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def caminho_pedido_parada(caminho_banco: Path | None = None) -> Path:
    """Arquivo-sinal usado pelo painel remoto para pedir parada do robô."""
    return Path(caminho_banco or CAMINHO_BANCO_PADRAO).parent / "parar.txt"


def caminho_parada_manual(caminho_banco: Path | None = None) -> Path:
    """Arquivo-sinal que impede retomada automatica apos parada manual."""
    return Path(caminho_banco or CAMINHO_BANCO_PADRAO).parent / "parada-manual.txt"


def garantir(caminho: Path | None = None) -> None:
    """Cria o banco vazio se ainda não houver nenhum.

    Vale para a primeira abertura numa máquina recém-instalada: sem isto, a
    tela tentaria ler um arquivo inexistente em modo somente-leitura e
    abriria com erro, antes mesmo de a pessoa importar a primeira planilha.
    """
    conn = conectar(caminho)
    try:
        criar_schema(conn)
    finally:
        conn.close()


def criar_schema(conn: sqlite3.Connection) -> None:
    """Cria as tabelas. Seguro chamar sempre que o sistema iniciar."""
    conn.executescript(CAMINHO_SCHEMA.read_text(encoding="utf-8"))
