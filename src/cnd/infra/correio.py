"""Envio de e-mail: SMTP ou Microsoft Graph.

Por que existem dois caminhos:

**SMTP** é o simples — usuário e senha. Só que em conta corporativa com
autenticação em dois fatores ele não funciona, e não é defeito: o MFA
existe justamente para impedir que uma senha sozinha dê acesso à caixa.
A Microsoft ainda desliga o SMTP AUTH por padrão desde 2022.

**Graph** é o caminho que a TI aprova nesse cenário. O robô não se
autentica como pessoa: ele é um *aplicativo registrado*, com identidade
própria, permissão restrita a enviar e-mail e nada mais. Não há senha de
usuário envolvida, então não há MFA a satisfazer — e se o robô for
desativado, revoga-se o aplicativo sem mexer em nenhuma conta.

Sem dependência externa: `urllib` já vem no Python.
"""
from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

URL_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
URL_ENVIO = "https://graph.microsoft.com/v1.0/users/{remetente}/sendMail"
ESCOPO = "https://graph.microsoft.com/.default"


class FalhaNoEnvio(RuntimeError):
    """Erro de envio, com a mensagem já traduzida para algo acionável."""


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------

def _montar(cfg, assunto: str, corpo: str) -> EmailMessage:
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = cfg.remetente or cfg.smtp_usuario
    mensagem["To"] = ", ".join(cfg.destinatarios)
    mensagem.set_content(corpo)
    return mensagem


def enviar_por_smtp(cfg, assunto: str, corpo: str) -> None:
    import smtplib

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_porta, timeout=30) as servidor:
            servidor.starttls()
            servidor.login(cfg.smtp_usuario, cfg.smtp_senha)
            servidor.send_message(_montar(cfg, assunto, corpo))
    except smtplib.SMTPAuthenticationError as erro:
        raise FalhaNoEnvio(_explicar_erro_smtp(str(erro))) from erro


def enviar_por_relay(cfg, assunto: str, corpo: str) -> None:
    """Direct Send: entrega direto no servidor da empresa, sem autenticação.

    É o mecanismo que impressoras e sistemas internos usam. Não há login,
    então não há senha nem autenticador a satisfazer — o Microsoft 365
    aceita porque reconhece o domínio e o destinatário é interno.

    Duas limitações, e as duas são aceitáveis aqui:
      - só entrega para destinatários DE DENTRO da organização;
      - o remetente precisa ser de um domínio da empresa.

    Como os avisos vão de @empresa para @empresa, serve exatamente.
    """
    import smtplib

    try:
        # Tempo curto de propósito: quando a porta 25 está bloqueada — e ela
        # costuma estar, porque provedores a fecham para conter spam — a
        # conexão não é recusada, ela fica pendurada. Sem este limite, cada
        # aviso levaria dois minutos para descobrir que não vai sair.
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_porta, timeout=15) as servidor:
            # Alguns relays internos não usam TLS; seguir sem ele é o
            # comportamento esperado ali, não uma falha a registrar.
            with contextlib.suppress(smtplib.SMTPNotSupportedError):
                servidor.starttls()
            servidor.send_message(_montar(cfg, assunto, corpo))
    except smtplib.SMTPRecipientsRefused as erro:
        raise FalhaNoEnvio(
            "O servidor recusou o destinatário. Sem autenticação, o Microsoft "
            "365 só entrega para endereços DE DENTRO da organização — confira "
            f"se todos os destinatários são do domínio da empresa. Detalhe: {erro}"
        ) from erro
    except smtplib.SMTPSenderRefused as erro:
        raise FalhaNoEnvio(
            "O servidor recusou o remetente. Ele precisa ser um endereço de um "
            "domínio aceito pela organização (ex.: robo.cnd@suaempresa.com.br). "
            f"Detalhe: {erro}"
        ) from erro
    except (TimeoutError, OSError) as erro:
        raise FalhaNoEnvio(
            f"Não consegui falar com {cfg.smtp_host}:{cfg.smtp_porta}. "
            f"A causa mais comum é a porta 25 estar bloqueada na saída — quase "
            f"todo provedor de internet a fecha para conter spam, e a conexão "
            f"fica pendurada em vez de ser recusada. Se for o caso, use "
            f'metodo = "graph", que trafega por HTTPS e nunca é bloqueado. '
            f"Detalhe: {type(erro).__name__}: {erro}"
        ) from erro


def _explicar_erro_smtp(bruto: str) -> str:
    if "SmtpClientAuthentication is disabled" in bruto:
        return (
            "O Microsoft 365 recusou: SMTP AUTH está desligado nesta caixa "
            "(padrão desde 2022). O administrador precisa habilitar em "
            "admin.microsoft.com > Usuários > Email > Aplicativos de email. "
            "Se a conta tiver autenticação em dois fatores, nem isso resolve — "
            "use metodo = \"graph\"."
        )
    if "basic authentication is disabled" in bruto.lower():
        return (
            "A organização bloqueia autenticação básica (senha). É o esperado "
            "quando há autenticação em dois fatores. Use metodo = \"graph\"."
        )
    return f"O servidor recusou as credenciais: {bruto}"


