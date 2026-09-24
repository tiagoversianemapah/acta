"""Prefeitura de Sao Luis (MA) - Certidao Negativa de Debitos Municipais.

Portal oficial (SEMFAZ, "Sistema Tributario Municipal"), mapeado em
24/09/2026:

    https://stm.semfaz.saoluis.ma.gov.br/credenciamento/jsp/emissaoCertidao/
        emissaoPublicaCertidao.jsf

JSF 1.2 com RichFaces 3.3 em JBoss, como a SEFAZ-MA - e, como ela, cabe
inteiro em HTTP, sem navegador:

    GET  a pagina                      -> sessao (JSESSIONID), ids do JSF
    AJAX radio "Pessoa Juridica"       -> o campo vira CNPJ e o combo de
                                          certidoes vira o da PJ
    AJAX botao verde com o CNPJ        -> razao social, ou "CNPJ nao
                                          encontrado" (sem cadastro aqui)
    GET  a imagem do captcha           -> cada GET sorteia um codigo novo
    AJAX "Emitir certidao"             -> redirect para emissaoSucesso.jsf,
                                          ou avisos na faixa do portal
    POST "Imprimir Certidao"           -> o PDF

O captcha e trivial - quatro glifos [A-Z1-9] num JPEG 65x20 (sem zero: o
"O" e sempre letra), peso e italico sorteados, sem ruido que importe -, entao
o leitor e o mesmo da SEFAZ-MA (`captcha_ma`), com ~85% de acerto por imagem
medido em 24/09/2026 (os erros sao quase todos D/O e 6/G). Errar custa
pouco: o portal responde "Codigo de verificacao esta incorreto" sem emitir
nada, e o GET seguinte da imagem ja traz outro codigo na mesma sessao.

O banco de templates vem SEMEADO no pacote (`captcha_sao_luis.json`, ao lado
deste arquivo), para o orgao funcionar na maquina no dia em que for ligado,
sem ferramenta de treino. O que o portal confirma na operacao vai para o banco
da maquina, `data/ocr/sao_luis.json`, que passa a valer no lugar da semente.

O combo "Certidao" oferece Negativa, Positiva com Efeito de Negativa e Baixa:
o robo escolhe sempre a Negativa. Tendo certidao recente, o portal devolve a
MESMA (numero e lavratura antigos) em vez de lavrar outra - visto em
24/09/2026 com uma lavrada em 22/09. Nao ha opcao na tela para forcar uma
nova, entao a data da lavratura vai na mensagem do item, para quem confere.

Os ids `j_id10:j_idNN` sao gerados pelo JSF e podem mudar num redeploy; por
isso sao LIDOS das paginas, com os valores observados como reserva.
"""
from __future__ import annotations

import contextlib
import html
import http.cookiejar
import re
import shutil
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from cnd.adapters.estadual.captcha_ma import BancoCaptcha
from cnd.core import tempo
from cnd.core.modelos import COM_PDF, Desfecho, Documento, ResultadoTentativa
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.db import RAIZ_PROJETO
from cnd.infra.log import obter

log = obter("adapter.sao_luis")

URL_CONSULTA = ("https://stm.semfaz.saoluis.ma.gov.br/credenciamento/jsp/"
                "emissaoCertidao/emissaoPublicaCertidao.jsf")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

# As paginas vem em ISO-8859-1 e as respostas AJAX em UTF-8, cada uma com o
# charset no header. Este e o padrao quando o header nao disser.
CHARSET = "iso-8859-1"

SEMENTE = Path(__file__).with_name("captcha_sao_luis.json")
BANCO_PADRAO = RAIZ_PROJETO / "data" / "ocr" / "sao_luis.json"

FINALIDADE_PADRAO = "Comprovação de regularidade fiscal"

# O valor do radio "Pessoa Juridica" (1 e Pessoa Fisica, 3 e Imovel).
TIPO_PJ = "2"

