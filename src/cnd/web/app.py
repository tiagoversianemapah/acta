"""Painel web — FastAPI + páginas renderizadas no servidor (ADR-005).

Processo separado do orquestrador: um restart aqui não para a fila, e uma
falha de renderização não derruba o robô. Também é este processo que vigia
o heartbeat do orquestrador e dispara o alerta de "o robô parou".
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from cnd.core import breaker, fila, tempo
from cnd.infra import alertas, heartbeat
from cnd.infra.config import carregar as carregar_config
from cnd.infra.db import conectar, conectar_leitura
from cnd.infra.log import configurar as configurar_log
from cnd.infra.log import obter
from cnd.web import api, consultas, relatorio

log = obter("web")
cfg = carregar_config()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def ler() -> sqlite3.Connection:
    return conectar_leitura(cfg.banco)


def escrever() -> sqlite3.Connection:
    return conectar(cfg.banco)


async def vigiar_orquestrador() -> None:
    """Avisa quando o robô emudece TENDO TRABALHO NA FILA.

    A condição da fila é o coração disto. O robô não é um serviço de pé o
    ano inteiro — é tarefa mensal, e ficar parado é o estado normal em uns
    28 dias de cada 30. A versão anterior cobrava sinal de vida sempre, e
    teria disparado "Orquestrador fora do ar" o mês inteiro dizendo que
    está tudo errado justamente quando está tudo certo.

    Isso não seria só ruído: é o mecanismo que mata o canal. Depois de duas
    semanas de alarme falso ninguém abre mais o aviso, e o de lote travado
    morre junto com os outros. Alerta que dispara sem motivo é pior do que
    alerta nenhum.
    """
    while True:
        try:
            with contextlib.closing(ler()) as conn:
                idade = heartbeat.segundos_desde(conn, "orquestrador")
                na_fila = consultas.pendentes(conn)
            limite = cfg.alertas.heartbeat_timeout_s
            mudo = idade is not None and idade > limite

            if mudo and na_fila:
                alertas.abrir_incidente(
                    cfg.alertas, "heartbeat",
                    "Robô parado com trabalho na fila",
                    f"O robô não dá sinal de vida há {int(idade // 60)} "
                    f"minutos e ainda há {na_fila} itens para consultar.",
                    acao="1. Conferir se a máquina está ligada e com o "
                         "Windows logado.\n"
                         "2. Abrir o ACTA nela e apertar Iniciar robô.\n"
                         "3. Se voltar a parar, ver o Registro para o motivo.",
                    dados={"Itens na fila": str(na_fila),
                           "Sem sinal há": f"{int(idade // 60)} min"},
                )
            else:
                # Fila vazia é fim de trabalho, não incidente. O incidente
                # só fecha se algum dia chegou a abrir.
                alertas.fechar_incidente(
                    cfg.alertas, "heartbeat", "Robô normalizado",
                    "O robô voltou a trabalhar." if na_fila else
                    "A fila esvaziou.",
                )
        except Exception:
            log.exception("falha_ao_vigiar_heartbeat")
        await asyncio.sleep(60)


@contextlib.asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    configurar_log(cfg.pasta_logs, "web")
    tarefa = asyncio.create_task(vigiar_orquestrador())
    log.info("painel_no_ar")
    yield
    tarefa.cancel()


app = FastAPI(title="CND Bot", lifespan=ciclo_de_vida)
app.include_router(api.montar(lambda: cfg, ler))


# ----------------------------------------------------------------------
# Autenticação
# ----------------------------------------------------------------------

# O verificador externo precisa alcançar /ping sem credencial — é ele que
# avisa quando a máquina inteira cai, e não há ninguém para digitar senha
# nesse momento. A rota devolve só sinal de vida e tamanho da fila: nenhum
# dado de cliente.
ROTAS_LIVRES = frozenset({"/ping"})


def _senha_apresentada(request: Request) -> str | None:
    """A senha, venha ela de onde vier.

    Duas formas porque são dois públicos: o aplicativo de mesa manda um
    cabeçalho próprio, e o navegador só sabe mandar Basic — que tem a
    vantagem de o próprio Windows exibir a caixa de login e lembrar da
    resposta durante a sessão, sem precisarmos escrever tela de login.
    """
    cabecalho = request.headers.get("x-cnd-senha")
    if cabecalho:
        return cabecalho

    autorizacao = request.headers.get("authorization", "")
    if autorizacao.lower().startswith("basic "):
        with contextlib.suppress(Exception):
            cru = base64.b64decode(autorizacao[6:]).decode("utf-8")
            return cru.split(":", 1)[1] if ":" in cru else cru
    return None


@app.middleware("http")
async def exigir_senha(request: Request, seguir):
    """Fecha o painel INTEIRO, não só a API.

    A API já pedia senha, mas as páginas e os downloads não — e são elas
    que carregam o que interessa proteger: razão social, CNPJ e os PDFs das
    certidões de toda a carteira. Numa máquina publicada com
    `--host 0.0.0.0`, qualquer computador do escritório baixaria o pacote
    inteiro apontando o navegador para a porta. Dado de cliente não pode
    sair do controle da Mapah (RNF-06).

    Sem senha configurada, nada muda: é a instalação de máquina única,
    ouvindo só em 127.0.0.1.
    """
    esperada = cfg.rede.senha
    if not esperada or request.url.path in ROTAS_LIVRES:
        return await seguir(request)

    apresentada = _senha_apresentada(request)
    # compare_digest e não '==': a comparação comum sai no primeiro
    # caractere diferente, e o tempo de resposta entrega o tamanho do
    # acerto. Custa nada usar a versão que não vaza isso.
    if apresentada and secrets.compare_digest(apresentada, esperada):
        return await seguir(request)

    return JSONResponse(
        {"detail": "senha da rede inválida ou ausente"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Robo CND"'},
    )


# ----------------------------------------------------------------------
# Painel
# ----------------------------------------------------------------------

def _contexto_painel(conn: sqlite3.Connection, lote_id: int | None) -> dict:
    todos = consultas.lotes(conn)
    if lote_id is None and todos:
        lote_id = todos[0]["id"]

    resumos = []
    for orgao in consultas.orgaos_do_lote(conn, lote_id):
        r = consultas.resumo(conn, orgao, lote_id)
        resumos.append({"r": r, "eta": consultas.eta_horas(r)})

    idade = heartbeat.segundos_desde(conn, "orquestrador")
    return {
        "lotes": todos,
        "lote_id": lote_id,
        "resumos": resumos,
        "rotulos": consultas.ROTULOS,
        "orquestrador_vivo": idade is not None and idade <= cfg.alertas.heartbeat_timeout_s,
        "orquestrador_idade": idade,
        "agora": tempo.agora_iso(),
        "mes": relatorio.mes_corrente(),
    }


@app.get("/", response_class=HTMLResponse)
def painel(request: Request, lote: int | None = None):
    with contextlib.closing(ler()) as conn:
        ctx = _contexto_painel(conn, lote)
    return templates.TemplateResponse(request, "painel.html", ctx)


@app.get("/fragmento/resumo", response_class=HTMLResponse)
def fragmento_resumo(request: Request, lote: int | None = None):
    """Pedaço recarregado a cada 5s pelo JavaScript da página — só os cartões."""
    with contextlib.closing(ler()) as conn:
        ctx = _contexto_painel(conn, lote)
    return templates.TemplateResponse(request, "_cards.html", ctx)


# ----------------------------------------------------------------------
# Triagem
# ----------------------------------------------------------------------

@app.get("/jobs", response_class=HTMLResponse)
def listar_jobs(request: Request, lote: int | None = None, orgao: str | None = None,
                status: str | None = None, desfecho: str | None = None,
                busca: str | None = None):
    with contextlib.closing(ler()) as conn:
        linhas = consultas.jobs(conn, lote, orgao or None, status or None,
                                desfecho or None, busca or None)
        orgaos = consultas.orgaos_do_lote(conn, lote)
        lotes = consultas.lotes(conn)
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": linhas, "orgaos": orgaos, "lotes": lotes,
        "lote_id": lote, "filtros": {"orgao": orgao, "status": status,
                                     "desfecho": desfecho, "busca": busca},
    })


@app.get("/job/{job_id}", response_class=HTMLResponse)
def detalhe_job(request: Request, job_id: int):
    with contextlib.closing(ler()) as conn:
        job = conn.execute(
            """
            SELECT j.*, e.documento, e.nome FROM job j
              JOIN empresa e ON e.id = j.empresa_id WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        tentativas = consultas.tentativas_do_job(conn, job_id)
        certidao = conn.execute(
            "SELECT * FROM certidao WHERE job_id = ?", (job_id,)
        ).fetchone()
    return templates.TemplateResponse(request, "job.html", {
        "job": job, "tentativas": tentativas, "certidao": certidao,
    })


