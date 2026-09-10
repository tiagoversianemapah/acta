"""O contrato que todo órgão precisa cumprir — ver ADR-003.

Todo conhecimento específico de um portal (URLs, seletores, como detectar
captcha, como ler o resultado) mora dentro do adapter daquele órgão. O
núcleo do sistema só conhece o vocabulário de `Desfecho`.

Os adapters moram em subpacotes por âmbito — `federal/`, `estadual/` e
`municipal/` — porque é o âmbito que decide quem é o órgão e, na prática,
o que o portal parece. `base.py`, `calibragem.py` e `fake.py` ficam na
raiz: servem a todos os âmbitos.

Consequência prática: adicionar CRF, RFB-PF ou uma Sefaz estadual é
escrever um arquivo novo no subpacote do âmbito e citá-lo em
`MODULOS_POR_ADAPTER`. Nada da fila, do ritmo, do breaker ou do painel
precisa mudar.
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import Protocol, runtime_checkable

from cnd.core.modelos import Documento, ResultadoTentativa
from cnd.infra.config import Config, ConfigOrgao

# O config nomeia o adapter curto (`adapter = "crf"`), sem dizer o âmbito:
# é o operador que escreve aquele arquivo, e cobrar dele o caminho do módulo
# seria vazar a arrumação do código para dentro da configuração. Este mapa faz
# a tradução. Adapter que não estiver aqui é procurado em `cnd.adapters.<nome>`,
# que é onde ficam os que não pertencem a âmbito nenhum — `fake`, por exemplo.
MODULOS_POR_ADAPTER = {
    "crf": "cnd.adapters.federal.crf",
    "rfb_cego": "cnd.adapters.federal.rfb.cego",
    "rfb_matriz": "cnd.adapters.federal.rfb.matriz",
    "rfb_pdf": "cnd.adapters.federal.rfb.pdf",
    "rfb_pj": "cnd.adapters.federal.rfb.pj",
    "sefaz_es": "cnd.adapters.estadual.sefaz_es",
    "sefaz_go": "cnd.adapters.estadual.sefaz_go",
}


@runtime_checkable
class AdapterOrgao(Protocol):
    """Ciclo de vida de um adapter, do ponto de vista do worker."""

    orgao: str

    def preparar(self) -> None:
        """Abre o que for preciso (navegador, sessão). Chamado uma vez."""

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        """Executa o fluxo completo do portal para um documento.

        NUNCA lança exceção de negócio: todo desfecho — inclusive captcha
        e erro técnico — volta como ResultadoTentativa. Exceções
        inesperadas são capturadas pelo worker e viram ERRO_TECNICO com
        evidência salva.
        """

    def reiniciar_sessao(self) -> None:
        """Descarta a sessão e começa uma nova.

        Chamado depois de falha retentável (captcha, bloqueio temporário ou
        erro técnico), para a próxima tentativa não herdar uma aba marcada,
        travada ou fora do fluxo esperado.
        """

    def encerrar(self) -> None:
        """Fecha tudo. Chamado no desligamento."""


def nome_modulo(adapter: str) -> str:
    """Módulo real do adapter, a partir do nome curto que vem do config."""
    chave = adapter.strip()
    if chave.startswith("cnd.adapters."):
        return chave
    if chave in MODULOS_POR_ADAPTER:
        return MODULOS_POR_ADAPTER[chave]
    return f"cnd.adapters.{chave}"


def adapter_existe(adapter: str) -> bool:
    return importlib.util.find_spec(nome_modulo(adapter)) is not None


def carregar(orgao: ConfigOrgao, cfg: Config) -> AdapterOrgao:
    """Instancia o adapter declarado no config (`adapter = "rfb_pj"`).

    O módulo precisa expor uma função `criar(orgao, cfg)`. É assim que o
    orquestrador liga um órgão sem conhecer nenhum adapter em particular.
    """
    try:
        modulo = importlib.import_module(nome_modulo(orgao.adapter))
    except ModuleNotFoundError as erro:
        raise RuntimeError(
            f"Órgão {orgao.codigo}: adapter '{orgao.adapter}' não existe "
            f"(esperado em src/cnd/adapters, no subpacote do âmbito)"
        ) from erro

    if not hasattr(modulo, "criar"):
        raise RuntimeError(
            f"O adapter '{orgao.adapter}' precisa expor uma função criar(orgao, cfg)"
        )
    return modulo.criar(orgao, cfg)
