"""SEFAZ-MT - Certidao conjunta SEFAZ/PGE por HTTP.

Portal oficial mapeado em 21/09/2026:

    https://www.sefaz.mt.gov.br/cnd/certidao/servlet/ServletRotdAberto?origem=60

O formulario inicial carrega Turnstile, mas o servlet aceita o POST de emissao
sem token visivel. O portal tem dois caminhos:

* se ja existe CND/CPEND vigente, ele oferece "Reimprimir Certidao Vigente"
  ou "Emitir nova Certidao". O robo SEMPRE emite nova (origem 62): quem
  recebe a certidao exige emissao do mes corrente, e a vigente pode ser de
  dois meses atras - mesma regra da Receita, ver docs/fluxos/rfb-pj.md.
  Reimprimir entregava a antiga como se fosse de hoje (22/09/2026).
* se nao existe certidao vigente, ou depois de pedir nova, ele mostra uma
  tela "REQUERIMENTO" com refresh de 15s; depois de alguns polls no mesmo
  servlet, devolve o PDF.
"""
from __future__ import annotations

import contextlib
import html
import http.cookiejar
import re
import shutil
import time
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

log = obter("adapter.sefaz_mt")

URL_CONSULTA = (
    "https://www.sefaz.mt.gov.br/cnd/certidao/servlet/"
    "ServletRotdAberto?origem=60"
)
URL_SERVLET = "https://www.sefaz.mt.gov.br/cnd/certidao/servlet/ServletRotdAberto"

INDICE_MODELO_CERTIDAO = "19"
TIPO_DOCUMENTO_CNPJ = "2"
ORIGEM_CONSULTA = "76"
# O link "Emitir nova Certidao" da tela de vigente, capturado no navegador
# em 22/09/2026.
ORIGEM_EMITIR_NOVA = "62"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_BLOQUEIO = "O portal recusou a consulta."
ERRO_CAPTCHA = "O portal exigiu verificacao de seguranca."

RE_CHARSET = re.compile(r"charset=([A-Za-z0-9_-]+)", re.IGNORECASE)
RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
RE_TAG = re.compile(r"<[^>]+>")
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
RE_VALIDADE = re.compile(r"valid[ao]\s+ate[:\s]*(\d{2}/\d{2}/\d{4})")
RE_AUTENTICACAO = re.compile(
    r"(?:numero\s+de\s+autenticacao|autenticacao)[:\s]*([0-9a-z]{6,})")
RE_CND = re.compile(r"cnd\s+n[ro.]*\s*([0-9]{6,})")
RE_POSITIVA = re.compile(
    r"(?<!nao )consta(?:m)?\s+(?:pendencia|debito)|"
    r"possui\s+(?:pendencia|debito)|existem\s+pendencias|"
    r"certidao positiva de debitos"
)


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


def _limpar_html(bruto: str) -> str:
    sem_script = RE_SCRIPT.sub(" ", bruto)
    sem_tags = RE_TAG.sub(" ", sem_script)
    return " ".join(html.unescape(sem_tags).split())


def texto_da_resposta(resposta: RespostaPortal) -> str:
    return _limpar_html(_decodificar_resposta(resposta))


def _data_curta(texto: str) -> date | None:
    with contextlib.suppress(ValueError):
        return datetime.strptime(texto, "%d/%m/%Y").date()
    return None


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def _extrair_validade(conteudo: str) -> date | None:
    normalizado = " ".join(_sem_acento(conteudo).split())
    if achado := RE_VALIDADE.search(normalizado):
        return _data_curta(achado.group(1))
    datas = [_data_curta(a.group(1)) for a in RE_DATA.finditer(conteudo)]
    datas = [d for d in datas if d is not None]
    return max(datas) if datas else None


def _extrair_codigo(conteudo: str) -> str | None:
    normalizado = " ".join(_sem_acento(conteudo).split())
    if achado := RE_AUTENTICACAO.search(normalizado):
        return achado.group(1).upper()
    if achado := RE_CND.search(normalizado):
        return achado.group(1)
    return None


def _e_pdf(resposta: RespostaPortal) -> bool:
    return (
        "application/pdf" in resposta.content_type.lower()
        and resposta.corpo.lstrip().startswith(b"%PDF")
    )