@app.get("/ping")
def ping():
    """Saúde do sistema, para vigilância EXTERNA.

    Existe por causa de um buraco que nenhum alerta interno cobre: se a
    máquina inteira cair — falta de luz, reinício do Windows, rede — o
    processo que vigia o robô morre junto com ele, e o silêncio é
    indistinguível de "está tudo bem".

    Um verificador de fora (um fluxo agendado do Power Automate, por
    exemplo) chama este endereço de tempos em tempos. Se não responder, ou
    responder 503, a máquina ou o robô estão fora — e aí o aviso sai de um
    lugar que não depende deles.

    503 apenas quando há TRABALHO NA FILA e o robô está mudo. Fila vazia é
    fim de tarefa, não pane: o robô roda por temporada, e devolver 503 o
    mês inteiro faria o verificador externo chamar todo dia sem motivo —
    até alguém desligá-lo, justamente antes do dia em que ele importaria.
    """
    try:
        with contextlib.closing(ler()) as conn:
            idade = heartbeat.segundos_desde(conn, "orquestrador")
            pendentes = consultas.pendentes(conn)
    except Exception as erro:
        return JSONResponse({"ok": False, "motivo": f"banco inacessível: {erro}"},
                            status_code=503)

    ativo = idade is not None and idade <= cfg.alertas.heartbeat_timeout_s
    vivo = ativo or not pendentes
    corpo = {
        "ok": vivo,
        "robo": "em execução" if ativo else
                ("ocioso" if not pendentes else "parado"),
        "ultimo_sinal_ha_s": round(idade) if idade is not None else None,
        "itens_na_fila": pendentes,
        "agora": tempo.agora_iso(),
    }
    if not vivo:
        corpo["motivo"] = (
            f"{pendentes} itens na fila e o robô nunca foi iniciado"
            if idade is None else
            f"{pendentes} itens na fila e sem sinal de vida há "
            f"{idade / 60:.0f} minutos")
    return JSONResponse(corpo, status_code=200 if vivo else 503)


