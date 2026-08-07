"""Alertas por e-mail — ver docs/05, seção 4.

A máquina roda sem ninguém olhando: o e-mail é o canal primário de
incidente, o painel é para acompanhamento ativo.

Duas proteções contra enxurrada de e-mail:
  - supressão: o mesmo alerta não se repete dentro da janela configurada
  - normalização: quando a condição se resolve, chega um aviso de "voltou"
"""
from __future__ import annotations

import threading

from cnd.core import tempo
from cnd.infra import correio
from cnd.infra.config import ConfigAlertas
from cnd.infra.log import obter

log = obter("alertas")

_ultimo_envio: dict[str, float] = {}
_ativos: set[str] = set()
_enviados_recentes: list[float] = []
_trava = threading.Lock()


def _pode_enviar(chave: str, supressao_s: int) -> bool:
    import time

    agora = time.monotonic()
    with _trava:
        anterior = _ultimo_envio.get(chave)
        if anterior is not None and (agora - anterior) < supressao_s:
            return False
        _ultimo_envio[chave] = agora
        return True


def _dentro_do_teto(limite_por_hora: int) -> bool:
    """Teto geral de envios, independente do tipo de alerta.

    A supressão por chave impede repetir o MESMO aviso; isto impede uma
    enxurrada de avisos DIFERENTES. Sem esse teto, um defeito em laço
    dispararia centenas de e-mails numa madrugada e o provedor suspenderia
    a conta — fazendo o robô perder a voz justamente quando algo está
    errado de verdade.
    """
    import time

    if limite_por_hora <= 0:
        return True

    agora = time.monotonic()
    with _trava:
        _enviados_recentes[:] = [t for t in _enviados_recentes if agora - t < 3600]
        if len(_enviados_recentes) >= limite_por_hora:
            return False
        _enviados_recentes.append(agora)
        return True


def enviar(cfg: ConfigAlertas, assunto: str, corpo: str,
           chave: str | None = None, forcar: bool = False,
           dados: dict | None = None,
           acoes: list[tuple[str, str]] | None = None,
           severidade: str = "info", acao: str = "") -> bool:
    """Publica um aviso. Devolve True se saiu de fato.

    `chave` identifica o TIPO de alerta, para efeito de supressão
    (ex.: 'breaker:RFB_PJ'). Sem chave, não há supressão.

    `dados` vira tabela de campos, `acoes` vira botões e `severidade`
    define a cor — quem olha o canal de relance entende a gravidade sem
    precisar ler.
    """
    if not cfg.habilitado:
        log.info("alerta_nao_enviado_smtp_desligado", extra={"assunto": assunto})
        return False

    if chave and not forcar and not _pode_enviar(chave, cfg.supressao_s):
        log.info("alerta_suprimido", extra={"chave": chave, "assunto": assunto})
        return False

    if not forcar and not _dentro_do_teto(cfg.limite_por_hora):
        log.warning("teto_de_alertas_atingido",
                    extra={"limite_por_hora": cfg.limite_por_hora,
                           "assunto": assunto,
                           "nota": "o aviso está no log; o e-mail foi retido "
                                   "para não derrubar a conta de envio"})
        return False

    try:
        correio.enviar(cfg, assunto, corpo, dados=dados, acoes=acoes,
                       severidade=severidade, acao=acao,
                       rodape=f"Robô de certidões · {tempo.agora_iso()[:19].replace('T', ' ')} UTC")
        log.info("alerta_enviado", extra={"assunto": assunto, "chave": chave,
                                          "metodo": cfg.metodo})
        return True
    except correio.FalhaNoEnvio as erro:
        # A mensagem já vem traduzida para algo que dá para agir.
        log.error("alerta_nao_saiu", extra={"assunto": assunto,
                                            "metodo": cfg.metodo,
                                            "motivo": str(erro)})
        return False
    except Exception as erro:  # nunca deixar o alerta derrubar o robô
        log.error("alerta_falhou", extra={"assunto": assunto, "erro": str(erro)})
        return False


def abrir_incidente(cfg: ConfigAlertas, chave: str, assunto: str,
                    corpo: str, **extras) -> None:
    """Marca a condição como ativa e avisa (respeitando a supressão)."""
    with _trava:
        _ativos.add(chave)
    enviar(cfg, assunto, corpo, chave=chave, **extras)


def fechar_incidente(cfg: ConfigAlertas, chave: str, assunto: str,
                     corpo: str, **extras) -> None:
    """Se a condição estava ativa, avisa que normalizou e limpa o estado."""
    with _trava:
        se_estava_ativo = chave in _ativos
        _ativos.discard(chave)
        _ultimo_envio.pop(chave, None)
    if se_estava_ativo:
        enviar(cfg, assunto, corpo, **extras)
