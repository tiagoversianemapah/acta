"""A tela é uma só: as automações cegas usam uma de cada vez.

RFB PJ, RFB PF e SEFAZ-ES mexem no mouse e no teclado de verdade, leem a
tela por pixel e fecham TODOS os Edge da máquina a cada sessão. Ligadas
juntas, cada uma rodava no seu thread sem saber da outra: uma fechava o Edge
da outra no meio da consulta, e a que perdia a corrida da partida nem
subia (17/09/2026). O CRF entra também: não mexe no mouse, mas abre um Edge
visível que os cegos fechariam — e em que poderiam clicar. GO e MA falam
HTTP e não passam por aqui. Quem entra é o adapter que declara `usa_tela`.

A regra, decidida pela operação:

  - vale a ordem da FILA: quem chegou primeiro (ou foi mandado "Rodar
    agora") usa a tela e vai até o fim da fila dele;
  - quando o primeiro não pode trabalhar — disjuntor aberto, fora da
    janela de horário, ou só retentativas agendadas para depois —, o
    seguinte usa a tela nesse meio-tempo;
  - assim que o primeiro volta a ter item para pegar, a tela volta para
    ele, no fim do item que o outro estiver fazendo. Um item nunca é
    interrompido no meio.

Quem retoma a tela depois de outra automação reabre o próprio navegador
antes de consultar: a outra pode ter fechado o dele.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from cnd.infra.log import obter

log = obter("orquestrador")

Ordem = tuple[int, int, int]


class VezDaTela:
    def __init__(self) -> None:
        self._estado = threading.Lock()     # protege os dados abaixo
        self._tela = threading.Lock()       # a tela em si: um item por vez
        self._candidatas: dict[str, Ordem] = {}
        self._ultima: str | None = None
        self._com_a_tela_desde: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Quem quer a tela
    # ------------------------------------------------------------------
    def anunciar(self, orgao: str, ordem: Ordem | None) -> None:
        """Diz se este órgão tem item para pegar AGORA, e onde está na fila.

        None tira o órgão da disputa: ele está pausado, fora da janela ou
        sem item pronto. É isso que deixa o seguinte usar a tela.
        """
        with self._estado:
            if ordem is None:
                self._candidatas.pop(orgao, None)
            else:
                self._candidatas[orgao] = ordem

    def e_a_vez(self, orgao: str) -> bool:
        """Este órgão é o primeiro da fila entre os que podem trabalhar?"""
        with self._estado:
            if orgao not in self._candidatas:
                return False
            return min(self._candidatas, key=self._candidatas.__getitem__) == orgao

    def dispensa_cobranca(self, orgao: str, carencia_s: float) -> bool:
        """Ficar sem concluir é esperado agora?

        Sim para quem espera a vez — a primeira da fila pode levar horas —
        e para quem acabou de receber a tela: o relógio de "sem progresso"
        conta desde a última consulta, que foi antes da espera. Sem isto o
        vigia acusava "parado há 30 min" a automação que só aguardava.
        """
        with self._estado:
            if orgao in self._candidatas and min(
                    self._candidatas, key=self._candidatas.__getitem__) != orgao:
                return True
            desde = self._com_a_tela_desde.get(orgao)
            return desde is not None and time.monotonic() - desde < carencia_s

    def primeira(self) -> str | None:
        with self._estado:
            if not self._candidatas:
                return None
            return min(self._candidatas, key=self._candidatas.__getitem__)

    # ------------------------------------------------------------------
    # Quem está com a tela
    # ------------------------------------------------------------------
    def preparar(self, orgao: str, preparar: Callable[[], None]) -> None:
        """Abre o navegador da automação sem atropelar a que está abrindo.

        Na partida, todas as automações ligadas preparam ao mesmo tempo, e
        cada uma começa fechando todos os Edge: a segunda matava a janela
        que a primeira acabara de abrir.
        """
        with self._tela:
            try:
                preparar()
            finally:
                # Mesmo se falhar: o preparar fecha todos os Edge antes de
                # abrir o seu, e a automação que vinha usando a tela precisa
                # saber que o dela foi embora — senão consulta num navegador
                # que não existe mais.
                with self._estado:
                    self._ultima = orgao

    @contextmanager
    def usar(self, orgao: str, reabrir: Callable[[], None]) -> Iterator[None]:
        """Segura a tela durante um item inteiro.

        `reabrir` roda quando a tela vem de outra automação, antes do item.
        """
        with self._tela:
            with self._estado:
                veio_de = self._ultima
                if veio_de != orgao:
                    self._com_a_tela_desde[orgao] = time.monotonic()
            if veio_de is not None and veio_de != orgao:
                log.info("tela_retomada", extra={"orgao": orgao,
                                                 "vinha_de": veio_de})
                reabrir()
            try:
                yield
            finally:
                with self._estado:
                    self._ultima = orgao
