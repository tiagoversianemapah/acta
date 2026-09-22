"""Prefeitura de Vitoria (ES) - Certidao Negativa de Debitos de Tributos
Municipais.

Portal oficial, mapeado em 22/09/2026:

    https://tributario.vitoria.es.gov.br/Servicos/CertidaoNegativa/CertidaoNegativa.aspx

SEM captcha, e SEM navegador: e um ASP.NET WebForms comum, e todo o fluxo
cabe em tres POSTs e um GET.

    1. GET  a pagina, para pegar __VIEWSTATE e __EVENTVALIDATION;
    2. POST escolhendo o tipo de documento (postback do radio CNPJ);
    3. POST com o documento e o botao "Continuar" - a resposta ja traz o
       veredito na tela e um botao "Emitir";
    4. GET  no link do "Emitir", que devolve o PDF pronto.

O link do passo 4 vem dentro do `onclick` do botao, como
`ExibirRelatorioRetPDF.aspx?qs=<token>`. O token e da SESSAO: sem os cookies
dos passos anteriores ele nao vale, e por isso a sessao inteira acontece no
mesmo `opener`.

Sempre EMITIR, nunca segunda via. Tendo certidao dos ultimos 60 dias, o
portal avisa e oferece a 2a via - mas quem recebe a certidao exige emissao do
mes corrente, a mesma regra da Receita e do MT (docs/fluxos/rfb-pj.md). O
botao "Emitir" tira uma nova, e e nele que o robo clica.

Empresa com debito nao recebe certidao nenhuma: a tela diz que "as
informacoes disponiveis nao sao suficientes para que se considere sua
situacao fiscal regular", lista as pendencias e nao oferece o botao. Isso e
POSITIVA, e a tela vira evidencia - e a mesma frase que a SEFAZ-GO usa.

Documento com digito errado tambem nao produz tela de erro: o portal
simplesmente devolve o formulario, sem veredito e sem botao. Como a planilha
ja valida o documento na importacao, isso aqui vira PENDENCIA_MANUAL - alguem
precisa olhar, e insistir nao muda o resultado.
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

from cnd.core import tempo
from cnd.core.modelos import COM_PDF, Desfecho, Documento, ResultadoTentativa
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.vitoria")

URL_CONSULTA = ("https://tributario.vitoria.es.gov.br/Servicos/"
                "CertidaoNegativa/CertidaoNegativa.aspx")

# Os nomes dos campos do WebForms. Sao gerados pelo ASP.NET a partir da
# arvore de controles, entao mudam se a Prefeitura mexer na tela - e ai o
# sintoma e o portal devolver o formulario sem veredito.
CAMPO_TIPO = "ctl00$conteudo$rblTipoDocumento"
CAMPO_DOCUMENTO = "ctl00$conteudo$txtTermoBusca"
CAMPO_CONTINUAR = "ctl00$conteudo$btnEnviar"
RADIO_POR_TIPO = {"CNPJ": "1", "CPF": "2"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_BLOQUEIO = "O portal recusou a consulta."
ERRO_SEM_VEREDITO = ("O portal de Vitoria devolveu o formulario sem resposta "
                     "para este documento.")

RE_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)
RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
RE_TAG = re.compile(r"<[^>]+>")
RE_OCULTO = re.compile(
    r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]*value="([^"]*)"',
    re.IGNORECASE)
RE_LINK_EMITIR = re.compile(r"window\.open\('([^']+)'", re.IGNORECASE)
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
RE_VALIDADE = re.compile(r"valido\s+ate\s+o\s+dia\s+(\d{2}/\d{2}/\d{4})")
# O recorte da resposta. O cabecalho da pagina repete "Certidao Negativa de
# Debitos" em TODA consulta, inclusive na que acusa pendencia: classificar o
# texto inteiro daria negativa para todo mundo. O veredito mora entre o
# rotulo do campo e o balao de ajuda, no fim.
RE_INICIO_RESULTADO = re.compile(
    r"informe\s*:\s*(?:cnpj|cpf|inscricao fiscal)\s*:", re.IGNORECASE)
RE_FIM_RESULTADO = re.compile(
    r"ajuda\s+documento utilizado para fins", re.IGNORECASE)
RE_CHAVE = re.compile(
    r"entre\s+com\s+a\s+chave[:\s]*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@dataclass(frozen=True)
class RespostaPortal:
    status: int
    content_type: str
    corpo: bytes


def _sem_acento(texto: str | None) -> str:
    sem = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sem if not unicodedata.combining(c)).lower()


def _charset(content_type: str) -> str:
    if achado := RE_CHARSET.search(content_type or ""):
        return achado.group(1)
    return "utf-8"


def _decodificar_resposta(resposta: RespostaPortal) -> str:
    try:
        return resposta.corpo.decode(_charset(resposta.content_type),
                                     errors="replace")
    except LookupError:
        return resposta.corpo.decode("utf-8", errors="replace")


def texto_da_resposta(resposta: RespostaPortal) -> str:
    bruto = _decodificar_resposta(resposta)
    sem_script = RE_SCRIPT.sub(" ", bruto)
    return " ".join(html.unescape(RE_TAG.sub(" ", sem_script)).split())


def campos_ocultos(pagina: str) -> dict[str, str]:
    """__VIEWSTATE e companhia, que todo POST do WebForms precisa devolver."""
    return {nome: html.unescape(valor)
            for nome, valor in RE_OCULTO.findall(pagina)}


def link_de_emissao(pagina: str) -> str | None:
    """O endereco do PDF, que mora no `onclick` do botao Emitir.

    Sem botao nao ha certidao: e assim que a tela responde a documento que o
    portal nao aceitou.
    """
    achado = RE_LINK_EMITIR.search(html.unescape(pagina))
    return achado.group(1) if achado else None


def _data_curta(texto: str) -> date | None:
    with contextlib.suppress(ValueError):
        return datetime.strptime(texto, "%d/%m/%Y").date()
    return None


def _extrair_validade(conteudo: str) -> date | None:
    normalizado = " ".join(_sem_acento(conteudo).split())
    if achado := RE_VALIDADE.search(normalizado):
        return _data_curta(achado.group(1))
    datas = [_data_curta(a.group(1)) for a in RE_DATA.finditer(conteudo)]
    datas = [d for d in datas if d is not None]
    return max(datas) if datas else None


def _extrair_codigo(conteudo: str) -> str | None:
    """A chave de autenticidade do PDF - e o que valida o documento no site."""
    normalizado = " ".join(_sem_acento(conteudo).split())
    if achado := RE_CHAVE.search(normalizado):
        return achado.group(1).upper()
    return None


def _e_pdf(resposta: RespostaPortal) -> bool:
    return (
        "application/pdf" in (resposta.content_type or "").lower()
        and resposta.corpo.lstrip().startswith(b"%PDF")
    )


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def resultado_da_tela(texto: str) -> str:
    """So o que o portal respondeu, sem o cabecalho nem o balao de ajuda."""
    recorte = texto
    if achado := RE_INICIO_RESULTADO.search(_sem_acento(recorte)):
        recorte = recorte[achado.end():]
    if achado := RE_FIM_RESULTADO.search(_sem_acento(recorte)):
        recorte = recorte[:achado.start()]
    return recorte.strip(" ×")


def classificar_texto(texto: str) -> Desfecho:
    """Classifica a TELA, que e onde o portal diz o veredito antes do PDF.

    NEGATIVA e CPEN so valem com o PDF em maos: sem ele o robo nao tem o
    documento que a entrega promete, e prometer certidao que nao existe e
    pior que tentar de novo. Por isso quem chama usa isto para decidir o que
    fazer, e o desfecho final sai de `ler_pdf`.
    """
    t = _sem_acento(resultado_da_tela(texto))
    if "acesso negado" in t or "requisicao foi bloqueada" in t:
        return Desfecho.BLOQUEIO_TEMPORARIO
    if "captcha" in t or "verificacao de seguranca" in t:
        return Desfecho.CAPTCHA
    # Como o portal diz "tem debito": nao ha botao Emitir nessa tela, e a
    # frase e a mesma da SEFAZ-GO (22/09/2026).
    if ("nao sao suficientes para que se considere sua situacao fiscal regular"
            in t or "pendencias encontradas" in t):
        return Desfecho.POSITIVA
    if "positiva com efeito de negativa" in t:
        return Desfecho.CPEN
    if "documento valido ate o dia" in t:
        return Desfecho.NEGATIVA
    return Desfecho.ERRO_TECNICO


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
    """O PDF manda no desfecho: e ele que vai para o cliente.

    Negativa cita o artigo 205 do CTN; a positiva com efeito de negativa, o
    206 e a "exigibilidade suspensa". Empresa sem cadastro em Vitoria recebe
    negativa com o aviso "nao possui registros nos cadastros da PMV" - e
    certidao legitima, emitida pela Prefeitura.
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
    comuns = dict(
        validade=_extrair_validade(conteudo),
        codigo_controle=_extrair_codigo(conteudo),
        mensagem_portal=texto_tela.strip()[:500],
    )

    if ("positiva com efeito de negativa" in normalizado
            or "exigibilidade suspensa" in normalizado
            or "artigo 206" in normalizado):
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)
    if ("nao constam pendencias" in normalizado
            or "nao possui registros nos cadastros" in normalizado
            or "artigo 205" in normalizado):
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho,
                                  **comuns)
    if "constam" in normalizado and "debito" in normalizado:
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho,
                                  **comuns)

    log.warning("pdf_vitoria_nao_reconhecido",
                extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
    return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                              mensagem_portal=ERRO_GENERICO,
                              evidencia=caminho)


