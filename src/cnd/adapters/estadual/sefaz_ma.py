"""SEFAZ-MA — Certidão Negativa de Débito (e de Dívida Ativa).

O serviço oficial é um app JSF/RichFaces antigo (rodapé "Sefaz/COTEC —
2005-2026"), em ISO-8859-1:

    https://sistemas1.sefaz.ma.gov.br/certidoes/jsp/
        emissaoCertidaoNegativa/emissaoCertidaoNegativa.jsf         (CND)
        emissaoCertidaoNegativaDividaAtiva/...DividaAtiva.jsf       (CNDA)

Mapeado em 14/09/2026 (ver docs/fluxos/sefaz-ma.md). Como o GO, ele devolve o
PDF no próprio POST — não precisa de robô cego. O que muda é o captcha: aqui
ele aparece em TODA emissão. É fixo, não heurístico (o oposto da Receita), e é
trivial de ler; e, decisivo, uma validação vale para a SESSÃO inteira. Isto é
o que torna o OCR local viável e o que o ADR-006 registra.

O caminho, por documento (página nova a cada consulta, por padrão):

    GET  .../emissaoCertidaoNegativa.jsf   -> sessão, cookies, ViewState, captcha
    AJAX troca o tipo para CPF/CNPJ  (senão o campo do documento não existe: 500)
    lê o captcha (captcha_ma) e o valida no portal   (AJAX, sem gastar emissão)
    POST form1:btn -> e a resposta do botão é a verdade:
        PDF               -> NEGATIVA
        "é devedor"       -> POSITIVA (há débito; o portal não emite PDF)
        nem um nem outro  -> a leitura errou; pede outra imagem e tenta de novo

A própria VALIDAÇÃO também responde por duas recusas do portal, e é preciso
lê-las: com o código CERTO ela pode voltar "é devedor" ou "Existe Inscrição
Estadual ativa para este CPF/CNPJ. Favor emitir pela Inscrição Estadual" —
nos dois casos junto de um `if(false)` igualzinho ao de captcha errado. Quem
olhar só o `if(true)` conclui "captcha errado" e retenta para sempre.

Detalhe que custou depuração: para um devedor a validação responde `if(false)`
— igual a captcha errado —, e o "é devedor" só aparece de fato no POST do
botão. Por isso o botão decide, não a validação. E o charset importa: a
validação vem em UTF-8 e o formulário em ISO-8859-1; cada resposta é decodada
pelo seu próprio `charset` (ver `_decodificar_resposta`).

Uma validação vale para a sessão inteira; `sessao_por_documento = false` liga
essa economia (um captcha por sessão em vez de por documento).

Os ids `form1:j_idNN` são gerados pelo RichFaces e podem mudar num redeploy do
portal; por isso são LIDOS da página quando possível, com os valores
observados como reserva.
"""
from __future__ import annotations

import contextlib
import html
import http.cookiejar
import re
import shutil
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from cnd.adapters.estadual.captcha_ma import BancoCaptcha
from cnd.core import tempo
from cnd.core.modelos import (
    COM_PDF,
    Desfecho,
    Documento,
    ResultadoTentativa,
)
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.db import RAIZ_PROJETO
from cnd.infra.log import obter

log = obter("adapter.sefaz_ma")

URL_CND = ("https://sistemas1.sefaz.ma.gov.br/certidoes/jsp/"
           "emissaoCertidaoNegativa/emissaoCertidaoNegativa.jsf")
URL_CNDA = ("https://sistemas1.sefaz.ma.gov.br/certidoes/jsp/"
            "emissaoCertidaoNegativaDividaAtiva/"
            "emissaoCertidaoNegativaDividaAtiva.jsf")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

# O app não manda charset no header das telas AJAX úteis, e o conteúdo é
# ISO-8859-1. Ler como UTF-8 troca "Certidão" por ruído.
CHARSET = "iso-8859-1"