def classificar_texto(texto: str) -> Desfecho:
    """Classifica respostas sem PDF.

    NEGATIVA e CPEN nunca saem daqui: sem PDF, o portal nao entregou a
    certidao que o relatorio promete.
    """
    t = _sem_acento(texto)
    if "acesso negado" in t and (
        "politica de seguranca" in t or "requisicao foi bloqueada" in t
    ):
        return Desfecho.BLOQUEIO_TEMPORARIO
    if "turnstile" in t or "verificacao de seguranca" in t:
        return Desfecho.CAPTCHA
    if "cnpj invalido" in t or "cpf invalido" in t:
        return Desfecho.PENDENCIA_MANUAL
    if "documento informado esta incorreto" in t:
        return Desfecho.PENDENCIA_MANUAL
    if "nao foi possivel emitir" in t and (
        "pendenc" in t or "debito" in t or "certidao negativa" in t
    ):
        return Desfecho.POSITIVA
    if "nao sao suficientes para que se considere sua situacao regular" in t:
        return Desfecho.POSITIVA
    if RE_POSITIVA.search(t):
        return Desfecho.POSITIVA
    return Desfecho.ERRO_TECNICO


def _mensagem(desfecho: Desfecho, texto: str) -> str:
    if desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
        return ERRO_BLOQUEIO
    if desfecho == Desfecho.CAPTCHA:
        return ERRO_CAPTCHA
    if desfecho == Desfecho.ERRO_TECNICO:
        return ERRO_GENERICO
    return " ".join((texto or "").split())[:500]


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
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

    if "positiva com efeito" in normalizado or "efeito de negativa" in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)
    if "certidao negativa de debitos" in normalizado:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if "nao consta" in normalizado and "pendencia" in normalizado:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if RE_POSITIVA.search(normalizado):
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    log.warning("pdf_sefaz_mt_nao_reconhecido",
                extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
    return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                              mensagem_portal=ERRO_GENERICO,
                              evidencia=caminho)


def _tem_vigente(texto: str) -> bool:
    return "reimprimir certidao vigente" in _sem_acento(texto)


def _em_processamento(texto: str) -> bool:
    t = _sem_acento(texto)
    return "requerimento" in t or "aguarde" in t