# Ids observados em 24/09/2026 - reserva, quando a leitura da pagina falhar.
FORM_PADRAO = "j_id10"
VIEWSTATE_PADRAO = "j_id1"
CAMPO_TIPO_PADRAO = "j_id10:j_id18"
ID_TIPO_PADRAO = "j_id10:j_id22"
CAMPO_CAPTCHA_PADRAO = "j_id10:captcha"
ID_EMITIR_PADRAO = "j_id10:j_id58"
CAMPO_CNPJ_PADRAO = "j_id10:j_id28"
ID_CONFERIR_PADRAO = "j_id10:j_id30"
CAMPO_MODELO_PADRAO = "j_id10:colecaoModelos"
MODELO_NEGATIVA_PADRAO = "47"
CAMPO_FINALIDADE_PADRAO = "j_id10:j_id47"

RE_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)
RE_TAG = re.compile(r"<[^>]+>")
RE_VIEWSTATE = re.compile(
    r'name="javax\.faces\.ViewState"[^>]*\svalue="([^"]*)"')
# O formulario da certidao e o que envolve o bloco `<form>:dados`; os outros
# dois da pagina sao o menu e a barra de navegacao.
RE_FORM = re.compile(r'<span id="([^":]+):dados"')
RE_CAPTCHA_IMG = re.compile(r'<img src="([^"]*Paint2DResource[^"]*)"')
RE_CAMPO_CAPTCHA = re.compile(r'<input id="[^"]*" type="text" name="([^"]*)"'
                              r'[^>]*maxlength="5"')
RE_RADIO_PJ = re.compile(
    r'<input type="radio"[^>]*name="([^"]+)"[^>]*value="' + TIPO_PJ + r'"'
    r'[^>]*\'parameters\':\{\'([^\']+)\'')
RE_EMITIR = re.compile(r'<a class="btn btn-primary[^"]*" href="#" id="([^"]+)"')
RE_CAMPO_CNPJ = re.compile(r'<input type="text" name="([^"]+)" class="[^"]*\bcnpj\b')
RE_CONFERIR = re.compile(r'<a class="btn btn-success add-on" href="#" id="([^"]+)"')
RE_SELECT_MODELO = re.compile(
    r'<select id="[^"]*" name="([^"]*colecaoModelos)"[^>]*>(.*?)</select>',
    re.DOTALL)
RE_OPCAO = re.compile(r'<option value="([^"]*)">(.*?)</option>', re.DOTALL)
RE_CAMPO_FINALIDADE = re.compile(
    r'<input type="text" name="([^"]+)"[^>]*maxlength="280"')
# A razao social volta no campo desabilitado logo depois do CNPJ.
RE_RAZAO = re.compile(r'<input type="text" name="[^"]+" value="([^"]*)" '
                      r'class="span6" disabled="disabled"')
RE_REDIRECT = re.compile(r'<meta name="Location" content="([^"]+)"')
RE_IMPRIMIR = re.compile(r'id="(([^":]+):btImprimirCertidao)"')

# A faixa de avisos do JSF, onde o portal diz por que nao emitiu. Vale pela
# ESTRUTURA, e nao por frase conhecida: a mensagem vai inteira para o item, e
# a proxima que ainda nao vimos cai de pe (a licao da SEFAZ-MA, 16/09/2026).
RE_AVISO = re.compile(r'<span class="pf-messages-[a-z]+-detail">(.*?)</span>',
                      re.IGNORECASE | re.DOTALL)
# O unico aviso que NAO e recusa: a leitura do captcha errou, e a resposta
# certa e pedir outra imagem. Visto em 24/09/2026: "Codigo de verificacao
# esta incorreto".
RE_AVISO_DE_CAPTCHA = re.compile(r"codigo de verificacao")
RE_DEBITO = re.compile(r"debito|pendencia")

# --- leitura do PDF ---------------------------------------------------------
# Conferido contra a certidao real de 24/09/2026: "CERTIDAO NEGATIVA" no
# titulo e "nao consta debito fiscal relativo a pessoa juridica" no corpo.
RE_VALIDADE = re.compile(r"validade\s*:\s*(\d{2}/\d{2}/\d{4})")
RE_AUTENTICIDADE = re.compile(r"autenticidade\W+no\W*([0-9a-f]{32})")
RE_LAVRADA = re.compile(r"lavrada em .*?em (\d{1,2} de [a-z]+ de \d{4})"
                        r"(?: as (\d{1,2}:\d{2}))?")

ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_CAPTCHA = "Não foi possível acertar o código de verificação do portal."
ERRO_SEM_BANCO = ("Leitor de captcha de Sao Luis sem banco de templates: "
                  "reinstale o ACTA ou rode ferramentas/treinar_ocr_sao_luis.py.")