# Ids do RichFaces observados em 14/09/2026 — reserva, quando a leitura da
# página falhar. Ver o docstring do módulo.
CAMPO_CAPTCHA_PADRAO = "form1:j_id20"
CAMPO_CNPJ_PADRAO = "form1:cpfCnpj"
ID_CONTAINER_PADRAO = "form1:j_id6"
ID_RADIO_CNPJ_PADRAO = "form1:j_id15"
ID_VALIDAR_PADRAO = "form1:j_id28"
VIEWSTATE_PADRAO = "j_id1"

RE_VIEWSTATE = re.compile(
    r'name="javax\.faces\.ViewState"[^>]*\svalue="([^"]*)"')
RE_CAPTCHA_IMG = re.compile(r'<img src="([^"]*Paint2DResource[^"]*)"')
RE_CAMPO_CAPTCHA = re.compile(r'<input[^>]*\sname="([^"]*)"[^>]*maxlength="4"')
RE_CONTAINER = re.compile(r"containerId':'([^']*)'")
RE_RADIO_CNPJ = re.compile(
    r'<input type="radio"[^>]*value="2"[^>]*similarityGroupingId\':\'([^\']*)\'')
# O botão que dispara a validação do captcha é o único cujo onclick acaba
# mandando clicar no submit escondido `form1:btn`.
RE_VALIDAR = re.compile(
    r'id="([^"]*)"[^>]*onclick="[^"]*form1:btn\'\)\.click\(\)')

RE_ARMADA = "if(true){document.getElementById('form1:btn')"
RE_NAO_ARMADA = "if(false){document.getElementById('form1:btn')"
RE_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)

# Quando há débito, o portal não emite PDF: volta ao formulário com a faixa
# "Este CPF/CNPJ é devedor." É a POSITIVA da SEFAZ-MA (conferido em 14/09/2026).
RE_DEVEDOR = re.compile(r"cpf/cnpj e devedor|e devedor")
# A faixa de avisos do JSF — onde o portal escreve o motivo de não emitir.
# Vale por REGRA, não por mensagem conhecida: já apareceram "é devedor",
# "Existe Inscrição Estadual ativa..." e "Existe pendência de IPVA ou de Auto
# de IPVA", todas com `if(false)` igual ao de captcha errado. Cada uma custou
# um lote travado até alguém ler o HTML. Reconhecer a FAIXA resolve também a
# próxima, que ainda não vimos.
RE_AVISO_PORTAL = re.compile(
    r'<span class="pf-messages-[a-z]+-detail">(.*?)</span>',
    re.IGNORECASE | re.DOTALL)

# Nem todo aviso é recusa de cadastro: "Código da imagem inválido." é o
# portal dizendo que a LEITURA errou, e a resposta certa é pedir outra
# imagem. Fechar o item nesse caso é pior que o bug que a faixa veio
# resolver — ele vira uma pendência que ninguém tem como tratar, porque não
# há pendência nenhuma. Foram 17 itens assim em 16/09/2026.
RE_AVISO_DE_CAPTCHA = re.compile(r"(codigo|imagem)[^.]{0,30}invalid")

RE_TAG = re.compile(r"<[^>]+>")
# `<style>` sai junto do `<script>`: as telas do portal abrem com meia dúzia
# de regras CSS, e era isso que ocupava o `trecho` de 200 caracteres do log —
# o "Ocorreu um erro de sistema" ficava logo DEPOIS do corte, invisível para
# quem lia o Registro tentando entender a falha (16/09/2026).
RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>",
                       re.IGNORECASE | re.DOTALL)

# --- leitura do PDF ---------------------------------------------------------
# "não constam débitos relativos aos tributos estaduais" é a linha operativa
# da certidão negativa, conferida contra o documento real de 14/09/2026.
RE_NEGATIVA = re.compile(r"nao constam debitos|nao consta debito|nada consta")
# O `(?<!nao )` impede que o "consta debito" de dentro de "nao consta debito"
# conte como débito — um é substring do outro.
RE_POSITIVA = re.compile(r"(?<!nao )constam debitos|possui debito|"
                         r"(?<!nao )consta debito")
