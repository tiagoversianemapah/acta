"""SEFAZ-GO - Certidao de Debito inscrito em Divida Ativa.

O portal oficial esta em:

    https://www.sefaz.go.gov.br/Certidao/Emissao/001frmEmiteCertidao_c.asp

Mapeado em 27/08/2026. O fluxo e simples o bastante para nao precisar do
robo cego:

    1. abrir a tela de emissao, para ganhar a sessao/cookies;
    2. postar CNPJ como "TipoDocumento = 2", certidao "01" e espolio "N";
    3. se vier PDF, salvar e classificar pelo conteudo;
    4. se vier a tela "Confirma o Nome do Contribuinte", postar de novo com
       "Certidao.ConfirmaNomeContribuinte = Sim";
    5. se vier "Acesso Negado", tratar como bloqueio temporario do portal.

Foi testado tambem com Playwright dirigindo o Edge. Ele passa pelo formulario,
mas a resposta PDF inline vira a pagina do visualizador interno do Edge, e nao
os bytes do PDF. O POST HTTP com sessao/cookies devolve o PDF real diretamente,
sem coordenada de tela, sem janela de impressao e sem calibragem.
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
from datetime import date, datetime, timedelta
from pathlib import Path

from cnd.core import tempo
from cnd.core.modelos import (
    COM_PDF,
    Desfecho,
    Documento,
    ResultadoTentativa,
)
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.sefaz_go")

URL_CONSULTA = "https://www.sefaz.go.gov.br/Certidao/Emissao/001frmEmiteCertidao_c.asp"
URL_CERTIDAO = "https://www.sefaz.go.gov.br/Certidao/Emissao/certidao.asp"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)

RE_CHARSET = re.compile(r"charset=([A-Za-z0-9_-]+)", re.IGNORECASE)
RE_TAG = re.compile(r"<[^>]+>")
RE_SCRIPT = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
RE_VALIDADOR = re.compile(r"VALIDADOR:\s*([0-9.]+)", re.IGNORECASE)
# O titulo da certidao e o unico sinal que nao e prosa. O portal escreve
# "CERTIDAO DE DEBITO INSCRITO EM DIVIDA ATIVA - NEGATIVA" (ou POSITIVA).
RE_TITULO_NEGATIVA = re.compile(r"divida ativa\s*[-–]\s*negativa")
RE_TITULO_POSITIVA = re.compile(
    r"divida ativa\s*[-–]\s*positiva|certidao positiva")
# "consta debito" que NAO seja o final de "nao consta debito".
RE_CONSTA_DEBITO = re.compile(r"(?<!nao )consta debito")
# O DESPACHO e a linha operativa da certidao - o que o Estado esta
# afirmando sobre aquele contribuinte. Conferido num documento real em
# 28/08/2026: entre "DESPACHO (Certidao valida para a matriz e suas
# filiais):" e "FUNDAMENTO LEGAL:" vem so "NAO CONSTA DEBITO".
#
# Classificar por ele, e nao pelo texto inteiro, evita a armadilha do
# rodape: "Fica ressalvado o direito de a Fazenda ... inscrever na divida
# ativa e COBRAR EVENTUAIS DEBITOS QUE VIEREM A SER APURADOS" aparece em
# TODA certidao, inclusive na negativa.
RE_DESPACHO = re.compile(r"despacho[^:]*:\s*(.*?)\s*fundamento legal",
                         re.DOTALL)
RE_NUMERO_CERTIDAO = re.compile(r"NR\.\s*CERTID[AÃA]O:\s*N[ºO]\s*([0-9.]+)",
                                re.IGNORECASE)
RE_VALIDA_ATE = re.compile(r"V[AÁ]LID[AO]\s+AT[EÉ]\s+(\d{2}/\d{2}/\d{4})",
                           re.IGNORECASE)
RE_VALIDA_POR = re.compile(r"VALID[AO]\s+POR\s+(\d+)\s+DIAS", re.IGNORECASE)
RE_DATA_EMISSAO = re.compile(
    r"LOCAL E DATA:\s*[A-ZÃÁÂÇÉÊÍÓÔÕÚ ]+,\s*(\d{1,2})\s+"
    r"([A-ZÃÁÂÇÉÊÍÓÔÕÚ]+)\s+DE\s+(\d{4})",
    re.IGNORECASE,
)

MESES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


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
    # As paginas ASP antigas do portal nao mandam charset no header, mas o
    # conteudo vem em ISO-8859-1. UTF-8 aqui troca "Certidao" por ruido e
    # quebra exatamente os textos que classificam o desfecho.
    return "iso-8859-1"


def texto_da_resposta(resposta: RespostaPortal) -> str:
    bruto = resposta.corpo.decode(_charset(resposta.content_type), errors="replace")
    sem_script = RE_SCRIPT.sub(" ", bruto)
    sem_tags = RE_TAG.sub(" ", sem_script)
    return " ".join(html.unescape(sem_tags).split())


def pede_confirmacao(texto: str) -> bool:
    return "confirma o nome do contribuinte" in _sem_acento(texto)


def classificar_texto(texto: str) -> Desfecho:
    """Classifica respostas SEM PDF: bloqueio, recusa ou tela inesperada.

    Esta funcao NUNCA devolve NEGATIVA nem CPEN, e isso e regra e nao
    esquecimento. As duas sao CONCLUSIVAS e entram no relatorio como
    "certidao em maos" (ver web/relatorio.COM_CERTIDAO) e no contador de
    Negativas da tela. Fechar um item assim sem PDF nenhum diria que a
    certidao existe quando ela nao existe, e o item nunca mais seria
    retentado - o erro silencioso, que so aparece quando o cliente cobra.

    Portal que responde "nao consta debito" mas nao entrega o documento
    nao concluiu o trabalho: vira ERRO_TECNICO, que e RETENTAVEL, e a
    proxima tentativa costuma trazer o PDF.

    POSITIVA continua saindo daqui porque positiva nao tem documento a
    entregar - e o mesmo caminho que o adapter da Receita usa.
    """
    t = _sem_acento(texto)
    if "acesso negado" in t and (
        "politica de seguranca" in t or "requisicao foi bloqueada" in t
    ):
        return Desfecho.BLOQUEIO_TEMPORARIO
    if "cnpj invalido" in t or "cpf invalido" in t:
        return Desfecho.PENDENCIA_MANUAL
    if "nao consta debito" in t:
        # Sem PDF, "nao consta debito" e promessa, nao entrega.
        return Desfecho.ERRO_TECNICO
    if "consta debito" in t:
        return Desfecho.POSITIVA
    return Desfecho.ERRO_TECNICO


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def _extrair_codigo(conteudo: str) -> str | None:
    if achado := RE_VALIDADOR.search(conteudo):
        return achado.group(1).rstrip(".")
    if achado := RE_NUMERO_CERTIDAO.search(conteudo):
        return achado.group(1).rstrip(".")
    return None


def _data_por_extenso(conteudo: str) -> date | None:
    if not (achado := RE_DATA_EMISSAO.search(conteudo)):
        return None
    mes = MESES.get(_sem_acento(achado.group(2)))
    if mes is None:
        return None
    with contextlib.suppress(ValueError):
        return date(int(achado.group(3)), mes, int(achado.group(1)))
    return None


def _extrair_validade(conteudo: str) -> date | None:
    if achado := RE_VALIDA_ATE.search(conteudo):
        with contextlib.suppress(ValueError):
            return datetime.strptime(achado.group(1), "%d/%m/%Y").date()

    prazo = RE_VALIDA_POR.search(conteudo)
    emitida_em = _data_por_extenso(conteudo)
    if prazo and emitida_em:
        return emitida_em + timedelta(days=int(prazo.group(1)))
    return None


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
    """Classifica a certidao pelo PDF, que e a unica resposta entregavel."""
    try:
        conteudo = _texto_pdf(caminho)
    except Exception as erro:
        log.warning("pdf_ilegivel", extra={"arquivo": str(caminho), "erro": str(erro)})
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=f"PDF baixado mas ilegivel: {erro}"[:300],
            evidencia=caminho,
        )

    # Espacos colapsados antes de comparar: o texto extraido do PDF vem com
    # quebra de linha no meio das frases, e uma quebra entre "nao" e
    # "consta debito" nao casaria com nenhum dos marcadores abaixo.
    normalizado = " ".join(_sem_acento(conteudo).split())
    comuns = dict(
        validade=_extrair_validade(conteudo),
        codigo_controle=_extrair_codigo(conteudo),
        mensagem_portal=texto_tela.strip()[:500],
    )

    # A ordem e: DESPACHO, depois titulo, depois o texto inteiro. O
    # DESPACHO e o que o Estado afirma sobre o contribuinte; o resto e
    # cabecalho e rodape, e o rodape fala de divida ativa e de debitos em
    # TODA certidao, inclusive na negativa.
    despacho = RE_DESPACHO.search(normalizado)
    escopo = despacho.group(1) if despacho else normalizado

    if "positiva com efeito" in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)
    if RE_TITULO_NEGATIVA.search(normalizado):
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if RE_TITULO_POSITIVA.search(normalizado):
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    # `(?<!nao )` impede que o "consta debito" de dentro de "nao consta
    # debito" conte como debito - um e substring do outro.
    nega = "nao consta debito" in escopo
    afirma = bool(RE_CONSTA_DEBITO.search(escopo))
    if nega and not afirma:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if afirma and not nega:
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    # Diz as duas coisas, ou nenhuma. Chutar aqui e que seria o erro: melhor
    # nao entregar do que entregar a positiva de alguem como negativa.
    log.warning("pdf_sefaz_go_nao_reconhecido",
                extra={"arquivo": str(caminho), "ambiguo": nega and afirma,
                       "achou_despacho": bool(despacho),
                       "trecho": (escopo or normalizado)[:250]})
    return ResultadoTentativa(
        Desfecho.ERRO_TECNICO,
        mensagem_portal=("PDF da SEFAZ-GO diz negativa E positiva ao mesmo "
                         "tempo - conferir manualmente") if nega and afirma
                        else "PDF da SEFAZ-GO nao reconhecido - conferir manualmente",
        evidencia=caminho,
    )


@dataclass
class AdapterSEFAZGO:
    orgao: str
    cfg: Config
    timeout_s: float = 30.0
    _opener: object | None = field(default=None, repr=False)

    def preparar(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        log.info("sessao_pronta", extra={"orgao": self.orgao})

    def reiniciar_sessao(self) -> None:
        self.preparar()

    def encerrar(self) -> None:
        self._opener = None

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        # O formulario tem CPF e CNPJ (TipoDocumento 1 e 2), mas este
        # adapter so monta o POST de CNPJ. Mandar um CPF nos campos de CNPJ
        # nao daria erro: o portal consultaria OUTRO documento e devolveria
        # uma certidao de alguem, que e pior do que falhar.
        #
        # A ingestao ja recusa CPF para este orgao com recado proprio; isto
        # aqui e a segunda porta, para quando o job chega por outro caminho.
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=f"A SEFAZ-GO deste robo consulta CNPJ, e "
                                f"{doc.documento} e {doc.tipo}.",
            )

        if self._opener is None:
            self.preparar()

        try:
            return self._consultar(doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento, "erro": str(erro)[:200]})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=f"{type(erro).__name__}: {erro}"[:500],
            )

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
        # `raise` e nao `assert`: assert some com `python -O`, e ai o erro
        # viraria um AttributeError sem explicacao la dentro do urllib.
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
        return self._abrir(urllib.request.Request(URL_CERTIDAO, data=corpo, headers=headers))

    def _dados(self, doc: Documento) -> dict[str, str]:
        """Os campos do formulario, TODOS eles.

        Conferido contra o formulario real em 28/08/2026. Montamos o POST a
        mao, entao nao herdamos os `checked` do HTML: campo que nao vai
        explicito simplesmente nao chega ao servidor.

        `Render` e o que decide o FORMATO da resposta - pdf, html ou xml. O
        formulario nasce com pdf marcado, e nos nao mandavamos o campo: a
        resposta podia voltar como pagina, e o adapter so entrega o que vem
        como PDF de verdade.

        `ValidarEmissao_Emitir` separa emitir (0) de validar (1). Sem ele,
        depender do padrao do servidor era apostar em algo que ninguem
        prometeu.
        """
        return {
            "Certidao.Tipo": "01",              # Divida Ativa
            "Certidao.TipoDocumento": "2",      # 1 = CPF, 2 = CNPJ
            "Certidao.NumeroDocumento": doc.documento,
            "Certidao.NumeroDocumentoCNPJ": doc.documento,
            "Certidao.Espolio": "N",
            "Certidao.Render": "pdf",
            "Certidao.ValidarEmissao_Emitir": "0",
        }

    def _consultar(self, doc: Documento) -> ResultadoTentativa:
        inicial = self._get(URL_CONSULTA)
        texto_inicial = texto_da_resposta(inicial)
        desfecho_inicial = classificar_texto(texto_inicial)
        if inicial.status >= 400 or desfecho_inicial == Desfecho.BLOQUEIO_TEMPORARIO:
            return ResultadoTentativa(
                desfecho_inicial,
                mensagem_portal=texto_inicial[:500],
            )

        dados = self._dados(doc)
        resposta = self._post(dados, URL_CONSULTA)
        return self._interpretar(resposta, doc, dados)

    def _interpretar(self, resposta: RespostaPortal, doc: Documento,
                    dados: dict[str, str]) -> ResultadoTentativa:
        if _e_pdf(resposta):
            return self._salvar_pdf(resposta.corpo, doc, "SEFAZ-GO emitiu PDF")

        texto = texto_da_resposta(resposta)
        if pede_confirmacao(texto):
            confirmacao = {
                **dados,
                "Certidao.ConfirmaNomeContribuinte": "Sim",
            }
            confirmada = self._post(confirmacao, URL_CERTIDAO)
            if _e_pdf(confirmada):
                return self._salvar_pdf(
                    confirmada.corpo, doc,
                    "SEFAZ-GO emitiu PDF apos confirmar nome do contribuinte",
                )
            texto = texto_da_resposta(confirmada)

        return ResultadoTentativa(
            classificar_texto(texto),
            mensagem_portal=texto[:500],
        )

    def _salvar_pdf(self, conteudo: bytes, doc: Documento,
                    mensagem: str) -> ResultadoTentativa:
        """Guarda o PDF e o classifica. So o entregavel vai para certidoes.

        O PDF chega antes de sabermos o que ele diz, entao ele nasce em
        `evidencias` e so MUDA para `certidoes` se a leitura disser
        NEGATIVA ou CPEN. Escrever direto em `certidoes` deixava a positiva
        gravada ao lado das entregaveis: o pacote ZIP nao a levaria (ele e
        montado da tabela `certidao`, e positiva nao gera linha), mas quem
        abrisse a pasta para distribuir na mao acharia uma positiva no meio
        das negativas - o erro que ninguem percebe ate o cliente perceber.
        E o mesmo criterio do adapter da Receita, que manda positiva para
        `pasta_evidencias`.
        """
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


def _e_pdf(resposta: RespostaPortal) -> bool:
    return (
        "application/pdf" in resposta.content_type.lower()
        and resposta.corpo.lstrip().startswith(b"%PDF")
    )


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterSEFAZGO:
    ajustes = orgao.extras.get("http", {})
    return AdapterSEFAZGO(
        orgao=orgao.codigo,
        cfg=cfg,
        timeout_s=float(ajustes.get("timeout_s", 30.0)),
    )
