"""Prepara a aba Carteira para o painel web."""
from __future__ import annotations

import contextlib
import math
import sqlite3
import urllib.parse
from collections.abc import Callable

from cnd.core import tempo
from cnd.core.documentos import formatar
from cnd.core.modelos import Desfecho, Status
from cnd.desktop import remoto
from cnd.infra.config import Maquina
from cnd.web import consultas, diagnostico

ITENS_POR_PAGINA = 10
STATUS_CARTEIRA = (
    Status.PENDING,
    Status.RUNNING,
    Status.RETRY_WAIT,
    Status.DONE,
    Status.FAILED,
)

ROTULOS_DESFECHO = {
    Desfecho.NEGATIVA: "Negativa",
    Desfecho.CPEN: "Efeito de negativa",
    Desfecho.POSITIVA: "Positiva",
    Desfecho.PENDENCIA_MANUAL: "Informações insuficientes",
    Desfecho.INAPTA: "CNPJ inapto",
    Desfecho.APROVEITADA: "Já emitida no mês",
    Desfecho.BLOQUEIO_TEMPORARIO: "Bloqueio temporário",
    Desfecho.RESULTADO_PENDENTE: "Resultado pendente",
    Desfecho.CAPTCHA: "Captcha",
    Desfecho.ERRO_TECNICO: "Erro técnico",
}

DESFECHOS_OK = frozenset({
    Desfecho.NEGATIVA,
    Desfecho.CPEN,
    Desfecho.APROVEITADA,
})
DESFECHOS_ALERTA = frozenset({
    Desfecho.POSITIVA,
    Desfecho.PENDENCIA_MANUAL,
    Desfecho.INAPTA,
    Desfecho.BLOQUEIO_TEMPORARIO,
    Desfecho.RESULTADO_PENDENTE,
})
DESFECHOS_ERRO = frozenset({Desfecho.CAPTCHA, Desfecho.ERRO_TECNICO})


def normalizar_pagina(pagina: int | None) -> int:
    return max(int(pagina or 1), 1)


def normalizar_limite(limite: int | None) -> int:
    return min(max(int(limite or ITENS_POR_PAGINA), 5), 100)


def _formatar_data(valor: str | None) -> str:
    if not valor:
        return "-"
    try:
        momento = tempo.de_iso(valor).astimezone()
        return f"{momento:%d/%m/%Y %H:%M}"
    except Exception:
        texto = str(valor)
        if len(texto) >= 16:
            return texto[:16].replace("T", " ")
        return texto or "-"


def _filtros(lote_id: int | None, orgao: str | None, status: str | None,
             desfecho: str | None, busca: str | None) -> dict:
    return {
        "lote_id": lote_id,
        "orgao": orgao or None,
        "status": status or None,
        "desfecho": desfecho or None,
        "busca": busca or None,
    }


def _url(base: str, **params: object) -> str:
    limpos = {
        chave: str(valor)
        for chave, valor in params.items()
        if valor not in (None, "")
    }
    consulta = urllib.parse.urlencode(limpos)
    return f"{base}?{consulta}" if consulta else base


def preparar_item(item: dict, ident: str, rotulo: str) -> dict:
    preparado = dict(item)
    params = {"maquina": ident}
    if preparado.get("lote_id"):
        params["arquivo"] = str(preparado["lote_id"])
    atualizado = preparado.get("atualizado_em")
    atualizado_fmt = _formatar_data(atualizado)
    preparado.update({
        "maquina_id": ident,
        "maquina": rotulo,
        "detalhe_url": _url(f"/job/{preparado['id']}", **params),
        "documento_fmt": formatar(str(preparado.get("documento") or "")),
        "atualizado_fmt": atualizado_fmt,
        "atualizado_hora": atualizado_fmt[11:] if atualizado else "-",
    })
    return preparado