RE_NAO_INSCRITO = re.compile(r"nao inscrito no cadastro")
RE_VALIDADE = re.compile(r"validade[^:]*:.*?(\d{2}/\d{2}/\d{4})")
RE_NUMERO = re.compile(r"N[ºo°]?\s*([0-9]{3,}/[0-9]{2})")


@dataclass(frozen=True)
class RespostaPortal:
    status: int
    content_type: str
    corpo: bytes


@dataclass(frozen=True)
class Formulario:
    """O que uma página do formulário revela para montar os POSTs."""

    viewstate: str
    captcha_src: str
    campo_captcha: str = CAMPO_CAPTCHA_PADRAO
    container: str = ID_CONTAINER_PADRAO
    id_radio_cnpj: str = ID_RADIO_CNPJ_PADRAO
    id_validar: str = ID_VALIDAR_PADRAO


def _sem_acento(texto: str | None) -> str:
    sem = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sem if not unicodedata.combining(c)).lower()


def _decodificar_resposta(resposta: RespostaPortal) -> str:
    achado = RE_CHARSET.search(resposta.content_type or "")
    charset = achado.group(1) if achado else CHARSET
    try:
        return resposta.corpo.decode(charset, errors="replace")
    except LookupError:
        return resposta.corpo.decode(CHARSET, errors="replace")


def texto_da_resposta(resposta: RespostaPortal) -> str:
    bruto = _decodificar_resposta(resposta)
    sem_script = RE_SCRIPT.sub(" ", bruto)
    sem_tags = RE_TAG.sub(" ", sem_script)
    return " ".join(html.unescape(sem_tags).split())


def aviso_do_portal(corpo: str) -> str:
    """O que o portal escreveu na faixa de avisos, limpo. Vazio se não houve.

    É o motivo específico — "Existe pendência de IPVA ou de Auto de IPVA.",
    por exemplo — e é ele que vai para o item, sem paráfrase nossa: quem abre
    o painel precisa ler o que o portal disse, não o nosso resumo dele.

    A tela de erro de sistema do portal (o `NullPointerException`) NÃO usa
    esta faixa, e é por isso que ela serve para separar "o portal recusou,
    e disse por quê" de "o portal quebrou".
    """
    partes = []
    for bruto in RE_AVISO_PORTAL.findall(corpo):
        texto = " ".join(html.unescape(RE_TAG.sub(" ", bruto)).split())
        if texto:
            partes.append(texto)
    return " ".join(partes)


def ler_formulario(corpo: bytes) -> Formulario | None:
    """Extrai da página os ids e o endereço da imagem do captcha.

    None quando não há imagem de captcha na página — é o sinal de que não
    veio o formulário esperado (portal fora do ar, tela de erro), e não vale
    seguir montando POST em cima de lixo.
    """
    pagina = corpo.decode(CHARSET, errors="replace")
    achou_img = RE_CAPTCHA_IMG.search(pagina)
    if not achou_img:
        return None

    def _ou(regex: re.Pattern[str], padrao: str) -> str:
        achado = regex.search(pagina)
        return achado.group(1) if achado else padrao

    return Formulario(
        viewstate=_ou(RE_VIEWSTATE, VIEWSTATE_PADRAO),
        captcha_src=achou_img.group(1),
        campo_captcha=_ou(RE_CAMPO_CAPTCHA, CAMPO_CAPTCHA_PADRAO),
        container=_ou(RE_CONTAINER, ID_CONTAINER_PADRAO),
        id_radio_cnpj=_ou(RE_RADIO_CNPJ, ID_RADIO_CNPJ_PADRAO),
        id_validar=_ou(RE_VALIDAR, ID_VALIDAR_PADRAO),
    )


