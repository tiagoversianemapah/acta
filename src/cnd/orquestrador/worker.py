"""O worker: um job por vez, do começo ao fim.

Um thread por worker, um worker (ou mais) por órgão, cada um com a própria
conexão — conexões SQLite não são compartilháveis entre threads.

O que ele faz, em uma frase: tira um job da fila, respeita o ritmo, entrega
ao adapter e traduz o resultado em transição de estado — sem saber nada
sobre navegador nem sobre o site do órgão. Quem sobe e derruba estes
threads é `loop.executar`.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field, replace

from cnd.adapters.base import AdapterOrgao
from cnd.adapters.base import carregar as carregar_adapter
from cnd.adapters.base import usa_tela as usa_tela_o_adapter
from cnd.core import breaker, fila, perfis, ritmo, tempo
from cnd.core.modelos import (
    CONCLUSIVOS,
    RETENTAVEIS,
    Desfecho,
    ResultadoTentativa,
)
from cnd.infra import alertas, heartbeat
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.db import conectar
from cnd.infra.log import obter
from cnd.orquestrador import retentativa
from cnd.orquestrador.vez_da_tela import VezDaTela

log = obter("orquestrador")

PAUSA_SEM_TRABALHO_S = 5.0
PAUSA_BREAKER_ABERTO_S = 15.0
PAUSA_FORA_DA_JANELA_S = 60.0
# Curta de propósito: é o tempo que a automação seguinte leva para perceber
# que a tela ficou livre — ou que a primeira da fila voltou a precisar dela.
PAUSA_AGUARDANDO_A_TELA_S = 3.0
INTERVALO_HEARTBEAT_S = 60.0


class PortaoDeRitmo:
    """Garante que duas consultas ao MESMO órgão nunca saiam ao mesmo tempo.

    Com um worker só isso é trivial; com vários, é este portão que mantém
    o ritmo total do órgão igual ao configurado (docs/04, seção 3).
    """

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._liberado_em = 0.0

    def aguardar(self, espera_s: float, parar: threading.Event) -> None:
        with self._trava:
            agora = time.monotonic()
            alvo = max(agora, self._liberado_em)
            self._liberado_em = alvo + espera_s
            dormir = alvo - agora
        if dormir > 0:
            parar.wait(dormir)


@dataclass
class Contexto:
    cfg: Config
    parar: threading.Event
    limite: int | None = None        # teto de jobs nesta execução (pilotos)
    # Uma só para todos os workers: é o que faz as automações cegas usarem
    # a tela uma de cada vez. Ver orquestrador/vez_da_tela.py.
    vez_da_tela: VezDaTela = field(default_factory=VezDaTela)
    _feitos: int = 0
    _trava: threading.Lock = field(default_factory=threading.Lock)

    def reservar_vaga(self) -> bool:
        """Consome uma vaga da cota. False quando o limite foi atingido.

        Reservar ANTES de tirar o job da fila evita que um item fique preso
        em RUNNING quando a cota acaba.
        """
        if self.limite is None:
            return True
        with self._trava:
            if self._feitos >= self.limite:
                return False
            self._feitos += 1
            return True

    def cancelar_reserva(self) -> None:
        """Desfaz a reserva quando nenhum job chegou a ser processado."""
        if self.limite is None:
            return
        with self._trava:
            self._feitos = max(0, self._feitos - 1)


class Worker(threading.Thread):
    def __init__(self, ctx: Contexto, orgao: ConfigOrgao, numero: int,
                 portao: PortaoDeRitmo, principal: bool) -> None:
        super().__init__(name=f"{orgao.codigo}-{numero}", daemon=True)
        self.ctx = ctx
        self.orgao = orgao
        self.numero = numero
        self.portao = portao
        self.principal = principal          # só o worker 0 bate o heartbeat
        self.conn = None
        self.adapter: AdapterOrgao | None = None
        self._ultimo_heartbeat = 0.0
        # Mexe no mouse e na tela de verdade? Então divide a tela com as
        # outras automações cegas. Quem diz é o adapter (`usa_tela`).
        self.usa_tela = False
        self._esperando_a_vez = False

    # ------------------------------------------------------------------
    def run(self) -> None:
        self.conn = conectar(self.ctx.cfg.banco)
        try:
            self.usa_tela = usa_tela_o_adapter(self.orgao.adapter)
            self.adapter = carregar_adapter(self.orgao, self.ctx.cfg)
            if self.usa_tela:
                self.ctx.vez_da_tela.preparar(self.orgao.codigo,
                                              self.adapter.preparar)
            else:
                self.adapter.preparar()
        except Exception as erro:
            # Sai da disputa pela tela: adapter que não subiu nunca vai
            # trabalhar, e a fila dele seguraria a vez das outras.
            self._sair_da_disputa_pela_tela()
            log.error("adapter_nao_carregou",
                      extra={"orgao": self.orgao.codigo, "erro": str(erro)})
            alertas.abrir_incidente(
                self.ctx.cfg.alertas, f"adapter:{self.orgao.codigo}",
                f"Falha ao iniciar {self.orgao.codigo}",
                "Nenhum item será processado neste órgão.",
                acao="1. Conferir se o Edge está instalado e fecha sem erro.\n"
                     "2. Rodar `cnd calibrar --conferir` para validar as medidas "
                     "da tela.\n"
                     "3. Reiniciar com `cnd rodar`.",
                dados={"Órgão": self.orgao.codigo, "Motivo": str(erro)[:300]},
                severidade="erro",
            )
            return

        log.info("worker_iniciado", extra={"orgao": self.orgao.codigo, "worker": self.numero})
        try:
            while not self.ctx.parar.is_set():
                self._passo()
        finally:
            self._sair_da_disputa_pela_tela()
            try:
                self.adapter.encerrar()
            finally:
                self.conn.close()
            log.info("worker_encerrado",
                     extra={"orgao": self.orgao.codigo, "worker": self.numero})

    # ------------------------------------------------------------------
    def _passo(self) -> None:
        self._bater_ponto()

        if not tempo.dentro_da_janela(self.orgao.pacing.janela_ativa):
            self._sair_da_disputa_pela_tela()
            self.ctx.parar.wait(PAUSA_FORA_DA_JANELA_S)
            return

        if not breaker.pode_despachar(
            self.conn, self.orgao.codigo, self.orgao.breaker
        ):
            # Sem este aviso, o robô parece travado: ele está obedecendo o
            # disjuntor, mas em silêncio. Foi o que mais confundiu durante o
            # desenvolvimento — "não faz nada" era, na verdade, "está de
            # castigo por causa da rodada anterior".
            estado = breaker.consultar(self.conn, self.orgao.codigo)
            log.warning("parado_pelo_disjuntor", extra={
                "orgao": self.orgao.codigo,
                "motivo": estado.motivo,
                "retoma_em": estado.aberto_ate,
                "dica": "use --reiniciar-ritmo para zerar",
            })
            # Pausado não segura a tela: a automação seguinte da fila
            # trabalha enquanto esta espera o disjuntor.
            self._sair_da_disputa_pela_tela()
            self.ctx.parar.wait(PAUSA_BREAKER_ABERTO_S)
            return

        if self.usa_tela and not self._chegou_a_vez():
            return

        if not self.ctx.reservar_vaga():
            log.info("limite_da_execucao_atingido", extra={"limite": self.ctx.limite})
            self.ctx.parar.set()
            return

        try:
            job = fila.reivindicar(self.conn, self.orgao.codigo)
        except Exception:
            self.ctx.cancelar_reserva()
            raise
        if job is None:
            self.ctx.cancelar_reserva()
            self.ctx.parar.wait(PAUSA_SEM_TRABALHO_S)
            return

        # Idempotência (RNF-04): certidão já emitida NESTE MÊS e NESTA
        # PLANILHA dispensa ir ao portal. Mês e não validade; planilha
        # porque duas remessas são trabalhos separados mesmo com os mesmos
        # CNPJs — ver fila.certidao_do_mes.
        vigente = fila.certidao_do_mes(self.conn, job.doc.empresa_id,
                                       self.orgao.codigo, job.lote_id)
        if vigente is not None:
            tentativa_id = fila.abrir_tentativa(self.conn, job, self.numero)
            resultado = ResultadoTentativa(
                desfecho=Desfecho.APROVEITADA,
                mensagem_portal=f"já emitida em {vigente['emitida_em']}",
            )
            fila.fechar_tentativa(self.conn, tentativa_id, resultado)
            fila.concluir(self.conn, job, resultado)
            log.info("job_aproveitado", extra={"job": job.job_id,
                                               "documento": job.doc.documento})
            return

        # Respeita o ritmo antes de bater no portal. Sem este log, quem olha a
        # tela vê o navegador parado e acha que travou.
        estado_ritmo = ritmo.estado(self.conn, self.orgao.codigo, self.orgao.pacing)
        espera = ritmo.proxima_espera(estado_ritmo.intervalo_s, self.orgao.pacing.jitter)
        if espera >= 1.0:
            log.info("aguardando_o_ritmo", extra={
                "orgao": self.orgao.codigo,
                "segundos": round(espera, 1),
                "proximo": job.doc.documento,
            })
        self.portao.aguardar(espera, self.ctx.parar)
        if self.ctx.parar.is_set():
            # Devolve o job para a fila em vez de deixá-lo preso em RUNNING.
            self.ctx.cancelar_reserva()
            fila.devolver(self.conn, job)
            return

        with self._tela():
            if self.ctx.parar.is_set():
                # Parada pedida enquanto esperava a tela: o item volta para a
                # fila em vez de sair mais uma consulta.
                self.ctx.cancelar_reserva()
                fila.devolver(self.conn, job)
                return

            tentativa_id = fila.abrir_tentativa(self.conn, job, self.numero)
            inicio = time.monotonic()
            resultado = self._emitir_com_retentativa_rapida(job)
            duracao = time.monotonic() - inicio

            fila.fechar_tentativa(self.conn, tentativa_id, resultado)
            log.info("tentativa", extra={
                "job": job.job_id,
                "orgao": self.orgao.codigo,
                "documento": job.doc.documento,
                "desfecho": str(resultado.desfecho),
                "tentativa": job.tentativas + 1,
                "duracao_s": round(duracao, 3),
            })

            # Dentro da tela: em desfecho retentável o ajuste de ritmo
            # reinicia a sessão, e isso abre e fecha o Edge.
            self._ajustar_ritmo(resultado.desfecho)
            self._avaliar_breaker(resultado.desfecho)
            self._encerrar_job(job, resultado)

    # ------------------------------------------------------------------
    # A tela compartilhada (só automações cegas)
    # ------------------------------------------------------------------
    def _chegou_a_vez(self) -> bool:
        """Diz se este órgão pode usar a tela agora.

        False já inclui a espera: quem chama só precisa voltar. Quem decide
        é a ordem da fila no banco, igual para todos os workers.
        """
        vez = self.ctx.vez_da_tela
        vez.impedir(self.orgao.codigo, False)
        dono = vez.quem_tem_a_vez(self.conn)

        if dono == self.orgao.codigo:
            if self._esperando_a_vez:
                log.info("vez_da_tela_chegou", extra={"orgao": self.orgao.codigo})
            self._esperando_a_vez = False
            return True

        if dono is None:
            # Ninguém tem item pronto, nem este órgão: espera como quem não
            # tem trabalho, sem ocupar a tela.
            self._esperando_a_vez = False
            self.ctx.parar.wait(PAUSA_SEM_TRABALHO_S)
            return False

        # Log só na mudança: repetido a cada 3s, esconderia o resto.
        if not self._esperando_a_vez:
            log.info("aguardando_a_vez_da_tela", extra={
                "orgao": self.orgao.codigo, "primeira_da_fila": dono,
            })
            self._esperando_a_vez = True
        self.ctx.parar.wait(PAUSA_AGUARDANDO_A_TELA_S)
        return False

    def _sair_da_disputa_pela_tela(self) -> None:
        if self.usa_tela:
            self.ctx.vez_da_tela.impedir(self.orgao.codigo, True)
            self._esperando_a_vez = False

    def _tela(self):
        if not self.usa_tela:
            return contextlib.nullcontext()
        return self.ctx.vez_da_tela.usar(self.orgao.codigo,
                                         self._reabrir_navegador)

    def _reabrir_navegador(self) -> None:
        """A tela veio de outra automação, que pode ter fechado nosso Edge.

        Falhar aqui não derruba o worker: a consulta seguinte tenta mesmo
        assim, e o que der errado nela vira desfecho com evidência.
        """
        try:
            self.adapter.reiniciar_sessao()
        except Exception:
            log.exception("falha_ao_reabrir_navegador_na_vez_da_tela",
                          extra={"orgao": self.orgao.codigo})

    # ------------------------------------------------------------------
    def _emitir_no_adapter(self, job) -> ResultadoTentativa:
        try:
            return self.adapter.emitir(job.doc)
        except Exception as erro:
            log.exception("adapter_estourou", extra={"job": job.job_id,
                                                     "orgao": self.orgao.codigo})
            return ResultadoTentativa(
                desfecho=Desfecho.ERRO_TECNICO,
                mensagem_portal=f"exceção inesperada: {erro}",
            )

    def _emitir_com_retentativa_rapida(self, job) -> ResultadoTentativa:
        primeiro = self._emitir_no_adapter(job)
        if not retentativa.deve_retentativa_rapida(primeiro):
            return primeiro

        espera = retentativa.espera_retentativa_rapida(
            tuple(self.orgao.retry.retentativa_bloqueio_s)
        )
        log.warning("micro_retentativa_bloqueio", extra={
            "job": job.job_id,
            "orgao": self.orgao.codigo,
            "documento": job.doc.documento,
            "codigo": retentativa.codigo_portal(primeiro),
            "espera_s": round(espera, 1),
        })
        self._reiniciar_sessao(primeiro.desfecho)
        self.portao.aguardar(espera, self.ctx.parar)
        if self.ctx.parar.is_set():
            return primeiro

        segundo = self._emitir_no_adapter(job)
        return replace(
            segundo,
            mensagem_portal=retentativa.mensagem_com_retentativa(primeiro, segundo),
        )

    def _reiniciar_sessao(self, desfecho: Desfecho) -> None:
        try:
            self.adapter.reiniciar_sessao()
            log.info("sessao_reiniciada_apos_falha",
                     extra={"orgao": self.orgao.codigo,
                            "desfecho": str(desfecho)})
        except Exception:
            log.exception("falha_ao_reiniciar_sessao",
                          extra={"orgao": self.orgao.codigo,
                                 "desfecho": str(desfecho)})

    def _ajustar_ritmo(self, desfecho: Desfecho) -> None:
        if desfecho == Desfecho.CAPTCHA:
            if perfis.captcha_sem_castigo(self.orgao.codigo):
                estado = ritmo.estado(self.conn, self.orgao.codigo, self.orgao.pacing)
                log.info("ritmo_mantido", extra={"orgao": self.orgao.codigo,
                                                 "desfecho": str(desfecho),
                                                 "intervalo_s": round(estado.intervalo_s, 2)})
            else:
                novo = ritmo.registrar_captcha(self.conn, self.orgao.codigo, self.orgao.pacing)
                log.info("ritmo_punido", extra={"orgao": self.orgao.codigo,
                                                "desfecho": str(desfecho),
                                                "intervalo_s": round(novo.intervalo_s, 2)})
        elif desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
            novo = ritmo.registrar_bloqueio_temporario(
                self.conn, self.orgao.codigo, self.orgao.pacing
            )
            log.info("ritmo_punido", extra={"orgao": self.orgao.codigo,
                                            "desfecho": str(desfecho),
                                            "intervalo_s": round(novo.intervalo_s, 2)})
        elif desfecho not in RETENTAVEIS:
            antes = ritmo.estado(self.conn, self.orgao.codigo, self.orgao.pacing)
            novo = ritmo.registrar_sucesso(self.conn, self.orgao.codigo, self.orgao.pacing)
            if novo.intervalo_s < antes.intervalo_s:
                log.info("ritmo_acelerado", extra={"orgao": self.orgao.codigo,
                                                  "intervalo_s": round(novo.intervalo_s, 2)})

        if desfecho in RETENTAVEIS:
            self._reiniciar_sessao(desfecho)

    def _avaliar_breaker(self, desfecho: Desfecho) -> None:
        # Órgão isento nem entra no disjuntor: `pode_despachar` já mantém a
        # linha dele limpa, e avaliar aqui só geraria leitura e escrita para
        # concluir, toda vez, que não há nada a fazer.
        if not breaker.ativo_para(self.orgao.codigo, self.orgao.breaker):
            return

        antes = breaker.consultar(self.conn, self.orgao.codigo)
        depois = breaker.avaliar(self.conn, self.orgao.codigo, desfecho, self.orgao.breaker)

        if depois.estado == breaker.ABERTO and antes.estado != breaker.ABERTO:
            log.warning("breaker_aberto", extra={"orgao": self.orgao.codigo,
                                                 "motivo": depois.motivo,
                                                 "ate": depois.aberto_ate})
            alertas.abrir_incidente(
                self.ctx.cfg.alertas,
                f"breaker:{self.orgao.codigo}",
                f"{self.orgao.codigo} suspenso",
                "O portal recusou várias consultas seguidas.",
                acao="Nada. O robô retoma sozinho na hora indicada.\n"
                     "Se isso se repetir mais de 3 vezes no mesmo dia, avise "
                     "para revermos o ritmo das consultas.",
                dados={
                    "Motivo": depois.motivo or "-",
                    "Retoma às": (depois.aberto_ate or "-")[11:19],
                    "Ocorrência": f"{depois.aberturas}ª",
                },
                severidade="aviso",
            )
        elif depois.estado == breaker.FECHADO and antes.estado != breaker.FECHADO:
            log.info("breaker_fechado", extra={"orgao": self.orgao.codigo})
            alertas.fechar_incidente(
                self.ctx.cfg.alertas,
                f"breaker:{self.orgao.codigo}",
                f"{self.orgao.codigo} normalizado",
                "As consultas voltaram a ser aceitas pelo portal.",
                severidade="ok",
            )

    def _encerrar_job(self, job, resultado: ResultadoTentativa) -> None:
        if resultado.desfecho in CONCLUSIVOS:
            fila.concluir(self.conn, job, resultado)
            return

        proxima = job.tentativas + 1
        if proxima >= self.orgao.retry.max_tentativas:
            fila.falhar(self.conn, job, resultado.desfecho)
            log.warning("job_falhou", extra={"job": job.job_id,
                                             "documento": job.doc.documento,
                                             "desfecho": str(resultado.desfecho)})
            return

        espera = self._espera_retry(resultado.desfecho, proxima)
        fila.reagendar(self.conn, job, resultado.desfecho, espera)
        log.info("job_reagendado", extra={"job": job.job_id,
                                          "desfecho": str(resultado.desfecho),
                                          "em_s": espera})

    def _espera_retry(self, desfecho: Desfecho, tentativa: int) -> float:
        # Piso que não depende do config: máquina já instalada continua com o
        # backoff antigo de captcha (horas) no config.toml dela, e esperar
        # horas por um erro de leitura nosso seria parar o lote à toa.
        if (desfecho == Desfecho.CAPTCHA
                and perfis.captcha_sem_castigo(self.orgao.codigo)):
            return 0.0
        return self.orgao.retry.espera(desfecho, tentativa)

    def _bater_ponto(self) -> None:
        if not self.principal:
            return
        agora = time.monotonic()
        if agora - self._ultimo_heartbeat >= INTERVALO_HEARTBEAT_S:
            heartbeat.bater(self.conn)
            self._ultimo_heartbeat = agora