@dataclass(frozen=True)
class RespostaPortal:
    status: int
    content_type: str
    corpo: bytes


@dataclass(frozen=True)
class Formulario:
    """Os ids que as duas primeiras telas revelam para montar os POSTs."""

    form: str = FORM_PADRAO
    viewstate: str = VIEWSTATE_PADRAO
    campo_tipo: str = CAMPO_TIPO_PADRAO
    id_tipo: str = ID_TIPO_PADRAO
    captcha_src: str = ""
    campo_captcha: str = CAMPO_CAPTCHA_PADRAO
    id_emitir: str = ID_EMITIR_PADRAO
    campo_cnpj: str = CAMPO_CNPJ_PADRAO
    id_conferir: str = ID_CONFERIR_PADRAO
    campo_modelo: str = CAMPO_MODELO_PADRAO
    modelo: str = MODELO_NEGATIVA_PADRAO
    campo_finalidade: str = CAMPO_FINALIDADE_PADRAO


def _sem_acento(texto: str | None) -> str:
    sem = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sem if not unicodedata.combining(c)).lower()


def decodificar(resposta: RespostaPortal) -> str:
    achado = RE_CHARSET.search(resposta.content_type or "")
    charset = achado.group(1) if achado else CHARSET
    try:
        return resposta.corpo.decode(charset, errors="replace")
    except LookupError:
        return resposta.corpo.decode(CHARSET, errors="replace")


def _limpo(trecho: str) -> str:
    return " ".join(html.unescape(RE_TAG.sub(" ", trecho)).split())


def _desfazer_dupla_codificacao(texto: str) -> str:
    """"CÃ³digo" -> "Código".

    Os avisos da resposta AJAX chegam em UTF-8 codificado DUAS vezes (o
    resto da resposta vem certo). Sem desfazer, o aviso de captcha errado
    nao e reconhecido - e cada leitura errada passaria por recusa do portal.
    Texto que ja esta certo nao sobrevive ao caminho de volta e fica como
    veio.
    """
    try:
        return texto.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto


def _ou(regex: re.Pattern[str], pagina: str, padrao: str) -> str:
    achado = regex.search(pagina)
    return achado.group(1) if achado else padrao


def ler_formulario(pagina: str) -> Formulario | None:
    """Os ids da pagina inicial. None se ela nao trouxe o captcha.

    Sem a imagem nao veio o formulario esperado (portal fora do ar, tela de
    erro), e montar POST em cima disso so produz o 500 do JBoss.
    """
    imagem = RE_CAPTCHA_IMG.search(pagina)
    if not imagem:
        return None
    radio = RE_RADIO_PJ.search(pagina)
    return Formulario(
        form=_ou(RE_FORM, pagina, FORM_PADRAO),
        viewstate=_ou(RE_VIEWSTATE, pagina, VIEWSTATE_PADRAO),
        campo_tipo=radio.group(1) if radio else CAMPO_TIPO_PADRAO,
        id_tipo=radio.group(2) if radio else ID_TIPO_PADRAO,
        captcha_src=html.unescape(imagem.group(1)),
        campo_captcha=_ou(RE_CAMPO_CAPTCHA, pagina, CAMPO_CAPTCHA_PADRAO),
        id_emitir=_ou(RE_EMITIR, pagina, ID_EMITIR_PADRAO),
    )


def modelo_negativa(opcoes: list[tuple[str, str]]) -> str | None:
    """O valor da "Certidao Negativa" no combo - nunca a positiva nem a baixa."""
    for valor, rotulo in opcoes:
        texto = _sem_acento(rotulo)
        if valor and "negativa" in texto and "positiva" not in texto:
            return valor
    return None


