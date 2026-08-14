"""O orquestrador: quem decide quando e o quê executar.

Um thread por worker, um worker (ou mais) por órgão. Cada thread tem a
própria conexão com o banco — conexões SQLite não são compartilháveis
entre threads.

O que este arquivo faz, em uma frase: tira um job da fila, respeita o
ritmo, entrega ao adapter, e traduz o resultado em transição de estado —
sem saber absolutamente nada sobre navegador ou sobre o site do órgão.
"""
from __future__ import annotations

import contextlib
import random
import re
import signal
import threading
import time
from dataclasses import dataclass, field, replace

from cnd.adapters.base import AdapterOrgao
from cnd.adapters.base import carregar as carregar_adapter
from cnd.core import breaker, fila, ritmo, tempo
from cnd.core.modelos import CONCLUSIVOS, RETENTAVEIS, Desfecho, ResultadoTentativa
from cnd.infra import alertas, heartbeat
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.config import carregar as carregar_config
from cnd.infra.db import caminho_parada_manual, caminho_pedido_parada, conectar
from cnd.infra.log import configurar as configurar_log
from cnd.infra.log import obter
from cnd.orquestrador import vigilancia

log = obter("orquestrador")

PAUSA_SEM_TRABALHO_S = 5.0
PAUSA_BREAKER_ABERTO_S = 15.0
PAUSA_FORA_DA_JANELA_S = 60.0
INTERVALO_HEARTBEAT_S = 60.0
RE_CODIGO_PORTAL = re.compile(r"\b(001|023|033|106)\b")


def _codigo_portal(resultado: ResultadoTentativa) -> str | None:
    texto = resultado.mensagem_portal or ""
    achado = RE_CODIGO_PORTAL.search(texto)
    return achado.group(1) if achado else None


def _deve_retentativa_rapida(resultado: ResultadoTentativa) -> bool:
    if resultado.desfecho != Desfecho.BLOQUEIO_TEMPORARIO:
        return False
    return _codigo_portal(resultado) in {"023", "106"}


def _espera_retentativa_rapida(valores: tuple[float, ...]) -> float:
    if not valores:
        return 0.0
    faixa = [max(0.0, float(valor)) for valor in valores]
    if len(faixa) == 1:
        return faixa[0]
    inicio, fim = sorted((faixa[0], faixa[1]))
    if fim <= inicio:
        return inicio
    return random.uniform(inicio, fim)


