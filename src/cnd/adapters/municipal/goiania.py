"""Prefeitura de Goiania - Certidao Conjunta de Regularidade Fiscal.

Portal oficial, menu SOBAC:

    https://www.goiania.go.gov.br/sistemas/sccer/asp/sccer00000f0.asp

O item "Por Pessoa" abre `sccer00300f0.asp` e emite a certidao por CPF/CNPJ.
Embora a tela tenha imagem de captcha, em 21/09/2026 o POST do botao
"Emitir Certidao" aceitou `txt_captcha` vazio. Este adapter nao tenta ler nem
responder o captcha: ele reproduz o clique no botao, com o campo vazio.

A resposta vem em HTML para impressao, nao em PDF. Para o contrato do ACTA, que
entrega PDFs, o adapter guarda o HTML original em evidencias e gera um PDF
texto com o conteudo oficial retornado pelo portal.
"""
from __future__ import annotations

import contextlib
import html
import http.cookiejar
import os
import re
import shutil
import subprocess
import textwrap
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

log = obter("adapter.goiania")

URL_CONSULTA = "https://www.goiania.go.gov.br/sistemas/sccer/asp/sccer00300f0.asp"
URL_EMISSAO = "https://www.goiania.go.gov.br/sistemas/sccer/asp/sccer00300w0.asp"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_CAPTCHA = "O portal exigiu o codigo de seguranca."
ERRO_BLOQUEIO = "O portal recusou a consulta."

RE_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)
RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
RE_TAG = re.compile(r"<[^>]+>")
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
RE_VALIDADE = re.compile(r"validade[:\s]*ate\s+(\d{2}/\d{2}/\d{4})")
RE_NUMERO = re.compile(r"numero\s+da\s+certidao[:\s]*([0-9.\-]+)")
RE_POSITIVA = re.compile(
    r"positiva\s+de\s+debitos|(?<!nao )constam?\s+debitos?"
)
RE_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)

CSS_IMPRESSAO = """
<base href="https://www.goiania.go.gov.br/">
<meta charset="utf-8">
<style>
  @page { size: A4 portrait; margin: 10mm; }
  html, body { margin: 0; padding: 0; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  table { page-break-inside: avoid; }
  img { max-width: 100%; }
</style>
"""


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
    # As telas ASP antigas do municipio usam ISO-8859-1, e muitas respostas
    # declaram no header com "Charset" maiusculo. Sem charset, este e o melhor
    # padrao para manter acentos legiveis.
    return "iso-8859-1"


def _decodificar_resposta(resposta: RespostaPortal) -> str:
    try:
        return resposta.corpo.decode(_charset(resposta.content_type),
                                     errors="replace")
    except LookupError:
        return resposta.corpo.decode("iso-8859-1", errors="replace")


def texto_da_resposta(resposta: RespostaPortal) -> str:
    bruto = _decodificar_resposta(resposta)
    sem_script = RE_SCRIPT.sub(" ", bruto)
    sem_tags = RE_TAG.sub(" ", sem_script)
    return " ".join(html.unescape(sem_tags).split())


def _data_curta(texto: str) -> date | None:
    with contextlib.suppress(ValueError):
        return datetime.strptime(texto, "%d/%m/%Y").date()
    return None


def _extrair_validade(texto: str) -> date | None:
    normalizado = " ".join(_sem_acento(texto).split())
    if achado := RE_VALIDADE.search(normalizado):
        return _data_curta(achado.group(1))
    datas = [_data_curta(a.group(1)) for a in RE_DATA.finditer(texto)]
    datas = [d for d in datas if d is not None]
    return max(datas) if datas else None


def _extrair_codigo(texto: str) -> str | None:
    normalizado = " ".join(_sem_acento(texto).split())
    if achado := RE_NUMERO.search(normalizado):
        return achado.group(1).strip(" .-") if achado.group(1) else None
    return None