# ----------------------------------------------------------------------
# Microsoft Graph
# ----------------------------------------------------------------------

_token_cache: dict[str, tuple[str, float]] = {}


def _obter_token(cfg) -> str:
    """Token de aplicativo, reaproveitado até perto de expirar."""
    chave = f"{cfg.graph_tenant_id}:{cfg.graph_client_id}"
    guardado = _token_cache.get(chave)
    if guardado and guardado[1] > time.time() + 60:
        return guardado[0]

    dados = urllib.parse.urlencode({
        "client_id": cfg.graph_client_id,
        "client_secret": cfg.graph_client_secret,
        "scope": ESCOPO,
        "grant_type": "client_credentials",
    }).encode()

    pedido = urllib.request.Request(
        URL_TOKEN.format(tenant=cfg.graph_tenant_id), data=dados,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:
            corpo = json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        raise FalhaNoEnvio(_explicar_erro_graph(erro)) from erro

    token = corpo["access_token"]
    _token_cache[chave] = (token, time.time() + int(corpo.get("expires_in", 3600)))
    return token


def enviar_por_graph(cfg, assunto: str, corpo: str) -> None:
    token = _obter_token(cfg)
    remetente = cfg.remetente or cfg.smtp_usuario

    mensagem = {
        "message": {
            "subject": assunto,
            "body": {"contentType": "Text", "content": corpo},
            "toRecipients": [
                {"emailAddress": {"address": endereco}}
                for endereco in cfg.destinatarios
            ],
        },
        # Não guardamos cópia: são avisos de máquina, e encher a pasta de
        # Enviados de uma caixa corporativa não ajuda ninguém.
        "saveToSentItems": False,
    }

    pedido = urllib.request.Request(
        URL_ENVIO.format(remetente=urllib.parse.quote(remetente)),
        data=json.dumps(mensagem).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30):
            return
    except urllib.error.HTTPError as erro:
        raise FalhaNoEnvio(_explicar_erro_graph(erro)) from erro


def _explicar_erro_graph(erro: urllib.error.HTTPError) -> str:
    try:
        detalhe = json.loads(erro.read())
    except Exception:
        detalhe = {}

    codigo = (detalhe.get("error", {}).get("code")
              if isinstance(detalhe.get("error"), dict) else detalhe.get("error"))
    texto = (detalhe.get("error", {}).get("message")
             if isinstance(detalhe.get("error"), dict)
             else detalhe.get("error_description", ""))

    if erro.code == 401:
        return (f"Credenciais do aplicativo recusadas ({codigo}). Confira "
                f"graph_tenant_id, graph_client_id e o segredo — segredo de "
                f"aplicativo expira, e o Azure não avisa. Detalhe: {texto}")
    if erro.code == 403:
        return ("O aplicativo não tem permissão para enviar e-mail. A TI "
                "precisa conceder a permissão de APLICATIVO 'Mail.Send' no "
                "registro e clicar em 'Conceder consentimento do "
                "administrador'. Permissão delegada não serve aqui. "
                f"Detalhe: {texto}")
    if erro.code == 404:
        return (f"A caixa remetente não foi encontrada. O campo 'remetente' "
                f"precisa ser um e-mail que existe no tenant. Detalhe: {texto}")
    return f"Graph recusou (HTTP {erro.code} {codigo}): {texto}"


# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Microsoft Teams
# ----------------------------------------------------------------------

# Severidade -> como o cartão se apresenta. A faixa colorida no topo é o
# que permite ler o canal de relance: verde passou, âmbar merece atenção,
# vermelho precisa de alguém.
APARENCIA = {
    "ok":     ("good",      "✓", "Good"),
    "aviso":  ("warning",   "!", "Warning"),
    "erro":   ("attention", "✕", "Attention"),
    "info":   ("emphasis",  "i", "Default"),
}


def montar_cartao(assunto: str, corpo: str, dados: dict | None = None,
                  acoes: list[tuple[str, str]] | None = None,
                  severidade: str = "info", rodape: str = "",
                  acao: str = "") -> dict:
    """Monta o Adaptive Card do Teams.

    Separado do envio para poder ser testado sem rede — a aparência do
    aviso é justamente o que a gente quer conferir sem depender do Teams.
    """
    estilo, sinal, cor = APARENCIA.get(severidade, APARENCIA["info"])

    corpo_cartao: list[dict] = [
        {
            "type": "Container",
            "style": estilo,
            "bleed": True,
            "items": [{
                "type": "ColumnSet",
                "columns": [
                    {"type": "Column", "width": "auto",
                     "verticalContentAlignment": "Center",
                     "items": [{"type": "TextBlock", "text": sinal,
                                "size": "Large", "weight": "Bolder",
                                "color": cor, "spacing": "None"}]},
                    {"type": "Column", "width": "stretch",
                     "items": [
                         {"type": "TextBlock", "text": "**Robô CND**",
                          "size": "Medium", "wrap": True, "spacing": "None"},
                         {"type": "TextBlock", "text": assunto,
                          "wrap": True, "spacing": "None"},
                     ]},
                ],
            }],
        },
    ]

    if corpo.strip():
        corpo_cartao.append({"type": "TextBlock", "text": corpo.strip(),
                             "wrap": True, "spacing": "Small", "isSubtle": True})

    # O que a pessoa deve fazer, destacado. Sem isto o aviso informa mas não
    # orienta, e quem lê fica sem saber se precisa agir ou só tomar ciência.
    if acao.strip():
        corpo_cartao.append({
            "type": "Container",
            "style": "emphasis",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": "O QUE FAZER", "size": "Small",
                 "weight": "Bolder", "isSubtle": True, "spacing": "None"},
                {"type": "TextBlock", "text": acao.strip(), "wrap": True,
                 "spacing": "None"},
            ],
        })

    if dados:
        corpo_cartao.append({
            "type": "FactSet",
            "spacing": "Small",
            "facts": [{"title": str(k), "value": str(v)} for k, v in dados.items()],
        })

    if rodape:
        corpo_cartao.append({"type": "TextBlock", "text": rodape,
                             "isSubtle": True, "size": "Small",
                             "spacing": "Small", "wrap": True})

    cartao = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "msteams": {"width": "Full"},
                "body": corpo_cartao,
            },
        }],
    }

    if acoes:
        cartao["attachments"][0]["content"]["actions"] = [
            {"type": "Action.OpenUrl", "title": titulo, "url": url}
            for titulo, url in acoes
        ]

    return cartao