def completar_com_pj(formulario: Formulario, pagina: str) -> Formulario:
    """Os campos que so existem depois do radio "Pessoa Juridica"."""
    modelo = formulario.modelo
    campo_modelo = formulario.campo_modelo
    if select := RE_SELECT_MODELO.search(pagina):
        campo_modelo = select.group(1)
        opcoes = [(v, _limpo(r)) for v, r in RE_OPCAO.findall(select.group(2))]
        modelo = modelo_negativa(opcoes) or modelo
    return replace(
        formulario,
        campo_cnpj=_ou(RE_CAMPO_CNPJ, pagina, formulario.campo_cnpj),
        id_conferir=_ou(RE_CONFERIR, pagina, formulario.id_conferir),
        campo_modelo=campo_modelo,
        modelo=modelo,
        campo_finalidade=_ou(RE_CAMPO_FINALIDADE, pagina,
                             formulario.campo_finalidade),
    )


def avisos_do_portal(pagina: str) -> list[str]:
    """Cada aviso da faixa do portal, limpo e na ordem em que veio.

    Uma lista, e nao um texto so: os avisos se ACUMULAM - com o CNPJ e o
    captcha errados, o portal diz as duas coisas -, e e preciso separar o
    que e da leitura do captcha do que e recusa do portal.
    """
    return [texto for bruto in RE_AVISO.findall(pagina)
            if (texto := _desfazer_dupla_codificacao(_limpo(bruto)))]


def e_aviso_de_captcha(aviso: str) -> bool:
    return bool(RE_AVISO_DE_CAPTCHA.search(_sem_acento(aviso)))


def razao_social(pagina: str) -> str:
    return html.unescape(_ou(RE_RAZAO, pagina, "")).strip()


def destino_do_redirect(pagina: str) -> str | None:
    """O `Location` da resposta AJAX que diz "emitiu, va buscar"."""
    achado = RE_REDIRECT.search(pagina)
    return html.unescape(achado.group(1)) if achado else None


def _e_pdf(resposta: RespostaPortal) -> bool:
    return ("application/pdf" in (resposta.content_type or "").lower()
            and resposta.corpo.lstrip().startswith(b"%PDF"))


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def _extrair_validade(normalizado: str) -> date | None:
    if achado := RE_VALIDADE.search(normalizado):
        with contextlib.suppress(ValueError):
            return datetime.strptime(achado.group(1), "%d/%m/%Y").date()
    return None


def _extrair_codigo(normalizado: str) -> str | None:
    """O codigo de autenticidade - e o que valida a certidao no site."""
    achado = RE_AUTENTICIDADE.search(normalizado)
    return achado.group(1).upper() if achado else None


def _lavratura(normalizado: str) -> str | None:
    if achado := RE_LAVRADA.search(normalizado):
        data, hora = achado.groups()
        return f"{data} as {hora}" if hora else data
    return None


