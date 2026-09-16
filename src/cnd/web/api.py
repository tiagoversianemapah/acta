"""API de leitura, para o aplicativo de mesa consultar as máquinas.

Por que HTTP e não um banco compartilhado: cada máquina já roda este
processo web. Falar com elas por rede dispensa servidor de banco, pasta
compartilhada e qualquer instalação nova — e evita o problema clássico de
arquivo SQLite acessado por vários computadores ao mesmo tempo, que a rede
Windows não trava de forma confiável e acaba corrompendo o banco.

Cada máquina continua dona do seu próprio banco. O aplicativo só pergunta.
"""
from __future__ import annotations

import contextlib
import platform
import sqlite3
from collections.abc import Callable

from fastapi import APIRouter

from cnd.core import breaker, controle, recuperacao, tempo
from cnd.infra import heartbeat, maquina
from cnd.infra.config import Config, nome_do_orgao
from cnd.web import consultas, relatorio

# Quantas linhas de "o que o robô acabou de fazer" cada máquina devolve.
# Suficiente para ver o ritmo sem transformar a resposta num relatório.
ITENS_DE_ATIVIDADE = 6
MAX_LINHAS_LOG = 300


def _detalhes_do_breaker(cfg: Config, codigo: str, estado: breaker.EstadoBreaker) -> dict:
    parametros = (
        cfg.orgaos[codigo].breaker if codigo in cfg.orgaos
        else breaker.ParametrosBreaker()
    )
    pausa_s = breaker.cooldown_atual_s(estado, parametros)
    return {
        "disjuntor_aberturas": estado.aberturas,
        "disjuntor_pausa_s": pausa_s,
        "disjuntor_pausa": consultas.rotulo_duracao(pausa_s),
    }


def _breaker_para_exibir(
    cfg: Config, codigo: str, estado: breaker.EstadoBreaker
) -> breaker.EstadoBreaker:
    parametros = (
        cfg.orgaos[codigo].breaker if codigo in cfg.orgaos
        else breaker.ParametrosBreaker()
    )
    if breaker.ativo_para(codigo, parametros):
        return estado
    return breaker.EstadoBreaker(breaker.FECHADO, None, 0, None)


def _robo_ativo_por_sinal(idade: float | None, limite_s: int) -> bool:
    ativo = idade is not None and idade <= limite_s
    if ativo and maquina.processo_robo_rodando() is False:
        return False
    return ativo


def _tail_log(arquivo, linhas: int) -> list[str]:
    with contextlib.suppress(OSError):
        return arquivo.read_text(encoding="utf-8", errors="replace").splitlines()[-linhas:]
    return []