def enviar_por_teams(cfg, assunto: str, corpo: str, dados: dict | None = None,
                     acoes: list[tuple[str, str]] | None = None,
                     severidade: str = "info", rodape: str = "",
                     acao: str = "") -> None:
    """Publica num canal do Teams via webhook.

    O caminho mais simples que existe aqui: uma URL, nenhuma credencial,
    nenhum registro de aplicativo, nenhum risco à reputação do domínio.
    Quem cria o webhook é o dono do canal, sem passar pela TI.

    E, diferente de mandar para fora, a mensagem fica dentro do tenant da
    empresa — o que importa porque os avisos carregam razão social e CNPJ
    de clientes (RNF-06).
    """
    cartao = montar_cartao(assunto, corpo, dados, acoes, severidade,
                           rodape, acao)

    pedido = urllib.request.Request(
        cfg.teams_webhook,
        data=json.dumps(cartao).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30):
            return
    except urllib.error.HTTPError as erro:
        if erro.code in (401, 403):
            raise FalhaNoEnvio(
                "O Teams recusou o webhook. A URL provavelmente expirou ou o "
                "fluxo foi desativado — recrie em Canal > ... > Fluxos de "
                "trabalho > 'Publicar no canal quando uma solicitação de "
                "webhook for recebida'."
            ) from erro
        if erro.code == 404:
            raise FalhaNoEnvio(
                "Webhook não encontrado (404). Confira se a URL foi copiada "
                "inteira — ela é longa e costuma ser cortada na cópia."
            ) from erro
        raise FalhaNoEnvio(f"Teams recusou (HTTP {erro.code}).") from erro
    except OSError as erro:
        raise FalhaNoEnvio(
            f"Não consegui falar com o Teams: {type(erro).__name__}: {erro}"
        ) from erro


# ----------------------------------------------------------------------

def enviar(cfg, assunto: str, corpo: str, dados: dict | None = None,
           acoes: list[tuple[str, str]] | None = None,
           severidade: str = "info", rodape: str = "",
           acao: str = "") -> None:
    """Despacha pelo método configurado.

    Os campos estruturados (`dados`, `acoes`, `severidade`) enriquecem o
    cartão do Teams. No e-mail, viram texto — o formato é mais pobre, mas a
    informação é a mesma.
    """
    if cfg.metodo == "teams":
        enviar_por_teams(cfg, assunto, corpo, dados, acoes, severidade,
                         rodape, acao)
        return

    if acao:
        corpo += f"\n\nO QUE FAZER\n{acao}"
    if dados:
        corpo += "\n\n" + "\n".join(f"  {k}: {v}" for k, v in dados.items())
    if acoes:
        corpo += "\n\n" + "\n".join(f"  {titulo}: {url}" for titulo, url in acoes)
    if rodape:
        corpo += f"\n\n{rodape}"
    if cfg.metodo == "graph":
        enviar_por_graph(cfg, assunto, corpo)
    elif cfg.metodo == "relay":
        enviar_por_relay(cfg, assunto, corpo)
    else:
        enviar_por_smtp(cfg, assunto, corpo)
