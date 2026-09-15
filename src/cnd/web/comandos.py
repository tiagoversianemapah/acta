"""Rotas que MEXEM na máquina: enviar planilha, parar robô e atualizar.

Separadas das de leitura de propósito. Até aqui toda a API era só de
leitura, e era isso que tornava seguro publicar o painel na rede interna: na
pior hipótese alguém enxergava dados. Com estas rotas, quem alcança a porta
passa a poder fazer a máquina trabalhar — então elas têm duas travas que as
outras não têm.

A PRIMEIRA é a senha, aqui OBRIGATÓRIA. Sem `[rede] senha` preenchida elas
recusam tudo em vez de ficarem abertas: a capacidade perigosa nasce
desligada e só existe depois de alguém configurá-la de propósito.

A SEGUNDA é a área de trabalho. O robô cego move o mouse de verdade e lê a
tela; iniciar pela web foi desativado justamente para não criar um processo
fora da sessão normal do Windows.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from cnd.adapters.base import adapter_existe
from cnd.core import breaker, controle, fila, tempo
from cnd.core.modelos import Status
from cnd.infra import maquina
from cnd.infra.config import Config
from cnd.infra.db import caminho_parada_manual, conectar
from cnd.infra.log import obter

log = obter("web")

# Planilha da carteira inteira não passa de alguns megabytes; o limite
# existe para um envio errado não encher o disco da máquina do robô.
LIMITE_DA_PLANILHA_MB = 25

# Fora da assinatura porque o FastAPI exige o marcador como padrão, e
# chamada em argumento padrão é avaliada uma vez na importação.
ARQUIVO_ENVIADO = File(...)
ORIGEM_ATUALIZACAO = Form(...)
ROBO_SEM_SINAL_RECUPERAVEL_S = 180.0

# Digitada por extenso para zerar a máquina. Ninguém envia isto por engano.
CONFIRMACAO_DE_ZERAGEM = "APAGAR TUDO"

ATUALIZADOR_PS1 = r"""
param(
    [Parameter(Mandatory = $true)][string]$Origem,
    [Parameter(Mandatory = $true)][string]$Pasta,
    [Parameter(Mandatory = $true)][string]$Sha256
)

$ErrorActionPreference = "Stop"
Start-Sleep -Seconds 2