def classificar_certidao(texto: str) -> ResultadoTentativa:
    normalizado = " ".join(_sem_acento(texto).split())
    comuns = dict(
        validade=_extrair_validade(texto),
        codigo_controle=_extrair_codigo(texto),
        mensagem_portal=" ".join((texto or "").split())[:500],
    )

    if "acesso negado" in normalizado and (
        "politica de seguranca" in normalizado
        or "requisicao foi bloqueada" in normalizado
    ):
        return ResultadoTentativa(Desfecho.BLOQUEIO_TEMPORARIO, **comuns)
    if "digite os caracteres" in normalizado or "codigo de seguranca" in normalizado:
        return ResultadoTentativa(Desfecho.CAPTCHA, **comuns)
    if "cpf/cnpj invalido" in normalizado or "cpf ou cnpj invalido" in normalizado:
        return ResultadoTentativa(Desfecho.PENDENCIA_MANUAL, **comuns)
    if "informe 1 - cpf ou 2 - cnpj" in normalizado:
        return ResultadoTentativa(Desfecho.ERRO_TECNICO, **comuns)

    if "positiva com efeito" in normalizado or "efeito de negativa" in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, **comuns)
    if (
        "certidao conjunta de regularidade fiscal" in normalizado
        and "nao consta debito vencido ou a vencer" in normalizado
    ):
        return ResultadoTentativa(Desfecho.NEGATIVA, **comuns)
    if "certidao negativa" in normalizado and "nao consta debito" in normalizado:
        return ResultadoTentativa(Desfecho.NEGATIVA, **comuns)
    if RE_POSITIVA.search(normalizado):
        return ResultadoTentativa(Desfecho.POSITIVA, **comuns)

    log.warning("resposta_goiania_nao_reconhecida",
                extra={"trecho": normalizado[:300]})
    return ResultadoTentativa(Desfecho.ERRO_TECNICO,
                              mensagem_portal=ERRO_GENERICO,
                              validade=comuns["validade"],
                              codigo_controle=comuns["codigo_controle"])


def _mensagem(desfecho: Desfecho, texto: str) -> str:
    if desfecho == Desfecho.CAPTCHA:
        return ERRO_CAPTCHA
    if desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
        return ERRO_BLOQUEIO
    if desfecho == Desfecho.ERRO_TECNICO:
        return ERRO_GENERICO
    return " ".join((texto or "").split())[:500]


def _escape_pdf_texto(texto: str) -> bytes:
    bruto = texto.encode("cp1252", errors="replace")
    saida = bytearray()
    for byte in bruto:
        if byte in (ord("\\"), ord("("), ord(")")):
            saida.append(ord("\\"))
            saida.append(byte)
        elif byte in (10, 13):
            saida.append(ord(" "))
        else:
            saida.append(byte)
    return b"(" + bytes(saida) + b")"