# Mensagens para o relatório do cliente. Curtas e sempre iguais: quem lê quer
# saber que o item não fechou, não qual exceção do Python apareceu. O detalhe
# vai para o log. Mesma política do adapter da Receita e do GO.
ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_CAPTCHA = "Não foi possível validar o código de segurança do portal."
ERRO_SEM_TREINO = ("Leitor de captcha da SEFAZ-MA sem treino nesta máquina: "
                   "rode ferramentas/treinar_ocr_sefaz_ma.py.")


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def _extrair_validade(conteudo: str) -> date | None:
    achado = RE_VALIDADE.search(" ".join(_sem_acento(conteudo).split()))
    if not achado:
        return None
    with contextlib.suppress(ValueError):
        return datetime.strptime(achado.group(1), "%d/%m/%Y").date()
    return None


def _extrair_codigo(conteudo: str) -> str | None:
    achado = RE_NUMERO.search(conteudo)
    return achado.group(1) if achado else None


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
    """Classifica a certidão pelo PDF, que é a única resposta entregável.

    Só NEGATIVA e CPEN (que aqui não observamos) são entregáveis. Qualquer
    outra coisa — texto que afirma e nega ao mesmo tempo, ou nenhum dos dois
    marcadores — vira ERRO_TECNICO e o PDF vai para conferência manual:
    melhor não entregar do que entregar a positiva de alguém como negativa.
    """
    try:
        conteudo = _texto_pdf(caminho)
    except Exception as erro:
        log.warning("pdf_ilegivel", extra={"arquivo": str(caminho),
                                            "erro": str(erro)[:300]})
        return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                  mensagem_portal=ERRO_GENERICO,
                                  evidencia=caminho)

    normalizado = " ".join(_sem_acento(conteudo).split())
    mensagem = texto_tela.strip()[:500]
    if RE_NAO_INSCRITO.search(normalizado):
        # Ainda é uma certidão negativa válida — o Estado certifica que não há
        # débito —, mas o CNPJ não consta no cadastro de ICMS do MA. Fica na
        # mensagem para quem confere perceber, sem barrar a entrega.
        mensagem = ("CNPJ não inscrito no cadastro de contribuintes do "
                    "ICMS-MA. " + mensagem).strip()[:500]
    comuns = dict(
        validade=_extrair_validade(conteudo),
        codigo_controle=_extrair_codigo(conteudo),
        mensagem_portal=mensagem,
    )

    nega = bool(RE_NEGATIVA.search(normalizado))
    afirma = bool(RE_POSITIVA.search(normalizado))
    if nega and not afirma:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if afirma and not nega:
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    log.warning("pdf_sefaz_ma_nao_reconhecido",
                extra={"arquivo": str(caminho), "ambiguo": nega and afirma,
                       "trecho": normalizado[:250]})
    return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                              mensagem_portal=ERRO_GENERICO,
                              evidencia=caminho)


def _e_pdf(resposta: RespostaPortal) -> bool:
    return ("application/pdf" in resposta.content_type.lower()
            and resposta.corpo.lstrip().startswith(b"%PDF"))


def _contexto_ssl() -> ssl.SSLContext:
    """Contexto TLS para portais estaduais no pacote Windows.

    No robô empacotado, o OpenSSL do Python pode não achar a cadeia local que o
    Windows/Edge aceitam. `truststore` usa a loja do sistema; `certifi` fica como
    reserva para instalações sem essa dependência.
    """
    with contextlib.suppress(Exception):
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with contextlib.suppress(Exception):
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


