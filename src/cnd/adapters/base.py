"""O contrato que todo órgão precisa cumprir — ver ADR-003.

Todo conhecimento específico de um portal (URLs, seletores, como detectar
captcha, como ler o resultado) mora dentro do adapter daquele órgão. O
núcleo do sistema só conhece o vocabulário de `Desfecho`.

Consequência prática: adicionar CRF, RFB-PF ou uma Sefaz estadual é
escrever um arquivo novo nesta pasta. Nada da fila, do ritmo, do breaker
ou do painel precisa mudar.
"""
from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from cnd.core.modelos import Documento, ResultadoTentativa
from cnd.infra.config import Config, ConfigOrgao


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
        """Descarta a sessão e começa uma nova. Chamado depois de captcha,
        para a próxima tentativa não herdar a sessão marcada."""

    def encerrar(self) -> None:
        """Fecha tudo. Chamado no desligamento."""


def carregar(orgao: ConfigOrgao, cfg: Config) -> AdapterOrgao:
    """Instancia o adapter declarado no config (`adapter = "rfb_pj"`).

    O módulo precisa expor uma função `criar(orgao, cfg)`. É assim que o
    orquestrador liga um órgão sem conhecer nenhum adapter em particular.
    """
    try:
        modulo = importlib.import_module(f"cnd.adapters.{orgao.adapter}")
    except ModuleNotFoundError as erro:
        raise RuntimeError(
            f"Órgão {orgao.codigo}: adapter '{orgao.adapter}' não existe "
            f"(esperado em src/cnd/adapters/{orgao.adapter}.py)"
        ) from erro

    if not hasattr(modulo, "criar"):
        raise RuntimeError(
            f"O adapter '{orgao.adapter}' precisa expor uma função criar(orgao, cfg)"
        )
    return modulo.criar(orgao, cfg)