def arquivos_do_estado(estado: remoto.EstadoRemoto | None) -> list[dict]:
    arquivos = []
    for bruto in (estado.dados.get("lotes", []) if estado else []):
        arquivo_id = bruto.get("id")
        if arquivo_id is None:
            continue
        arquivos.append({
            "id": int(arquivo_id),
            "nome": bruto.get("arquivo") or bruto.get("descricao") or f"Arquivo {arquivo_id}",
            "descricao": bruto.get("descricao") or "",
            "itens": int(bruto.get("itens") or 0),
            "criado_em": bruto.get("criado_em") or "",
            "encerrado_em": bruto.get("encerrado_em") or "",
            "criado_fmt": _formatar_data(bruto.get("criado_em")),
        })
    return arquivos


def escolher_arquivo(arquivos: list[dict], solicitado: int | None) -> dict | None:
    if not arquivos:
        return None
    if solicitado is not None:
        for arquivo in arquivos:
            if arquivo["id"] == solicitado:
                return arquivo
    return arquivos[0]


def _status_vazio() -> dict[str, int]:
    return {str(status): 0 for status in STATUS_CARTEIRA}


def _contar_status_local(conn: sqlite3.Connection, filtros: dict) -> dict[str, int]:
    base = {**filtros, "status": None}
    return {
        str(status): consultas.contar_jobs(conn, **{**base, "status": str(status)})
        for status in STATUS_CARTEIRA
    }


def _contar_status_remoto(maquina, senha: str, filtros: dict) -> dict[str, int] | None:
    base = {**filtros, "status": None}
    contagens: dict[str, int] = {}
    for status in STATUS_CARTEIRA:
        total = remoto.contar_itens(maquina, senha, **{**base, "status": str(status)})
        if total is None:
            return None
        contagens[str(status)] = total
    return contagens


def _somar_status(destino: dict[str, int], origem: dict[str, int]) -> None:
    for chave, valor in origem.items():
        destino[chave] = destino.get(chave, 0) + int(valor or 0)


def metricas(contagens_status: dict[str, int], total_filtrado: int) -> dict:
    total_fila = (
        contagens_status.get(str(Status.PENDING), 0)
        + contagens_status.get(str(Status.RUNNING), 0)
        + contagens_status.get(str(Status.RETRY_WAIT), 0)
    )
    dados = {
        "total_fila": total_fila,
        "total_filtrado": total_filtrado,
        "concluidos": contagens_status.get(str(Status.DONE), 0),
        "retry": contagens_status.get(str(Status.RETRY_WAIT), 0),
        "erros": contagens_status.get(str(Status.FAILED), 0),
        "em_execucao": contagens_status.get(str(Status.RUNNING), 0),
    }
    return {**dados, **{f"{chave}_fmt": diagnostico.numero_pt(valor)
                        for chave, valor in dados.items()}}


def listar(
    maquina_id: str | None,
    lote_id: int | None,
    orgao: str | None,
    status: str | None,
    desfecho: str | None,
    busca: str | None,
    pagina: int,
    limite: int,
    maquinas_fila: list[tuple[str, str, Maquina | None]],
    abrir_leitura: Callable[[], sqlite3.Connection],
    senha: str,
) -> tuple[list[dict], list[str], list[str], int, dict[str, int]]:
    filtros = _filtros(lote_id, orgao, status, desfecho, busca)
    escolhidos = {
        ident for ident, _rotulo, _maquina in maquinas_fila
        if not maquina_id or ident == maquina_id
    }
    offset = (pagina - 1) * limite
    itens: list[dict] = []
    mudas: list[str] = []
    orgaos: set[str] = set()
    total = 0
    contagens_status = _status_vazio()

    for ident, rotulo, maquina_cfg in maquinas_fila:
        if ident not in escolhidos:
            continue

        deslocamento = offset if len(escolhidos) == 1 else 0
        quantidade = limite if len(escolhidos) == 1 else offset + limite

        if maquina_cfg is None:
            with contextlib.closing(abrir_leitura()) as conn:
                _somar_status(contagens_status, _contar_status_local(conn, filtros))
                total += consultas.contar_jobs(conn, **filtros)
                linhas = [
                    dict(linha)
                    for linha in consultas.jobs(
                        conn, **filtros, limite=quantidade, offset=deslocamento
                    )
                ]
        else:
            status_remoto = _contar_status_remoto(maquina_cfg, senha, filtros)
            total_remoto = remoto.contar_itens(maquina_cfg, senha, **filtros)
            linhas_remotas = remoto.listar_itens(
                maquina_cfg, senha, **filtros, limite=quantidade,
                offset=deslocamento
            )
            if linhas_remotas is None or total_remoto is None or status_remoto is None:
                mudas.append(rotulo)
                continue
            _somar_status(contagens_status, status_remoto)
            total += total_remoto
            linhas = linhas_remotas

        for linha in linhas:
            item = preparar_item(dict(linha), ident, rotulo)
            itens.append(item)
            if item.get("orgao"):
                orgaos.add(item["orgao"])

    itens.sort(key=lambda i: i.get("atualizado_em") or "", reverse=True)
    if len(escolhidos) == 1:
        return itens[:limite], mudas, sorted(orgaos), total, contagens_status
    return itens[offset:offset + limite], mudas, sorted(orgaos), total, contagens_status


