"""Painel web — FastAPI + páginas renderizadas no servidor (ADR-005).

Processo separado do orquestrador: um restart aqui não para a fila, e uma
falha de renderização não derruba o robô. Também é este processo que vigia
o heartbeat do orquestrador e dispara o alerta de "o robô parou".
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cnd.adapters.base import adapter_existe
from cnd.core import breaker, fila, tempo
from cnd.core.documentos import formatar
from cnd.desktop import remoto
from cnd.infra import alertas, carteiras, heartbeat, maquina
from cnd.infra.config import CAMINHO_PADRAO, Config, Maquina
from cnd.infra.config import carregar as carregar_config
from cnd.infra.db import RAIZ_PROJETO, conectar, conectar_leitura, criar_schema
from cnd.infra.log import configurar as configurar_log
from cnd.infra.log import obter
from cnd.web import api, carteira, comandos, consultas, diagnostico, relatorio

log = obter("web")
cfg = carregar_config()


def _bytes_do_arquivo() -> bytes | None:
    """O conteúdo cru do config.toml, ou None se ele sumiu."""
    try:
        return CAMINHO_PADRAO.read_bytes()
    except OSError:
        return None


_config_bytes = _bytes_do_arquivo()


def config_atual() -> Config:
    """A config do disco, relida quando o arquivo muda.

    Ler uma vez só na importação fazia o painel discordar do robô: editar o
    config.toml e reiniciar apenas o robô deixava os dois processos com
    parâmetros diferentes, e quem responde à API — inclusive a normalização
    do ritmo no início manual — decidia pelos valores velhos. Levou meia
    hora para achar em 15/09/2026, com o ritmo do SEFAZ-ES preso em 300s.

    Compara o CONTEÚDO, não a data: dois salvamentos no mesmo tique do
    relógio saem com `st_mtime_ns` idêntico (medido aqui no Windows), e é
    exatamente o que acontece quando um editor grava um temporário e
    renomeia por cima. O arquivo tem ~14 KB; relê-lo por requisição custa
    menos que errar a config.

    Config inválida NÃO derruba o painel: arquivo sendo salvo passa por um
    instante ilegível, e trocar a config boa por uma exceção justamente aí
    seria pior que continuar com a anterior.
    """
    global cfg, _config_bytes

    crus = _bytes_do_arquivo()
    if crus is None or crus == _config_bytes:
        return cfg

    try:
        nova = carregar_config()
    except Exception as erro:
        # Guarda os bytes ruins para não repetir a tentativa — e o log — a
        # cada requisição enquanto o arquivo estiver quebrado.
        _config_bytes = crus
        log.warning("config_ilegivel_mantendo_a_anterior",
                    extra={"erro": str(erro)[:300]})
        return cfg

    cfg, _config_bytes = nova, crus
    log.info("config_relida", extra={"arquivo": str(CAMINHO_PADRAO)})
    return cfg


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app_static = Path(__file__).parent / "static"
PLANILHA_ENVIADA = File(...)


def _rotulo_orgao(codigo: str) -> str:
    orgao = cfg.orgaos.get(codigo)
    return orgao.rotulo if orgao else codigo


def automacoes() -> list[dict]:
    """As automações que a máquina pode rodar, e por que algumas não podem.

    A planilha já diz a automação pela ABA — `RFB` é Receita PJ, `CRF` é
    regularidade do empregador. Isso existia no importador desde sempre,
    mas o envio mandava `["RFB"]` fixo: uma máquina só sabia fazer uma
    coisa. Aqui a lista vira dado da tela, para quem envia escolher.

    Indisponível continua aparecendo, e com o motivo. Sumir com a opção
    faria a pessoa procurar no lugar errado por algo que não está lá —
    ela precisa ver que existe e que falta ligar.
    """
    from cnd.ingestao.planilha import ABA_PARA_ORGAO

    lista = []
    for aba, (codigo, tipo) in ABA_PARA_ORGAO.items():
        orgao = cfg.orgaos.get(codigo)
        adapter_ok = (
            orgao is not None
            and adapter_existe(orgao.adapter)
        )
        if orgao is None:
            motivo = "não está no config desta máquina"
        elif not adapter_ok:
            # Antes de "desligada": ligar no config não constrói adapter
            # nenhum, e dizer só que está desligada mandaria a pessoa
            # trocar `ativo = true` para descobrir o problema de verdade.
            motivo = "automação ainda não construída"
        elif not orgao.ativo:
            motivo = "desligada no config"
        else:
            motivo = ""
        lista.append({
            "aba": aba,
            "codigo": codigo,
            "tipo": tipo,
            "rotulo": orgao.rotulo if orgao else codigo,
            "disponivel": not motivo,
            "motivo": motivo,
            "pode_ligar": bool(orgao and adapter_ok and not orgao.ativo),
        })
    lista.sort(key=lambda a: (not a["disponivel"], a["rotulo"]))
    return lista


def _chave_do_css() -> str:
    """Some no endereço do CSS para o navegador buscar a folha nova.

    É o mtime do arquivo, e não a versão do ACTA, por dois motivos. A
    versão vem do metadado do pacote e volta VAZIA quando ele não está
    instalado — numa pasta copiada, que é como o ACTA se instala, `?v=`
    ficaria constante e não invalidaria nada. E conserto de CSS costuma
    sair sem subir versão: em 18/08/2026 a tela apareceu com marcação nova
    e estilo velho, e só não ficou assim porque a versão subiu junto, por
    acaso. O mtime muda quando o arquivo muda, que é a pergunta certa.
    """
    try:
        return str(int((app_static / "app.css").stat().st_mtime))
    except OSError:
        return maquina.versao() or "0"


templates.env.globals.update(
    versao_acta=maquina.versao() or "",
    chave_do_css=_chave_do_css(),
    automacoes=automacoes,
    rotulo_orgao=_rotulo_orgao,
)


def ler() -> sqlite3.Connection:
    return conectar_leitura(cfg.banco)


def escrever() -> sqlite3.Connection:
    return conectar(cfg.banco)


INTERVALO_RETOMADA_AUTOMATICA_S = 300.0
_ultima_retomada_automatica = -INTERVALO_RETOMADA_AUTOMATICA_S


def _tentar_retomada_automatica(idade: float | None, na_fila: int) -> bool:
    """Relanca o robo quando ele morreu no meio de uma fila."""
    global _ultima_retomada_automatica

    if not cfg.rede.roda_robo or not na_fila:
        return False
    if idade is None or idade <= cfg.alertas.heartbeat_timeout_s:
        return False

    agora = time.monotonic()
    if agora - _ultima_retomada_automatica < INTERVALO_RETOMADA_AUTOMATICA_S:
        return False
    _ultima_retomada_automatica = agora

    ok, situacao = comandos.iniciar_robo_da_maquina(
        cfg, RAIZ_PROJETO, automatico=True
    )
    ja_estava_rodando = situacao.lower().startswith("ja esta")
    if ok and not ja_estava_rodando:
        log.warning(
            "robo_retomado_automaticamente",
            extra={"sem_sinal_ha_s": round(idade), "itens_na_fila": na_fila,
                   "situacao": situacao},
        )
        return True
    if ok:
        log.warning(
            "retomada_automatica_encontrou_robo_sem_heartbeat",
            extra={"sem_sinal_ha_s": round(idade), "itens_na_fila": na_fila,
                   "situacao": situacao},
        )
        return False

    log.warning(
        "retomada_automatica_nao_iniciou",
        extra={"sem_sinal_ha_s": round(idade), "itens_na_fila": na_fila,
               "motivo": situacao},
    )
    return False


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
            # Robô parado porque ALGUÉM mandou parar não é incidente, e
            # não se relança. Sem esta condição, enviar uma planilha e não
            # apertar "Iniciar robô" deixava o vigia tentando religar de
            # cinco em cinco minutos, apanhando da parada manual, e ainda
            # abrindo "Robô parado com trabalho na fila" a cada minuto —
            # o alarme falso que este arquivo inteiro existe para evitar.
            de_proposito = comandos.parada_manual_ativa(cfg.banco)

            retomou = (_tentar_retomada_automatica(idade, na_fila)
                       if mudo and not de_proposito else False)

            if mudo and na_fila and not retomou and not de_proposito:
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
app.mount("/static", StaticFiles(directory=str(app_static)), name="static")
app.include_router(api.montar(config_atual, ler))
# As rotas que mexem na máquina ficam num roteador separado, e exigem senha
# configurada — a capacidade perigosa nasce desligada.
app.include_router(comandos.montar(config_atual, RAIZ_PROJETO))


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
# Pedidos vindos de outro site
# ----------------------------------------------------------------------

# GET e HEAD não mudam nada nesta aplicação; o resto muda.
METODOS_QUE_MUDAM = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _mesmo_host(url: str, host: str) -> bool:
    if not url or not host:
        return False
    return urllib.parse.urlparse(url).netloc.lower() == host.strip().lower()


def _veio_de_outro_site(request: Request) -> bool:
    """Se este POST foi disparado por uma página que não é a nossa.

    A senha do painel entra por Basic, e Basic é credencial AMBIENTE: uma
    vez digitada, o navegador a reenvia sozinho em qualquer pedido para
    esta máquina — inclusive num formulário escondido numa página
    qualquer da internet. Sem esta conferência, uma aba aberta noutro site
    zerava a máquina com `POST /api/zerar`, e o navegador anexava a senha
    por conta própria.

    Vale mesmo SEM senha configurada, que é o caso pior: aí não há nem
    senha a exigir, e `http://127.0.0.1:8000` é endereço conhecido.

    Três sinais, do mais confiável ao mais antigo. Nenhum deles presente
    significa que quem chamou não é navegador — é o aplicativo de mesa ou
    o console pedindo pela rede, e esses não têm site de origem.
    """
    local = request.headers.get("sec-fetch-site", "").strip().lower()
    if local:
        # O navegador diz de onde veio, e não dá para forjar por script.
        # 'none' é a barra de endereço; 'same-origin', a nossa própria tela.
        return local not in {"same-origin", "none"}

    host = request.headers.get("host", "")
    origem = request.headers.get("origin", "")
    if origem:
        return not _mesmo_host(origem, host)
    referencia = request.headers.get("referer", "")
    if referencia:
        return not _mesmo_host(referencia, host)
    return False


@app.middleware("http")
async def recusar_pedido_de_outro_site(request: Request, seguir):
    if request.method in METODOS_QUE_MUDAM and _veio_de_outro_site(request):
        log.warning("pedido_de_outro_site", extra={
            "rota": request.url.path,
            "origem": request.headers.get("origin")
                      or request.headers.get("referer") or "",
        })
        return JSONResponse(
            {"detail": "Este comando só vale a partir da tela do próprio "
                       "painel."},
            status_code=403,
        )
    return await seguir(request)


# ----------------------------------------------------------------------
# Painel
# ----------------------------------------------------------------------

def _maquinas_operacao() -> list[remoto.EstadoRemoto]:
    """Estados exibidos na tela de operacao, locais ou remotos."""
    return remoto.consultar_todas(cfg)


def _indice_da_maquina(
    estados: list[remoto.EstadoRemoto], maquina: int | None
) -> int | None:
    if not estados:
        return None
    if maquina is not None and 0 <= maquina < len(estados):
        return maquina
    return min(range(len(estados)), key=lambda i: estados[i].gravidade)


def _totais(orgaos: list[dict]) -> dict:
    por_desfecho: dict[str, int] = {}
    for orgao in orgaos:
        for chave, valor in orgao.get("por_desfecho", {}).items():
            por_desfecho[chave] = por_desfecho.get(chave, 0) + int(valor or 0)

    total = sum(int(o.get("total") or 0) for o in orgaos)
    concluidos = sum(int(o.get("concluidos") or 0) for o in orgaos)
    falhados = sum(int(o.get("falhados") or 0) for o in orgaos)
    pendentes = sum(int(o.get("pendentes") or 0) for o in orgaos)
    em_execucao = sum(int(o.get("em_execucao") or 0) for o in orgaos)
    return {
        "total": total,
        "concluidos": concluidos,
        "falhados": falhados,
        "pendentes": pendentes,
        "em_execucao": em_execucao,
        "percentual": (concluidos / total * 100) if total else 0.0,
        # Quantos ainda não terminaram, do jeito que a pergunta é feita.
        # "Pendentes" mostrava só quem está na fila e deixava os falhados de
        # fora, então o cartão dizia 4 quando faltavam 7 — e a diferença só
        # aparecia numa coluna lá embaixo. Desde que a recuperação automática
        # existe, falhado também vai ser tentado de novo: para quem olha, as
        # duas coisas são igualmente "ainda não terminou".
        "restantes": max(total - concluidos, 0),
        "por_desfecho": por_desfecho,
        "negativas": por_desfecho.get("NEGATIVA", 0),
        "positivas": por_desfecho.get("POSITIVA", 0),
        "cpen": por_desfecho.get("CPEN", 0),
        "insuficientes": por_desfecho.get("PENDENCIA_MANUAL", 0),
        # Sem isto o cartão não fecha com o "X de Y": os inaptos entram em
        # concluídos e não apareciam em lugar nenhum do resumo, deixando uma
        # diferença sem explicação para quem confere.
        "inaptos": por_desfecho.get("INAPTA", 0),
        "aproveitadas": por_desfecho.get("APROVEITADA", 0),
    }


def _orgaos_para_exportar(
    selecionada: remoto.EstadoRemoto | None, mes: str,
) -> list[dict]:
    if not selecionada or not selecionada.online:
        return []

    if selecionada.local:
        with contextlib.closing(ler()) as conn:
            return [
                {
                    "orgao": codigo,
                    "rotulo": _rotulo_orgao(codigo),
                    "total": consultas.resumo(conn, codigo, None, mes).total,
                }
                for codigo in consultas.orgaos_do_lote(conn, None, mes)
            ]

    remoto_lista = remoto.orgaos_do_mes(
        selecionada.maquina, cfg.rede.senha, mes
    )
    return remoto_lista or selecionada.orgaos


def _url_destino(
    rota: str, maquina: int | str | None = None,
    mensagem: str | None = None, erro: str | None = None,
    arquivo: int | None = None,
) -> str:
    params: dict[str, str] = {}
    if maquina is not None:
        params["maquina"] = str(maquina)
    if arquivo is not None:
        params["arquivo"] = str(arquivo)
    if mensagem:
        params["mensagem"] = mensagem[:220]
    if erro:
        params["erro"] = erro[:220]
    consulta = urllib.parse.urlencode(params)
    return f"{rota}?{consulta}" if consulta else rota


_CORRECOES_FLASH = (
    (r"\bRobo\b", "Robô"),
    (r"\brobo\b", "robô"),
    (r"\bMaquina\b", "Máquina"),
    (r"\bmaquina\b", "máquina"),
    (r"\bAtualizacao\b", "Atualização"),
    (r"\batualizacao\b", "atualização"),
    (r"\bnao\b", "não"),
    (r"\bNao\b", "Não"),
    (r"\besta\b", "está"),
    (r"\bEsta\b", "Está"),
    (r"\bja\b", "já"),
    (r"\bJa\b", "Já"),
    (r"\bexecucao\b", "execução"),
    (r"\bExecucao\b", "Execução"),
    (r"\bsessao\b", "sessão"),
    (r"\bSessao\b", "Sessão"),
)


def _formatar_flash(texto: str | None) -> str | None:
    if not texto:
        return None

    frase = " ".join(str(texto).split())
    for origem, destino in _CORRECOES_FLASH:
        frase = re.sub(origem, destino, frase)
    if frase:
        frase = frase[:1].upper() + frase[1:]
    if frase and frase[-1] not in ".!?":
        frase += "."
    return frase


def _ip_da_rede() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _origem_atualizacao(request: Request) -> str:
    host = request.url.hostname or ""
    if host in {"", "localhost", "127.0.0.1", "::1"}:
        host = _ip_da_rede()
    return f"http://{host}:8899"


def _origem_atualizacao_salva() -> str:
    """De onde esta máquina se atualizou da última vez.

    É o melhor palpite que existe para o prompt: o console publica sempre
    do mesmo lugar, e quem atualiza uma máquina vai atualizar as outras
    logo em seguida.
    """
    arquivo = RAIZ_PROJETO / "data" / "ultima_origem_atualizacao.txt"
    with contextlib.suppress(OSError):
        return arquivo.read_text(encoding="utf-8-sig").strip().rstrip("/")
    return ""


# Global de template, e não item de contexto de UMA tela: o botão Atualizar
# vive no Diagnóstico, cujo contexto é montado em web/diagnostico.py e não
# passava por aqui. Enquanto o palpite era item de contexto, a tela do botão
# ficava sem ele e caía num IP escrito no HTML.
templates.env.globals["origem_atualizacao_salva"] = _origem_atualizacao_salva


def _reler_com_lote(
    estados: list[remoto.EstadoRemoto], indice: int | None, arquivo: int | None
) -> None:
    """Refaz a leitura da máquina escolhida pedindo os números de uma planilha.

    Só a selecionada, e só quando alguém escolheu de fato: o id do lote é
    numerado por máquina, então repassá-lo às outras traria os números da
    planilha errada — e o custo seria uma ida à rede por máquina, à toa.
    """
    if arquivo is None or indice is None or not estados:
        return
    estado = estados[indice]
    if not estado.online:
        return
    estados[indice] = (
        remoto.consultar_local(cfg, arquivo) if estado.local
        else remoto.consultar(estado.maquina, cfg.rede.senha, arquivo)
    )


def _contexto_painel(
    maquina: int | None,
    mensagem: str | None = None,
    erro: str | None = None,
    request: Request | None = None,
    arquivo: int | None = None,
) -> dict:
    estados = _maquinas_operacao()
    indice = _indice_da_maquina(estados, maquina)
    _reler_com_lote(estados, indice, arquivo)
    selecionada = estados[indice] if indice is not None else None
    dados = selecionada.dados if selecionada else {}
    orgaos = selecionada.orgaos if selecionada else []
    totais = _totais(orgaos)
    # Com a mesma planilha enviada mais de uma vez, o nome não distingue
    # nada: os arquivos vêm formatados com a data de envio junto.
    lotes = carteira.arquivos_do_estado(selecionada)
    planilhas_salvas = [{
        "token": g.token,
        "nome": g.nome,
        "quando": g.quando_curto,
    } for g in carteiras.listar(cfg.banco)]
    em_execucao = dados.get("lote_em_execucao")
    rodando = next((lote for lote in lotes if lote["id"] == em_execucao), None)
    meses = dados.get("meses", [])
    mes_atual = meses[0] if meses else relatorio.mes_corrente()
    base_download = (
        "" if not selecionada or selecionada.local else selecionada.maquina.base
    )

    return {
        "estados": estados,
        "selecionada": selecionada,
        "selecionada_idx": indice,
        "dados": dados,
        "orgaos": orgaos,
        "orgaos_exportacao": _orgaos_para_exportar(selecionada, mes_atual),
        "totais": totais,
        "bloqueios": [o for o in orgaos if o.get("disjuntor") == "ABERTO"],
        # Órgãos com trabalho parado esperando a próxima rodada. Sem esta
        # lista a tela não tem como avisar que o lote continua aberto.
        "orgaos_aguardando_rodada": [
            o for o in orgaos if (o.get("falhados") or 0) > 0
        ],
        "atividade": selecionada.atividade if selecionada else [],
        "saude": selecionada.saude if selecionada else {},
        "lotes": lotes,
        "planilhas_salvas": (
            planilhas_salvas
            if selecionada and selecionada.online and selecionada.roda_robo
            else []
        ),
        "lote_id": dados.get("lote_id"),
        "lote_nome": dados.get("lote_nome") or "sem planilha ativa",
        # O que veio na URL, para o seletor não "voltar sozinho" quando o
        # robô mudar de planilha no meio de uma conferência.
        "arquivo_escolhido": arquivo,
        # E, junto, qual planilha está de fato rodando: olhar números
        # parados sem saber que o trabalho corre em OUTRO arquivo foi
        # exatamente o que fez a tela parecer travada.
        "lote_em_execucao": em_execucao,
        "lote_em_execucao_nome": rodando["nome"] if rodando else "",
        "mes": mes_atual,
        "base_download": base_download,
        "usa_rede": bool(cfg.rede.maquinas),
        "pode_controlar_local": bool(selecionada and selecionada.local),
        "mensagem_operacao": _formatar_flash(mensagem),
        "erro_operacao": _formatar_flash(erro),
        "agora": tempo.agora_iso(),
    }


@app.get("/", response_class=HTMLResponse)
def painel(
    request: Request, maquina: int | None = None,
    mensagem: str | None = None, erro: str | None = None,
    arquivo: int | None = None,
):
    ctx = _contexto_painel(maquina, mensagem, erro, request, arquivo)
    return templates.TemplateResponse(request, "painel.html", ctx)


@app.get("/fragmento/resumo", response_class=HTMLResponse)
def fragmento_resumo(request: Request, maquina: int | None = None,
                     arquivo: int | None = None):
    """Pedaço recarregado pelo JavaScript: estado operacional da máquina."""
    ctx = _contexto_painel(maquina, request=request, arquivo=arquivo)
    return templates.TemplateResponse(request, "_cards.html", ctx)


def _contexto_diagnostico(maquina: int | None, dias: int) -> dict:
    dias = diagnostico.normalizar_dias(dias)
    estados = _maquinas_operacao()
    indice = _indice_da_maquina(estados, maquina)
    selecionada = estados[indice] if indice is not None else None
    diagnosticos, erro = diagnostico.coletar_da_maquina(
        selecionada, dias, ler, cfg.rede.senha
    )
    return diagnostico.contexto(
        estados, selecionada, indice, diagnosticos, erro, dias
    )


# ----------------------------------------------------------------------
# Fila
# ----------------------------------------------------------------------

TODAS_AS_MAQUINAS = "__todas__"
LOCAL_ID = "__local__"


def _maquinas_da_fila() -> list[tuple[str, str, Maquina | None]]:
    if cfg.rede.maquinas:
        return [
            (str(indice), maquina_cfg.rotulo, maquina_cfg)
            for indice, maquina_cfg in enumerate(cfg.rede.maquinas)
        ]
    if cfg.rede.roda_robo:
        return [(LOCAL_ID, cfg.rede.nome or "Esta máquina", None)]
    return []


def _maquina_da_carteira(maquina_id: str | None) -> tuple[str, int | None]:
    maquinas = _maquinas_da_fila()
    if not maquinas:
        return "", None

    ids = {ident for ident, _rotulo, _maquina in maquinas}
    escolhido = maquina_id if maquina_id in ids else maquinas[0][0]
    indice = int(escolhido) if escolhido.isdigit() else 0
    return escolhido, indice


def _ids_escolhidos(maquina_id: str | None) -> set[str]:
    ids = {ident for ident, _rotulo, _maquina in _maquinas_da_fila()}
    if maquina_id and maquina_id in ids:
        return {maquina_id}
    return ids


def _certidao_do_item(item: dict) -> dict | None:
    if not item.get("caminho_pdf"):
        return None
    return {
        "tipo": item.get("tipo"),
        "emitida_em": item.get("emitida_em"),
        "valida_ate": item.get("valida_ate"),
        "codigo_controle": item.get("codigo_controle"),
        "caminho_pdf": item.get("caminho_pdf"),
    }


def _buscar_item(
    job_id: int, maquina_id: str | None
) -> tuple[dict | None, list[dict], dict | None, str]:
    candidatos = [
        m for m in _maquinas_da_fila()
        if m[0] in _ids_escolhidos(maquina_id)
    ] or _maquinas_da_fila()

    for ident, rotulo, maquina_cfg in candidatos:
        if maquina_cfg is None:
            with contextlib.closing(ler()) as conn:
                linhas = consultas.jobs(conn, job_id=job_id, limite=1)
                if not linhas:
                    continue
                item = dict(linhas[0])
                item["maquina_id"] = ident
                item["maquina"] = rotulo
                item["documento_fmt"] = formatar(item.get("documento") or "")
                tentativas = [
                    dict(linha) for linha in consultas.tentativas_do_job(conn, job_id)
                ]
                return item, tentativas, _certidao_do_item(item), rotulo

        else:
            linhas = remoto.listar_itens(
                maquina_cfg, cfg.rede.senha, job_id=job_id, limite=1
            )
            if not linhas:
                continue
            item = dict(linhas[0])
            item["maquina_id"] = ident
            item["maquina"] = rotulo
            item["documento_fmt"] = formatar(item.get("documento") or "")
            tentativas = remoto.tentativas_do_job(maquina_cfg, cfg.rede.senha, job_id)
            return item, tentativas, _certidao_do_item(item), rotulo

    return None, [], None, ""

@app.get("/jobs", response_class=HTMLResponse)
def listar_jobs(
    request: Request,
    maquina: str | None = None,
    arquivo: int | None = None,
    orgao: str | None = None,
    status: str | None = None,
    desfecho: str | None = None,
    busca: str | None = None,
    pagina: int = 1,
    limite: int = carteira.ITENS_POR_PAGINA,
    mensagem: str | None = None,
    erro: str | None = None,
):
    estados = _maquinas_operacao()
    maquina_id, indice = _maquina_da_carteira(maquina)
    selecionada = estados[indice] if indice is not None and indice < len(estados) else None
    pagina = carteira.normalizar_pagina(pagina)
    limite = carteira.normalizar_limite(limite)
    maquinas_fila = _maquinas_da_fila()
    arquivos = carteira.arquivos_do_estado(selecionada)
    arquivo_atual = carteira.escolher_arquivo(arquivos, arquivo)
    arquivo_id = arquivo_atual["id"] if arquivo_atual else None
    filtros = carteira.filtros_contexto(
        maquina_id, arquivo_id, orgao, status, desfecho, busca
    )
    linhas, mudas, orgaos, total, contagens_status = carteira.listar(
        maquina_id, arquivo_id, orgao, status, desfecho, busca, pagina, limite,
        maquinas_fila, ler, cfg.rede.senha,
    )
    ctx = carteira.contexto(
        estados, selecionada, indice, filtros, arquivos, arquivo_atual, linhas,
        mudas, orgaos, total, contagens_status, pagina, limite,
    )
    ctx.update({"mensagem_carteira": mensagem, "erro_carteira": erro})
    return templates.TemplateResponse(request, "jobs.html", ctx)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def detalhe_job(
    request: Request, job_id: int, maquina: str | None = None,
    arquivo: int | None = None,
):
    job, tentativas, certidao, origem = _buscar_item(job_id, maquina)
    return templates.TemplateResponse(request, "job.html", {
        "job": job, "tentativas": tentativas, "certidao": certidao,
        "rotulos": consultas.ROTULOS,
        "origem": origem,
        "voltar_url": (
            _url_destino(
                "/jobs",
                maquina,
                arquivo=arquivo,
            ) if maquina or arquivo else "/jobs"
        ),
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

    ativo = api._robo_ativo_por_sinal(idade, cfg.alertas.heartbeat_timeout_s)
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
def saude(request: Request, maquina: int | None = None, dias: int = 7):
    return templates.TemplateResponse(
        request, "saude.html", _contexto_diagnostico(maquina, dias)
    )


@app.get("/diagnostico.json")
def baixar_diagnostico(maquina: int | None = None, dias: int = 7):
    ctx = _contexto_diagnostico(maquina, dias)
    nome = ctx["selecionada"].nome if ctx["selecionada"] else "diagnostico"
    arquivo = "".join(c if c.isalnum() else "_" for c in nome).strip("_")
    return JSONResponse(
        {
            "maquina": nome,
            "periodo": ctx["periodo"],
            "dias": ctx["dias"],
            "erro": ctx["erro_diagnostico"],
            "orgaos": ctx["diagnosticos"],
            "gerado_em": tempo.agora_iso(),
        },
        headers={
            "Content-Disposition": (
                f'attachment; filename="diagnostico_{arquivo or "acta"}.json"'
            )
        },
    )


# ----------------------------------------------------------------------
# Ações
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Envio de planilha em dois passos
# ----------------------------------------------------------------------
# As abas de um arquivo só existem depois de abri-lo, então não há como
# oferecer a escolha antes de recebê-lo. O arquivo passa primeiro por uma
# cópia temporária, é validado, e depois fica em `data/planilhas` com teto
# de três versões recentes. O token da URL é só o nome guardado, nunca um
# caminho arbitrário vindo do navegador.
def _guardar_planilha(caminho: Path) -> str:
    """Guarda em `data/planilhas` e devolve o token usado nas URLs."""
    return carteiras.guardar(cfg.banco, caminho, caminho.name).token


def _indice_para_planilha(indice: int | None) -> int | None:
    if cfg.rede.maquinas:
        return (
            indice
            if indice is not None and 0 <= indice < len(cfg.rede.maquinas)
            else 0
        )
    return 0 if cfg.rede.roda_robo else None


def _pegar_planilha(token: str, indice: int | None = None) -> dict | None:
    guardada = carteiras.buscar(cfg.banco, token)
    if guardada is None:
        return None
    return {
        "caminho": guardada.caminho,
        "maquina": _indice_para_planilha(indice),
        "nome": guardada.nome,
        "quando": guardada.quando_curto,
    }


def _mapa_das_abas(caminho: Path) -> list[dict]:
    """As abas do arquivo, cada uma com a automação que ela sugere.

    A sugestão vem do nome — aba "CRF" provavelmente é FGTS — mas é só
    sugestão: quem manda é a escolha na tela. Aba vazia e aba sem sugestão
    aparecem do mesmo jeito, porque sumir com elas faria a pessoa procurar
    uma aba que ela sabe que existe.
    """
    from cnd.ingestao.planilha import ABA_PARA_ORGAO, abas_da_planilha

    mapa = []
    for nome, linhas in abas_da_planilha(caminho):
        sugerido = ABA_PARA_ORGAO.get(nome.strip().upper(), ("", ""))[0]
        mapa.append({
            "nome": nome,
            "linhas": linhas,
            "sugerido": sugerido,
        })
    return mapa


def _ja_na_fila_no_mes(indice: int | None, orgao: str) -> dict:
    if cfg.rede.maquinas:
        if indice is None or not (0 <= indice < len(cfg.rede.maquinas)):
            return {}
        return remoto.ja_na_fila_no_mes(
            cfg.rede.maquinas[indice], cfg.rede.senha, orgao
        ) or {}

    with contextlib.closing(ler()) as conn:
        return consultas.ja_na_fila_no_mes(conn, orgao)


def _avisos_importacao(indice: int | None) -> dict[str, dict]:
    avisos = {}
    for opcao in automacoes():
        if not opcao["disponivel"]:
            continue
        info = _ja_na_fila_no_mes(indice, opcao["codigo"])
        itens = int(info.get("itens") or 0)
        if not itens:
            continue
        planilha = str(info.get("planilha") or "outro lote")
        avisos[opcao["codigo"]] = {
            "itens": itens,
            "planilha": planilha,
            "texto": (
                f"Já existem {itens} item(ns) de {opcao['rotulo']} neste mês "
                f"em {planilha}. Importar cria um lote novo e consulta o "
                f"portal de novo."
            ),
        }
    return avisos


async def _salvar_planilha_temporaria(arquivo: UploadFile) -> Path:
    nome = Path(arquivo.filename or "planilha.xlsx").name
    if not nome.lower().endswith((".xlsx", ".xlsm")):
        raise ValueError("Envie um arquivo .xlsx ou .xlsm.")

    # Quem chama só recebe o caminho quando dá certo — então quem falha
    # limpa a própria pasta AQUI. Antes, estourar o limite deixava um
    # `acta_upload_*` com o pedaço já gravado no %TEMP% para sempre: o
    # chamador redireciona com a mensagem de erro e não tem o que apagar,
    # porque nunca chegou a saber o nome da pasta.
    pasta = Path(tempfile.mkdtemp(prefix="acta_upload_"))
    destino = pasta / carteiras.nome_seguro(nome)
    try:
        tamanho = 0
        with destino.open("wb") as saida:
            while bloco := await arquivo.read(1 << 20):
                tamanho += len(bloco)
                if tamanho > comandos.LIMITE_DA_PLANILHA_MB * 1024 * 1024:
                    raise ValueError(
                        f"Planilha maior que {comandos.LIMITE_DA_PLANILHA_MB} MB."
                    )
                saida.write(bloco)
    except BaseException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    return destino


def _importar_planilha_local(caminho: Path, aba: str = "",
                             orgao: str = "", nome: str = "") -> dict:
    from cnd.ingestao.planilha import importar

    conn = conectar(cfg.banco)
    try:
        criar_schema(conn)
        nome_lote = nome or caminho.name
        lote_id, leitura = importar(
            conn, caminho, f"Importacao de {nome_lote}",
            [aba] if aba else None, orgao or None, arquivo_origem=nome_lote,
        )
        resposta = {
            "lote": lote_id,
            "criados": len(leitura.itens),
            # Os motivos vêm junto, como no envio pela rede: é deles que a
            # mensagem da tela monta o "12× é um CPF...".
            "rejeitados": [
                {"linha": r.linha, "valor": r.valor_original, "motivo": r.motivo}
                for r in leitura.rejeitados[:20]
            ],
            "total_rejeitados": len(leitura.rejeitados),
        }
    finally:
        conn.close()

    # Ver comandos.enviar_planilha: entregar a planilha não manda começar.
    comandos._marcar_parada_manual(cfg.banco)
    return resposta


def _automacao_valida(orgao: str) -> str:
    """Recusa automação que esta máquina não roda.

    Confere o ÓRGÃO, e não mais a aba: com a aba de nome livre ("Clientes
    GO"), ela deixou de dizer o que roda. O seletor já só oferece as
    prontas, mas quem manda o formulário é o navegador — sem esta
    conferência bastaria editar o HTML para encher a fila de itens que
    nenhum adapter sabe executar, e o erro só apareceria lá na frente, um
    a um, dentro do worker.
    """
    escolhido = (orgao or "").strip().upper()
    if not escolhido:
        return ""          # sem escolha: vale o atalho pelo nome da aba
    for opcao in automacoes():
        if opcao["codigo"] == escolhido:
            if not opcao["disponivel"]:
                raise ValueError(f"{opcao['rotulo']}: {opcao['motivo']}.")
            return escolhido
    raise ValueError(f"Automação desconhecida: {orgao!r}.")


def _mensagem_importacao(resposta: dict, inicio: str | None = None) -> str:
    criados = int(resposta.get("criados") or 0)
    rejeitados = int(resposta.get("total_rejeitados") or 0)
    partes = [f"{criados} item(ns) entraram na fila"]
    if rejeitados:
        # "12 rejeitado(s)" não diz o que fazer. O motivo é o que resolve —
        # e quase sempre são poucos motivos repetidos em muitas linhas, daí
        # contar por motivo em vez de listar linha a linha.
        contagem: dict[str, int] = {}
        for item in resposta.get("rejeitados") or []:
            motivo = str(item.get("motivo") or "").strip()
            if motivo:
                contagem[motivo] = contagem.get(motivo, 0) + 1
        if contagem:
            maiores = sorted(contagem.items(), key=lambda x: -x[1])[:2]
            detalhe = "; ".join(
                f"{quantas}× {motivo}" for motivo, quantas in maiores
            )
            partes.append(f"{rejeitados} rejeitado(s) — {detalhe}")
        else:
            partes.append(f"{rejeitados} rejeitado(s)")
    if inicio:
        partes.append(inicio)
    return "; ".join(partes)


def _resetar_pausa_local(orgao: str) -> None:
    with contextlib.closing(escrever()) as conn:
        breaker.fechar(conn, orgao)
        conn.execute("UPDATE breaker SET aberturas = 0 WHERE orgao = ?", (orgao,))


@app.post("/acoes/maquina/{indice}/planilha")
async def acao_enviar_planilha(indice: int, arquivo: UploadFile = PLANILHA_ENVIADA):
    """Passo 1: recebe o arquivo e leva para a tela de mapeamento.

    Não importa nada ainda. As abas do arquivo só existem depois de
    abri-lo, então a escolha de "qual aba roda qual automação" só pode ser
    oferecida agora — e é ela que decide o que entra na fila.
    """
    try:
        caminho = await _salvar_planilha_temporaria(arquivo)
    except Exception as erro:
        log.warning("envio_planilha_falhou",
                    extra={"maquina": indice, "erro": str(erro)})
        return RedirectResponse(_url_destino("/", indice, erro=str(erro)),
                                status_code=303)

    try:
        abas = _mapa_das_abas(caminho)
    except Exception as erro:
        shutil.rmtree(caminho.parent, ignore_errors=True)
        return RedirectResponse(
            _url_destino("/", indice,
                         erro=f"Não consegui ler a planilha: {erro}"),
            status_code=303)

    if not abas:
        shutil.rmtree(caminho.parent, ignore_errors=True)
        return RedirectResponse(
            _url_destino("/", indice, erro="A planilha não tem nenhuma aba."),
            status_code=303)

    try:
        token = _guardar_planilha(caminho)
    except Exception as erro:
        log.warning("guardar_planilha_falhou",
                    extra={"maquina": indice, "erro": str(erro)})
        return RedirectResponse(_url_destino("/", indice, erro=str(erro)),
                                status_code=303)
    finally:
        shutil.rmtree(caminho.parent, ignore_errors=True)

    return RedirectResponse(_url_destino(f"/planilha/{token}", indice),
                            status_code=303)


@app.get("/planilha/{token}", response_class=HTMLResponse)
def tela_mapear_planilha(
    request: Request, token: str, maquina: int | None = None,
    erro: str | None = None, mensagem: str | None = None,
):
    """Passo 2: o que a planilha tem, e o que fazer com cada aba."""
    indice = _indice_para_planilha(maquina)
    guardada = _pegar_planilha(token, indice)
    if guardada is None:
        return RedirectResponse(
            _url_destino("/", indice,
                         erro="Planilha guardada não encontrada."),
            status_code=303)
    autos = automacoes()
    abas = _mapa_das_abas(guardada["caminho"])
    return templates.TemplateResponse(request, "planilha.html", {
        "token": token,
        "arquivo": guardada["nome"],
        "abas": abas,
        "abas_com_linhas": sum(1 for aba in abas if aba["linhas"]),
        "total_linhas_planilha": sum(aba["linhas"] for aba in abas),
        "automacoes": autos,
        "automacoes_por_codigo": {a["codigo"]: a for a in autos},
        "indisponiveis": [a for a in autos if not a["disponivel"]],
        "avisos": _avisos_importacao(indice),
        "selecionada_idx": indice,
        "erro_planilha": _formatar_flash(erro),
        "mensagem_planilha": _formatar_flash(mensagem),
        "pagina": "painel",
    })


@app.post("/planilha/{token}/cancelar")
def acao_cancelar_planilha(token: str, maquina: int | None = None):
    del token
    indice = _indice_para_planilha(maquina)
    return RedirectResponse(
        _url_destino("/", indice, mensagem="Nada entrou na fila."),
        status_code=303,
    )


@app.post("/planilha/{token}/confirmar")
async def acao_confirmar_planilha(
    request: Request, token: str, maquina: int | None = None,
):
    """Passo 3: importa cada aba na automação que você escolheu.

    Um envio só resolve a planilha inteira: a carteira vem com RFB e CRF
    no mesmo arquivo, e mandá-lo duas vezes seria trabalho repetido para
    um problema que é de tela, não de dados.
    """
    indice = _indice_para_planilha(maquina)
    guardada = _pegar_planilha(token, indice)
    if guardada is None:
        return RedirectResponse(
            _url_destino("/", indice,
                         erro="Planilha guardada não encontrada."),
            status_code=303)

    indice = guardada["maquina"]
    caminho = guardada["caminho"]
    formulario = await request.form()

    # Cada aba manda um campo "orgao__<nome da aba>"; vazio quer dizer
    # "não importar esta". Sem par escolhido não há o que fazer.
    pares: list[tuple[str, str]] = []
    for chave, valor in formulario.items():
        if chave.startswith("orgao__") and str(valor).strip():
            pares.append((chave[len("orgao__"):], str(valor).strip()))

    if not pares:
        return RedirectResponse(
            _url_destino(f"/planilha/{token}", indice,
                         erro="Escolha ao menos uma aba."),
            status_code=303,
        )

    try:
        for _, escolhido in pares:
            _automacao_valida(escolhido)

        criados = rejeitados = 0
        detalhes: list[dict] = []
        ultimo_lote = None
        for aba, escolhido in pares:
            if cfg.rede.maquinas:
                if indice is None or not (0 <= indice < len(cfg.rede.maquinas)):
                    raise ValueError("Máquina não encontrada.")
                resposta = remoto.enviar_planilha(
                    cfg.rede.maquinas[indice], caminho, cfg.rede.senha,
                    aba=aba, orgao=escolhido, nome=guardada["nome"])
            else:
                if indice not in (0, None) or not cfg.rede.roda_robo:
                    raise ValueError("Máquina não encontrada.")
                resposta = _importar_planilha_local(
                    caminho, aba, escolhido, guardada["nome"])
            criados += int(resposta.get("criados") or 0)
            rejeitados += int(resposta.get("total_rejeitados") or 0)
            detalhes.extend(resposta.get("rejeitados") or [])
            ultimo_lote = int(resposta.get("lote") or 0) or ultimo_lote

        resumo = {"criados": criados, "total_rejeitados": rejeitados,
                  "rejeitados": detalhes}
        return RedirectResponse(
            _url_destino("/jobs", indice, arquivo=ultimo_lote,
                         mensagem=_mensagem_importacao(resumo)),
            status_code=303)
    except Exception as erro:
        log.warning("importacao_falhou",
                    extra={"maquina": indice, "erro": str(erro)})
        return RedirectResponse(_url_destino("/", indice, erro=str(erro)),
                                status_code=303)


@app.post("/acoes/maquina/{indice}/robo/{acao}")
def acao_robo_remoto(indice: int, acao: str):
    if acao not in {"iniciar", "parar"}:
        return RedirectResponse(
            _url_destino("/", indice, erro="Comando inválido."),
            status_code=303,
        )
    if not cfg.rede.maquinas:
        return RedirectResponse(
            _url_destino("/", indice, erro="Nenhuma máquina remota cadastrada."),
            status_code=303,
        )
    if indice < 0 or indice >= len(cfg.rede.maquinas):
        return RedirectResponse(
            _url_destino("/", erro="Máquina não encontrada."),
            status_code=303,
        )

    try:
        resposta = remoto.comandar_robo(
            cfg.rede.maquinas[indice], iniciar=acao == "iniciar",
            senha=cfg.rede.senha
        )
    except Exception as erro:
        log.warning(
            "comando_remoto_falhou",
            extra={"maquina": indice, "acao": acao, "erro": str(erro)},
        )
        return RedirectResponse(
            _url_destino("/", indice, erro=str(erro)),
            status_code=303,
        )

    situacao = resposta.get("mensagem") or resposta.get("situacao") or (
        "Robô iniciado." if acao == "iniciar" else "Parada do robô solicitada."
    )
    if not str(situacao).lower().startswith("robô") and acao == "iniciar":
        situacao = f"Robô {str(situacao).strip().rstrip('.!?').lower()}."
    elif acao == "parar" and "parada" in str(situacao).lower():
        situacao = "Parada do robô solicitada."
    return RedirectResponse(
        _url_destino("/", indice, mensagem=situacao),
        status_code=303,
    )


@app.post("/acoes/maquina/{indice}/breaker/retomar")
def acao_resetar_pausa_toda(indice: int):
    """Botão sempre visível: destrava a máquina sem exigir saber o órgão."""
    try:
        if cfg.rede.maquinas:
            if indice < 0 or indice >= len(cfg.rede.maquinas):
                raise RuntimeError("Maquina nao encontrada.")
            resposta = remoto.retomar_todas_as_pausas(
                cfg.rede.maquinas[indice], cfg.rede.senha
            )
            if resposta is None:
                raise RuntimeError("Nao consegui resetar a pausa da maquina.")
        else:
            if indice != 0:
                raise RuntimeError("Maquina nao encontrada.")
            for orgao_ativo in cfg.ativos():
                _resetar_pausa_local(orgao_ativo.codigo)
    except Exception as erro:
        log.warning("reset_pausa_falhou",
                    extra={"maquina": indice, "erro": str(erro)})
        return RedirectResponse(_url_destino("/", indice, erro=str(erro)),
                                status_code=303)

    log.info("breaker_retomado_manualmente", extra={"maquina": indice})
    return RedirectResponse(
        _url_destino("/", indice, mensagem="Pausa resetada."),
        status_code=303,
    )


@app.post("/acoes/maquina/{indice}/breaker/{orgao}/retomar")
def acao_resetar_pausa_maquina(indice: int, orgao: str):
    try:
        if cfg.rede.maquinas:
            if indice < 0 or indice >= len(cfg.rede.maquinas):
                raise RuntimeError("Maquina nao encontrada.")
            resposta = remoto.retomar_pausa(
                cfg.rede.maquinas[indice], orgao, cfg.rede.senha
            )
            if resposta is None:
                raise RuntimeError("Nao consegui resetar a pausa da maquina.")
        else:
            if indice != 0:
                raise RuntimeError("Maquina nao encontrada.")
            _resetar_pausa_local(orgao)
    except Exception as erro:
        log.warning(
            "reset_pausa_falhou",
            extra={"maquina": indice, "orgao": orgao, "erro": str(erro)},
        )
        return RedirectResponse(
            _url_destino("/", indice, erro=str(erro)),
            status_code=303,
        )

    log.info("breaker_retomado_manualmente", extra={"orgao": orgao, "maquina": indice})
    return RedirectResponse(
        _url_destino("/", indice, mensagem="Pausa resetada."),
        status_code=303,
    )


@app.post("/acoes/maquina/{indice}/atualizar")
def acao_atualizar_maquina(
    indice: int, request: Request, origem: str = Form(default=""),
    sha256: str = Form(default=""),
):
    if not cfg.rede.maquinas:
        return RedirectResponse(
            _url_destino("/", indice, erro="Nenhuma máquina remota cadastrada."),
            status_code=303,
        )
    if indice < 0 or indice >= len(cfg.rede.maquinas):
        return RedirectResponse(
            _url_destino("/", erro="Máquina não encontrada."),
            status_code=303,
        )

    origem = (origem or _origem_atualizacao(request)).strip()
    sha256 = (sha256 or "").strip()
    if not sha256:
        return RedirectResponse(
            _url_destino("/", indice,
                         erro="Informe o SHA-256 do pacote. Ele sai impresso "
                              "ao publicar (python empacotar/publicar.py)."),
            status_code=303,
        )
    try:
        resposta = remoto.atualizar(
            cfg.rede.maquinas[indice], cfg.rede.senha, origem, sha256
        )
    except Exception as erro:
        log.warning(
            "atualizacao_remota_falhou",
            extra={"maquina": indice, "origem": origem, "erro": str(erro)},
        )
        return RedirectResponse(
            _url_destino("/", indice, erro=f"Atualização não iniciou: {erro}"),
            status_code=303,
        )

    situacao = resposta.get("mensagem") or resposta.get("situacao") or "Atualização iniciada."
    return RedirectResponse(
        _url_destino("/", indice, mensagem=situacao),
        status_code=303,
    )


@app.post("/acoes/reenfileirar")
def acao_reenfileirar(
    orgao: str = Form(default=""), maquina: str = Form(default=""),
    arquivo: int = Form(default=0),
):
    maquinas = {
        ident: maquina_cfg
        for ident, _rotulo, maquina_cfg in _maquinas_da_fila()
    }
    maquina_cfg = maquinas.get(maquina)
    if maquina_cfg is None:
        with contextlib.closing(escrever()) as conn:
            quantidade = fila.reenfileirar_falhados(
                conn, orgao or None, arquivo or None
            )
    else:
        resposta = remoto.reenfileirar_falhados(
            maquina_cfg, cfg.rede.senha, orgao or None, arquivo or None
        )
        if resposta is None:
            return RedirectResponse(
                _url_destino(
                    "/jobs",
                    maquina=maquina or None,
                    arquivo=arquivo or None,
                    erro="Não consegui reenviar falhas da máquina.",
                ),
                status_code=303,
            )
        quantidade = int(resposta.get("quantidade") or 0)
    log.info(
        "reenfileirados",
        extra={
            "orgao": orgao or "todos",
            "maquina": maquina or "local",
            "arquivo": arquivo or None,
            "quantidade": quantidade,
        },
    )
    return RedirectResponse(
        _url_destino(
            "/jobs",
            maquina=maquina or None,
            arquivo=arquivo or None,
            mensagem=f"{quantidade} item(ns) de volta na fila.",
        ),
        status_code=303,
    )


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
    _resetar_pausa_local(orgao)
    log.info("breaker_retomado_manualmente", extra={"orgao": orgao})
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------

def _orgaos_do_download(request: Request) -> tuple[str, ...]:
    escolhidos = []
    for valor in request.query_params.getlist("orgao"):
        codigo = str(valor or "").strip().upper()
        if codigo and codigo not in escolhidos:
            escolhidos.append(codigo)
    return tuple(escolhidos)


def _sufixo_download(orgaos: tuple[str, ...]) -> str:
    if not orgaos:
        return ""
    if len(orgaos) > 1:
        return "_selecionadas"
    return "_" + re.sub(r"[^0-9a-z_]+", "_", orgaos[0].lower()).strip("_")


@app.get("/relatorio/{mes}.xlsx")
def baixar_relatorio(request: Request, mes: str):
    """Planilha do mês, opcionalmente filtrada por automações.

    Mesmo recorte do pacote de certidões e da tela: quem marca Receita e
    SEFAZ-GO espera receber só essas automações no arquivo.
    """
    orgaos = _orgaos_do_download(request)
    with contextlib.closing(ler()) as conn:
        conteudo = relatorio.gerar_bytes(conn, relatorio.Recorte(mes, orgaos))
    sufixo = _sufixo_download(orgaos)
    return Response(
        conteudo,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="relatorio_{mes}{sufixo}.xlsx"'},
    )


@app.get("/certidoes/{mes}.zip")
def baixar_pdfs(
    request: Request, mes: str, somente_negativas: bool = False,
):
    """Pacote das certidões emitidas no mês (`2026-08`), por automações.

    `?somente_negativas=1` deixa de fora as CPEN, para quem precisa só das
    empresas totalmente limpas. Repetir `?orgao=...` entrega só as
    automações escolhidas.
    """
    orgaos = _orgaos_do_download(request)
    with contextlib.closing(ler()) as conn:
        conteudo = relatorio.zipar_pdfs(
            conn, mes, somente_negativas,
            nomes={codigo: o.rotulo for codigo, o in cfg.orgaos.items()},
            orgao=orgaos)
    sufixo = "_negativas" if somente_negativas else ""
    sufixo = _sufixo_download(orgaos) + sufixo
    return Response(
        conteudo, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="certidoes_{mes}{sufixo}.zip"'},
    )
