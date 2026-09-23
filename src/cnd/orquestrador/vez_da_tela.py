"""A tela é uma só: as automações que mexem no Edge usam uma de cada vez.

RFB PJ, RFB PF e SEFAZ-ES mexem no mouse e no teclado de verdade, leem a
tela por pixel e fecham TODOS os Edge da máquina a cada sessão. Ligadas
juntas, cada uma rodava no seu thread sem saber da outra: uma fechava o Edge
da outra no meio da consulta, e a que perdia a corrida da partida nem
subia (17/09/2026). O CRF entra também: não mexe no mouse, mas abre um Edge
visível que os cegos fechariam — e em que poderiam clicar. GO e MA falam
HTTP e não passam por aqui. Quem entra é o adapter cujo módulo declara
`USA_TELA` (ver adapters/base.usa_tela).

A regra, decidida pela operação:

  - disputa a tela só quem tem item na planilha DA VEZ — a fila entrega
    uma planilha por vez (core/fila.lote_da_vez), e dar a tela a quem não
    vai conseguir reivindicar nada é deixá-la parada;
  - vale a ordem da FILA: quem chegou primeiro (ou foi mandado "Rodar
    agora") usa a tela e vai até o fim da fila dele;
  - quando o primeiro não pode trabalhar — disjuntor aberto, fora da
    janela de horário, ou só retentativas agendadas para depois —, o
    seguinte usa a tela nesse meio-tempo;
  - assim que o primeiro volta a ter item para pegar, a tela volta para
    ele, no fim do item que o outro estiver fazendo. Um item nunca é
    interrompido no meio.

Quem manda é o BANCO, e não quem pediu primeiro: a vez sai da mesma ordem
que `fila.reivindicar` usa, lida na hora por quem pergunta. A primeira
versão disto decidia pelo que cada worker tinha anunciado, e na partida a
tela ia para o thread que acordasse antes — a ordem da fila virava sorteio.

Quem retoma a tela depois de outra automação reabre o próprio navegador
antes de consultar: a outra pode ter fechado o dele.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from cnd.core import fila
from cnd.infra.log import obter

log = obter("orquestrador")


class VezDaTela:
    def __init__(self) -> None:
        self._estado = threading.Lock()     # protege os dados abaixo
        self._tela = threading.Lock()       # a tela em si: um item por vez
        self._orgaos: tuple[str, ...] = ()
        self._impedidos: set[str] = set()
        self._ultima: str | None = None
        self._com_a_tela_desde: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Quem disputa
    # ------------------------------------------------------------------
    def registrar(self, orgaos: Iterable[str]) -> None:
        """As automações de tela desta execução, antes de subir os workers.

        Registrar antes é o que torna a ordem previsível desde o primeiro
        item: quem pergunta já enxerga todas as filas, e não só as dos
        threads que acordaram.
        """
        with self._estado:
            self._orgaos = tuple(orgaos)
            self._impedidos.clear()

    def impedir(self, orgao: str, impedido: bool = True) -> None:
        """Marca que este órgão não pode trabalhar agora — ou que voltou.

        Pausado pelo disjuntor, fora da janela de horário ou com o adapter
        que não subiu. Sem isto, a fila dele seguraria a tela parada.

        Vale para TODOS os órgãos, inclusive os de HTTP, que não disputam
        tela nenhuma: quem também precisa da resposta é a FILA, para não
        deixar a planilha de um órgão parado segurar a vez das outras
        (ver core/fila.lote_da_vez). Aqui porque o registro já existia e
        já chega ao vigia; a disputa da tela continua só entre os
        registrados em `registrar`.
        """
        with self._estado:
            if impedido:
                self._impedidos.add(orgao)
            else:
                self._impedidos.discard(orgao)

    def impedidos(self) -> frozenset[str]:
        """Quem não consegue trabalhar agora, para quem decide a vez."""
        with self._estado:
            return frozenset(self._impedidos)

    # ------------------------------------------------------------------
    # De quem é a vez
    # ------------------------------------------------------------------
    def quem_tem_a_vez(self, conn: sqlite3.Connection) -> str | None:
        """O órgão de tela com o item mais antigo pronto para agora.

        None quando nenhum tem item para pegar NA PLANILHA DA VEZ — e aí
        ninguém ocupa a tela, que é o certo: quem só tem trabalho em
        planilha que ainda vai chegar espera como quem não tem trabalho.
        A ordem é a de `fila.ordem_na_fila`: "Rodar agora" na frente,
        depois a planilha que chegou primeiro.
        """
        with self._estado:
            candidatos = [o for o in self._orgaos if o not in self._impedidos]
            parados = frozenset(self._impedidos)
        # Os impedidos entram na pergunta: a planilha de quem está parado
        # não segura a vez, senão a tela ficava livre e a fila não
        # entregava item a ninguém.
        ordens = {
            orgao: fila.ordem_na_fila(conn, orgao, parados)
            for orgao in candidatos
        }
        prontos = {o: v for o, v in ordens.items() if v is not None}
        if not prontos:
            return None
        return min(prontos, key=prontos.__getitem__)

    def dispensa_cobranca(self, orgao: str, carencia_s: float,
                          conn: sqlite3.Connection) -> bool:
        """Ficar sem concluir é esperado agora?

        Sim para quem espera a vez — a primeira da fila pode levar horas —
        e para quem acabou de receber a tela: o relógio de "sem progresso"
        conta desde a última consulta, que foi antes da espera. Sem isto o
        vigia acusava "parado há 30 min" a automação que só aguardava.
        """
        with self._estado:
            registrado = orgao in self._orgaos
            desde = self._com_a_tela_desde.get(orgao)
        if registrado:
            dono = self.quem_tem_a_vez(conn)
            if dono is not None and dono != orgao:
                return True
        return desde is not None and time.monotonic() - desde < carencia_s

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