@app.get("/saude", response_class=HTMLResponse)
def saude(request: Request):
    with contextlib.closing(ler()) as conn:
        dados = {orgao: consultas.captcha_por_hora(conn, orgao)
                 for orgao in consultas.orgaos_do_lote(conn, None)}
        idade = heartbeat.segundos_desde(conn, "orquestrador")
    return templates.TemplateResponse(request, "saude.html", {
        "dados": dados, "idade": idade,
        "timeout": cfg.alertas.heartbeat_timeout_s,
    })


# ----------------------------------------------------------------------
# Ações
# ----------------------------------------------------------------------

@app.post("/acoes/reenfileirar")
def acao_reenfileirar(orgao: str = Form(default="")):
    with contextlib.closing(escrever()) as conn:
        quantidade = fila.reenfileirar_falhados(conn, orgao or None)
    log.info("reenfileirados", extra={"orgao": orgao or "todos", "quantidade": quantidade})
    return RedirectResponse("/jobs?status=FAILED", status_code=303)


@app.post("/acoes/breaker/{orgao}/pausar")
def acao_pausar(orgao: str):
    with contextlib.closing(escrever()) as conn:
        parametros = (cfg.orgaos[orgao].breaker if orgao in cfg.orgaos
                      else breaker.ParametrosBreaker())
        breaker.abrir(conn, orgao, "pausado manualmente pelo operador", parametros)
    log.warning("breaker_pausado_manualmente", extra={"orgao": orgao})
    return RedirectResponse("/", status_code=303)


@app.post("/acoes/breaker/{orgao}/retomar")
def acao_retomar(orgao: str):
    with contextlib.closing(escrever()) as conn:
        breaker.fechar(conn, orgao)
    log.info("breaker_retomado_manualmente", extra={"orgao": orgao})
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------

@app.get("/relatorio/{lote_id}.xlsx")
def baixar_relatorio(lote_id: int):
    with contextlib.closing(ler()) as conn:
        conteudo = relatorio.gerar_bytes(conn, lote_id)
    return Response(
        conteudo,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="relatorio_lote_{lote_id}.xlsx"'},
    )


@app.get("/certidoes/{mes}.zip")
def baixar_pdfs(mes: str, somente_negativas: bool = False,
                orgao: str | None = None):
    """Pacote das certidões emitidas no mês (`2026-08`), por órgão.

    `?somente_negativas=1` deixa de fora as CPEN, para quem precisa só das
    empresas totalmente limpas. `?orgao=RFB_PJ` entrega um órgão de cada
    vez, para quando a federal fecha antes das estaduais.
    """
    with contextlib.closing(ler()) as conn:
        conteudo = relatorio.zipar_pdfs(
            conn, mes, somente_negativas,
            nomes={codigo: o.rotulo for codigo, o in cfg.orgaos.items()},
            orgao=orgao)
    sufixo = "_negativas" if somente_negativas else ""
    if orgao:
        sufixo = f"_{orgao.lower()}{sufixo}"
    return Response(
        conteudo, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="certidoes_{mes}{sufixo}.zip"'},
    )