@dataclass
class AdapterSEFAZMA:
    orgao: str
    cfg: Config
    url_consulta: str = URL_CND
    timeout_s: float = 30.0
    tentativas_captcha: int = 12
    # Página nova (sessão nova, captcha novo) a CADA documento: consultou,
    # pegou o PDF, reinicia e vai de novo. É o fluxo mais robusto — não herda
    # uma sessão que expirou no meio da carteira — e o padrão. Uma validação
    # do MA arma a sessão para várias emissões; quem quiser essa economia
    # (um captcha por sessão, não por documento) põe isto em false no config.
    sessao_por_documento: bool = True
    caminho_banco: Path = field(
        default_factory=lambda: RAIZ_PROJETO / "data" / "ocr" / "sefaz_ma.json")
    _opener: object | None = field(default=None, repr=False)
    _banco: BancoCaptcha | None = field(default=None, repr=False)
    _viewstate: str = field(default=VIEWSTATE_PADRAO, repr=False)
    _campo_captcha: str = field(default=CAMPO_CAPTCHA_PADRAO, repr=False)
    _ultimo_codigo: str = field(default="", repr=False)
    _sessao_armada: bool = field(default=False, repr=False)

    def preparar(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=_contexto_ssl()),
        )
        self._sessao_armada = False
        self._campo_captcha = CAMPO_CAPTCHA_PADRAO

    def _garantir_banco(self) -> None:
        """Carrega o banco de captcha uma vez; banco vazio = não treinado."""
        if self._banco is None:
            self._banco = BancoCaptcha.carregar(self.caminho_banco)
            if self._banco.vazio:
                log.warning("captcha_sem_treino",
                            extra={"orgao": self.orgao,
                                   "banco": str(self.caminho_banco)})
            else:
                log.info("banco_captcha_carregado",
                         extra={"orgao": self.orgao,
                                "templates": self._banco.total})

    def reiniciar_sessao(self) -> None:
        # Mantém o banco de captcha (é aprendizado da máquina, não da sessão);
        # descarta só os cookies e a marca de sessão armada.
        self.preparar()

    def encerrar(self) -> None:
        self._opener = None
        self._sessao_armada = False
        self._campo_captcha = CAMPO_CAPTCHA_PADRAO

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        # Segunda porta contra CPF: a ingestão já recusa CPF para este órgão,
        # mas mandar um CPF no campo de CNPJ não daria erro no portal — ele
        # consultaria outro documento e devolveria a certidão de alguém.
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(f"A SEFAZ-MA deste robô consulta CNPJ, e "
                                 f"{doc.documento} é {doc.tipo}."))

        self._garantir_banco()
        if self._banco.vazio:
            # Sem banco não há como ler o captcha. ERRO_TECNICO é retentável,
            # mas o conserto é humano (treinar), então o disjuntor vai pausar
            # o órgão depressa em vez de martelar o portal à toa.
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_SEM_TREINO)

        # Página nova por documento: a sessão anterior é descartada, então a
        # próxima consulta começa do zero — GET, captcha, emissão.
        if self._opener is None or self.sessao_por_documento:
            self.preparar()

        try:
            return self._consultar(doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento,
                "erro": f"{type(erro).__name__}: {erro}"[:300]})
            self._sessao_armada = False
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

    # --- HTTP -------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/pdf,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
            "Referer": self.url_consulta,
        }
        if extra:
            headers.update(extra)
        return headers

    def _abrir(self, request: urllib.request.Request) -> RespostaPortal:
        if self._opener is None:
            raise RuntimeError("sessao nao preparada: chame preparar() antes")
        try:
            with self._opener.open(request, timeout=self.timeout_s) as resposta:
                return RespostaPortal(int(resposta.status),
                                      resposta.headers.get("Content-Type", ""),
                                      resposta.read())
        except urllib.error.HTTPError as erro:
            return RespostaPortal(int(erro.code),
                                  erro.headers.get("Content-Type", ""),
                                  erro.read())

    def _get(self, url: str) -> RespostaPortal:
        return self._abrir(urllib.request.Request(url, headers=self._headers()))

    def _post(self, dados: dict[str, str],
              content_type: str = "application/x-www-form-urlencoded"
                                  "; charset=ISO-8859-1") -> RespostaPortal:
        corpo = urllib.parse.urlencode(dados, encoding=CHARSET).encode(CHARSET)
        request = urllib.request.Request(
            self.url_consulta, data=corpo,
            headers=self._headers({"Content-Type": content_type}))
        return self._abrir(request)

    # --- fluxo ------------------------------------------------------------

    def _consultar(self, doc: Documento) -> ResultadoTentativa:
        # Sessão já armada (modo reuso): tenta emitir direto. Se a sessão caiu,
        # cai no laço abaixo e rearma sozinho.
        if self._sessao_armada:
            resultado = self._tentar_emitir(doc)
            if resultado is not None:
                return resultado
            self._sessao_armada = False

        assert self._banco is not None
        ultimo_aviso = ""
        for tentativa in range(self.tentativas_captcha):
            formulario = self._nova_pagina(tentativa)
            if formulario is None:
                continue
            imagem = self._baixar_captcha(formulario.captcha_src)
            if imagem is None:
                continue
            palpite = self._banco.ler(imagem)
            if not palpite:
                continue

            # Sem trocar o tipo para CPF/CNPJ, o campo do documento não existe
            # na árvore do JSF e o portal responde 500. O captcha sobrevive à
            # troca (é da sessão, não da tela).
            self._trocar_para_cnpj(formulario)
            estado, aviso = self._validar_captcha(formulario, doc, palpite)
            ultimo_aviso = aviso or ultimo_aviso
            self._viewstate = formulario.viewstate
            self._campo_captcha = formulario.campo_captcha
            self._ultimo_codigo = palpite

            if estado == "recusa":
                # Captcha certo; o portal é que não emite para esta empresa, e
                # disse o motivo. Insistir não muda nada: é trabalho para uma
                # pessoa, com o recado do portal em mãos — por isso ele vai
                # inteiro para o item, em vez de virar "recusado pelo portal".
                #
                # A sessão NÃO fica armada: esta resposta veio com `if(false)`,
                # ou seja, o portal recusou antes de liberar o botão. Dizer o
                # contrário faria o próximo documento — no modo de sessão
                # reaproveitada — gastar um POST que só volta erro de sistema.
                self._aprender(imagem, palpite)
                self._sessao_armada = False
                log.info("portal_recusou", extra={"documento": doc.documento,
                                                  "aviso": aviso[:200]})
                return ResultadoTentativa(Desfecho.PENDENCIA_MANUAL,
                                          mensagem_portal=aviso)

            if estado == "devedor":
                # O "é devedor" já veio na validação: captcha certo, há débito.
                self._aprender(imagem, palpite)
                self._sessao_armada = True
                log.info("devedor", extra={"documento": doc.documento})
                return ResultadoTentativa(
                    Desfecho.POSITIVA,
                    mensagem_portal="Este CPF/CNPJ é devedor.")

            # Aqui está o pulo do gato: mesmo quando a validação diz `if(false)`
            # (que também é a resposta de um DEVEDOR, não só de captcha errado),
            # tentamos EMITIR e deixamos a resposta do botão decidir — PDF é
            # NEGATIVA, "é devedor" é POSITIVA. Só se o botão não devolver nada
            # útil é que a leitura de fato errou, e aí tentamos outra imagem.
            resultado = self._tentar_emitir(doc)
            if resultado is not None:
                self._aprender(imagem, palpite)
                self._sessao_armada = True
                log.info("emitido",
                         extra={"tentativas": tentativa + 1,
                                "desfecho": str(resultado.desfecho),
                                "templates": self._banco.total})
                return resultado
            # Nada conclusivo: a leitura errou (ou a sessão caiu). Próxima imagem.
            self._sessao_armada = False

        # Nenhuma imagem levou a uma resposta conclusiva. CAPTCHA é retentável e
        # é o desfecho que o ritmo/disjuntor entendem como "recuar". Quando o
        # portal chegou a dizer algo — "Código da imagem inválido." —, é a
        # frase DELE que vai para o item: quem abre o painel precisa saber se
        # o robô não leu a imagem ou se nem chegou a receber resposta.
        return ResultadoTentativa(Desfecho.CAPTCHA,
                                  mensagem_portal=ultimo_aviso or ERRO_CAPTCHA)

    def _nova_pagina(self, tentativa: int) -> Formulario | None:
        inicial = self._get(self.url_consulta)
        formulario = ler_formulario(inicial.corpo)
        if formulario is None:
            log.warning("formulario_ausente",
                        extra={"status": inicial.status, "tentativa": tentativa})
        return formulario

    def _tentar_emitir(self, doc: Documento) -> ResultadoTentativa | None:
        """POSTa o botão e classifica a resposta.

        NEGATIVA (PDF), POSITIVA (devedor) e a recusa com motivo são
        conclusivas. None significa que não veio resposta útil — sessão caída
        ou erro de sistema do portal —, e quem chamou decide rearmar.
        """
        resposta = self._emitir_pdf(doc)
        if _e_pdf(resposta):
            return self._salvar_pdf(resposta.corpo, doc)
        texto = texto_da_resposta(resposta)
        if RE_DEVEDOR.search(_sem_acento(texto)):
            return ResultadoTentativa(Desfecho.POSITIVA,
                                      mensagem_portal="Este CPF/CNPJ é devedor.")
        # Mesma regra da validação: aviso na faixa do portal é recusa com
        # motivo, e o motivo vai inteiro para o item — menos quando o motivo
        # é a própria leitura do captcha, que pede outra imagem.
        aviso = aviso_do_portal(_decodificar_resposta(resposta))
        if aviso and not RE_AVISO_DE_CAPTCHA.search(_sem_acento(aviso)):
            return ResultadoTentativa(Desfecho.PENDENCIA_MANUAL,
                                      mensagem_portal=aviso)
        log.warning("emissao_sem_pdf",
                    extra={"documento": doc.documento, "status": resposta.status,
                           "trecho": texto[:200]})
        return None

    def _baixar_captcha(self, src: str):
        from io import BytesIO

        from PIL import Image

        url = urllib.parse.urljoin(self.url_consulta, src)
        resposta = self._abrir(
            urllib.request.Request(url, headers=self._headers()))
        try:
            imagem = Image.open(BytesIO(resposta.corpo))
            imagem.load()
            return imagem
        except Exception as erro:
            log.warning("captcha_ilegivel", extra={"erro": str(erro)[:200]})
            return None

    def _trocar_para_cnpj(self, formulario: Formulario) -> None:
        """AJAX que troca o formulário de Inscrição Estadual para CPF/CNPJ.

        É o clique no rádio "CPF/CNPJ". Só depois dele o campo do documento
        passa a existir no servidor; sem isso, validar e emitir dão 500.
        """
        dados = {
            "AJAXREQUEST": formulario.container,
            "form1": "form1",
            "form1:tipoEmissao": "2",
            "form1:inscricaoEstadual": "",
            formulario.campo_captcha: "",
            "javax.faces.ViewState": formulario.viewstate,
            formulario.id_radio_cnpj: formulario.id_radio_cnpj,
        }
        self._post(dados)

    def _validar_captcha(self, formulario: Formulario, doc: Documento,
                         codigo: str) -> str:
        """Confere o palpite no portal (AJAX do botão), sem gastar emissão.

        Devolve `(estado, aviso)`, com o estado sendo um de quatro:
          "devedor" — o portal respondeu "Este CPF/CNPJ é devedor": captcha
                      CERTO, e há débito (POSITIVA).
          "recusa"  — veio um aviso na faixa do portal: captcha CERTO também,
                      e ele não vai emitir por algum motivo de cadastro. O
                      texto dele volta em `aviso`.
          "armada"  — `if(true){...btn.click()}`: captcha certo, pode emitir.
          "captcha" — `if(false)` e faixa VAZIA: aí sim a leitura errou.

        A regra que importa é a diferença entre as duas últimas. Uma recusa
        do portal responde `if(false)` igual a um captcha errado, e olhar só
        o `if(true)` faz o item retentar até acabar a paciência do retry:
        aconteceu com 33 itens em 16/09/2026 ("Existe Inscrição Estadual
        ativa...") e com mais um no mesmo lote ("Existe pendência de IPVA ou
        de Auto de IPVA"). Reconhecer a FAIXA, e não cada frase, é o que faz
        a próxima mensagem — que ainda não vimos — cair de pé.
        """
        dados = {
            "AJAXREQUEST": formulario.container,
            "form1": "form1",
            "form1:tipoEmissao": "2",
            CAMPO_CNPJ_PADRAO: doc.documento,
            formulario.campo_captcha: codigo.upper(),
            "javax.faces.ViewState": formulario.viewstate,
            formulario.id_validar: formulario.id_validar,
        }
        resposta = self._post(dados)
        corpo = _decodificar_resposta(resposta)
        # O aviso é texto visível e pode vir com entidade (&eacute;), por isso
        # é lido e limpo por `aviso_do_portal`. Já o `if(true)` é JavaScript e
        # vive no corpo cru.
        aviso = aviso_do_portal(corpo)
        if aviso:
            limpo = _sem_acento(aviso)
            if RE_DEVEDOR.search(limpo):
                return "devedor", aviso
            if RE_AVISO_DE_CAPTCHA.search(limpo):
                return "captcha", aviso
            return "recusa", aviso
        if RE_ARMADA in corpo:
            return "armada", ""
        return "captcha", ""

    def _emitir_pdf(self, doc: Documento) -> RespostaPortal:
        dados = {
            "form1": "form1",
            "form1:tipoEmissao": "2",
            CAMPO_CNPJ_PADRAO: doc.documento,
            self._campo_captcha: self._ultimo_codigo.upper(),
            "javax.faces.ViewState": self._viewstate,
            "form1:btn": "Emitir Certidão",
        }
        return self._post(dados)

    def _aprender(self, imagem, codigo: str) -> None:
        assert self._banco is not None
        try:
            novos = self._banco.aprender(imagem, codigo)
            if novos:
                self._banco.salvar(self.caminho_banco)
        except Exception as erro:
            # Aprender é bônus; nunca pode derrubar uma emissão.
            log.warning("captcha_aprender_falhou",
                        extra={"erro": str(erro)[:200]})

    def _salvar_pdf(self, conteudo: bytes, doc: Documento) -> ResultadoTentativa:
        """Guarda o PDF e o classifica. Só o entregável vai para certidoes.

        O PDF chega antes de sabermos o que ele diz, então nasce em
        `evidencias` e só MUDA para `certidoes` se a leitura disser NEGATIVA
        (ou CPEN). É o mesmo critério do GO e da Receita: positiva não fica ao
        lado das entregáveis para ninguém distribuir por engano.
        """
        provisorio = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                      / f"{tempo.agora_iso()[:19].replace(':', '')}-certidao.pdf")
        provisorio.parent.mkdir(parents=True, exist_ok=True)
        provisorio.write_bytes(conteudo)

        resultado = ler_pdf(provisorio, "SEFAZ-MA emitiu PDF")
        if resultado.desfecho not in COM_PDF:
            return resultado

        destino = caminho_certidao(self.cfg.pasta_certidoes, doc.lote_id,
                                   self.orgao, doc.documento,
                                   resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(provisorio), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None)


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterSEFAZMA:
    urls = orgao.extras.get("urls", {})
    ocr = orgao.extras.get("ocr", {})
    http_cfg = orgao.extras.get("http", {})
    banco = ocr.get("banco")
    return AdapterSEFAZMA(
        orgao=orgao.codigo,
        cfg=cfg,
        url_consulta=str(urls.get("consulta", URL_CND)),
        timeout_s=float(http_cfg.get("timeout_s", 30.0)),
        tentativas_captcha=int(ocr.get("tentativas_captcha", 12)),
        sessao_por_documento=bool(ocr.get("sessao_por_documento", True)),
        caminho_banco=(Path(banco) if banco
                       else RAIZ_PROJETO / "data" / "ocr" / "sefaz_ma.json"),
    )