def ler_pdf(caminho: Path) -> ResultadoTentativa:
    """O PDF manda no desfecho: e ele que vai para o cliente.

    A positiva com efeito de negativa e testada ANTES da negativa - o titulo
    dela tambem diz "negativa". Qualquer coisa sem os marcadores vira
    ERRO_TECNICO com o PDF de evidencia: melhor nao entregar do que entregar
    a positiva de alguem como negativa.
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
    lavrada = _lavratura(normalizado)
    comuns = dict(
        validade=_extrair_validade(normalizado),
        codigo_controle=_extrair_codigo(normalizado),
        mensagem_portal=("Prefeitura de Sao Luis emitiu PDF"
                         + (f", lavrado em {lavrada}" if lavrada else "")),
    )

    if "positiva com efeito" in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)
    if "certidao positiva" in normalizado:
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho,
                                  **comuns)
    if ("certidao negativa" in normalizado
            and "nao consta debito" in normalizado):
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho,
                                  **comuns)

    log.warning("pdf_sao_luis_nao_reconhecido",
                extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
    return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                              mensagem_portal=ERRO_GENERICO,
                              evidencia=caminho)


@dataclass
class Portal:
    """Uma sessao no portal: cookies proprios e o HTTP de cada passo.

    Separada do adapter porque a ferramenta de treino do captcha anda pelo
    mesmo caminho, e o caminho so pode estar escrito num lugar.
    """

    url: str = URL_CONSULTA
    timeout_s: float = 30.0
    _opener: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _abrir(self, request: urllib.request.Request) -> RespostaPortal:
        try:
            with self._opener.open(request, timeout=self.timeout_s) as resposta:
                return RespostaPortal(int(resposta.status),
                                      resposta.headers.get("Content-Type", ""),
                                      resposta.read())
        except urllib.error.HTTPError as erro:
            return RespostaPortal(int(erro.code),
                                  erro.headers.get("Content-Type", ""),
                                  erro.read())

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/pdf,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
            "Referer": referer,
        }

    def get(self, url: str) -> RespostaPortal:
        return self._abrir(urllib.request.Request(
            url, headers=self._headers(self.url)))

    def post(self, url: str, dados: dict[str, str]) -> RespostaPortal:
        # UTF-8, como o XMLHttpRequest do A4J manda: e o que deixa a
        # finalidade chegar com acento.
        corpo = urllib.parse.urlencode(dados, encoding="utf-8").encode("ascii")
        headers = self._headers(url)
        headers["Content-Type"] = ("application/x-www-form-urlencoded; "
                                   "charset=UTF-8")
        return self._abrir(urllib.request.Request(url, data=corpo,
                                                  headers=headers))

    def _ajax(self, formulario: Formulario, botao: str,
              campos: dict[str, str]) -> RespostaPortal:
        return self.post(self.url, {
            "AJAXREQUEST": "_viewRoot",
            formulario.form: formulario.form,
            **campos,
            "javax.faces.ViewState": formulario.viewstate,
            botao: botao,
        })

    def abrir(self) -> Formulario | None:
        """GET da pagina e o radio "Pessoa Juridica"; None sem formulario."""
        inicial = self.get(self.url)
        formulario = ler_formulario(decodificar(inicial))
        if formulario is None:
            log.warning("formulario_ausente", extra={"status": inicial.status})
            return None
        escolha = self._ajax(formulario, formulario.id_tipo,
                             {formulario.campo_tipo: TIPO_PJ})
        return completar_com_pj(formulario, decodificar(escolha))

    def conferir_cnpj(self, formulario: Formulario,
                      cnpj: str) -> RespostaPortal:
        """O botao verde: o portal busca a razao social pelo CNPJ.

        Sem ele o "Emitir" recusa com "a razao social deve ser recuperada
        pressionando o botao verde".
        """
        return self._ajax(formulario, formulario.id_conferir, {
            formulario.campo_tipo: TIPO_PJ,
            formulario.campo_cnpj: cnpj,
            formulario.campo_modelo: "",
            formulario.campo_finalidade: "",
            formulario.campo_captcha: "",
        })

    def captcha(self, formulario: Formulario):
        """Uma imagem nova - e, com ela, um codigo novo na sessao."""
        from io import BytesIO

        from PIL import Image

        resposta = self.get(urllib.parse.urljoin(self.url,
                                                 formulario.captcha_src))
        try:
            imagem = Image.open(BytesIO(resposta.corpo))
            imagem.load()
            return imagem
        except Exception as erro:
            log.warning("captcha_ilegivel", extra={"status": resposta.status,
                                                   "erro": str(erro)[:200]})
            return None

    def emitir(self, formulario: Formulario, cnpj: str, codigo: str,
               finalidade: str) -> RespostaPortal:
        return self._ajax(formulario, formulario.id_emitir, {
            formulario.campo_tipo: TIPO_PJ,
            formulario.campo_cnpj: cnpj,
            formulario.campo_modelo: formulario.modelo,
            formulario.campo_finalidade: finalidade,
            formulario.campo_captcha: codigo.upper(),
        })

    def conferir_captcha(self, formulario: Formulario, codigo: str) -> bool:
        """Pergunta ao portal se o codigo esta certo, SEM emitir certidao.

        Emite sem CNPJ: o portal recusa por falta dele e, na mesma resposta,
        diz se o codigo estava errado - os avisos se acumulam. Nenhum aviso
        de captcha, entao, e o codigo certo (conferido em 24/09/2026). Serve
        a ferramenta de treino.
        """
        resposta = decodificar(self.emitir(formulario, "", codigo, ""))
        if destino_do_redirect(resposta):
            # Nao deveria acontecer sem CNPJ. Se acontecer, o portal mudou e
            # a ferramenta tem de parar antes de sair emitindo certidao.
            raise RuntimeError("o portal emitiu sem CNPJ: o oraculo nao vale")
        avisos = avisos_do_portal(resposta)
        return bool(avisos) and not any(e_aviso_de_captcha(a) for a in avisos)

    def imprimir(self, url_sucesso: str) -> RespostaPortal:
        """A tela de sucesso e o botao "Imprimir Certidao", que devolve o PDF."""
        pagina = decodificar(self.get(url_sucesso))
        botao = RE_IMPRIMIR.search(pagina)
        if not botao:
            raise RuntimeError("tela de sucesso sem o botao Imprimir Certidao")
        id_botao, form = botao.groups()
        return self.post(url_sucesso, {
            form: form,
            "javax.faces.ViewState": _ou(RE_VIEWSTATE, pagina,
                                         VIEWSTATE_PADRAO),
            id_botao: id_botao,
        })


@dataclass
class AdapterSaoLuis:
    orgao: str
    cfg: Config
    url_consulta: str = URL_CONSULTA
    timeout_s: float = 30.0
    tentativas_captcha: int = 12
    finalidade: str = FINALIDADE_PADRAO
    caminho_banco: Path = BANCO_PADRAO
    _banco: BancoCaptcha | None = field(default=None, repr=False)

    def preparar(self) -> None:
        self._garantir_banco()
        log.info("sessao_pronta", extra={"orgao": self.orgao})

    def reiniciar_sessao(self) -> None:
        # A sessao do portal ja e nova a cada documento; nao ha o que zerar.
        pass

    def encerrar(self) -> None:
        pass

    def _garantir_banco(self) -> None:
        """O banco da maquina, se ja houver; senao, a semente do pacote."""
        if self._banco is not None:
            return
        banco = BancoCaptcha.carregar(self.caminho_banco)
        origem = self.caminho_banco
        if banco.vazio:
            banco, origem = BancoCaptcha.carregar(SEMENTE), SEMENTE
        self._banco = banco
        log.info("banco_captcha_carregado",
                 extra={"orgao": self.orgao, "banco": str(origem),
                        "templates": banco.total})

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(f"A Prefeitura de Sao Luis deste robo "
                                 f"consulta CNPJ, e {doc.documento} e "
                                 f"{doc.tipo}."))

        self._garantir_banco()
        if self._banco.vazio:
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_SEM_BANCO)

        # Sessao nova a cada documento: o JSF guarda na sessao o CNPJ
        # conferido e a razao social, e herdar a do anterior misturaria uma
        # empresa com a seguinte.
        portal = Portal(self.url_consulta, self.timeout_s)
        try:
            return self._consultar(portal, doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento,
                "erro": f"{type(erro).__name__}: {erro}"[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

    def _consultar(self, portal: Portal, doc: Documento) -> ResultadoTentativa:
        formulario = portal.abrir()
        if formulario is None:
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

        conferido = decodificar(portal.conferir_cnpj(formulario, doc.documento))
        if avisos := avisos_do_portal(conferido):
            # "CNPJ nao encontrado": a empresa nao tem cadastro na Prefeitura.
            # Insistir nao muda nada, e quem abre o item precisa ler o que o
            # portal disse.
            log.info("cnpj_recusado", extra={"documento": doc.documento,
                                             "aviso": avisos[0][:200]})
            return ResultadoTentativa(Desfecho.PENDENCIA_MANUAL,
                                      mensagem_portal=" ".join(avisos))
        if not razao_social(conferido):
            log.warning("sem_razao_social", extra={"documento": doc.documento,
                                                   "trecho": _limpo(conferido)[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

        assert self._banco is not None
        ultimo_aviso = ""
        for tentativa in range(self.tentativas_captcha):
            imagem = portal.captcha(formulario)
            if imagem is None:
                continue
            palpite = self._banco.ler(imagem)
            if not palpite:
                # Glifos grudados (uns 10% das imagens): o proximo GET traz
                # outro codigo, e sai mais barato que arriscar.
                continue

            resposta = decodificar(portal.emitir(formulario, doc.documento,
                                                 palpite, self.finalidade))
            if destino := destino_do_redirect(resposta):
                self._aprender(imagem, palpite)
                log.info("emitido", extra={"documento": doc.documento,
                                           "tentativas": tentativa + 1})
                return self._baixar_certidao(portal, destino, doc)

            avisos = avisos_do_portal(resposta)
            recusas = [a for a in avisos if not e_aviso_de_captcha(a)]
            if recusas:
                # O portal disse por que nao emite, e o motivo vai inteiro
                # para o item. Captcha errado junto nao muda nada: os avisos
                # se acumulam, e a recusa continuaria com outro codigo.
                if len(recusas) == len(avisos):
                    self._aprender(imagem, palpite)
                mensagem = " ".join(recusas)
                desfecho = (Desfecho.POSITIVA
                            if RE_DEBITO.search(_sem_acento(mensagem))
                            else Desfecho.PENDENCIA_MANUAL)
                log.info("portal_recusou", extra={"documento": doc.documento,
                                                  "aviso": mensagem[:200]})
                return ResultadoTentativa(desfecho, mensagem_portal=mensagem)
            if avisos:
                ultimo_aviso = avisos[0]
                continue

            # Nem redirect nem aviso: e o 500 do JBoss, ou a sessao caiu.
            log.warning("emissao_sem_resposta",
                        extra={"documento": doc.documento,
                               "trecho": _limpo(resposta)[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

        # Nenhuma leitura passou. Quando o portal chegou a responder, e a
        # frase dele que vai para o item.
        return ResultadoTentativa(Desfecho.CAPTCHA,
                                  mensagem_portal=ultimo_aviso or ERRO_CAPTCHA)

    def _aprender(self, imagem, codigo: str) -> None:
        assert self._banco is not None
        try:
            if self._banco.aprender(imagem, codigo):
                self._banco.salvar(self.caminho_banco)
        except Exception as erro:
            # Aprender e bonus; nunca pode derrubar uma emissao.
            log.warning("captcha_aprender_falhou",
                        extra={"erro": str(erro)[:200]})

    def _baixar_certidao(self, portal: Portal, destino: str,
                         doc: Documento) -> ResultadoTentativa:
        resposta = portal.imprimir(urllib.parse.urljoin(portal.url, destino))
        if not _e_pdf(resposta):
            log.warning("impressao_sem_pdf",
                        extra={"documento": doc.documento,
                               "status": resposta.status,
                               "trecho": _limpo(decodificar(resposta))[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)
        return self._salvar_pdf(resposta.corpo, doc)

    def _salvar_pdf(self, conteudo: bytes,
                    doc: Documento) -> ResultadoTentativa:
        """O PDF nasce em evidencias e so muda para certidoes se for entregavel."""
        provisorio = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                      / f"{tempo.agora_iso()[:19].replace(':', '')}-certidao.pdf")
        provisorio.parent.mkdir(parents=True, exist_ok=True)
        provisorio.write_bytes(conteudo)

        resultado = ler_pdf(provisorio)
        if resultado.desfecho not in COM_PDF:
            return resultado

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(provisorio), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None)


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterSaoLuis:
    urls = orgao.extras.get("urls", {})
    http_cfg = orgao.extras.get("http", {})
    ocr = orgao.extras.get("ocr", {})
    banco = ocr.get("banco")
    return AdapterSaoLuis(
        orgao=orgao.codigo,
        cfg=cfg,
        url_consulta=str(urls.get("consulta", URL_CONSULTA)),
        timeout_s=float(http_cfg.get("timeout_s", 30.0)),
        tentativas_captcha=int(ocr.get("tentativas_captcha", 12)),
        finalidade=str(orgao.extras.get("finalidade", FINALIDADE_PADRAO)),
        caminho_banco=Path(banco) if banco else BANCO_PADRAO,
    )