def _mensagem_com_retentativa(
    primeiro: ResultadoTentativa, segundo: ResultadoTentativa
) -> str | None:
    segunda = " ".join((segundo.mensagem_portal or "").split())
    primeira = " ".join((primeiro.mensagem_portal or "").split())
    codigo = _codigo_portal(primeiro)
    detalhe = "micro-retentativa apos bloqueio temporario"
    if codigo:
        detalhe += f" {codigo}"
    if primeira:
        detalhe += f"; primeira resposta: {primeira[:250]}"
    mensagem = "; ".join(parte for parte in (segunda, detalhe) if parte)
    return mensagem[:500] if mensagem else None


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

    # ------------------------------------------------------------------
    def run(self) -> None:
        self.conn = conectar(self.ctx.cfg.banco)
        try:
            self.adapter = carregar_adapter(self.orgao, self.ctx.cfg)
            self.adapter.preparar()
        except Exception as erro:
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
            self.ctx.parar.wait(PAUSA_FORA_DA_JANELA_S)
            return

        if not breaker.pode_despachar(self.conn, self.orgao.codigo):
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
            self.ctx.parar.wait(PAUSA_BREAKER_ABERTO_S)
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

        self._ajustar_ritmo(resultado.desfecho)
        self._avaliar_breaker(resultado.desfecho)
        self._encerrar_job(job, resultado)

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
        if not _deve_retentativa_rapida(primeiro):
            return primeiro

        espera = _espera_retentativa_rapida(
            tuple(self.orgao.retry.retentativa_bloqueio_s)
        )
        log.warning("micro_retentativa_bloqueio", extra={
            "job": job.job_id,
            "orgao": self.orgao.codigo,
            "documento": job.doc.documento,
            "codigo": _codigo_portal(primeiro),
            "espera_s": round(espera, 1),
        })
        self._reiniciar_sessao(primeiro.desfecho)
        self.portao.aguardar(espera, self.ctx.parar)
        if self.ctx.parar.is_set():
            return primeiro

        segundo = self._emitir_no_adapter(job)
        return replace(
            segundo,
            mensagem_portal=_mensagem_com_retentativa(primeiro, segundo),
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

        espera = self.orgao.retry.espera(resultado.desfecho, proxima)
        fila.reagendar(self.conn, job, resultado.desfecho, espera)
        log.info("job_reagendado", extra={"job": job.job_id,
                                          "desfecho": str(resultado.desfecho),
                                          "em_s": espera})

    def _bater_ponto(self) -> None:
        if not self.principal:
            return
        agora = time.monotonic()
        if agora - self._ultimo_heartbeat >= INTERVALO_HEARTBEAT_S:
            heartbeat.bater(self.conn)
            self._ultimo_heartbeat = agora


# ----------------------------------------------------------------------
SEGUNDOS_PARA_CONSIDERAR_VIVO = 150
INTERVALO_VIGILANCIA_S = 60.0


class Vigia:
    """Observa o lote e transforma em e-mail o que precisa de gente.

    Roda no laço principal, fora dos workers: assim continua olhando mesmo
    quando todos os órgãos estão parados — que é justamente quando alguém
    precisa ser avisado.
    """

    def __init__(self, cfg: Config, orgaos: list[ConfigOrgao]) -> None:
        self.cfg = cfg
        self.orgaos = orgaos
        self._proxima = 0.0
        self._falhas_vistas_ate = tempo.agora_iso()

    def rodada(self, conn) -> None:
        if time.monotonic() < self._proxima:
            return
        self._proxima = time.monotonic() + INTERVALO_VIGILANCIA_S
        try:
            self._avisar_lotes_concluidos(conn)
            for orgao in self.orgaos:
                self._avisar_travamento(conn, orgao)
            self._avisar_falhas_definitivas(conn)
        except Exception:
            log.exception("falha_na_vigilancia")

    # ------------------------------------------------------------------
    def _avisar_lotes_concluidos(self, conn) -> None:
        for resumo in vigilancia.lotes_recem_concluidos(conn):
            log.info("lote_concluido", extra={"lote": resumo.lote_id,
                                              "total": resumo.total})
            alertas.enviar(
                self.cfg.alertas,
                f"Lote #{resumo.lote_id} concluído",
                f"{resumo.com_certidao} certidões obtidas de {resumo.total} "
                f"empresas.",
                acao=self._acao_do_lote(resumo),
                dados=resumo.como_campos(),
                acoes=self._links_do_lote(resumo.lote_id),
                severidade="aviso" if (resumo.falhados or resumo.sem_certidao)
                            else "ok",
            )

    def _acao_do_lote(self, resumo) -> str:
        """O que a pessoa faz agora que o lote terminou."""
        passos = ["1. Baixar a planilha e os PDFs pelos botões abaixo.",
                  "2. Enviar as certidões aos clientes."]
        proximo = 3
        if resumo.sem_certidao:
            passos.append(f"{proximo}. Tratar as {resumo.sem_certidao} empresas "
                          f"sem certidão (abas Positivas e Pendencia manual "
                          f"da planilha) — essas exigem regularização ou "
                          f"atendimento no e-CAC.")
            proximo += 1
        if resumo.falhados:
            passos.append(f"{proximo}. Conferir os {resumo.falhados} itens que "
                          f"não concluíram (aba Erros) e reenviar pelo painel "
                          f"se for problema passageiro.")
        return "\n".join(passos)

    def _links_do_lote(self, lote_id: int) -> list[tuple[str, str]]:
        """Botões para baixar a planilha e os PDFs.

        O Teams não aceita anexo, e o pacote de PDFs (~150 MB num lote
        completo) não caberia em e-mail de qualquer forma. Link tem outra
        vantagem: o arquivo é gerado no clique, sempre atualizado — anexo
        congela no momento do envio.
        """
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        if not base:
            return []
        # A planilha é do lote (o que aquela importação produziu); o pacote
        # de certidões é do mês, que é o corte que o cliente recebe.
        mes = tempo.agora_iso()[:7]
        return [("Baixar planilha", f"{base}/relatorio/{mes}.xlsx"),
                ("Baixar certidões do mês", f"{base}/certidoes/{mes}.zip"),
                ("Abrir painel", base)]

    def _avisar_travamento(self, conn, orgao: ConfigOrgao) -> None:
        """Fila com trabalho e nada concluindo = travado, mesmo com o
        processo vivo. O heartbeat sozinho não pega este caso."""
        parado_ha = vigilancia.minutos_sem_progresso(conn, orgao.codigo)
        chave = f"travado:{orgao.codigo}"

        if parado_ha is None or parado_ha < vigilancia.MINUTOS_SEM_PROGRESSO:
            alertas.fechar_incidente(
                self.cfg.alertas, chave,
                f"{orgao.codigo} normalizado",
                "O robô voltou a concluir consultas.",
                severidade="ok",
            )
            return

        estado = breaker.consultar(conn, orgao.codigo)
        log.warning("sem_progresso", extra={"orgao": orgao.codigo,
                                            "minutos": round(parado_ha)})

        if estado.estado == breaker.FECHADO:
            o_que_fazer = (
                "Acessar o servidor e verificar, nesta ordem:\n"
                "1. A janela do Edge está aberta e maximizada?\n"
                "2. Alguém está usando o computador? O robô precisa da tela "
                "só para ele.\n"
                "3. Há alguma janela por cima do navegador?\n"
                "Se tudo estiver certo, reiniciar com `cnd rodar --forcar`."
            )
        else:
            o_que_fazer = ("Nada agora — o robô está de castigo e retoma "
                           "sozinho. Se passar de 2h assim, avise.")

        pendentes = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE orgao = ? AND status IN "
            "('PENDING','RETRY_WAIT')", (orgao.codigo,)).fetchone()["n"]

        alertas.abrir_incidente(
            self.cfg.alertas, chave,
            f"{orgao.codigo} parado há {parado_ha:.0f} min",
            "Há itens na fila, mas nenhuma consulta conclui.",
            acao=o_que_fazer,
            dados={
                "Itens esperando": f"{pendentes:,}".replace(",", "."),
                "Disjuntor": ("fechado (deveria estar trabalhando)"
                              if estado.estado == breaker.FECHADO
                              else f"{estado.estado.lower()}"),
                **({"Motivo da pausa": estado.motivo} if estado.motivo else {}),
                **({"Retoma às": estado.aberto_ate[11:19]}
                   if estado.aberto_ate else {}),
            },
            acoes=self._link_painel(),
            severidade="erro",
        )

    def _link_painel(self) -> list[tuple[str, str]]:
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        return [("Abrir painel", base)] if base else []

    def _avisar_falhas_definitivas(self, conn) -> None:
        """Resumo, não um e-mail por item: 200 avisos de falha viram ruído,
        e ruído faz a caixa de entrada ser ignorada no dia que importa."""
        marca = self._falhas_vistas_ate
        novas: list = []
        for orgao in self.orgaos:
            novas.extend(vigilancia.falhas_definitivas(conn, orgao.codigo, marca))

        if not novas:
            return

        self._falhas_vistas_ate = tempo.agora_iso()
        log.warning("falhas_definitivas", extra={"quantidade": len(novas)})
        alertas.enviar(
            self.cfg.alertas,
            f"{len(novas)} item(ns) não concluíram",
            "Tentaram 3 vezes e o portal não devolveu certidão.",
            acao="1. Abrir o painel em Itens > filtrar Situação: Falhou.\n"
                 "2. Conferir a coluna Retorno do portal — normalmente é "
                 "empresa que exige atendimento no e-CAC.\n"
                 "3. Se for problema passageiro, clicar em Reenviar itens "
                 "com falha.",
            dados=vigilancia.campos_das_falhas(novas),
            acoes=self._link_das_falhas(),
            severidade="aviso",
            chave="falhas",
        )

    def _link_das_falhas(self) -> list[tuple[str, str]]:
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        return [("Ver no painel", f"{base}/jobs?status=FAILED")] if base else []