def _pdf_de_texto(texto: str) -> bytes:
    """PDF simples, sem dependencia externa, para certidao HTML imprimivel."""
    linhas: list[str] = []
    for bloco in (texto or "").splitlines():
        limpo = " ".join(bloco.split())
        if not limpo:
            linhas.append("")
            continue
        linhas.extend(textwrap.wrap(limpo, width=96) or [""])

    if not linhas:
        linhas = ["Certidao emitida pelo portal da Prefeitura de Goiania."]

    por_pagina = 58
    paginas = [linhas[i:i + por_pagina] for i in range(0, len(linhas), por_pagina)]

    objetos: list[bytes] = []
    objetos.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    kids = []
    for indice in range(len(paginas)):
        kids.append(f"{4 + indice * 2} 0 R".encode("ascii"))
    objetos.append(
        b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count "
        + str(len(paginas)).encode("ascii") + b" >>"
    )
    objetos.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )

    for indice, linhas_pagina in enumerate(paginas):
        pagina_obj = 4 + indice * 2
        conteudo_obj = pagina_obj + 1
        objetos.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {conteudo_obj} 0 R >>".encode("ascii")
        )
        comandos = [b"BT", b"/F1 10 Tf", b"50 800 Td", b"12 TL"]
        for linha in linhas_pagina:
            comandos.append(_escape_pdf_texto(linha) + b" Tj")
            comandos.append(b"T*")
        comandos.append(b"ET")
        stream = b"\n".join(comandos)
        objetos.append(
            b"<< /Length " + str(len(stream)).encode("ascii")
            + b" >>\nstream\n" + stream + b"\nendstream"
        )

    saida = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for numero, objeto in enumerate(objetos, start=1):
        offsets.append(len(saida))
        saida.extend(f"{numero} 0 obj\n".encode("ascii"))
        saida.extend(objeto)
        saida.extend(b"\nendobj\n")

    inicio_xref = len(saida)
    saida.extend(f"xref\n0 {len(objetos) + 1}\n".encode("ascii"))
    saida.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        saida.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    saida.extend(
        b"trailer\n<< /Size " + str(len(objetos) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(inicio_xref).encode("ascii") + b"\n%%EOF\n"
    )
    return bytes(saida)


def _html_para_impressao(resposta: RespostaPortal) -> str:
    """HTML em UTF-8 para o navegador imprimir sem perder acentos."""
    bruto = _decodificar_resposta(resposta)
    if achado := RE_HEAD.search(bruto):
        return bruto[:achado.end()] + CSS_IMPRESSAO + bruto[achado.end():]
    return f"<!doctype html><html><head>{CSS_IMPRESSAO}</head><body>{bruto}</body></html>"


def _abrir_chromium(playwright):
    ultimo_erro: Exception | None = None
    for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception as erro:
            ultimo_erro = erro
    if ultimo_erro:
        raise ultimo_erro
    raise RuntimeError("nenhum navegador Chromium disponivel")


def _pdf_com_playwright(html_path: Path, destino: Path) -> None:
    from playwright.sync_api import sync_playwright

    html_pronto = html_path.read_text(encoding="utf-8")
    with sync_playwright() as p:
        browser = _abrir_chromium(p)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.route(
                URL_EMISSAO,
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html_pronto,
                ),
            )
            page.goto(URL_EMISSAO, wait_until="load")
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=5000)
            page.emulate_media(media="print")
            page.pdf(
                path=str(destino),
                format="A4",
                print_background=True,
                # Sem data e endereco impressos no alto: a certidao vai para
                # o cliente, e ali aquilo e o carimbo de uma pagina salva do
                # navegador, nao parte do documento da Prefeitura. E o mesmo
                # que desmarcar "Cabecalhos e rodapes" ao imprimir a mao
                # (22/09/2026). O caminho do navegador do sistema ja saia
                # limpo, por --no-pdf-header-footer.
                display_header_footer=False,
                margin={
                    "top": "10mm",
                    "right": "10mm",
                    "bottom": "10mm",
                    "left": "10mm",
                },
            )
        finally:
            browser.close()


def _candidatos_navegador() -> list[Path]:
    vistos: set[Path] = set()
    candidatos: list[Path] = []

    for nome in ("msedge", "msedge.exe", "chrome", "chrome.exe"):
        if achado := shutil.which(nome):
            caminho = Path(achado)
            if caminho not in vistos:
                vistos.add(caminho)
                candidatos.append(caminho)

    bases = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relativos = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    ]
    for base in bases:
        if not base:
            continue
        for relativo in relativos:
            caminho = Path(base) / relativo
            if caminho.exists() and caminho not in vistos:
                vistos.add(caminho)
                candidatos.append(caminho)
    return candidatos


def _pdf_com_navegador_sistema(html_path: Path, destino: Path) -> None:
    erros: list[str] = []
    perfil = destino.parent / f".{destino.stem}-perfil"
    for navegador in _candidatos_navegador():
        if destino.exists():
            destino.unlink()
        with contextlib.suppress(Exception):
            shutil.rmtree(perfil)
        cmd = [
            str(navegador),
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--user-data-dir={perfil}",
            f"--print-to-pdf={destino}",
            html_path.resolve().as_uri(),
        ]
        try:
            proc = subprocess.run(
                cmd, check=False, capture_output=True, timeout=60)
        except Exception as erro:
            erros.append(f"{navegador}: {type(erro).__name__}: {erro}")
            continue
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(perfil)
        if proc.returncode == 0 and destino.exists() and destino.stat().st_size > 1000:
            return
        saida = (proc.stderr or proc.stdout or b"").decode(
            "utf-8", errors="replace")[:300]
        erros.append(f"{navegador}: rc={proc.returncode} {saida}")
    raise RuntimeError("; ".join(erros) or "navegador headless indisponivel")