$logDir = Join-Path $Pasta "data\logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir "atualizacao.log"
$origemLog = Join-Path $Pasta "data\ultima_origem_atualizacao.txt"
function Registrar([string]$Mensagem) {
    $linha = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $Mensagem
    Add-Content -Path $log -Value $linha
}
function PainelRespondendo() {
    try {
        Invoke-WebRequest "http://127.0.0.1:8000/ping" `
            -UseBasicParsing -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}
function IniciarPainelDireto() {
    Start-Process -FilePath (Join-Path $Pasta "cnd.exe") `
        -ArgumentList "painel --host 0.0.0.0" -WindowStyle Minimized
}

$zip = Join-Path $env:TEMP "acta-atualizacao.zip"
$tmp = Join-Path $env:TEMP "acta-atualizacao"

try {
    Set-Content -Path $origemLog -Value $Origem -Encoding UTF8
    Registrar "baixando de $Origem"
    Invoke-WebRequest "$Origem/acta.zip" -OutFile $zip -UseBasicParsing

    # Sempre, e nao "se veio hash". O zip baixado por HTTP simples vira
    # ACTA.exe e cnd.exe nesta maquina: conferir e a unica coisa entre o
    # que o console publicou e o que roda aqui. Falhar aqui e barato -
    # nada foi trocado ainda, os processos nem foram parados.
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $Sha256.ToLowerInvariant()) {
        throw "hash do pacote nao confere: baixei $hash, esperava $Sha256"
    }
    Registrar "hash conferido"

    # Conferir que morreram, e nao dormir 2s torcendo. Em 17/08/2026 o
    # robo ainda estava encerrando quando a copia comecou, segurou
    # _internal\libcrypto-3.dll, a copia parou no meio e a maquina ficou
    # com instalacao pela metade e painel morto - so voltou por AnyDesk.
    # Mesma licao do _matar_edge em adapters/federal/rfb/cego.py.
    Registrar "parando processos"
    try { schtasks /End /TN "ACTA Painel" 2>$null | Out-Null } catch {}
    $vivos = @()
    $limiteMorte = (Get-Date).AddSeconds(30)
    while ($true) {
        $vivos = @(Get-Process cnd, ACTA -ErrorAction SilentlyContinue)
        if ($vivos.Count -eq 0 -or (Get-Date) -ge $limiteMorte) { break }
        $vivos | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    if ($vivos.Count -gt 0) {
        Registrar ("AVISO: " + $vivos.Count + " processo(s) ainda vivos; a copia pode falhar")
    } else {
        Registrar "processos encerrados"
    }

    Registrar "extraindo"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $zip -DestinationPath $tmp -Force

    # Mesmo com tudo morto, o Windows solta o arquivo com atraso (antivirus,
    # indexador). Insistir por ~15s custa menos que deixar a pasta pela
    # metade, que e o unico estado do qual nao da para sair pela rede.
    Registrar "copiando para $Pasta"
    $tentativaCopia = 0
    while ($true) {
        try {
            Copy-Item "$tmp\*" $Pasta -Recurse -Force -ErrorAction Stop
            break
        } catch {
            $tentativaCopia++
            if ($tentativaCopia -ge 5) { throw }
            Registrar ("copia falhou (" + $tentativaCopia + "/5): " + $_.Exception.Message)
            Start-Sleep -Seconds 3
        }
    }

    Registrar "subindo painel"
    $agendadorOk = $false
    schtasks /Run /TN "ACTA Painel" | Out-Null
    $agendadorOk = ($LASTEXITCODE -eq 0)
    if ($agendadorOk) {
        Registrar "painel acionado pelo agendador"
    } else {
        Registrar "agendador indisponivel; usando inicio direto"
    }
    # Dar tempo ao agendador antes de chamar o fallback. Com 5s fixos, o
    # painel do agendador quase nunca tinha subido ainda, o fallback abria um
    # SEGUNDO painel, e sobrava um processo extra segurando arquivo - que e
    # justamente o que trava a copia da proxima atualizacao.
    $limitePainel = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $limitePainel -and -not (PainelRespondendo)) {
        Start-Sleep -Seconds 2
    }
    if (-not (PainelRespondendo)) {
        IniciarPainelDireto
        Registrar "painel iniciado por fallback direto"
    } else {
        Registrar "painel respondendo apos atualizacao"
    }

    Registrar "concluido"
} catch {
    Registrar ("ERRO: " + $_.Exception.Message)
    throw
} finally {
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
"""


def _recado_do_controle(acao: str, resultado) -> str:
    """O que a tela mostra depois. Diz o que MUDOU, não o que foi clicado."""
    return {
        "estacionar": "Automação estacionada. Retome quando quiser.",
        "retomar": "Automação retomada de onde parou.",
        "cancelar": "Automação cancelada. Os itens continuam guardados.",
        "agora": "Automação posta na frente da fila.",
    }.get((acao or "").strip().lower(), "Fila atualizada.")


def montar(obter_config: Callable[[], Config], raiz: Path) -> APIRouter:
    roteador = APIRouter(prefix="/api")

    def exigir_senha_configurada(cfg: Config) -> None:
        if not cfg.rede.senha:
            raise HTTPException(
                status_code=403,
                detail="Esta máquina não aceita comandos pela rede. Defina "
                       "[rede] senha no config.toml dela para liberar.")

    def exigir_area_de_trabalho() -> None:
        if not maquina.area_de_trabalho_disponivel():
            raise HTTPException(
                status_code=409,
                detail="A área de trabalho desta máquina está bloqueada. O "
                       "robô move o mouse de verdade e lê a tela — precisa da "
                       "sessão do Windows aberta e destravada. Entre nela "
                       "pelo AnyDesk, destrave e tente de novo.")

    @roteador.post("/fila/{lote_id}/{orgao}")
    def controlar_fila(lote_id: int, orgao: str, acao: str = Form(...)):
        """Estaciona, cancela, retoma ou põe na frente uma automação.

        Por planilha E automação: a carteira vem com RFB e CRF no mesmo
        arquivo, e parar um não pode parar o outro. Ver core/controle.py.

        Não mexe no robô. Se ele estiver rodando um item desta automação
        agora, aquele item termina — estacionar vale do próximo em diante.
        Interromper no meio deixaria o portal com uma consulta aberta e a
        certidão sem baixar, que é pior do que esperar dez segundos.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        acoes = {
            "estacionar": lambda c: controle.definir(
                c, lote_id, orgao, controle.ESTACIONADA),
            "retomar": lambda c: controle.definir(
                c, lote_id, orgao, controle.ATIVA),
            "cancelar": lambda c: controle.definir(
                c, lote_id, orgao, controle.CANCELADA),
            "agora": lambda c: controle.priorizar(c, lote_id, orgao),
        }
        executar = acoes.get((acao or "").strip().lower())
        if executar is None:
            raise HTTPException(
                status_code=400,
                detail=f"Ação desconhecida: {acao!r}. "
                       f"Use: {', '.join(sorted(acoes))}.")

        conn = conectar(cfg.banco)
        try:
            resultado = executar(conn)
            conn.commit()
        finally:
            conn.close()

        log.info("fila_controlada", extra={
            "lote": lote_id, "orgao": orgao, "acao": acao,
            "situacao": resultado.situacao, "prioridade": resultado.prioridade,
        })
        return _resposta(_recado_do_controle(acao, resultado))

    @roteador.post("/identidade")
    def renomear_maquina(nome: str = Form(...)):
        """Troca o nome com que ESTA máquina se apresenta.

        Existe porque o nome vive no config.toml dela, que não viaja com o
        pacote de atualização (é da instalação, não do programa) e não se
        alcança pela rede: só entrando na máquina. Enquanto uma máquina
        rodava uma automação só, o nome "PC Receita Federal 01" descrevia a
        verdade; com várias, ele passou a mentir — e corrigir isso exigia
        sessão de AnyDesk por máquina, que é justamente o que o painel
        existe para evitar.

        Escreve UMA chave, `[rede] nome`, por substituição da linha: o
        arquivo é cheio de comentário explicativo, e reescrevê-lo a partir
        do TOML lido apagaria tudo isso.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        limpo = " ".join((nome or "").split())
        if not limpo:
            raise HTTPException(status_code=400, detail="Nome vazio.")
        if len(limpo) > 60:
            raise HTTPException(status_code=400,
                                detail="Nome longo demais (máx. 60).")
        if '"' in limpo or "\\" in limpo:
            # Iriam quebrar a string TOML que a linha vira.
            raise HTTPException(status_code=400,
                                detail='Nome não pode ter aspas nem barras.')

        from cnd.infra.config import CAMINHO_PADRAO
        from cnd.infra.config import carregar as recarregar

        arquivo = Path(CAMINHO_PADRAO)
        try:
            texto = arquivo.read_text(encoding="utf-8-sig")
        except OSError as erro:
            raise HTTPException(status_code=500,
                                detail=f"Não li o config: {erro}") from erro

        # Só a chave `nome` da seção [rede], e não a de outra seção: cada
        # órgão também tem um `nome`, e trocar o primeiro que aparecesse
        # renomearia a Receita Federal.
        secao = re.search(r"(?ms)^\[rede\]\s*$(.*?)(?=^\[|\Z)", texto)
        if secao is None:
            raise HTTPException(status_code=500,
                                detail="Não achei a seção [rede] no config.")
        corpo = secao.group(1)
        trocado, quantas = re.subn(
            r"(?m)^(\s*nome\s*=\s*)(\".*?\"|'.*?')",
            lambda m: f'{m.group(1)}"{limpo}"', corpo, count=1)
        if not quantas:
            raise HTTPException(
                status_code=500,
                detail="Não achei a chave 'nome' dentro de [rede].")

        novo = texto[:secao.start(1)] + trocado + texto[secao.end(1):]
        try:
            arquivo.write_text(novo, encoding="utf-8")
        except OSError as erro:
            raise HTTPException(status_code=500,
                                detail=f"Não gravei o config: {erro}") from erro

        # Sem isto o nome só mudaria no próximo restart do painel, e quem
        # renomeou veria o nome velho e concluiria que não funcionou.
        from cnd.web import app as modulo_app
        with contextlib.suppress(Exception):
            modulo_app.cfg = recarregar()

        log.info("maquina_renomeada", extra={"nome": limpo})
        return _resposta(f"Máquina renomeada para {limpo}.")

    @roteador.post("/orgao/{orgao}/ativo")
    def definir_orgao_ativo(orgao: str, ativo: bool = Form(default=True)):
        """Liga ou desliga uma automação existente no config desta máquina."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        codigo = orgao.strip().upper()
        configurado = cfg.orgaos.get(codigo)
        if configurado is None:
            raise HTTPException(
                status_code=404,
                detail=f"Não achei [orgaos.{codigo}] no config.",
            )
        if ativo and not adapter_existe(configurado.adapter):
            raise HTTPException(
                status_code=409,
                detail=f"Adapter {configurado.adapter} não existe.",
            )

        from cnd.infra import ajustes
        try:
            ajustes.gravar_booleano(f"orgaos.{codigo}", "ativo", ativo)
        except OSError as erro:
            raise HTTPException(
                status_code=500,
                detail=f"Não gravei o config: {erro}",
            ) from erro

        from cnd.infra.config import carregar as recarregar
        from cnd.web import app as modulo_app
        with contextlib.suppress(Exception):
            modulo_app.cfg = recarregar()

        rotulo = configurado.rotulo
        estado = "ligada" if ativo else "desligada"
        log.info("orgao_config_alterado",
                 extra={"orgao": codigo, "ativo": ativo})
        return _resposta(f"{rotulo} {estado}.")

    @roteador.post("/ritmo/{orgao}/resetar")
    def resetar_ritmo(orgao: str,
                      intervalo_s: float | None = Form(default=None)):
        """Reseta o intervalo adaptativo de um órgão sem tocar na fila.

        O ritmo fica no banco para sobreviver a reinício, mas isso também faz
        um castigo antigo atravessar pilotos novos. Esta rota dá ao operador
        um reset cirúrgico: só muda `ritmo`, não apaga jobs, tentativas,
        certidões nem calibragem.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        codigo = orgao.strip().upper()
        configurado = cfg.orgaos.get(codigo)
        if configurado is None:
            raise HTTPException(
                status_code=404,
                detail=f"Não achei [orgaos.{codigo}] no config.",
            )

        p = configurado.pacing
        alvo = p.intervalo_inicial_s if intervalo_s is None else float(intervalo_s)
        teto = max(1.0, p.intervalo_teto_s, p.intervalo_inicial_s)
        if not 0.0 <= alvo <= teto:
            raise HTTPException(
                status_code=400,
                detail=f"intervalo_s deve ficar entre 0 e {teto:g}.",
            )

        with contextlib.closing(conectar(cfg.banco)) as conn:
            conn.execute(
                """
                INSERT INTO ritmo
                    (orgao, intervalo_s, consultas_limpas, atualizado_em)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(orgao) DO UPDATE SET
                    intervalo_s = excluded.intervalo_s,
                    consultas_limpas = 0,
                    atualizado_em = excluded.atualizado_em
                """,
                (codigo, alvo, tempo.agora_iso()),
            )

        log.warning("ritmo_resetado_manualmente",
                    extra={"orgao": codigo, "intervalo_s": round(alvo, 2)})
        return _resposta(
            f"Ritmo de {configurado.rotulo} resetado para {alvo:g}s."
        )

    @roteador.post("/planilha")
    async def enviar_planilha(arquivo: UploadFile = ARQUIVO_ENVIADO,
                              aba: str = Form(default=""),
                              orgao: str = Form(default=""),
                              nome: str = Form(default="")):
        """Recebe a planilha e cria o lote nesta máquina.

        Resolve o caminho que hoje obriga a entrar por AnyDesk em cada
        máquina só para arrastar um arquivo — quatro sessões remotas por mês
        para uma tarefa de dez segundos.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        nome_recebido = Path(arquivo.filename or "planilha.xlsx").name
        if not nome_recebido.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(status_code=400,
                                detail="Envie um arquivo .xlsx.")
        from cnd.infra import carteiras

        nome_arquivo = carteiras.nome_seguro(nome_recebido)
        nome_lote = carteiras.nome_seguro(nome or nome_recebido)

        destino = Path(tempfile.mkdtemp(prefix="acta_")) / nome_arquivo
        try:
            tamanho = 0
            with open(destino, "wb") as saida:
                while bloco := await arquivo.read(1 << 20):
                    tamanho += len(bloco)
                    if tamanho > LIMITE_DA_PLANILHA_MB * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail=f"planilha maior que "
                                   f"{LIMITE_DA_PLANILHA_MB} MB.")
                    saida.write(bloco)

            from cnd.infra.db import conectar, criar_schema
            from cnd.ingestao.planilha import importar

            conn = conectar(cfg.banco)
            try:
                criar_schema(conn)
                # `aba` é ONDE estão os dados; `orgao` é O QUE rodar com
                # eles. Sem orgao vale o atalho antigo (aba RFB é Receita),
                # que a linha de comando continua usando.
                escolhidas = [aba.strip()] if aba.strip() else None
                lote_id, leitura = importar(conn, destino,
                                            f"Importação de {nome_lote}",
                                            escolhidas, orgao.strip() or None,
                                            arquivo_origem=nome_lote)
            finally:
                conn.close()

            # Planilha entregue não é ordem de começar. Sem este marcador o
            # vigia do painel veria fila cheia e robô sem sinal e relançaria
            # sozinho em menos de um minuto — exatamente o começo automático
            # que se quis tirar. Apertar "Iniciar robô" limpa o marcador.
            _marcar_parada_manual(cfg.banco)

            return {
                "lote": lote_id,
                "criados": len(leitura.itens),
                "rejeitados": [
                    {"linha": r.linha, "valor": r.valor_original,
                     "motivo": r.motivo} for r in leitura.rejeitados[:20]],
                "total_rejeitados": len(leitura.rejeitados),
            }
        finally:
            shutil.rmtree(destino.parent, ignore_errors=True)

    @roteador.post("/zerar")
    def zerar_maquina(confirmar: str = Form(default="")):
        """Apaga TUDO nesta máquina: planilhas, itens, tentativas e PDFs.

        Não tem desfazer, então não basta alcançar a rota: é preciso
        escrever a confirmação por extenso. A senha da rede sozinha
        protegeria contra estranhos, não contra um clique errado de quem
        tem a senha — que é o risco real aqui.

        Calibragem, config.toml e logs ficam: ver infra/limpeza.py.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        if confirmar.strip().upper() != CONFIRMACAO_DE_ZERAGEM:
            raise HTTPException(
                status_code=400,
                detail=f"Para zerar a máquina, envie confirmar="
                       f"{CONFIRMACAO_DE_ZERAGEM}.",
            )
        if _robo_rodando(cfg.banco) and not _encerrar_robo_ocioso(cfg.banco):
            raise HTTPException(
                status_code=409,
                detail="O robô está em execução. Pare o robô antes de zerar.",
            )

        from cnd.infra import limpeza
        from cnd.infra.db import conectar, criar_schema

        # Sem este try, uma falha aqui escapava para o FastAPI e virava um
        # "500 Internal Server Error" sem corpo, sem log e sem pista: o
        # botao nao funcionava e a maquina nao dizia por que. O detalhe vai
        # para o Registro, que e onde se diagnostica; quem apertou o botao
        # recebe recado curto.
        conn = conectar(cfg.banco)
        try:
            criar_schema(conn)
            resultado = limpeza.zerar(
                conn, (cfg.pasta_certidoes, cfg.pasta_evidencias))
        except Exception as erro:
            log.exception("falha_ao_zerar", extra={
                "erro": f"{type(erro).__name__}: {erro}"[:500],
                "banco": str(cfg.banco)})
            raise HTTPException(
                status_code=500,
                detail="Nao consegui zerar a maquina. O motivo ficou no "
                       "Registro desta maquina (aba Diagnostico).",
            ) from erro
        finally:
            conn.close()

        return _resposta(
            f"Máquina zerada: {resultado.como_texto()}.",
            f"Máquina zerada: {resultado.como_texto()}.",
        )

    @roteador.post("/robo/iniciar")
    def iniciar_robo():
        """Inicia o robo visual a partir do painel web."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        _limpar_parada_manual(cfg.banco)
        exigir_area_de_trabalho()

        if _robo_rodando(cfg.banco):
            if _robo_ocioso_sem_sinal(cfg.banco):
                _encerrar_robo_ocioso(cfg.banco)
            else:
                return _resposta("Já está em execução.",
                                 "Robô já está em execução.")

        _limpar_pedido_de_parada(cfg.banco)
        _recuperar_jobs_orfaos(cfg.banco)
        _normalizar_ritmo_para_inicio(cfg)
        ok, situacao = _iniciar_robo_visual(raiz)
        if not ok:
            raise HTTPException(status_code=409, detail=situacao)
        return _resposta(situacao, _mensagem_robo(situacao))

    @roteador.post("/robo/parar")
    def parar_robo():
        """Pede ao orquestrador que encerre ao fim do item em andamento."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        from cnd.infra.db import caminho_pedido_parada

        pedido = caminho_pedido_parada(cfg.banco)
        pedido.parent.mkdir(parents=True, exist_ok=True)
        pedido.write_text("parar", encoding="utf-8")
        _marcar_parada_manual(cfg.banco)
        if _encerrar_robo_ocioso(cfg.banco):
            return _resposta("Parado.", "Robô parado.")
        if not _robo_rodando(cfg.banco):
            orfaos = _recuperar_jobs_orfaos(cfg.banco)
            if orfaos:
                item = "item devolvido" if orfaos == 1 else "itens devolvidos"
                return _resposta(
                    f"Parado; {orfaos} {item} à fila.",
                    f"Robô parado; {orfaos} {item} à fila.",
                )
            return _resposta("Parado.", "Robô parado.")
        return _resposta("Parada solicitada.", "Parada do robô solicitada.")

    @roteador.post("/reenfileirar")
    def reenfileirar(orgao: str = Form(default=""), lote_id: int = Form(default=0)):
        """Faz a fila andar agora: devolve as falhas E adianta as esperas.

        As duas coisas respondem à mesma pergunta de quem aperta o botão —
        "por que ele não está trabalhando?". Separá-las obrigaria a operação
        a saber a diferença entre FAILED e RETRY_WAIT para escolher o botão
        certo, quando o que ela quer é simplesmente que ande.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        conn = conectar(cfg.banco)
        try:
            quantidade = fila.reenfileirar_falhados(
                conn, orgao or None, lote_id or None
            )
            adiantados = fila.antecipar_esperas(
                conn, orgao or None, lote_id or None
            )
        finally:
            conn.close()
        return {"ok": True, "quantidade": quantidade + adiantados,
                "falhados": quantidade, "adiantados": adiantados}

    @roteador.post("/breaker/retomar")
    def retomar_todas_as_pausas():
        """Destrava TUDO nesta máquina, sem precisar saber o nome do órgão.

        Existe para o botão do painel poder ficar sempre visível. O de
        órgão único só aparecia quando já havia pausa — e quem quer
        destravar costuma chegar na tela justamente por causa da pausa,
        depois de ela ter começado.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        conn = conectar(cfg.banco)
        try:
            codigos = [o.codigo for o in cfg.ativos()]
            for codigo in codigos:
                breaker.fechar(conn, codigo)
                conn.execute("UPDATE breaker SET aberturas = 0 WHERE orgao = ?",
                             (codigo,))
        finally:
            conn.close()
        return _resposta("Pausa resetada.",
                         f"Pausa resetada em {len(codigos)} órgão(s).")

    @roteador.post("/breaker/{orgao}/retomar")
    def retomar_breaker(orgao: str):
        """Fecha a pausa automatica de um orgao nesta maquina."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        conn = conectar(cfg.banco)
        try:
            breaker.fechar(conn, orgao)
            conn.execute("UPDATE breaker SET aberturas = 0 WHERE orgao = ?", (orgao,))
        finally:
            conn.close()
        return _resposta("Pausa resetada.")

    @roteador.post("/atualizar")
    def atualizar(origem: str = ORIGEM_ATUALIZACAO,
                  sha256: str = Form(default="")):
        """Baixa um pacote publicado pelo console e atualiza esta instalacao."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        origem = _validar_origem_atualizacao(origem)
        sha256 = _validar_sha256(sha256)

        if not _robo_rodando(cfg.banco):
            _recuperar_jobs_orfaos(cfg.banco)
        if _ha_item_em_execucao(cfg.banco):
            raise HTTPException(
                status_code=409,
                detail="Há item em execução nesta máquina. Pare o robô e tente "
                       "atualizar novamente quando ele ficar ocioso.",
            )
        if _robo_rodando(cfg.banco) and not _encerrar_robo_ocioso(cfg.banco):
            raise HTTPException(
                status_code=409,
                detail="O robô ainda está ativo. Pare o robô antes de atualizar.",
            )
        if not _pacote_disponivel(origem):
            raise HTTPException(
                status_code=400,
                detail=f"Não consegui baixar o pacote em {origem}/acta.zip.",
            )
        _salvar_origem_atualizacao(raiz, origem)

        script = _escrever_atualizador()
        subprocess.Popen(
            [
                "powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Origem", origem, "-Pasta", str(raiz), "-Sha256", sha256,
            ],
            cwd=str(raiz),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return _resposta("Atualização iniciada.")

    return roteador


def _comando_robo(raiz: Path) -> list[str]:
    """Como chamar o robo, empacotado ou rodando pela fonte."""
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).with_name("cnd.exe")
        alvo = console if console.exists() else Path(sys.executable)
        return [str(alvo), "rodar", "--forcar"]
    return [sys.executable, "-m", "cnd.cli", "rodar", "--forcar"]


def _resposta(situacao: str, mensagem: str | None = None) -> dict:
    return {
        "ok": True,
        "situacao": situacao,
        "mensagem": mensagem or situacao,
    }


def _mensagem_robo(situacao: str) -> str:
    texto = situacao.strip().rstrip(".!?")
    if texto.lower().startswith("robô"):
        return f"{texto}."
    return f"Robô {texto[:1].lower()}{texto[1:]}."


def iniciar_robo_da_maquina(
    cfg: Config, raiz: Path, *, automatico: bool = False
) -> tuple[bool, str]:
    """Inicia o robo local pelo mesmo caminho usado pelo painel."""
    if automatico and _parada_manual_ativa(cfg.banco):
        return False, "Parada manual ativa; retomada automatica bloqueada."
    if not automatico:
        _limpar_parada_manual(cfg.banco)

    if not maquina.area_de_trabalho_disponivel():
        return (
            False,
            "A area de trabalho desta maquina esta bloqueada. O robo move "
            "o mouse de verdade e le a tela; precisa da sessao do Windows "
            "aberta e destravada.",
        )

    if _robo_rodando(cfg.banco):
        if _robo_ocioso_sem_sinal(cfg.banco):
            _encerrar_robo_ocioso(cfg.banco)
        else:
            return True, "Ja esta em execucao."

    _limpar_pedido_de_parada(cfg.banco)
    _recuperar_jobs_orfaos(cfg.banco)
    if not automatico:
        _normalizar_ritmo_para_inicio(cfg)
    return _iniciar_robo_visual(raiz)


def _marcar_parada_manual(banco: Path) -> None:
    marcador = caminho_parada_manual(banco)
    marcador.parent.mkdir(parents=True, exist_ok=True)
    marcador.write_text(tempo.agora_iso(), encoding="utf-8")


def _limpar_parada_manual(banco: Path) -> None:
    with contextlib.suppress(OSError):
        caminho_parada_manual(banco).unlink()


def parada_manual_ativa(banco: Path) -> bool:
    """Se um humano decidiu que este robô está parado.

    Pública porque o vigia do painel também precisa saber: parada
    intencional não é incidente e não deve ser desfeita sozinha.
    """
    return caminho_parada_manual(banco).exists()


# Nome antigo, ainda usado aqui dentro.
_parada_manual_ativa = parada_manual_ativa


def _ambiente_robo() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def _usuario_do_processo() -> str:
    if sys.platform != "win32":
        return os.environ.get("USER", "")
    try:
        fim = subprocess.run(
            ["whoami"], capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if fim.returncode == 0 and fim.stdout.strip():
            return fim.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USERNAME", "")


def _processo_e_system() -> bool:
    usuario = _usuario_do_processo().strip().lower()
    return usuario in {"system", "nt authority\\system"} or usuario.endswith("\\system")


def _usuario_interativo() -> str:
    """Usuario logado no console, visto ate quando o painel roda como SYSTEM."""
    if sys.platform != "win32":
        return ""
    script = "(Get-CimInstance Win32_ComputerSystem).UserName"
    try:
        fim = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    usuario = fim.stdout.strip() if fim.returncode == 0 else ""
    if not usuario or usuario.lower().endswith("\\system"):
        return ""
    return usuario


def _texto_do_comando_windows(partes: list[str]) -> str:
    return " ".join(f'"{parte}"' for parte in partes)


def _saida_curta(fim: subprocess.CompletedProcess) -> str:
    return " ".join((fim.stderr or fim.stdout or "").split())[:500]


def _iniciar_robo_direto(raiz: Path) -> tuple[bool, str]:
    try:
        subprocess.Popen(
            _comando_robo(raiz),
            cwd=str(raiz),
            env=_ambiente_robo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as erro:
        return False, f"Não consegui iniciar o robô: {erro}."
    return True, "Iniciado."


def _iniciar_robo_por_tarefa(raiz: Path) -> tuple[bool, str]:
    usuario = _usuario_interativo()
    if not usuario:
        return (
            False,
            "Não há usuário logado na sessão visual do Windows para receber o robô.",
        )

    comando = _texto_do_comando_windows(_comando_robo(raiz))
    criar = subprocess.run(
        [
            "schtasks", "/Create", "/TN", "ACTA Robo", "/TR", comando,
            "/SC", "ONCE", "/ST", "23:59", "/F", "/IT", "/RL", "HIGHEST",
            "/RU", usuario,
        ],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if criar.returncode != 0:
        return False, (
            "Não consegui preparar a tarefa interativa do robô: "
            f"{_saida_curta(criar) or 'schtasks falhou'}"
        )

    rodar = subprocess.run(
        ["schtasks", "/Run", "/TN", "ACTA Robo"],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if rodar.returncode != 0:
        return False, (
            "Não consegui disparar a tarefa interativa do robô: "
            f"{_saida_curta(rodar) or 'schtasks falhou'}"
        )
    return True, f"Iniciado na sessão de {usuario}."


def _iniciar_robo_visual(raiz: Path) -> tuple[bool, str]:
    processo = maquina.processo_robo_rodando()
    if processo is True:
        return True, "Já está em execução."
    if (
        sys.platform == "win32"
        and (_processo_e_system() or not maquina.processo_tem_area_de_trabalho())
    ):
        return _iniciar_robo_por_tarefa(raiz)
    return _iniciar_robo_direto(raiz)


def _robo_rodando(banco: Path) -> bool:
    """Se já há orquestrador vivo, pelo sinal de vida no banco."""
    import contextlib

    from cnd.infra import heartbeat
    from cnd.infra.db import conectar_leitura

    with contextlib.suppress(Exception), \
            contextlib.closing(conectar_leitura(banco)) as conn:
        idade = heartbeat.segundos_desde(conn, "orquestrador")
        if idade is None or idade >= 120:
            return maquina.processo_robo_rodando() is True
        processo = maquina.processo_robo_rodando()
        if processo is False:
            _limpar_heartbeat_robo(banco)
            return False
        return True
    return False


def _robo_ocioso_sem_sinal(banco: Path) -> bool:
    """Processo vivo, mas sem heartbeat recente e sem item em execucao."""
    import contextlib

    from cnd.infra import heartbeat
    from cnd.infra.db import conectar_leitura

    with contextlib.suppress(Exception), \
            contextlib.closing(conectar_leitura(banco)) as conn:
        idade = heartbeat.segundos_desde(conn, "orquestrador")
        return (
            idade is not None
            and idade >= ROBO_SEM_SINAL_RECUPERAVEL_S
            and not _ha_item_em_execucao(banco)
        )
    return False


def _limpar_pedido_de_parada(banco: Path) -> None:
    import contextlib

    from cnd.infra.db import caminho_pedido_parada

    with contextlib.suppress(OSError):
        caminho_pedido_parada(banco).unlink()


def _endereco_da_rede_local(endereco: str) -> bool:
    """Se o IP é de rede interna — inclusive a faixa da VPN da empresa.

    100.64.0.0/10 entra explicitamente: é a faixa CGNAT que o Tailscale
    usa, e é por ela que o console alcança as máquinas. O
    `ipaddress.is_private` deixou de considerá-la privada no Python 3.12.4,
    e confiar só nele barraria a rede que este projeto de fato usa.
    """
    import ipaddress

    try:
        ip = ipaddress.ip_address(endereco)
    except ValueError:
        return False
    # Loopback primeiro, e não junto do resto: em IPv6 o `::1` cai dentro
    # de `::/8` e é `is_reserved`, então a recusa abaixo barraria
    # `localhost` — que resolve para 127.0.0.1 E ::1, e é endereço legítimo
    # de quem atualiza a própria máquina.
    if ip.is_loopback:
        return True
    # `0.0.0.0` e `255.255.255.255` são classificados como PRIVADOS pelo
    # `ipaddress`, e passavam. Nenhum dos dois é máquina de onde se baixe
    # coisa alguma — um é "este host, esta rede" e o outro é broadcast.
    # Aceitá-los é aceitar um engano de digitação como se fosse endereço.
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return bool(ip.is_private or ip.is_link_local)


def _validar_origem_atualizacao(origem: str) -> str:
    """A origem tem de ser um endereço da rede interna, e não qualquer URL.

    Esta rota BAIXA E TROCA EXECUTÁVEIS. "É HTTP e tem host" aceitava
    `http://qualquer-coisa.com`: bastava um erro de digitação no prompt,
    ou um domínio parecido, para a máquina instalar o pacote de um
    estranho. O pacote nunca sai da rede da empresa (ver empacotar/
    publicar.py), então exigir endereço interno não tira nada de real.
    """
    import socket

    origem = (origem or "").strip().rstrip("/")
    partes = urllib.parse.urlparse(origem)
    # `hostname` e não `netloc`: em `http://:8899` o netloc é `":8899"` e
    # passa, mas o hostname é None — e `getaddrinfo(None, ...)` resolve
    # para o LOOPBACK, que é endereço interno. A URL sem máquina nenhuma
    # atravessava a conferência inteira e ia baixar de si mesma.
    if partes.scheme != "http" or not partes.hostname:
        raise HTTPException(
            status_code=400,
            detail="A origem da atualização deve ser uma URL HTTP da rede "
                   "local, com o endereço da máquina "
                   "(ex.: http://10.1.11.86:8899).",
        )
    # Credencial no endereço não serve para nada aqui — o publicador não
    # pede senha — e vaza: a origem é gravada em texto puro no log da
    # atualização e em data/ultima_origem_atualizacao.txt.
    if partes.username or partes.password:
        raise HTTPException(
            status_code=400,
            detail="A origem da atualização não leva usuário nem senha.",
        )
    # Só máquina e porta. O script monta `$Origem/acta.zip`, então um
    # `?x=1` viraria `...:8899?x=1/acta.zip` e um `#frag` cortaria o
    # caminho inteiro — endereços que não baixam nada e cuja falha só
    # aparece lá adiante, com o painel já derrubado.
    if partes.path or partes.query or partes.fragment:
        raise HTTPException(
            status_code=400,
            detail="A origem da atualização é só a máquina e a porta "
                   "(ex.: http://10.1.11.86:8899), sem caminho depois.",
        )

    try:
        enderecos = {
            info[4][0] for info in socket.getaddrinfo(
                partes.hostname, partes.port or 80, proto=socket.IPPROTO_TCP)
        }
    except OSError as erro:
        raise HTTPException(
            status_code=400,
            detail=f"Não resolvi o endereço {partes.hostname!r} da atualização.",
        ) from erro

    # TODOS, e não algum: um nome que resolve para um endereço interno e
    # outro externo escolheria o de fora na hora de baixar.
    if not enderecos or not all(_endereco_da_rede_local(e) for e in enderecos):
        raise HTTPException(
            status_code=400,
            detail="A atualização só pode vir de uma máquina da rede interna "
                   f"(ex.: http://10.1.11.86:8899). {partes.hostname} está "
                   "fora dela.",
        )
    # A forma canônica, e não o texto recebido: é ela que vai para o
    # script, para o log e para o arquivo de última origem.
    return f"http://{partes.netloc.lower()}"


def _validar_sha256(sha256: str) -> str:
    """O hash do pacote, OBRIGATÓRIO.

    Antes era opcional, e sem ele a máquina baixava por HTTP simples um zip
    que vira `ACTA.exe` e `cnd.exe` — quem conseguisse responder no lugar
    do console (ou apenas publicar na porta 8899 antes dele) trocava o
    programa inteiro. O hash sai impresso pelo `empacotar/publicar.py` e
    viaja por dentro do POST autenticado, que é outro caminho: é isso que
    faz a conferência valer alguma coisa.
    """
    valor = (sha256 or "").strip().lower()
    if not valor:
        raise HTTPException(
            status_code=400,
            detail="Informe o SHA-256 do pacote. Ele é impresso por "
                   "`python empacotar/publicar.py` ao publicar.",
        )
    if len(valor) != 64 or any(c not in "0123456789abcdef" for c in valor):
        raise HTTPException(status_code=400, detail="SHA-256 inválido.")
    return valor


def _pacote_disponivel(origem: str) -> bool:
    pedido = urllib.request.Request(f"{origem}/acta.zip", method="HEAD")
    try:
        with urllib.request.urlopen(pedido, timeout=10) as resposta:
            return 200 <= resposta.status < 400
    except Exception:
        return False


def _escrever_atualizador() -> Path:
    destino = Path(tempfile.gettempdir()) / f"acta-atualizar-{os.getpid()}.ps1"
    destino.write_text(ATUALIZADOR_PS1, encoding="utf-8")
    return destino


def _salvar_origem_atualizacao(raiz: Path, origem: str) -> None:
    with contextlib.suppress(OSError):
        arquivo = raiz / "data" / "ultima_origem_atualizacao.txt"
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(origem, encoding="utf-8")


def _ha_item_em_execucao(banco: Path) -> bool:
    import contextlib

    from cnd.infra.db import conectar_leitura

    with contextlib.suppress(Exception), \
            contextlib.closing(conectar_leitura(banco)) as conn:
        return bool(conn.execute(
            "SELECT 1 FROM job WHERE status = ? LIMIT 1", (Status.RUNNING,)
        ).fetchone())
    return True


def _recuperar_jobs_orfaos(banco: Path) -> int:
    import contextlib

    from cnd.core import fila

    with contextlib.closing(conectar(banco)) as conn:
        return fila.recuperar_orfaos(conn)


def _normalizar_ritmo_para_inicio(cfg: Config) -> int:
    """Manual start should not inherit an old excessive pacing penalty."""
    total = 0
    with contextlib.closing(conectar(cfg.banco)) as conn:
        for orgao in cfg.ativos():
            p = orgao.pacing
            linha = conn.execute(
                "SELECT intervalo_s FROM ritmo WHERE orgao = ?", (orgao.codigo,)
            ).fetchone()
            if linha is None:
                continue

            limite = max(
                p.intervalo_inicial_s,
                p.intervalo_inicial_s * p.fator_punicao_bloqueio,
            )
            if linha["intervalo_s"] <= limite:
                continue

            conn.execute(
                """
                UPDATE ritmo
                   SET intervalo_s = ?, consultas_limpas = 0, atualizado_em = ?
                 WHERE orgao = ?
                """,
                (p.intervalo_inicial_s, tempo.agora_iso(), orgao.codigo),
            )
            total += 1
    return total


def _limpar_heartbeat_robo(banco: Path) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        conn = conectar(banco)
        try:
            conn.execute("DELETE FROM heartbeat WHERE processo = 'orquestrador'")
        finally:
            conn.close()


def _encerrar_robo_ocioso(banco: Path) -> bool:
    """Derruba o processo do robo quando nao ha item em execucao."""
    if _ha_item_em_execucao(banco) or sys.platform != "win32":
        return False

    script = r"""
Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -and
    $_.CommandLine -match '\brodar\b' -and
    ($_.Name -ieq 'cnd.exe' -or $_.CommandLine -match 'cnd\.cli')
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
"""
    fim = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    if fim.returncode == 0:
        _limpar_heartbeat_robo(banco)
        return True
    return False