def montar(obter_config: Callable[[], Config],
           abrir_leitura: Callable[[], sqlite3.Connection]) -> APIRouter:
    """Monta as rotas de leitura desta máquina.

    O roteador nasce aqui dentro, e não no módulo: um roteador global
    acumularia as rotas a cada chamada, e a segunda montagem — em teste, ou
    num processo que sirva mais de uma configuração — registraria tudo em
    duplicidade.

    A configuração chega como função, não como valor: as rotas são montadas
    uma vez, na subida, e o valor capturado ali congelaria — inclusive para
    quem trocar a configuração depois, que é exatamente o que um teste faz.

    A senha não é conferida aqui. Ela vale para o painel inteiro e é
    aplicada uma vez só, no middleware de `web/app.py`: proteção espalhada
    por rota é proteção que um dia alguém esquece de repetir numa rota nova.
    """
    roteador = APIRouter(prefix="/api")

    @roteador.get("/estado")
    def estado(lote: int | None = None):
        """Panorama da máquina: robô, lote atual e situação de cada órgão.

        `lote` escolhe de qual planilha são os números. Sem ele, vale a que
        ainda tem fila real — ver consultas.lote_em_foco.
        """
        cfg = obter_config()
        adapter_cego = next(
            (o.adapter for o in cfg.ativos() if o.adapter in {"rfb_cego", "sefaz_es"}),
            "rfb_cego",
        )
        with contextlib.closing(abrir_leitura()) as conn:
            idade = heartbeat.segundos_desde(conn, "orquestrador")
            lotes = consultas.lotes(conn)
            em_foco = consultas.lote_em_foco(conn, lote)
            lote_id = em_foco["id"] if em_foco is not None else None

            orgaos = []
            for codigo in consultas.orgaos_do_lote(conn, lote_id):
                resumo = consultas.resumo(conn, codigo, lote_id)
                # `consultar_leitura`: esta rota abre o banco só para
                # ler, e `consultar` cria a linha do órgão quando ela
                # não existe. Ver breaker.consultar_leitura.
                estado_breaker = _breaker_para_exibir(
                    cfg, codigo, breaker.consultar_leitura(conn, codigo)
                )
                estado_recuperacao = recuperacao.estado(conn, codigo)
                # Estacionada, cancelada ou na frente da fila. Vai por órgão
                # porque é assim que se controla: a mesma planilha tem RFB e
                # CRF, e parar um não pode parar o outro.
                ctrl = (controle.situacao(conn, lote_id, codigo)
                        if lote_id is not None else controle.PADRAO)
                orgaos.append({
                    "orgao": codigo,
                    "situacao_fila": ctrl.situacao,
                    "prioridade": ctrl.prioridade,
                    # Quem conhece o nome de exibição é a máquina que atende
                    # o órgão; o console só repassa o que ela mandar.
                    "rotulo": (cfg.orgaos[codigo].rotulo if codigo in cfg.orgaos
                               else nome_do_orgao(codigo)),
                    "total": resumo.total,
                    "concluidos": resumo.concluidos,
                    "pendentes": resumo.pendentes,
                    "em_execucao": resumo.em_execucao,
                    "falhados": resumo.falhados,
                    "por_desfecho": resumo.por_desfecho,
                    "percentual": round(resumo.percentual, 1),
                    "intervalo_s": resumo.intervalo_s,
                    "por_hora": resumo.ritmo_por_hora,
                    "taxa_bloqueio": resumo.taxa_captcha,
                    "ultima_tentativa": resumo.ultima_tentativa,
                    "disjuntor": estado_breaker.estado,
                    "disjuntor_motivo": estado_breaker.motivo,
                    "disjuntor_ate": estado_breaker.aberto_ate,
                    **_detalhes_do_breaker(cfg, codigo, estado_breaker),
                    # Sem isto a tela mentia por omissão: "99,8% concluído"
                    # e "0 na fila" leem como lote terminado, quando há itens
                    # esperando a próxima rodada. Quem olha precisa saber que
                    # ainda não acabou, e quando volta.
                    "recuperacao_rodadas": estado_recuperacao.rodadas,
                    "recuperacao_em": estado_recuperacao.proxima_em,
                    "eta_horas": consultas.eta_horas(resumo),
                })

            robo_ativo = _robo_ativo_por_sinal(
                idade, cfg.alertas.heartbeat_timeout_s
            )
            return {
                "maquina": cfg.rede.nome or platform.node(),
                "agora": tempo.agora_iso(),
                # Memória, disco e tempo ligada. Disco cheio faz o robô
                # emitir a certidão e não conseguir salvá-la, que é o pior
                # jeito possível de descobrir que faltava espaço.
                "saude": maquina.ler(cfg.pasta_certidoes).como_dicionario(),
                "papel": "robo" if cfg.rede.roda_robo else "console",
                "versao": maquina.versao(),
                "calibragem": maquina.calibragem(
                    cfg.pasta_certidoes.parent / "calibragem", adapter_cego),
                "certidoes": maquina.certidoes(cfg.pasta_certidoes),
                # A própria máquina informa o AnyDesk dela — quem cadastrou
                # foi quem estava na frente, na hora de instalar.
                "anydesk": cfg.rede.anydesk,
                "robo_ativo": robo_ativo,
                "ultimo_sinal_ha_s": round(idade) if idade is not None else None,
                "lote_id": lote_id,
                "lote_nome": (em_foco["arquivo_origem"] or em_foco["descricao"])
                             if em_foco is not None else None,
                # Qual planilha o robô está emitindo AGORA, independente da
                # que a tela mostra: é o que permite avisar quem está
                # olhando um recorte parado que o trabalho corre em outro.
                "lote_em_execucao": consultas.lote_em_execucao(conn),
                # Vai junto do panorama, e não numa rota própria: a tela de
                # máquinas mostra os dois lado a lado, e uma segunda ida à
                # rede dobraria a espera de cada máquina consultada.
                "atividade": consultas.ultimas_tentativas(
                    conn, ITENS_DE_ATIVIDADE, lote_id),
                # Os meses com certidão guardada. O pacote é entregue por
                # mês, não por lote — cada máquina numera os lotes por conta,
                # e "lote 7" não quer dizer nada fora dela.
                "meses": relatorio.meses_com_certidao(conn),
                "lotes": [{"id": lote["id"], "descricao": lote["descricao"],
                           "arquivo": lote["arquivo_origem"],
                           "itens": lote["jobs"],
                           "criado_em": lote["criado_em"],
                           "encerrado_em": lote["encerrado_em"]}
                          for lote in lotes],
                "orgaos": orgaos,
            }

    @roteador.get("/fila/mes")
    def fila_no_mes(orgao: str, mes: str | None = None):
        """Resumo usado antes de reimportar uma aba para o mesmo órgão."""
        with contextlib.closing(abrir_leitura()) as conn:
            return consultas.ja_na_fila_no_mes(conn, orgao, mes)

    @roteador.get("/orgaos/mes")
    def orgaos_do_mes(mes: str | None = None):
        """Automações com itens no mês, para montar o recorte da entrega."""
        cfg = obter_config()
        mes = mes or tempo.agora_iso()[:7]
        with contextlib.closing(abrir_leitura()) as conn:
            resultado = []
            for codigo in consultas.orgaos_do_lote(conn, None, mes):
                resumo = consultas.resumo(conn, codigo, None, mes)
                resultado.append({
                    "orgao": codigo,
                    "rotulo": (
                        cfg.orgaos[codigo].rotulo if codigo in cfg.orgaos
                        else nome_do_orgao(codigo)
                    ),
                    "total": resumo.total,
                })
            return resultado

    @roteador.get("/itens")
    def itens(lote: int | None = None, lote_id: int | None = None,
              orgao: str | None = None,
              status: str | None = None, desfecho: str | None = None,
              busca: str | None = None, limite: int = 200,
              job_id: int | None = None, offset: int = 0,
              incluir_total: bool = False):
        lote_escolhido = lote if lote is not None else lote_id
        with contextlib.closing(abrir_leitura()) as conn:
            linhas = consultas.jobs(conn, lote_escolhido, orgao, status, desfecho,
                                    busca, min(limite, 1000), job_id=job_id,
                                    offset=max(offset, 0))
            itens = [dict(linha) for linha in linhas]
            if not incluir_total:
                return itens
            return {
                "total": consultas.contar_jobs(
                    conn, lote_escolhido, orgao, status, desfecho, busca,
                    job_id=job_id
                ),
                "itens": itens,
            }

    @roteador.get("/tentativas/{job_id}")
    def tentativas(job_id: int):
        """Histórico de um item — o que foi tentado, quando e com que resultado."""
        with contextlib.closing(abrir_leitura()) as conn:
            return [dict(linha) for linha in
                    consultas.tentativas_do_job(conn, job_id)]

    @roteador.get("/historico")
    def historico(dias: int = 7):
        """Tentativas por hora do dia — para ver quando a máquina trabalhou
        e quando o portal recusou."""
        with contextlib.closing(abrir_leitura()) as conn:
            return {orgao: consultas.captcha_por_hora(conn, orgao, dias)
                    for orgao in consultas.orgaos_do_lote(conn, None)}

    @roteador.get("/diagnostico")
    def diagnostico(dias: int = 7):
        """Diagnóstico operacional usado pela aba web e pelo console."""
        with contextlib.closing(abrir_leitura()) as conn:
            return {
                "dias": dias,
                "agora": tempo.agora_iso(),
                "orgaos": [
                    consultas.diagnostico_orgao(conn, orgao, dias)
                    for orgao in consultas.orgaos_do_lote(conn, None)
                ],
            }

    @roteador.get("/logs")
    def logs(linhas: int = 80):
        cfg = obter_config()
        limite = min(max(int(linhas or 80), 1), MAX_LINHAS_LOG)
        arquivos = []
        candidatos = sorted(
            cfg.pasta_logs.glob("*"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )
        for arquivo in candidatos:
            if arquivo.suffix not in {".jsonl", ".log"}:
                continue
            arquivos.append({
                "arquivo": arquivo.name,
                "linhas": _tail_log(arquivo, limite),
            })
            if len(arquivos) >= 5:
                break
        return {"linhas": limite, "arquivos": arquivos}

    return roteador
