"""Preparação da aba Diagnóstico.

Este módulo fica entre o banco/API e o template. A rota web só escolhe a
máquina; aqui entram a formatação, o fallback sem dados e o texto de insight.
Assim a tela não precisa conhecer regra de negócio, e o `app.py` não vira um
arquivo onde qualquer ajuste visual puxa meia aplicação junto.
"""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from datetime import timedelta

from cnd.core import tempo
from cnd.desktop import remoto
from cnd.web import consultas

DIAS_PADRAO = 7
DIAS_MAXIMOS = 30


def normalizar_dias(dias: int | None) -> int:
    return min(max(dias or DIAS_PADRAO, 1), DIAS_MAXIMOS)


def numero_pt(valor: int | float | None, casas: int = 0) -> str:
    if valor is None:
        return "0"
    texto = f"{float(valor):,.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def periodo(dias: int) -> str:
    fim = tempo.agora().astimezone()
    inicio = fim - timedelta(days=dias)
    return f"{inicio:%d/%m/%Y} - {fim:%d/%m/%Y}"


def vazio(orgao: str = "RFB_PJ") -> dict:
    return {
        "orgao": orgao,
        "consultas": 0,
        "erros": 0,
        "taxa_erro": 0.0,
        "bloqueios": 0,
        "captchas": 0,
        "tecnicos": 0,
        "insuficientes": 0,
        "serie": [
            {
                "hora": f"{hora:02d}",
                "total": 0,
                "captchas": 0,
                "bloqueios": 0,
                "recusas": 0,
                "insuficientes": 0,
                "erros": 0,
                "erros_operacionais": 0,
                "taxa_erro": 0.0,
                "altura": 0,
                "marcar": hora % 2 == 0,
            }
            for hora in range(24)
        ],
        "eixo": [5, 4, 3, 2, 1, 0],
        "horas": [],
        "pior_hora": None,
        "melhores": [],
        "principais_erros": [],
    }


def preparar(diag: dict) -> dict:
    preparado = dict(diag)
    preparado["consultas_fmt"] = numero_pt(preparado.get("consultas", 0))
    preparado["erros_fmt"] = numero_pt(preparado.get("erros", 0))
    preparado["taxa_erro_fmt"] = numero_pt(
        float(preparado.get("taxa_erro") or 0.0) * 100, 1
    )
    preparado["bloqueios_fmt"] = numero_pt(preparado.get("bloqueios", 0))
    preparado["insuficientes_fmt"] = numero_pt(preparado.get("insuficientes", 0))
    preparado["tem_dados"] = bool(preparado.get("consultas"))

    for linha in preparado.get("serie", []):
        linha["taxa_erro_fmt"] = numero_pt(
            float(linha.get("taxa_erro") or 0) * 100, 1
        )
    for linha in preparado.get("horas", []):
        linha["taxa_erro_fmt"] = numero_pt(
            float(linha.get("taxa_erro") or 0) * 100, 1
        )
        linha["erro_alto"] = bool(
            preparado.get("pior_hora")
            and linha.get("hora") == preparado["pior_hora"].get("hora")
            and linha.get("erros_operacionais")
        )
    return preparado


def insight(diag: dict) -> list[str]:
    if not diag.get("consultas"):
        return [
            "Ainda não há amostra suficiente para apontar um padrão de falhas.",
            "Quando o robô processar algumas consultas, esta área passa a "
            "indicar os horários mais estáveis.",
        ]

    pior = diag.get("pior_hora")
    if not pior or not pior.get("erros_operacionais"):
        melhores = diag.get("melhores", [])
        horarios = (
            ", ".join(f"{h['hora']}:00" for h in melhores) or "o período atual"
        )
        return [
            "Não houve concentração relevante de erro operacional no "
            "período analisado.",
            f"Os horários com melhor comportamento até agora são: {horarios}.",
        ]

    hora = pior.get("hora", "--")
    taxa = numero_pt(float(pior.get("taxa_erro") or 0) * 100, 1)
    return [
        f"O horário de {hora}:00 concentrou a maior taxa de erro no período analisado.",
        f"A taxa desse intervalo ficou em {taxa}% considerando erros técnicos, "
        "captcha e bloqueios.",
        "Recomendação: acompanhar esse intervalo e, se repetir, rodar fora dele "
        "ou revisar captcha/bloqueios.",
    ]


def coletar_local(
    abrir_leitura: Callable[[], sqlite3.Connection], dias: int
) -> list[dict]:
    with contextlib.closing(abrir_leitura()) as conn:
        return [
            consultas.diagnostico_orgao(conn, orgao, dias)
            for orgao in consultas.orgaos_do_lote(conn, None)
        ]


def coletar_da_maquina(
    selecionada: remoto.EstadoRemoto | None,
    dias: int,
    abrir_leitura: Callable[[], sqlite3.Connection],
    senha: str,
) -> tuple[list[dict], str | None]:
    if not selecionada or selecionada.local:
        return coletar_local(abrir_leitura, dias), None

    dados = remoto.diagnostico(selecionada.maquina, senha, dias)
    if dados is None:
        return [], f"Não consegui ler o diagnóstico de {selecionada.rotulo}."
    return list(dados.get("orgaos", [])), None


def contexto(
    estados: list[remoto.EstadoRemoto],
    selecionada: remoto.EstadoRemoto | None,
    selecionada_idx: int | None,
    diagnosticos: list[dict],
    erro: str | None,
    dias: int,
) -> dict:
    maquina_param = selecionada_idx if selecionada_idx is not None else ""
    principal = preparar(diagnosticos[0] if diagnosticos else vazio())
    agora = tempo.agora().astimezone()
    return {
        "estados": estados,
        "selecionada": selecionada,
        "selecionada_idx": selecionada_idx,
        "diagnostico": principal,
        "diagnosticos": [preparar(d) for d in diagnosticos],
        "insight": insight(principal),
        "periodo": periodo(dias),
        "dias": dias,
        "erro_diagnostico": erro,
        "exportar_url": f"/diagnostico.json?maquina={maquina_param}&dias={dias}",
        "ultima_atualizacao": f"hoje às {agora:%H:%M}",
    }