def pagina_contexto(
    pagina: int,
    limite: int,
    total: int,
    filtros: dict,
) -> dict:
    paginas = max(math.ceil(total / limite), 1)
    pagina = min(pagina, paginas)
    inicio = (pagina - 1) * limite + 1 if total else 0
    fim = min(pagina * limite, total)
    return {
        "pagina": pagina,
        "paginas": paginas,
        "limite": limite,
        "inicio": inicio,
        "fim": fim,
        "total": total,
        "tem_anterior": pagina > 1,
        "tem_proxima": pagina < paginas,
        "anterior_url": _url("/jobs", **filtros, pagina=pagina - 1, limite=limite),
        "proxima_url": _url("/jobs", **filtros, pagina=pagina + 1, limite=limite),
    }


def filtros_contexto(maquina: str | None, arquivo: int | None,
                     orgao: str | None, status: str | None,
                     desfecho: str | None, busca: str | None) -> dict:
    return {
        "maquina": maquina or "",
        "arquivo": arquivo or "",
        "orgao": orgao,
        "status": status,
        "desfecho": desfecho,
        "busca": busca,
    }


def orgaos_disponiveis(estados: list[remoto.EstadoRemoto], orgaos_itens: list[str]) -> list[str]:
    orgaos = set(orgaos_itens)
    for estado in estados:
        for resumo in estado.orgaos:
            if resumo.get("orgao"):
                orgaos.add(resumo["orgao"])
    return sorted(orgaos)


def rotulo_desfecho(desfecho: str | None) -> str:
    return ROTULOS_DESFECHO.get(desfecho, desfecho or "-")


def classe_desfecho(desfecho: str | None) -> str:
    if desfecho in DESFECHOS_OK:
        return "ok"
    if desfecho in DESFECHOS_ALERTA:
        return "alerta"
    if desfecho in DESFECHOS_ERRO:
        return "erro"
    return ""


def contexto(
    estados: list[remoto.EstadoRemoto],
    selecionada: remoto.EstadoRemoto | None,
    selecionada_idx: int | None,
    filtros: dict,
    arquivos: list[dict],
    arquivo_atual: dict | None,
    itens: list[dict],
    mudas: list[str],
    orgaos: list[str],
    total: int,
    contagens_status: dict[str, int],
    pagina: int,
    limite: int,
) -> dict:
    filtros_url = {
        **filtros,
        "maquina": filtros.get("maquina") or "",
        "arquivo": filtros.get("arquivo") or "",
    }
    estados_metricas = [selecionada] if selecionada else estados
    return {
        "estados": estados,
        "selecionada": selecionada,
        "selecionada_idx": selecionada_idx,
        "jobs": itens,
        "orgaos": orgaos_disponiveis(estados_metricas, orgaos),
        "arquivos": arquivos,
        "arquivos_recentes": arquivos[:3],
        "arquivo_atual": arquivo_atual,
        "maquinas_mudas": mudas,
        "filtros": filtros,
        "metricas": metricas(contagens_status, total),
        "paginacao": pagina_contexto(pagina, limite, total, filtros_url),
        "periodo": diagnostico.periodo(7),
        "reenfileirar_url": _url(
            "/acoes/reenfileirar", maquina=filtros.get("maquina") or ""
        ),
        "rotulo_desfecho": rotulo_desfecho,
        "classe_desfecho": classe_desfecho,
    }
