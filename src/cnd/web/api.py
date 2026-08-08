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

from cnd.core import breaker, tempo
from cnd.infra import heartbeat
from cnd.infra.config import Config
from cnd.web import consultas

# Quantas linhas de "o que o robô acabou de fazer" cada máquina devolve.
# Suficiente para ver o ritmo sem transformar a resposta num relatório.
ITENS_DE_ATIVIDADE = 6


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
    def estado():
        """Panorama da máquina: robô, lote atual e situação de cada órgão."""
        cfg = obter_config()
        with contextlib.closing(abrir_leitura()) as conn:
            idade = heartbeat.segundos_desde(conn, "orquestrador")
            lotes = consultas.lotes(conn)
            lote_id = lotes[0]["id"] if lotes else None

            orgaos = []
            for codigo in consultas.orgaos_do_lote(conn, lote_id):
                resumo = consultas.resumo(conn, codigo, lote_id)
                estado_breaker = breaker.consultar(conn, codigo)
                orgaos.append({
                    "orgao": codigo,
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
                    "eta_horas": consultas.eta_horas(resumo),
                })

            return {
                "maquina": cfg.rede.nome or platform.node(),
                "agora": tempo.agora_iso(),
                "robo_ativo": (idade is not None
                               and idade <= cfg.alertas.heartbeat_timeout_s),
                "ultimo_sinal_ha_s": round(idade) if idade is not None else None,
                "lote_id": lote_id,
                "lote_nome": (lotes[0]["arquivo_origem"] or lotes[0]["descricao"])
                             if lotes else None,
                # Vai junto do panorama, e não numa rota própria: a tela de
                # máquinas mostra os dois lado a lado, e uma segunda ida à
                # rede dobraria a espera de cada máquina consultada.
                "atividade": consultas.ultimas_tentativas(conn, ITENS_DE_ATIVIDADE),
                "lotes": [{"id": lote["id"], "descricao": lote["descricao"],
                           "arquivo": lote["arquivo_origem"],
                           "itens": lote["jobs"],
                           "criado_em": lote["criado_em"],
                           "encerrado_em": lote["encerrado_em"]}
                          for lote in lotes],
                "orgaos": orgaos,
            }

    @roteador.get("/itens")
    def itens(lote: int | None = None, orgao: str | None = None,
              status: str | None = None, desfecho: str | None = None,
              busca: str | None = None, limite: int = 200):
        with contextlib.closing(abrir_leitura()) as conn:
            linhas = consultas.jobs(conn, lote, orgao, status, desfecho,
                                    busca, min(limite, 1000))
            return [dict(linha) for linha in linhas]

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

    return roteador
