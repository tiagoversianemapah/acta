"""Log estruturado (uma linha JSON por evento).

JSON em vez de texto livre porque o log vira dado: dá para responder
"quantos captchas por hora do dia?" com uma linha de código, sem regex.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

CAMPOS_PADRAO = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class FormatadorJSON(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        dados = {
            "hora": datetime.fromtimestamp(registro.created, UTC)
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "nivel": registro.levelname,
            "evento": registro.getMessage(),
        }
        # Qualquer extra={...} passado na chamada entra no JSON.
        for chave, valor in registro.__dict__.items():
            if chave not in CAMPOS_PADRAO and not chave.startswith("_"):
                dados[chave] = valor
        if registro.exc_info:
            dados["excecao"] = self.formatException(registro.exc_info)
        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar(pasta: Path, nome: str = "cnd", nivel: int = logging.INFO) -> logging.Logger:
    """Log vai para o arquivo (JSON) e para a tela (legível)."""
    pasta.mkdir(parents=True, exist_ok=True)

    raiz = logging.getLogger("cnd")
    raiz.setLevel(nivel)
    raiz.handlers.clear()
    raiz.propagate = False

    arquivo = TimedRotatingFileHandler(
        pasta / f"{nome}.jsonl", when="midnight", backupCount=90, encoding="utf-8"
    )
    arquivo.setFormatter(FormatadorJSON())
    raiz.addHandler(arquivo)

    tela = logging.StreamHandler(sys.stdout)
    tela.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                        datefmt="%H:%M:%S"))
    raiz.addHandler(tela)
    return raiz


def obter(nome: str) -> logging.Logger:
    return logging.getLogger(f"cnd.{nome}")