def _gerar_pdf_certidao_html(
    resposta: RespostaPortal,
    evidencia: Path,
    destino: Path,
    texto: str,
) -> None:
    renderizavel = evidencia.with_name(f"{evidencia.stem}-render.html")
    renderizavel.write_text(_html_para_impressao(resposta), encoding="utf-8")
    try:
        try:
            _pdf_com_playwright(renderizavel, destino)
            return
        except Exception as erro_playwright:
            log.warning("pdf_goiania_playwright_falhou",
                        extra={"erro": f"{type(erro_playwright).__name__}: "
                                       f"{erro_playwright}"[:300]})
        try:
            _pdf_com_navegador_sistema(renderizavel, destino)
            return
        except Exception as erro_navegador:
            log.warning("pdf_goiania_navegador_falhou",
                        extra={"erro": f"{type(erro_navegador).__name__}: "
                                       f"{erro_navegador}"[:300]})
        destino.write_bytes(_pdf_de_texto(texto))
    finally:
        with contextlib.suppress(FileNotFoundError):
            renderizavel.unlink()


@dataclass
class AdapterGoiania:
    orgao: str
    cfg: Config
    url_consulta: str = URL_CONSULTA
    url_emissao: str = URL_EMISSAO
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
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(f"A Prefeitura de Goiania deste robo consulta "
                                 f"CNPJ, e {doc.documento} e {doc.tipo}."))
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

    def _get(self, url: str) -> RespostaPortal:
        return self._abrir(urllib.request.Request(url, headers=self._headers()))

    def _post(self, dados: dict[str, str], referer: str) -> RespostaPortal:
        corpo = urllib.parse.urlencode(dados).encode("ascii")
        headers = {
            **self._headers(referer),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._abrir(urllib.request.Request(
            self.url_emissao, data=corpo, headers=headers))

    def _consultar(self, doc: Documento) -> ResultadoTentativa:
        inicial = self._get(self.url_consulta)
        if inicial.status >= 400:
            texto = texto_da_resposta(inicial)
            desfecho = classificar_certidao(texto).desfecho
            return ResultadoTentativa(
                desfecho, mensagem_portal=_mensagem(desfecho, texto))

        resposta = self._post({
            "txt_nr_cpfcnpj": doc.documento,
            "sel_cpfcnpj": "2",
            "txt_captcha": "",
        }, self.url_consulta)
        return self._salvar_certidao_html(
            resposta, doc, "Prefeitura de Goiania emitiu HTML")

    def _salvar_certidao_html(
        self,
        resposta: RespostaPortal,
        doc: Documento,
        mensagem: str,
    ) -> ResultadoTentativa:
        texto = texto_da_resposta(resposta)
        resultado = classificar_certidao(texto)
        resultado = replace(resultado, mensagem_portal=_mensagem(
            resultado.desfecho, resultado.mensagem_portal or texto))

        evidencia = self._salvar_html(resposta.corpo, doc)
        if resultado.desfecho not in COM_PDF:
            return replace(resultado, evidencia=evidencia)

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        provisorio = evidencia.with_suffix(".pdf")
        _gerar_pdf_certidao_html(resposta, evidencia, provisorio, texto)
        shutil.move(str(provisorio), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None,
                       mensagem_portal=mensagem)

    def _salvar_html(self, conteudo: bytes, doc: Documento) -> Path:
        caminho = (self.cfg.pasta_evidencias / self.orgao / doc.documento
                   / f"{tempo.agora_iso()[:19].replace(':', '')}-certidao.html")
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(conteudo)
        return caminho


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterGoiania:
    urls = orgao.extras.get("urls", {})
    http_cfg = orgao.extras.get("http", {})
    return AdapterGoiania(
        orgao=orgao.codigo,
        cfg=cfg,
        url_consulta=str(urls.get("consulta", URL_CONSULTA)),
        url_emissao=str(urls.get("emissao", URL_EMISSAO)),
        timeout_s=float(http_cfg.get("timeout_s", 30.0)),
    )