@dataclass
class AdapterVitoria:
    orgao: str
    cfg: Config
    url_consulta: str = URL_CONSULTA
    timeout_s: float = 30.0
    _opener: object | None = field(default=None, repr=False)

    def preparar(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        log.info("sessao_pronta", extra={"orgao": self.orgao})

    def reiniciar_sessao(self) -> None:
        self.preparar()

    def encerrar(self) -> None:
        self._opener = None

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        if doc.tipo not in RADIO_POR_TIPO:
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(f"A Prefeitura de Vitoria consulta CNPJ ou "
                                 f"CPF, e {doc.documento} e {doc.tipo}."))

        # Sessao nova a cada documento: o token do PDF e da sessao, e o
        # WebForms guarda o passo anterior nela. Reaproveitar a de antes
        # misturaria a consulta de uma empresa com a da seguinte.
        self.preparar()

        try:
            return self._consultar(doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento,
                "erro": f"{type(erro).__name__}: {erro}"[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self, com_formulario: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/pdf,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
            "Referer": self.url_consulta,
        }
        if com_formulario:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _abrir(self, request: urllib.request.Request) -> RespostaPortal:
        if self._opener is None:
            raise RuntimeError("sessao nao preparada: chame preparar() antes")
        try:
            with self._opener.open(request, timeout=self.timeout_s) as resposta:
                return RespostaPortal(
                    status=int(resposta.status),
                    content_type=resposta.headers.get("Content-Type", ""),
                    corpo=resposta.read(),
                )
        except urllib.error.HTTPError as erro:
            return RespostaPortal(
                status=int(erro.code),
                content_type=erro.headers.get("Content-Type", ""),
                corpo=erro.read(),
            )

    def _get(self, url: str) -> RespostaPortal:
        return self._abrir(urllib.request.Request(
            url, headers=self._headers()))

    def _post(self, dados: dict[str, str]) -> RespostaPortal:
        corpo = urllib.parse.urlencode(dados).encode("utf-8")
        return self._abrir(urllib.request.Request(
            self.url_consulta, data=corpo,
            headers=self._headers(com_formulario=True)))

    # ------------------------------------------------------------------
    # Fluxo
    # ------------------------------------------------------------------
    def _consultar(self, doc: Documento) -> ResultadoTentativa:
        inicial = self._get(self.url_consulta)
        if inicial.status >= 400:
            texto = texto_da_resposta(inicial)
            desfecho = classificar_texto(texto)
            return ResultadoTentativa(desfecho,
                                      mensagem_portal=_mensagem(desfecho, texto))

        pagina = _decodificar_resposta(inicial)
        escolha = self._post({
            **campos_ocultos(pagina),
            "__EVENTTARGET": f"{CAMPO_TIPO}${RADIO_POR_TIPO[doc.tipo]}",
            "__EVENTARGUMENT": "",
            CAMPO_TIPO: doc.tipo,
        })

        resposta = self._post({
            **campos_ocultos(_decodificar_resposta(escolha)),
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            CAMPO_TIPO: doc.tipo,
            CAMPO_DOCUMENTO: doc.documento,
            CAMPO_CONTINUAR: "Continuar",
        })
        return self._interpretar(resposta, doc)

    def _interpretar(self, resposta: RespostaPortal,
                     doc: Documento) -> ResultadoTentativa:
        pagina = _decodificar_resposta(resposta)
        texto = texto_da_resposta(resposta)
        link = link_de_emissao(pagina)
        if link is not None:
            return self._baixar_certidao(link, doc, texto)

        desfecho = classificar_texto(texto)
        if desfecho == Desfecho.POSITIVA:
            # Sem botao porque nao ha certidao a emitir: a tela lista as
            # pendencias, e e ela que fica guardada para quem for tratar.
            return ResultadoTentativa(
                Desfecho.POSITIVA,
                mensagem_portal=_mensagem(desfecho, texto),
                evidencia=self._salvar_html(resposta.corpo, doc))
        if desfecho in (Desfecho.NEGATIVA, Desfecho.CPEN,
                        Desfecho.ERRO_TECNICO):
            # Veredito de certidao sem botao para baixa-la nao e resultado:
            # ou o portal mudou de tela, ou recusou o documento. Quem olha
            # decide; o robo nao inventa PDF.
            log.warning("sem_botao_emitir",
                        extra={"documento": doc.documento,
                               "trecho": texto[:300]})
            return ResultadoTentativa(Desfecho.PENDENCIA_MANUAL,
                                      mensagem_portal=ERRO_SEM_VEREDITO,
                                      evidencia=self._salvar_html(
                                          resposta.corpo, doc))
        return ResultadoTentativa(desfecho,
                                  mensagem_portal=_mensagem(desfecho, texto))

    def _salvar_html(self, conteudo: bytes, doc: Documento) -> Path:
        """A tela como ela veio, para quem for conferir a pendencia depois."""
        caminho = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                   / f"{tempo.agora_iso()[:19].replace(':', '')}-tela.html")
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(conteudo)
        return caminho

    def _baixar_certidao(self, link: str, doc: Documento,
                         texto_tela: str) -> ResultadoTentativa:
        url = urllib.parse.urljoin(self.url_consulta, link)
        resposta = self._get(url)
        if not _e_pdf(resposta):
            texto = texto_da_resposta(resposta)
            desfecho = classificar_texto(texto)
            log.warning("emissao_sem_pdf", extra={"documento": doc.documento,
                                                  "trecho": texto[:300]})
            if desfecho in (Desfecho.NEGATIVA, Desfecho.CPEN):
                desfecho = Desfecho.ERRO_TECNICO
            return ResultadoTentativa(desfecho,
                                      mensagem_portal=_mensagem(desfecho, texto))

        return self._salvar_pdf(resposta.corpo, doc, texto_tela)

    def _salvar_pdf(self, conteudo: bytes, doc: Documento,
                    texto_tela: str) -> ResultadoTentativa:
        provisorio = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                      / f"{tempo.agora_iso()[:19].replace(':', '')}-certidao.pdf")
        provisorio.parent.mkdir(parents=True, exist_ok=True)
        provisorio.write_bytes(conteudo)

        resultado = ler_pdf(provisorio, texto_tela)
        if resultado.desfecho not in COM_PDF:
            return resultado

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(provisorio), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None,
                       mensagem_portal="Prefeitura de Vitoria emitiu PDF")


def _mensagem(desfecho: Desfecho, texto: str) -> str:
    """O motivo como o PORTAL escreveu, nao a nossa parafrase.

    Quem abre o item precisa ler o que Vitoria respondeu - "nao sao
    suficientes para que se considere sua situacao fiscal regular" diz o que
    fazer; "erro na consulta" nao diz nada.
    """
    if desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
        return ERRO_BLOQUEIO
    if desfecho == Desfecho.ERRO_TECNICO:
        return ERRO_GENERICO
    return " ".join(resultado_da_tela(texto or "").split())[:500]


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterVitoria:
    urls = orgao.extras.get("urls", {})
    http_cfg = orgao.extras.get("http", {})
    return AdapterVitoria(
        orgao=orgao.codigo,
        cfg=cfg,
        url_consulta=str(urls.get("consulta", URL_CONSULTA)),
        timeout_s=float(http_cfg.get("timeout_s", 30.0)),
    )