def _ja_existe_outro(conn, forcar: bool) -> bool:
    """Impede dois orquestradores ao mesmo tempo.

    Dois processos sobre a mesma fila brigam de um jeito difícil de
    diagnosticar: um falha, abre o disjuntor, e o outro — que estaria
    funcionando — para de despachar obedecendo um freio que não era dele.
    Aconteceu de verdade em 07/08/2026, com seis processos esquecidos.
    """
    idade = heartbeat.segundos_desde(conn)
    if idade is None or idade > SEGUNDOS_PARA_CONSIDERAR_VIVO or forcar:
        return False

    log.error("ja_existe_orquestrador", extra={"ultimo_sinal_ha_s": round(idade)})
    print(
        f"\n  JÁ EXISTE UM ORQUESTRADOR RODANDO "
        f"(deu sinal de vida há {idade:.0f}s).\n"
        f"  Dois ao mesmo tempo brigam pela mesma fila.\n\n"
        f"  Feche o outro, ou use --forcar se tiver certeza de que ele morreu.\n"
    )
    return True


# Margem sobre o tamanho estimado do lote. Certidão que sai do portal e não
# encontra espaço é consulta gasta e documento perdido — vale pedir o dobro.
FOLGA_DE_DISCO = 2.0


def _conferir_espaco(conn, cfg: Config) -> str:
    """Recusa começar quando o lote não cabe no disco.

    Verificação de PRÉ-VOO, e não alarme depois do fato: disco cheio no meio
    do lote faz o robô emitir a certidão no portal e não conseguir salvar o
    PDF. A consulta foi gasta, o portal já contou aquela emissão, e o
    arquivo não existe. Avisar nesse ponto seria relatar um prejuízo.

    O tamanho médio vem dos PDFs que o próprio robô já baixou; sem nenhum
    ainda, não há como estimar e ele começa — errar para o lado de deixar
    trabalhar é melhor que travar por uma conta que não dá para fazer.
    """
    from cnd.infra import maquina
    from cnd.web import consultas

    pendentes = consultas.pendentes(conn)
    if not pendentes:
        return ""

    certidoes = maquina.certidoes(cfg.pasta_certidoes)
    media_kb = certidoes.get("media_kb") or 0
    if not media_kb:
        return ""

    livre_mb = maquina.ler(cfg.pasta_certidoes).disco_livre_gb * 1024
    precisa_mb = pendentes * media_kb / 1024
    if livre_mb >= precisa_mb * FOLGA_DE_DISCO:
        return ""
    return (f"{pendentes} itens na fila precisam de ~{precisa_mb:.0f} MB e o "
            f"disco tem {livre_mb:.0f} MB livres. Libere espaco antes de "
            f"comecar: certidao emitida sem onde salvar e consulta perdida.")


