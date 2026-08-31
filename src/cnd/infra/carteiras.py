"""As planilhas que a máquina guarda para poder enfileirar de novo.

Até aqui a planilha enviada era apagada assim que a importação terminava.
Isso fazia sentido enquanto o envio resolvia a carteira inteira de uma vez
— mas na prática se importa uma aba hoje e outra amanhã, porque cada
automação tem o seu ritmo e o seu risco. Para acrescentar a segunda, era
preciso reenviar o mesmo arquivo, achá-lo de novo na pasta de quem enviou,
e torcer para ser a mesma versão.

Guardar resolve isso, e o teto de três resolve o problema que guardar cria:
a pasta não cresce sem fim, e a máquina não vira arquivo morto de planilhas
de clientes. Ao enviar a quarta, sai a mais antiga.

Fica em `data/`, então sobrevive a atualização (o pacote não toca em
`data/`) e some quando alguém zera a máquina — que é o comportamento certo
nos dois casos.
"""
from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cnd.core import tempo
from cnd.infra.arquivos import _seguro
from cnd.infra.log import obter

log = obter("carteiras")

# Três porque é o que cobre o caso real: a planilha do mês, a do mês
# passado (para conferir o que mudou) e uma correção enviada no meio do
# caminho. A quarta seria arquivo morto.
QUANTAS_GUARDAR = 3

# O carimbo vai no NOME do arquivo, e não em banco: assim a pasta se explica
# sozinha para quem abrir por AnyDesk, e a ordem por nome é a ordem de
# chegada. `data/planilhas/2026-08-31T151230123-CND_MIA_0826.xlsx`.
# Colisão no mesmo milissegundo ganha `~2`, porque `~` nunca vem de `_seguro`.
RE_GUARDADA = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{6})(\d{3})?(?:~(\d+))?-(.+)$"
)


@dataclass(frozen=True)
class Guardada:
    """Uma planilha que está na máquina, pronta para ser enfileirada."""

    caminho: Path
    nome: str                 # como o arquivo se chamava ao ser enviado
    quando: datetime
    ordem: int = 1

    @property
    def token(self) -> str:
        """Identifica a planilha na URL sem expor o caminho no disco."""
        return self.caminho.name

    @property
    def quando_curto(self) -> str:
        return self.quando.strftime("%d/%m/%Y %H:%M")


def pasta(banco: Path) -> Path:
    """`data/planilhas`, ao lado do banco."""
    return banco.parent / "planilhas"


def nome_seguro(nome: str) -> str:
    return _seguro(Path(nome).name) or "planilha.xlsx"


def guardar(banco: Path, origem: Path, nome: str) -> Guardada:
    """Copia a planilha para a máquina e aposenta a mais antiga, se passar de três.

    O nome enviado é entrada de fora e passa por duas peneiras:

    `Path(nome).name` corta qualquer caminho — `../../ACTA/config.toml`
    vira `config.toml` e fica dentro da pasta.

    `_seguro` troca o que o Windows não aceita em nome de arquivo. Sem ele,
    `CND:MIA.xlsx` criava um *alternate data stream*: o arquivo aparecia
    como `CND`, sem extensão, e a planilha ficava inacessível pelo nome que
    a tela mostrava. Silencioso, porque a cópia não dá erro.
    """
    destino_pasta = pasta(banco)
    destino_pasta.mkdir(parents=True, exist_ok=True)

    carimbo = tempo.agora_iso()[:23].replace(":", "").replace(".", "")
    limpo = nome_seguro(nome)

    # Reenviar a MESMA planilha no mesmo segundo sobrescrevia a anterior sem
    # avisar — e é justamente o que acontece quando alguem manda a versao
    # corrigida logo em seguida. O sufixo mantem as duas.
    destino = destino_pasta / f"{carimbo}-{limpo}"
    sufixo = 2
    while destino.exists():
        destino = destino_pasta / f"{carimbo}~{sufixo}-{limpo}"
        sufixo += 1

    shutil.copy2(origem, destino)
    log.info("planilha_guardada", extra={"arquivo": destino.name})

    aposentar(banco)
    return _da_caminho(destino)


def listar(banco: Path) -> list[Guardada]:
    """As planilhas guardadas, da mais recente para a mais antiga."""
    destino_pasta = pasta(banco)
    if not destino_pasta.is_dir():
        return []
    achadas = [
        _da_caminho(c) for c in destino_pasta.iterdir()
        if c.is_file() and RE_GUARDADA.match(c.name)
    ]
    return sorted(achadas, key=lambda g: (g.quando, g.ordem, g.caminho.name),
                  reverse=True)


def buscar(banco: Path, token: str) -> Guardada | None:
    """Uma planilha pelo token, recusando token que tente sair da pasta.

    O token vem da URL, então ele é entrada de fora: `..%2F..%2Fconfig.toml`
    não pode virar caminho válido. Comparar pelo nome do arquivo, e não
    montar caminho por concatenação, fecha isso.
    """
    return next((g for g in listar(banco) if g.token == token), None)


def aposentar(banco: Path) -> int:
    """Deixa só as `QUANTAS_GUARDAR` mais recentes. Devolve quantas saíram."""
    guardadas = listar(banco)
    velhas = guardadas[QUANTAS_GUARDAR:]
    for g in velhas:
        with contextlib.suppress(OSError):
            g.caminho.unlink()
            log.info("planilha_aposentada", extra={"arquivo": g.caminho.name})
    return len(velhas)


def _da_caminho(caminho: Path) -> Guardada:
    achado = RE_GUARDADA.match(caminho.name)
    if achado:
        carimbo, milissegundos, sufixo, nome = achado.groups()
    else:
        carimbo, milissegundos, sufixo, nome = "", "", "", caminho.name
    try:
        formato = "%Y-%m-%dT%H%M%S%f" if milissegundos else "%Y-%m-%dT%H%M%S"
        quando = datetime.strptime(carimbo + (milissegundos or ""), formato)
    except ValueError:
        quando = datetime.fromtimestamp(caminho.stat().st_mtime)
    return Guardada(caminho=caminho, nome=nome, quando=quando,
                    ordem=int(sufixo or 1))
