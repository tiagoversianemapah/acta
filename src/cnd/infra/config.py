"""Leitura do config.toml.

Regra do projeto: se um dia alguém vai querer mudar sem programador,
vai no config e não no código.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cnd.core.breaker import ParametrosBreaker
from cnd.core.recuperacao import ParametrosRecuperacao
from cnd.core.ritmo import ParametrosRitmo
from cnd.infra.db import RAIZ_PROJETO

# Normalmente o `config.toml` ao lado do programa. `CND_CONFIG` aponta para
# outro arquivo — mesma ideia das outras variáveis `CND_*` — e é o que
# permite rodar a suíte num checkout limpo, onde o `config.toml`
# legitimamente não existe: ele é da INSTALAÇÃO, não do repositório.
#
# O arquivo continuar ausente continua sendo erro. Uma máquina sem config
# não tem banco, nem senha, nem órgão ligado, e fingir que tem só adiaria a
# descoberta para o meio de um lote.
CAMINHO_PADRAO = Path(os.environ.get("CND_CONFIG") or RAIZ_PROJETO / "config.toml")


@dataclass(frozen=True)
class ParametrosRetry:
    max_tentativas: int = 3
    backoff_erro_s: tuple[int, ...] = (120, 600, 2700)
    backoff_captcha_s: tuple[int, ...] = (3600, 14400, 43200)
    # O portal pede "alguns minutos" — esperar horas seria exagero.
    backoff_bloqueio_s: tuple[int, ...] = (300, 900, 1800)
    # Uma chance curta para 005/023/106 antes de devolver o item para a fila.
    retentativa_bloqueio_s: tuple[float, ...] = (30.0, 90.0)
    # Resultado pendente NÃO tem espera configurável, e isso é de propósito.
    #
    # "Retorne em alguns minutos" é o portal engasgado para todo mundo, não
    # recado sobre aquela empresa — então castigar o CNPJ é sempre errado,
    # em qualquer valor. Não é uma escolha de operação a ser calibrada; é
    # regra. Enquanto foi parâmetro (1h por tentativa, até 17/08/2026), a
    # regra podia ser desfeita por um arquivo desatualizado numa máquina —
    # e foi exatamente o que aconteceu: o config do robô continuou com
    # [3600,3600,3600] depois de o código já ter sido corrigido.
    #
    # O item volta para o FIM da fila na hora (reivindicar ordena por
    # proxima_execucao_em). Quem descansa é o órgão, pelo disjuntor: ver
    # ParametrosBreaker.cooldown_pendente_s.

    def espera(self, desfecho: str, tentativa: int) -> float:
        """Quanto esperar antes da tentativa seguinte (1 = primeira falha)."""
        from cnd.core.modelos import Desfecho

        if desfecho == Desfecho.RESULTADO_PENDENTE:
            return 0.0                      # ver o comentário acima
        if desfecho == Desfecho.CAPTCHA:
            tabela = self.backoff_captcha_s
        elif desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
            tabela = self.backoff_bloqueio_s
        else:
            tabela = self.backoff_erro_s
        indice = min(max(tentativa - 1, 0), len(tabela) - 1)
        return float(tabela[indice])


# Como cada órgão se chama para quem lê. O código (RFB_PJ) serve ao banco,
# ao log e ao config; ninguém do escritório fala assim. Vira nome de pasta
# dentro do pacote de certidões, então é o que o cliente enxerga.
NOMES_DE_ORGAO = {
    "RFB_PJ": "RECEITA FEDERAL",
    "RFB_PF": "RECEITA FEDERAL - PESSOA FISICA",
    "CRF": "FGTS - CAIXA",
    "SEFAZ_GO": "SEFAZ GOIAS",
    "FAKE": "SIMULACAO",
}


def nome_do_orgao(codigo: str) -> str:
    return NOMES_DE_ORGAO.get(codigo, codigo)


@dataclass(frozen=True)
class ConfigOrgao:
    codigo: str
    ativo: bool
    adapter: str
    workers: int
    pacing: ParametrosRitmo
    breaker: ParametrosBreaker
    retry: ParametrosRetry
    recuperacao: ParametrosRecuperacao = field(
        default_factory=ParametrosRecuperacao)
    # Nome de exibição. Vazio cai na tabela acima, e a tabela cai no
    # próprio código — um órgão novo funciona antes de alguém batizá-lo.
    nome: str = ""
    # Chaves livres da seção do órgão, para o adapter ler o que for dele.
    # Ex.: [orgaos.FAKE.simulacao] vira extras["simulacao"].
    extras: dict = field(default_factory=dict)

    @property
    def rotulo(self) -> str:
        return self.nome or nome_do_orgao(self.codigo)


@dataclass(frozen=True)
class ConfigAlertas:
    # Por onde o aviso sai:
    #   "teams" — webhook de canal. Sem credencial, sem TI, sem risco ao
    #             domínio, e a mensagem fica dentro do tenant da empresa.
    #   "graph" — e-mail por aplicativo registrado no Entra ID.
    #   "relay" — Direct Send. Sem senha, mas falha o SPF do domínio e a
    #             Microsoft vem desativando. Evitar.
    #   "smtp"  — usuário e senha. NÃO funciona com autenticação em dois
    #             fatores, e é assim que tem que ser.
    metodo: str = "teams"
    teams_webhook: str = ""
    # Endereço do painel na rede da empresa. Vira link nos avisos, para
    # baixar a planilha e os PDFs — o Teams não aceita anexo, e o pacote de
    # PDFs (~150 MB) não caberia em e-mail de qualquer forma.
    url_painel: str = ""
    smtp_host: str = ""
    smtp_porta: int = 587
    smtp_usuario: str = ""
    smtp_senha: str = ""
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    remetente: str = ""
    destinatarios: tuple[str, ...] = ()
    heartbeat_timeout_s: int = 300
    supressao_s: int = 1800
    # Teto geral de envios por hora. Protege a conta de e-mail contra um
    # defeito em laço, que a suprimir-por-tipo sozinha não pegaria.
    limite_por_hora: int = 20

    @property
    def habilitado(self) -> bool:
        if self.metodo == "teams":
            return bool(self.teams_webhook)
        if not self.destinatarios:
            return False
        if self.metodo == "graph":
            return bool(self.graph_tenant_id and self.graph_client_id
                        and self.graph_client_secret
                        and (self.remetente or self.smtp_usuario))
        if self.metodo == "relay":
            return bool(self.smtp_host and self.remetente)
        return bool(self.smtp_host and self.smtp_usuario and self.smtp_senha)

    def o_que_falta(self) -> list[str]:
        """Campos vazios, para o comando de teste dizer o que preencher."""
        faltando = []
        if self.metodo == "teams":
            if not self.teams_webhook:
                faltando.append("teams_webhook (ou a variável CND_TEAMS_WEBHOOK)")
            return faltando

        if not self.destinatarios:
            faltando.append("destinatarios")
        if self.metodo == "graph":
            for campo in ("graph_tenant_id", "graph_client_id", "graph_client_secret"):
                if not getattr(self, campo):
                    faltando.append(campo)
            if not (self.remetente or self.smtp_usuario):
                faltando.append("remetente")
        elif self.metodo == "relay":
            if not self.smtp_host:
                faltando.append("smtp_host (ex.: mapah-com-br.mail.protection.outlook.com)")
            if not self.remetente:
                faltando.append("remetente (ex.: robo.cnd@mapah.com.br)")
        else:
            if not self.smtp_host:
                faltando.append("smtp_host")
            if not self.smtp_usuario:
                faltando.append("smtp_usuario")
            if not self.smtp_senha:
                faltando.append("smtp_senha (ou a variável CND_SMTP_SENHA)")
        return faltando


@dataclass(frozen=True)
class Maquina:
    """Outra máquina da rede rodando o robô.

    `orgao` é o que ela faz ("Receita Federal"); `nome` é qual computador é
    ("PC-CND-01"). São coisas diferentes e as duas aparecem na tela: quem
    olha o painel quer o órgão, quem vai acessar a máquina quer o nome.

    `anydesk` é o número do AnyDesk instalado nela. Guardado aqui para que
    o botão de acesso remoto abra direto na máquina certa, sem ninguém ter
    que decorar nove dígitos.
    """

    nome: str
    url: str
    orgao: str = ""
    anydesk: str = ""

    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    @property
    def rotulo(self) -> str:
        """O que identifica a máquina na tela: o órgão, se houver."""
        return self.orgao or self.nome


@dataclass(frozen=True)
class ConfigRede:
    """Como esta máquina se identifica e quem ela consulta.

    Não há banco central: cada máquina é dona do seu, e o aplicativo
    pergunta a cada uma por HTTP. Isso dispensa servidor de banco, pasta
    compartilhada e qualquer instalação nova.
    """

    nome: str = ""                              # como aparece no aplicativo
    senha: str = ""                             # senha compartilhada, opcional
    # O AnyDesk DESTA máquina. Quem sabe o número é quem está na frente
    # dela; o console lê pela rede em vez de alguém redigitar em dois
    # lugares. O da lista `maquinas` continua valendo como reserva, para
    # quando a máquina estiver fora do ar — que é quando mais se precisa.
    anydesk: str = ""
    maquinas: tuple[Maquina, ...] = ()
    # "robo" (emite certidões) ou "console" (só acompanha). Vazio decide
    # sozinho: quem lista outras máquinas está acompanhando-as.
    papel: str = ""

    @property
    def roda_robo(self) -> bool:
        """Se esta máquina emite certidões.

        Manda o que o operador escreveu; sem nada escrito, a presença de
        outras máquinas na lista é o indício — quem acompanha três robôs
        não é um deles. O que está em jogo é mostrar ou não o botão de
        iniciar o robô, e botão que não serve àquela máquina é convite a
        alguém apertar por engano.
        """
        if self.papel:
            return self.papel.strip().lower() != "console"
        return not self.maquinas

    def todas(self) -> tuple[Maquina, ...]:
        """As máquinas a consultar. Vazio = só esta, pelo endereço local."""
        return self.maquinas or (Maquina(self.nome or "Esta máquina",
                                         "http://127.0.0.1:8000"),)


@dataclass(frozen=True)
class Config:
    banco: Path
    pasta_certidoes: Path
    pasta_evidencias: Path
    pasta_logs: Path
    alertas: ConfigAlertas
    rede: ConfigRede = field(default_factory=ConfigRede)
    orgaos: dict[str, ConfigOrgao] = field(default_factory=dict)

    def ativos(self) -> list[ConfigOrgao]:
        return [o for o in self.orgaos.values() if o.ativo]


def carregar(caminho: Path | None = None) -> Config:
    caminho = Path(caminho or CAMINHO_PADRAO)
    # utf-8-sig tolera o BOM que Bloco de Notas e PowerShell inserem —
    # sem isso, editar o config no Windows quebraria a subida do robô.
    dados = tomllib.loads(caminho.read_text(encoding="utf-8-sig"))

    geral = dados.get("geral", {})
    alertas_brutas = dict(dados.get("alertas", {}))
    # Segredos por variável de ambiente têm prioridade — não ficam no arquivo,
    # que é justamente o que permite versionar o config sem vazar credencial.
    if os.environ.get("CND_SMTP_SENHA"):
        alertas_brutas["smtp_senha"] = os.environ["CND_SMTP_SENHA"]
    if os.environ.get("CND_GRAPH_SECRET"):
        alertas_brutas["graph_client_secret"] = os.environ["CND_GRAPH_SECRET"]
    if os.environ.get("CND_TEAMS_WEBHOOK"):
        alertas_brutas["teams_webhook"] = os.environ["CND_TEAMS_WEBHOOK"]
    alertas_brutas["destinatarios"] = tuple(alertas_brutas.get("destinatarios", ()))
    alertas = ConfigAlertas(**{
        c: alertas_brutas[c] for c in ConfigAlertas.__dataclass_fields__
        if c in alertas_brutas
    })

    orgaos: dict[str, ConfigOrgao] = {}
    for codigo, bruto in dados.get("orgaos", {}).items():
        retry_bruto = bruto.get("retry", {})
        orgaos[codigo] = ConfigOrgao(
            codigo=codigo,
            ativo=bool(bruto.get("ativo", False)),
            adapter=bruto.get("adapter", codigo.lower()),
            workers=int(bruto.get("workers", 1)),
            nome=bruto.get("nome", ""),
            pacing=ParametrosRitmo.de_config(bruto.get("pacing", {})),
            breaker=ParametrosBreaker.de_config(bruto.get("breaker", {})),
            retry=ParametrosRetry(
                max_tentativas=int(retry_bruto.get("max_tentativas", 3)),
                backoff_erro_s=tuple(retry_bruto.get("backoff_erro_s", (120, 600, 2700))),
                backoff_captcha_s=tuple(retry_bruto.get("backoff_captcha_s", (3600, 14400, 43200))),
                backoff_bloqueio_s=tuple(retry_bruto.get("backoff_bloqueio_s", (300, 900, 1800))),
                retentativa_bloqueio_s=tuple(
                    retry_bruto.get("retentativa_bloqueio_s", (30.0, 90.0))
                ),
                # backoff_resultado_pendente_s foi removido de propósito e é
                # ignorado se ainda existir no config de alguma máquina.
            ),
            recuperacao=ParametrosRecuperacao.de_config(
                bruto.get("recuperacao", {})
            ),
            extras={c: v for c, v in bruto.items()
                    if c not in ("ativo", "adapter", "workers", "nome",
                                 "pacing", "breaker", "retry", "recuperacao")},
        )

    def caminho_de(chave: str, padrao: str) -> Path:
        valor = Path(geral.get(chave, padrao))
        return valor if valor.is_absolute() else RAIZ_PROJETO / valor

    rede_bruta = dados.get("rede", {})
    if os.environ.get("CND_REDE_SENHA"):
        rede_bruta = dict(rede_bruta, senha=os.environ["CND_REDE_SENHA"])
    rede = ConfigRede(
        nome=rede_bruta.get("nome", ""),
        senha=rede_bruta.get("senha", ""),
        papel=rede_bruta.get("papel", ""),
        anydesk=str(rede_bruta.get("anydesk", "")).strip(),
        maquinas=tuple(
            Maquina(nome=m.get("nome", m.get("url", "")), url=m.get("url", ""),
                    orgao=m.get("orgao", ""),
                    anydesk=str(m.get("anydesk", "")).strip())
            for m in rede_bruta.get("maquinas", []) if m.get("url")
        ),
    )

    return Config(
        rede=rede,
        banco=caminho_de("banco", "data/cnd.db"),
        pasta_certidoes=caminho_de("pasta_certidoes", "data/certidoes"),
        pasta_evidencias=caminho_de("pasta_evidencias", "data/evidencias"),
        pasta_logs=caminho_de("pasta_logs", "data/logs"),
        alertas=alertas,
        orgaos=orgaos,
    )
