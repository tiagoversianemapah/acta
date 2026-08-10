"""Rotas que MEXEM na máquina: enviar planilha e ligar ou parar o robô.

Separadas das de leitura de propósito. Até aqui toda a API era só de
leitura, e era isso que tornava seguro publicar o painel na rede interna: na
pior hipótese alguém enxergava dados. Com estas rotas, quem alcança a porta
passa a poder fazer a máquina trabalhar — então elas têm duas travas que as
outras não têm.

A PRIMEIRA é a senha, aqui OBRIGATÓRIA. Sem `[rede] senha` preenchida elas
recusam tudo em vez de ficarem abertas: a capacidade perigosa nasce
desligada e só existe depois de alguém configurá-la de propósito.

A SEGUNDA é a área de trabalho. O robô cego move o mouse de verdade e lê a
tela; com a estação bloqueada ele rodaria gastando consultas e gravando erro
atrás de erro. Melhor recusar com o motivo do que estragar um lote.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from cnd.infra import maquina
from cnd.infra.config import Config

# Planilha da carteira inteira não passa de alguns megabytes; o limite
# existe para um envio errado não encher o disco da máquina do robô.
LIMITE_DA_PLANILHA_MB = 25

# Fora da assinatura porque o FastAPI exige o marcador como padrão, e
# chamada em argumento padrão é avaliada uma vez na importação.
ARQUIVO_ENVIADO = File(...)


def montar(obter_config: Callable[[], Config], raiz: Path) -> APIRouter:
    roteador = APIRouter(prefix="/api")

    def exigir_senha_configurada(cfg: Config) -> None:
        if not cfg.rede.senha:
            raise HTTPException(
                status_code=403,
                detail="esta máquina não aceita comandos pela rede: defina "
                       "[rede] senha no config.toml dela para liberar")

    def exigir_area_de_trabalho() -> None:
        if not maquina.area_de_trabalho_disponivel():
            raise HTTPException(
                status_code=409,
                detail="a área de trabalho desta máquina está bloqueada. O "
                       "robô move o mouse de verdade e lê a tela — precisa da "
                       "sessão do Windows aberta e destravada. Entre nela "
                       "pelo AnyDesk, destrave e tente de novo.")

    @roteador.post("/planilha")
    async def enviar_planilha(arquivo: UploadFile = ARQUIVO_ENVIADO):
        """Recebe a planilha e cria o lote nesta máquina.

        Resolve o caminho que hoje obriga a entrar por AnyDesk em cada
        máquina só para arrastar um arquivo — quatro sessões remotas por mês
        para uma tarefa de dez segundos.
        """
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        nome = Path(arquivo.filename or "planilha.xlsx").name
        if not nome.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(status_code=400,
                                detail="envie um arquivo .xlsx")

        destino = Path(tempfile.mkdtemp(prefix="acta_")) / nome
        try:
            tamanho = 0
            with open(destino, "wb") as saida:
                while bloco := await arquivo.read(1 << 20):
                    tamanho += len(bloco)
                    if tamanho > LIMITE_DA_PLANILHA_MB * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail=f"planilha maior que "
                                   f"{LIMITE_DA_PLANILHA_MB} MB")
                    saida.write(bloco)

            from cnd.infra.db import conectar, criar_schema
            from cnd.ingestao.planilha import importar

            conn = conectar(cfg.banco)
            try:
                criar_schema(conn)
                lote_id, leitura = importar(conn, destino,
                                            f"Importação de {nome}", ["RFB"])
            finally:
                conn.close()

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

    @roteador.post("/robo/iniciar")
    def iniciar_robo():
        """Sobe o orquestrador nesta máquina, se a tela estiver disponível."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)
        exigir_area_de_trabalho()

        if _robo_rodando(cfg.banco):
            return {"ok": True, "situacao": "já estava rodando"}

        subprocess.Popen(
            [*_comando(raiz), "rodar", "--forcar"], cwd=str(raiz),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "situacao": "iniciado"}

    @roteador.post("/robo/parar")
    def parar_robo():
        """Pede ao orquestrador que encerre ao fim do item em andamento."""
        cfg = obter_config()
        exigir_senha_configurada(cfg)

        from cnd.infra.db import caminho_pedido_parada

        pedido = caminho_pedido_parada(cfg.banco)
        pedido.parent.mkdir(parents=True, exist_ok=True)
        pedido.write_text("parar", encoding="utf-8")
        return {"ok": True, "situacao": "parada pedida"}

    return roteador


def _comando(raiz: Path) -> list[str]:
    """Como chamar a linha de comando — empacotado ou do código-fonte."""
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).with_name("cnd.exe")
        return [str(console if console.exists() else sys.executable)]
    return [sys.executable, "-m", "cnd.cli"]


def _robo_rodando(banco: Path) -> bool:
    """Se já há orquestrador vivo, pelo sinal de vida no banco."""
    import contextlib

    from cnd.infra import heartbeat
    from cnd.infra.db import conectar_leitura

    with contextlib.suppress(Exception), \
            contextlib.closing(conectar_leitura(banco)) as conn:
        idade = heartbeat.segundos_desde(conn, "orquestrador")
        return idade is not None and idade < 120
    return False