def _consumir_pedido_de_parada(cfg: Config) -> bool:
    """True quando o painel remoto pediu para encerrar o robô."""
    pedido = caminho_pedido_parada(cfg.banco)
    if not pedido.exists():
        return False
    try:
        pedido.unlink()
    except OSError:
        log.warning("pedido_de_parada_nao_removido", extra={"arquivo": str(pedido)})
    return True


def executar(cfg: Config | None = None, ate_esvaziar: bool = False,
             limite: int | None = None, forcar: bool = False) -> None:
    """Sobe o orquestrador. Bloqueia até Ctrl+C.

    `ate_esvaziar` encerra quando a fila zera (teste).
    `limite` para depois de N jobs — é o modo piloto do roadmap, para medir
    a reação do portal com pouco a perder.
    """
    cfg = cfg or carregar_config()
    configurar_log(cfg.pasta_logs, "orquestrador")

    conn = conectar(cfg.banco)
    if _ja_existe_outro(conn, forcar):
        conn.close()
        return

    if _consumir_pedido_de_parada(cfg):
        log.info("pedido_de_parada_antigo_descartado")
    with contextlib.suppress(OSError):
        caminho_parada_manual(cfg.banco).unlink()

    if problema := _conferir_espaco(conn, cfg):
        log.error("disco_insuficiente", extra={"detalhe": problema})
        print(f"\n  NAO INICIADO: {problema}\n")
        conn.close()
        return

    orfaos = fila.recuperar_orfaos(conn)
    heartbeat.bater(conn)
    if orfaos:
        log.warning("orfaos_recuperados", extra={"quantidade": orfaos})

    ativos = cfg.ativos()
    if not ativos:
        log.error("nenhum_orgao_ativo")
        conn.close()
        return

    parar = threading.Event()
    ctx = Contexto(cfg=cfg, parar=parar, limite=limite)

    def encerrar(*_):
        log.info("encerrando")
        parar.set()

    try:
        signal.signal(signal.SIGINT, encerrar)
        signal.signal(signal.SIGTERM, encerrar)
    except ValueError:
        pass  # fora da thread principal (ex.: em teste)

    threads: list[Worker] = []
    for orgao in ativos:
        portao = PortaoDeRitmo()
        for numero in range(orgao.workers):
            worker = Worker(ctx, orgao, numero, portao, principal=(numero == 0))
            worker.start()
            threads.append(worker)

    log.info("orquestrador_no_ar", extra={
        "orgaos": [o.codigo for o in ativos],
        "workers": len(threads),
    })

    vigia = Vigia(cfg, ativos)
    try:
        while not parar.is_set():
            parar.wait(2.0)
            if _consumir_pedido_de_parada(cfg):
                log.info("parada_pedida_pelo_painel")
                parar.set()
                continue
            vigia.rodada(conn)
            if ate_esvaziar and not any(
                fila.ha_trabalho(conn, o.codigo) for o in ativos
            ):
                log.info("fila_vazia_encerrando")
                parar.set()
    except KeyboardInterrupt:
        parar.set()

    for thread in threads:
        thread.join(timeout=30)
    conn.close()
    log.info("orquestrador_parado")