@dataclass
class AdapterSEFAZMT:
    orgao: str
    cfg: Config
    url_consulta: str = URL_CONSULTA
    url_servlet: str = URL_SERVLET
    timeout_s: float = 30.0
    espera_processamento_s: float = 16.0
    tempo_processamento_s: float = 120.0
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
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(f"A SEFAZ-MT deste robo consulta CNPJ, e "
                                 f"{doc.documento} e {doc.tipo}."))

        if self._opener is None:
            self.preparar()

        try:
            return self._consultar(doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento,
                "erro": f"{type(erro).__name__}: {erro}"[:300]})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)

    def _headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/pdf,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
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

    def _get(self, url: str, referer: str = "") -> RespostaPortal:
        return self._abrir(urllib.request.Request(
            url, headers=self._headers(referer)))

    def _post(self, dados: dict[str, str], referer: str) -> RespostaPortal:
        corpo = urllib.parse.urlencode(dados).encode("ascii")
        headers = {
            **self._headers(referer),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._abrir(urllib.request.Request(
            self.url_servlet, data=corpo, headers=headers))

    def _consultar(self, doc: Documento) -> ResultadoTentativa:
        inicial = self._get(self.url_consulta)
        texto_inicial = texto_da_resposta(inicial)
        if inicial.status >= 400:
            desfecho = classificar_texto(texto_inicial)
            return ResultadoTentativa(
                desfecho,
                mensagem_portal=_mensagem(desfecho, texto_inicial),
            )

        resposta = self._post(self._dados_consulta(doc), self.url_consulta)
        return self._interpretar(resposta, doc)

    def _dados_consulta(self, doc: Documento) -> dict[str, str]:
        return {
            "origem": ORIGEM_CONSULTA,
            "indiceModlCertSelecionado": INDICE_MODELO_CERTIDAO,
            "numrDoctFinal": doc.documento,
            "tipoDoctSele": TIPO_DOCUMENTO_CNPJ,
            "tipoDocumento": "CNPJ",
            "numeroDocumento": doc.documento,
            "botaoSubmit": "     OK     ",
            "codgMenu": "",
            "nomeMenu": "",
            "barraMenu": "",
            "codgTemporario": "",
            "cf-turnstile-response": "",
        }

    def _interpretar(self, resposta: RespostaPortal,
                    doc: Documento) -> ResultadoTentativa:
        if not _e_pdf(resposta) and _tem_vigente(texto_da_resposta(resposta)):
            return self._emitir_nova(doc)
        return self._interpretar_emissao(resposta, doc)

    def _interpretar_emissao(self, resposta: RespostaPortal,
                             doc: Documento) -> ResultadoTentativa:
        if _e_pdf(resposta):
            return self._salvar_pdf(resposta.corpo, doc,
                                    "SEFAZ-MT emitiu PDF")

        texto = texto_da_resposta(resposta)
        if _em_processamento(texto):
            return self._esperar_processamento(doc)

        desfecho = classificar_texto(texto)
        if desfecho in (Desfecho.ERRO_TECNICO, Desfecho.BLOQUEIO_TEMPORARIO):
            log.warning("resposta_sem_pdf", extra={"desfecho": str(desfecho),
                                                   "trecho": texto[:300]})
        return ResultadoTentativa(
            desfecho,
            mensagem_portal=_mensagem(desfecho, texto),
        )

    def _esperar_processamento(self, doc: Documento) -> ResultadoTentativa:
        limite = time.monotonic() + max(1.0, self.tempo_processamento_s)
        ultimo_texto = ""
        while time.monotonic() < limite:
            time.sleep(max(0.0, self.espera_processamento_s))
            resposta = self._get(self.url_servlet, self.url_servlet)
            if _e_pdf(resposta):
                return self._salvar_pdf(
                    resposta.corpo, doc, "SEFAZ-MT emitiu PDF")

            ultimo_texto = texto_da_resposta(resposta)
            if _em_processamento(ultimo_texto):
                continue

            desfecho = classificar_texto(ultimo_texto)
            return ResultadoTentativa(
                desfecho,
                mensagem_portal=_mensagem(desfecho, ultimo_texto),
            )

        log.warning("processamento_sem_pdf", extra={"orgao": self.orgao,
                                                    "trecho": ultimo_texto[:300]})
        return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                  mensagem_portal=ERRO_GENERICO)

    def _emitir_nova(self, doc: Documento) -> ResultadoTentativa:
        # Os campos do clique em "Emitir nova Certidao", como o navegador
        # manda - inclusive "usuario" com o texto null.
        resposta = self._post({
            "numrDoct": "",
            "origem": ORIGEM_EMITIR_NOVA,
            "indiceModlCertSelecionado": INDICE_MODELO_CERTIDAO,
            "caracteres": "",
            "tipoDoctSele": TIPO_DOCUMENTO_CNPJ,
            "numrDoctFinal": doc.documento,
            "codgMenu": "",
            "nomeMenu": "",
            "barraMenu": "",
            "codgTemporario": "",
            "usuario": "null",
        }, self.url_servlet)

        # Nao volta para _interpretar: se o portal insistir na vigente, e
        # erro. Reimprimir e justamente o que nao pode acontecer.
        if not _e_pdf(resposta) and _tem_vigente(texto_da_resposta(resposta)):
            log.warning("emitir_nova_devolveu_vigente",
                        extra={"documento": doc.documento})
            return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                                      mensagem_portal=ERRO_GENERICO)
        return self._interpretar_emissao(resposta, doc)

    def _salvar_pdf(self, conteudo: bytes, doc: Documento,
                    mensagem: str) -> ResultadoTentativa:
        provisorio = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                      / f"{tempo.agora_iso()[:19].replace(':', '')}-certidao.pdf")
        provisorio.parent.mkdir(parents=True, exist_ok=True)
        provisorio.write_bytes(conteudo)

        resultado = ler_pdf(provisorio, mensagem)
        if resultado.desfecho not in COM_PDF:
            return resultado

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(provisorio), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None)


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterSEFAZMT:
    urls = orgao.extras.get("urls", {})
    http_cfg = orgao.extras.get("http", {})
    return AdapterSEFAZMT(
        orgao=orgao.codigo,
        cfg=cfg,
        url_consulta=str(urls.get("consulta", URL_CONSULTA)),
        url_servlet=str(urls.get("servlet", URL_SERVLET)),
        timeout_s=float(http_cfg.get("timeout_s", 30.0)),
        espera_processamento_s=float(
            http_cfg.get("espera_processamento_s", 16.0)),
        tempo_processamento_s=float(
            http_cfg.get("tempo_processamento_s", 120.0)),
    )
