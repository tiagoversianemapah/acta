"""O orquestrador: quem sobe, vigia e derruba o robô.

Este arquivo é o ciclo de VIDA do processo — a conferência de pré-voo
(nenhum outro orquestrador no ar, disco suficiente), a criação dos threads,
o laço que decide quando encerrar, e o desligamento limpo. O trabalho em si
mora ao lado:

    worker.py       tira o job da fila e entrega ao adapter
    vigia.py        transforma em e-mail o que precisa de gente
    retentativa.py  a segunda chance depois de um bloqueio temporário
    vigilancia.py   as perguntas puras que o vigia faz ao banco

A regra que mantém isso arrumado: aqui não entra nada que saiba o que é um
job. Se a função precisa saber, o lugar dela é em outro desses arquivos.
"""
from __future__ import annotations

import contextlib
import signal
import threading
import time

from cnd.core import fila
from cnd.infra import heartbeat
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.config import carregar as carregar_config
from cnd.infra.db import caminho_parada_manual, caminho_pedido_parada, conectar
from cnd.infra.log import configurar as configurar_log
from cnd.infra.log import obter
from cnd.orquestrador import vigia as vigia_mod
from cnd.orquestrador.vigia import SEGUNDOS_PARA_CONSIDERAR_VIVO
from cnd.orquestrador.worker import Contexto, PortaoDeRitmo, Worker

log = obter("orquestrador")

# Carência antes de o robô poder desistir por falta de trabalho. Quem
# aperta "Iniciar robô" numa máquina sem planilha veria o processo subir e
# morrer no mesmo segundo, e a tela voltaria ao botão como se o clique não
# tivesse funcionado. Meio minuto dá tempo de a importação de uma planilha
# recém-enviada aparecer no banco e, quando não há nada mesmo, de o painel
# registrar que ele rodou e encerrou.
CARENCIA_ANTES_DE_ENCERRAR_S = 30.0


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

    pendentes = consultas.pendentes(conn, (o.codigo for o in cfg.ativos()))
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


def _falhas_a_recuperar(conn, orgao: ConfigOrgao) -> int:
    """Falhas que o vigia ainda vai devolver para a fila.

    Com a recuperação ligada, item FAILED não é assunto encerrado: por
    decisão de operação (17/08/2026) o robô nunca desiste de item sem
    resposta do portal — ele reenfileira de 30min em 30min, dobrando até
    6h. Enquanto houver uma dessas, ainda há trabalho, mesmo com a fila
    zerada. Desligada a recuperação, ninguém mais mexe nelas e elas param
    de contar. Ver core/recuperacao.py.
    """
    if not orgao.recuperacao.ativa:
        return 0
    return fila.falhas_em_filas_ativas(conn, orgao.codigo)


def _nada_a_fazer(conn, ativos: list[ConfigOrgao]) -> bool:
    """Acabou de verdade: nenhum item pendente e nenhuma falha a recuperar."""
    return not any(
        fila.ha_trabalho(conn, o.codigo) or _falhas_a_recuperar(conn, o)
        for o in ativos
    )


def executar(cfg: Config | None = None, ate_esvaziar: bool = False,
             limite: int | None = None, forcar: bool = False) -> None:
    """Sobe o orquestrador. Bloqueia até acabar o trabalho ou até Ctrl+C.

    O robô encerra sozinho quando não sobra nada a fazer — nem item na
    fila, nem falha esperando nova rodada de recuperação. Antes ele ficava
    de pé indefinidamente depois do último CNPJ, segurando o navegador e
    aparecendo como "ocioso" na tela; quem olhava não sabia dizer se o
    lote tinha terminado ou se ele havia travado. Planilha nova sobe o
    robô de novo (ver web/comandos.iniciar_robo_da_maquina).

    `ate_esvaziar` encerra assim que a fila zera, sem esperar a
    recuperação das falhas — é o modo dos testes.
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

    vigia = vigia_mod.Vigia(cfg, ativos, vez_da_tela=ctx.vez_da_tela)
    # A carência conta do início, e não da última consulta: o caso que ela
    # protege é justamente o do robô que sobe sem nada para fazer.
    pode_encerrar_em = time.monotonic() + CARENCIA_ANTES_DE_ENCERRAR_S
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
                continue
            # Depois do vigia, e não antes: é ele quem devolve as falhas
            # para a fila, e perguntar antes veria vazio o que ele estava
            # prestes a reabastecer.
            if time.monotonic() >= pode_encerrar_em and _nada_a_fazer(conn, ativos):
                log.info("trabalho_concluido_encerrando", extra={
                    "orgaos": [o.codigo for o in ativos],
                })
                parar.set()
    except KeyboardInterrupt:
        parar.set()

    for thread in threads:
        thread.join(timeout=30)
    conn.close()
    log.info("orquestrador_parado")
