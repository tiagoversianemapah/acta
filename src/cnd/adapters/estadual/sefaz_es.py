"""SEFAZ-ES - Certidao Negativa de Debitos, por adapter cego.

Portal oficial:

    https://s2-internet.sefaz.es.gov.br/certidao/cnd

O formulario e renderizado por AJAX depois do menu "Certidao Negativa de
Debito" e a emissao exige Cloudflare Turnstile. Em Playwright/CDP o portal
nao libera token; no Edge comum, com a tela visivel, o desafio passa
invisivel. Por isso este adapter nao fala com o DOM: abre o Edge como uma
pessoa abriria e usa entrada real do Windows, coordenadas calibradas,
pixels da tela e o PDF salvo pelo visualizador embutido.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import json
import math
import random
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from cnd.adapters.federal.rfb_cego import (
    Calibragem,
    CalibragemAusente,
    JanelaOcupada,
    _achar_edge,
    _edge_rodando,
    _ponto_fracionario,
    _tem_veu_modal,
)
from cnd.core import tempo
from cnd.core.modelos import COM_PDF, Desfecho, Documento, ResultadoTentativa
from cnd.infra import entrada_real, perfil_edge, tela
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.sefaz_es")

URL_CONSULTA = "https://s2-internet.sefaz.es.gov.br/certidao/cnd"
URL_EMISSAO = "https://s2-internet.sefaz.es.gov.br/certidao/emitir-certidao-internet"
URL_CAPSOLVER = "https://api.capsolver.com"
DOMINIO_PORTAL = "s2-internet.sefaz.es.gov.br"
JS_ABRIR_FORMULARIO = "javascript:abreTela('R1')"

EXECUTAVEL_NAVEGADOR = "msedge.exe"
TITULO_JANELA = "Certid"

PONTOS_NECESSARIOS = (
    "menu_cnd",
    "campo_documento",
    "botao_emitir",
    "visor_pdf",
    "fundo_pagina",
    "faixa_alerta",
)

ERRO_GENERICO = "Erro na consulta. Ver o Registro para o detalhe."
ERRO_CAPTCHA = "O portal exigiu verificacao de seguranca."
ERRO_SALVAR_PDF = "O portal abriu a certidao, mas o robo nao conseguiu salvar o PDF."
ERRO_BLOQUEIO = "O portal recusou a consulta."

BRILHO_DO_CAMPO = 200
TEMPO_JANELA_ABRIR_S = 25.0
TEMPO_EDGE_MORRER_S = 15.0
TEMPO_PAGINA_INICIAL_S = 45.0
TEMPO_FORMULARIO_S = 20.0
TEMPO_TURNSTILE_S = 5.0
TEMPO_REACAO_S = 60.0
TEMPO_SALVAR_PDF_S = 25.0
TENTATIVAS_TURNSTILE = 4
INTERVALO_REACAO_S = 0.20
TEMPO_PRIMEIRA_LEITURA_TEXTO_S = 2.5
INTERVALO_LEITURA_TEXTO_S = 1.5
LARGURA_MINIMA_JANELA_VISIVEL = 800
ALTURA_MINIMA_JANELA_VISIVEL = 500
COORDENADA_JANELA_MINIMIZADA = -10000
DISTANCIA_MAXIMA_CURSOR_ALVO = 40.0

PONTOS_VEU_MODAL = (
    (0.16, 0.42),
    (0.84, 0.42),
    (0.16, 0.68),
    (0.84, 0.68),
    (0.50, 0.82),
)
MINIMO_PONTOS_VEU_MODAL = 2

PONTOS_FAIXA_ALERTA = (
    (0.25, 0.20),
    (0.50, 0.20),
    (0.25, 0.23),
    (0.50, 0.23),
    (0.25, 0.26),
    (0.50, 0.26),
)
PONTOS_SCAN_FAIXA_ALERTA = tuple(
    (fx, fy)
    for fy in (0.16, 0.20, 0.24, 0.28)
    for fx in (0.12, 0.22, 0.34, 0.46, 0.58, 0.70, 0.82)
)
MINIMO_PONTOS_FAIXA_ALERTA = 3
OFFSET_X_ICONE_MENU_CND = 48
TEMPO_MENU_CND_FALLBACK_S = 6.0
TENTATIVAS_ABRIR_FORMULARIO = 2
RAIO_X_SCAN_BOTAO = 0.18
Y_SCAN_BOTAO_DEPOIS_DO_CAMPO = (0.03, 0.25)
PASSO_SCAN_BOTAO = 8
MINIMO_PONTOS_BOTAO = 8

VK_S = 0x53

RE_VALIDADE = re.compile(
    r"(?:valid[ao]\s+ate|validade)[:\s]*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
RE_VALIDA_POR = re.compile(r"valid[ao]\s+por\s+(\d+)\s+dias", re.IGNORECASE)
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
RE_CODIGO = re.compile(
    r"(?:codigo\s+de\s+controle|numero\s+da\s+certidao|certidao\s+n[ro.]*|"
    r"n[ro.]*\s+certidao)\s*[:.-]\s*([0-9A-Z./-]{6,})",
    re.IGNORECASE,
)

FRASES_RESPOSTA_TELA = (
    "complete a verificacao",
    "verificacao de seguranca invalida",
    "cnpj invalido",
    "cpf/cnpj informado esta incompleto",
    "cnpj informado esta incompleto",
    "nao foi possivel emitir certidao negativa",
    "possui debito",
    "constam debitos",
    "consta debito",
    "ocorreu um erro ao processar",
    "request header or cookie too large",
    "400 bad request",
)
FRASES_PAGINA_INICIAL = (
    "portal de sistemas",
    "certidao negativa de debito",
    "validacao de certidoes",
    "sefaz/es",
)


def _sem_acento(texto: str | None) -> str:
    sem = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sem if not unicodedata.combining(c)).lower()


def _texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((pagina.extract_text() or "")
                     for pagina in PdfReader(str(caminho)).pages)


def _data_curta(texto: str) -> date | None:
    with contextlib.suppress(ValueError):
        return datetime.strptime(texto, "%d/%m/%Y").date()
    return None


def _extrair_validade(conteudo: str) -> date | None:
    if achado := RE_VALIDADE.search(conteudo):
        return _data_curta(achado.group(1))

    prazo = RE_VALIDA_POR.search(conteudo)
    datas = [_data_curta(d.group(1)) for d in RE_DATA.finditer(conteudo)]
    datas = [d for d in datas if d is not None]
    if prazo and datas:
        return min(datas) + timedelta(days=int(prazo.group(1)))
    if len(datas) >= 2:
        return max(datas)
    return None


def _extrair_codigo(conteudo: str) -> str | None:
    if achado := RE_CODIGO.search(conteudo):
        return achado.group(1).strip(" .-/")
    return None


def _parece_botao(cor: tuple[int, int, int]) -> bool:
    r, g, b = cor
    return b > 150 and g > 120 and b > r + 40 and g > r + 25


def _texto_tem_resposta(texto: str) -> bool:
    t = _sem_acento(texto)
    return any(frase in t for frase in FRASES_RESPOSTA_TELA)


def _texto_indica_formulario(texto: str) -> bool:
    t = _sem_acento(texto)
    tem_campo = "cpf / cnpj" in t or "cpf/cnpj" in t
    tem_acao = "emitir certidao" in t or "digite o cpf" in t
    return tem_campo and tem_acao


def _turnstile_ainda_processando(texto: str) -> bool:
    return "complete a verificacao" in _sem_acento(texto)


def _mensagem_curta(texto: str) -> str:
    return " ".join((texto or "").split())[:500]


def classificar_texto(texto: str) -> Desfecho:
    """Classifica mensagens sem PDF.

    NEGATIVA e CPEN nao saem daqui: sem PDF nao ha certidao entregavel.
    """
    t = _sem_acento(texto)
    if "verificacao de seguranca invalida" in t or "complete a verificacao" in t:
        return Desfecho.CAPTCHA
    if "request header or cookie too large" in t or "400 bad request" in t:
        return Desfecho.ERRO_TECNICO
    if "cnpj invalido" in t or "cpf/cnpj informado esta incompleto" in t:
        return Desfecho.PENDENCIA_MANUAL
    if "nao foi possivel emitir certidao negativa" in t:
        return Desfecho.POSITIVA
    if "possui debito" in t or "constam debitos" in t or "consta debito" in t:
        return Desfecho.POSITIVA
    return Desfecho.ERRO_TECNICO


def _mensagem(desfecho: Desfecho, texto: str) -> str:
    if desfecho == Desfecho.CAPTCHA:
        return ERRO_CAPTCHA
    if desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
        return ERRO_BLOQUEIO
    if desfecho == Desfecho.ERRO_TECNICO:
        return ERRO_GENERICO
    return _mensagem_curta(texto)


def ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
    try:
        conteudo = _texto_pdf(caminho)
    except Exception as erro:
        log.warning("pdf_ilegivel", extra={"arquivo": str(caminho),
                                          "erro": str(erro)[:300]})
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=ERRO_GENERICO,
            evidencia=caminho,
        )

    normalizado = " ".join(_sem_acento(conteudo).split())
    comuns = dict(
        validade=_extrair_validade(conteudo),
        codigo_controle=_extrair_codigo(conteudo),
        mensagem_portal=texto_tela.strip()[:500],
    )

    if "positiva com efeito" in normalizado or "efeito de negativa" in normalizado:
        return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)
    if "certidao negativa" in normalizado or "nao consta debito" in normalizado:
        return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)
    if "certidao positiva" in normalizado or "consta debito" in normalizado:
        return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

    log.warning("pdf_sefaz_es_nao_reconhecido",
                extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
    return ResultadoTentativa(
        Desfecho.ERRO_TECNICO,
        mensagem_portal=ERRO_GENERICO,
        evidencia=caminho,
    )


def _pdf_da_resposta(dados: dict) -> bytes | None:
    """Extrai o PDF do JSON do portal.

    Mantida porque o mapeamento HTTP continua sendo util em testes e sondas,
    embora o adapter de producao nao leia a resposta AJAX por CDP.
    """
    if not dados.get("success"):
        return None
    bruto = ((dados.get("data") or {}).get("blbCertidao") or "").strip()
    if bruto.startswith("data:application/pdf;base64,"):
        bruto = bruto.split(",", 1)[1].strip()
    if not bruto:
        return None
    try:
        pdf = base64.b64decode(bruto, validate=True)
    except (binascii.Error, ValueError):
        return None
    return pdf if pdf.lstrip().startswith(b"%PDF") else None


def _mensagem_da_resposta(dados: dict) -> str:
    return str(dados.get("message") or dados.get("msg") or "").strip()


@dataclass(frozen=True)
class ConfigCaptcha:
    """Compatibilidade com config antigo.

    O adapter cego nao chama provider de captcha. O bloco antigo permanece
    aceitavel no TOML para nao quebrar instalacoes ja editadas.
    """

    provider: str = ""
    api_key_env: str = ""
    permitir_somente_host: str = ""
    timeout_s: float = 120.0
    poll_s: float = 2.0

    @classmethod
    def de_config(cls, bruto: dict | None) -> ConfigCaptcha:
        bruto = bruto or {}
        return cls(
            provider=str(bruto.get("provider", "")).strip().lower(),
            api_key_env=str(bruto.get("api_key_env", "")).strip(),
            permitir_somente_host=str(
                bruto.get("permitir_somente_host", "")
            ).strip().lower(),
            timeout_s=float(bruto.get("timeout_s", 120.0)),
            poll_s=float(bruto.get("poll_s", 2.0)),
        )


class CaptchaNaoResolvido(RuntimeError):
    """Provider configurado, mas nao devolveu token aproveitavel."""


class CapSolver:
    """Cliente legado, sem uso no fluxo cego."""

    def __init__(self, api_key: str, timeout_s: float = 120.0,
                 poll_s: float = 2.0) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def resolver_turnstile(self, website_url: str, website_key: str) -> str:
        criado = self._post("/createTask", {
            "clientKey": self.api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
            },
        })
        if criado.get("errorId"):
            raise CaptchaNaoResolvido(str(criado.get("errorDescription") or criado))
        task_id = criado.get("taskId")
        if not task_id:
            raise CaptchaNaoResolvido("CapSolver nao devolveu taskId")

        limite = time.monotonic() + self.timeout_s
        while time.monotonic() < limite:
            time.sleep(max(0.5, self.poll_s))
            resultado = self._post("/getTaskResult", {
                "clientKey": self.api_key,
                "taskId": task_id,
            })
            if resultado.get("errorId"):
                raise CaptchaNaoResolvido(
                    str(resultado.get("errorDescription") or resultado)
                )
            if resultado.get("status") == "ready":
                token = (resultado.get("solution") or {}).get("token")
                if token:
                    return str(token)
                raise CaptchaNaoResolvido("CapSolver retornou ready sem token")

        raise CaptchaNaoResolvido("CapSolver nao resolveu dentro do prazo")

    def _post(self, rota: str, dados: dict) -> dict:
        corpo = json.dumps(dados).encode("utf-8")
        req = urllib.request.Request(
            URL_CAPSOLVER + rota,
            data=corpo,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resposta:
            return json.loads(resposta.read().decode("utf-8"))


@dataclass
class AdapterSEFAZES:
    orgao: str
    cfg: Config
    caminho_calibragem: Path = Path("data/calibragem/sefaz_es.json")
    url_consulta: str = URL_CONSULTA
    url_emissao: str = URL_EMISSAO
    tempo_pagina_inicial_s: float = TEMPO_PAGINA_INICIAL_S
    tempo_formulario_s: float = TEMPO_FORMULARIO_S
    espera_turnstile_s: float = TEMPO_TURNSTILE_S
    tempo_reacao_s: float = TEMPO_REACAO_S
    tempo_salvar_pdf_s: float = TEMPO_SALVAR_PDF_S
    tentativas_turnstile: int = TENTATIVAS_TURNSTILE
    captcha: ConfigCaptcha = field(default_factory=ConfigCaptcha)
    _calibragem: Calibragem | None = field(default=None, repr=False)
    _ultimo_texto_portal: str | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def preparar(self) -> None:
        self._calibragem = Calibragem.carregar(self.caminho_calibragem)
        self._calibragem.conferir(pontos_necessarios=PONTOS_NECESSARIOS)
        self._abrir_navegador()
        self._calibragem.conferir(
            self._janela(), pontos_necessarios=PONTOS_NECESSARIOS
        )

    def reiniciar_sessao(self) -> None:
        log.info("reiniciando_sessao", extra={"orgao": self.orgao})
        self._abrir_navegador()

    def encerrar(self) -> None:
        self._matar_edge()

    def _abrir_navegador(self) -> None:
        self.encerrar()
        subprocess.Popen([_achar_edge(), "--new-window", "--start-maximized",
                          self.url_consulta])
        if not self._esperar_janela():
            log.warning("janela_do_edge_nao_apareceu",
                        extra={"orgao": self.orgao,
                               "esperou_s": TEMPO_JANELA_ABRIR_S})
        self._posicionar_janela_calibrada()
        entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        log.info("navegador_aberto_sem_automacao",
                 extra={"orgao": self.orgao, "janela": self._janela()})

    def _esperar_janela(self, segundos: float = TEMPO_JANELA_ABRIR_S) -> bool:
        largura_minima = self._calibragem.janela[2] * 0.6 if self._calibragem else 0
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            caixa = entrada_real.retangulo_janela(TITULO_JANELA,
                                                  EXECUTAVEL_NAVEGADOR)
            if caixa is not None and caixa[2] >= largura_minima:
                time.sleep(0.6)
                return True
            time.sleep(0.3)
        return False

    def _posicionar_janela_calibrada(self) -> None:
        if self._calibragem is None:
            return
        if not entrada_real.posicionar_janela(
                TITULO_JANELA, EXECUTAVEL_NAVEGADOR, self._calibragem.janela):
            log.warning("janela_do_edge_nao_posicionada",
                        extra={"calibrada": self._calibragem.janela})

    @staticmethod
    def _janela_desalinhada(atual: tuple[int, int, int, int],
                            esperada: tuple[int, int, int, int]) -> bool:
        ax, ay, aw, ah = atual
        ex, ey, ew, eh = esperada
        centro_atual = (ax + aw / 2, ay + ah / 2)
        centro_esperado = (ex + ew / 2, ey + eh / 2)
        return (
            abs(aw - ew) > 40
            or abs(ah - eh) > 80
            or abs(centro_atual[0] - centro_esperado[0]) > 120
            or abs(centro_atual[1] - centro_esperado[1]) > 120
        )

    @staticmethod
    def _janela_invisivel(atual: tuple[int, int, int, int]) -> bool:
        x, y, largura, altura = atual
        return (
            x < COORDENADA_JANELA_MINIMIZADA
            or y < COORDENADA_JANELA_MINIMIZADA
            or largura < LARGURA_MINIMA_JANELA_VISIVEL
            or altura < ALTURA_MINIMA_JANELA_VISIVEL
        )

    def _garantir_janela_visivel(self) -> None:
        atual = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        esperada = self._calibragem.janela if self._calibragem else None
        precisa_corrigir = (
            atual is not None
            and (self._janela_invisivel(atual)
                 or (esperada is not None and self._janela_desalinhada(atual, esperada)))
        )
        if not precisa_corrigir:
            return

        log.warning("janela_fora_do_tamanho_remaximizando",
                    extra={"agora": atual, "calibrada": esperada})
        self._posicionar_janela_calibrada()
        entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        time.sleep(1.0)
        entrada_real.garantir_em_primeiro_plano(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)

    def _exigir_foco(self) -> None:
        if (not entrada_real.em_primeiro_plano(TITULO_JANELA,
                                               EXECUTAVEL_NAVEGADOR)
                and not entrada_real.garantir_em_primeiro_plano(
                    TITULO_JANELA, EXECUTAVEL_NAVEGADOR)):
            raise JanelaOcupada(
                "o navegador perdeu o foco no meio da consulta; "
                "alguem mexeu no computador"
            )
        self._garantir_janela_visivel()

    def _janela(self) -> tuple[int, int, int, int]:
        caixa = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        if caixa is None:
            raise RuntimeError("janela do Edge nao encontrada")
        return caixa

    def _ponto(self, nome: str) -> tuple[int, int]:
        if self._calibragem is None:
            raise CalibragemAusente(
                "Falta preparar o adapter cego: carregue a calibragem antes."
            )
        return self._calibragem.ponto(nome, self._janela())

    def _matar_edge(self) -> bool:
        entrada_real.fechar_janelas(EXECUTAVEL_NAVEGADOR)
        subprocess.run(["taskkill", "/F", "/T", "/IM", EXECUTAVEL_NAVEGADOR],
                       capture_output=True, check=False)

        morreu = False
        limite = time.monotonic() + TEMPO_EDGE_MORRER_S
        while time.monotonic() < limite:
            if not _edge_rodando():
                morreu = True
                break
            time.sleep(0.3)

        if not morreu:
            log.warning("edge_nao_encerrou",
                        extra={"orgao": self.orgao,
                               "esperou_s": TEMPO_EDGE_MORRER_S})
        with contextlib.suppress(Exception):
            perfil_edge.marcar_saida_limpa()
        return morreu

    # ------------------------------------------------------------------
    # Fluxo
    # ------------------------------------------------------------------
    def emitir(self, doc: Documento) -> ResultadoTentativa:
        if doc.tipo != "CNPJ":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=f"A SEFAZ-ES deste robo consulta CNPJ, e "
                                f"{doc.documento} e {doc.tipo}.",
            )
        if self._calibragem is None:
            self.preparar()

        self._ultimo_texto_portal = None
        try:
            reacao, caminho_pdf = self._submeter(doc)
            if caminho_pdf is not None:
                return self._classificar_pdf_salvo(
                    caminho_pdf, doc, "SEFAZ-ES emitiu PDF (adapter cego)"
                )
            return self._diagnosticar_falha(doc, reacao)
        except Exception as erro:
            log.exception("falha_no_fluxo_cego", extra={"documento": doc.documento})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=f"{type(erro).__name__}: {erro}"[:400],
                evidencia=self._print(doc, "erro"),
            )

    def _submeter(self, doc: Documento) -> tuple[str, Path | None]:
        self._ultimo_texto_portal = None
        self._focar()
        entrada_real.ir_para_url(self.url_consulta)
        if not self._abrir_formulario():
            return "nada", None

        time.sleep(max(0.0, self.espera_turnstile_s))

        self._exigir_foco()
        self._clicar("campo_documento", self._ponto("campo_documento"))
        time.sleep(random.uniform(0.2, 0.5))
        entrada_real.limpar_campo()
        entrada_real.digitar(doc.documento)
        time.sleep(random.uniform(0.5, 1.2))

        for tentativa in range(1, max(1, self.tentativas_turnstile) + 1):
            self._exigir_foco()
            self._clicar("botao_emitir", self._ponto_botao_emitir())
            reacao, caminho = self._aguardar_reacao(doc, self.tempo_reacao_s)
            if reacao != "turnstile":
                return reacao, caminho

            log.info("turnstile_ainda_processando",
                     extra={"orgao": self.orgao, "tentativa": tentativa,
                            "limite": self.tentativas_turnstile})
            self._fechar_aviso()
            time.sleep(max(1.0, self.espera_turnstile_s))

        return "turnstile", None

    def _abrir_formulario(self) -> bool:
        for tentativa_site in range(1, TENTATIVAS_ABRIR_FORMULARIO + 1):
            if self._esperar_formulario(segundos=3.0):
                return True

            if not self._esperar_pagina_inicial():
                if tentativa_site < TENTATIVAS_ABRIR_FORMULARIO:
                    self._reiniciar_site("pagina_inicial_nao_carregou", tentativa_site)
                    continue
                return False

            if self._esperar_formulario(segundos=0.5):
                return True

            if self._abrir_formulario_por_javascript():
                return True

            if self._clicar_menu_cnd_e_esperar_formulario():
                return True

            if tentativa_site < TENTATIVAS_ABRIR_FORMULARIO:
                self._reiniciar_site("menu_cnd_nao_abriu_formulario", tentativa_site)

        return False

    def _abrir_formulario_por_javascript(self) -> bool:
        self._exigir_foco()
        log.info("abrindo_formulario_por_javascript",
                 extra={"orgao": self.orgao})
        entrada_real.ir_para_url(JS_ABRIR_FORMULARIO)
        return self._esperar_formulario(
            segundos=min(self.tempo_formulario_s, TEMPO_MENU_CND_FALLBACK_S)
        )

    def _clicar_menu_cnd_e_esperar_formulario(self) -> bool:
        pontos_menu = self._pontos_menu_cnd()
        for indice, ponto in enumerate(pontos_menu, start=1):
            self._exigir_foco()
            log.info("clicando_menu_cnd",
                     extra={"orgao": self.orgao, "tentativa": indice,
                            "ponto": ponto})
            self._clicar("menu_cnd", ponto)
            espera = (
                self.tempo_formulario_s
                if indice == len(pontos_menu)
                else min(TEMPO_MENU_CND_FALLBACK_S, self.tempo_formulario_s)
            )
            if self._esperar_formulario(segundos=espera):
                return True
        return False

    def _reiniciar_site(self, motivo: str, tentativa: int) -> None:
        self._exigir_foco()
        log.warning("reiniciando_site_sefaz_es",
                    extra={"orgao": self.orgao, "motivo": motivo,
                           "tentativa": tentativa,
                           "limite": TENTATIVAS_ABRIR_FORMULARIO})
        entrada_real.ir_para_url(self.url_consulta)
        time.sleep(random.uniform(1.0, 2.0))

    def _pontos_menu_cnd(self) -> list[tuple[int, int]]:
        texto = self._ponto("menu_cnd")
        janela = self._janela()
        icone = (janela[0] + OFFSET_X_ICONE_MENU_CND, texto[1])
        pontos: list[tuple[int, int]] = []
        for ponto in (icone, texto):
            if ponto not in pontos:
                pontos.append(ponto)
        return pontos

    def _clicar(self, alvo: str, ponto: tuple[int, int]) -> None:
        self._exigir_foco()
        antes = entrada_real.posicao()
        log.info("clique_fisico",
                 extra={"orgao": self.orgao, "alvo": alvo,
                        "ponto": ponto, "cursor_antes": antes})
        entrada_real.clicar(*ponto)
        depois = entrada_real.posicao()
        distancia = math.hypot(depois[0] - ponto[0], depois[1] - ponto[1])
        if distancia > DISTANCIA_MAXIMA_CURSOR_ALVO:
            log.warning("cursor_nao_chegou_ao_alvo",
                        extra={"orgao": self.orgao, "alvo": alvo,
                               "ponto": ponto, "cursor_depois": depois,
                               "distancia": round(distancia, 1)})

    def _esperar_pagina_inicial(self) -> bool:
        inicio = time.monotonic()
        limite = inicio + self.tempo_pagina_inicial_s
        proxima_leitura_texto = inicio
        ultimo_texto = ""

        while time.monotonic() < limite:
            imagem = tela.capturar()
            visivel, _campo, _botao = self._formulario_visivel(imagem)
            if visivel:
                return True

            if time.monotonic() >= proxima_leitura_texto:
                texto = self._texto_da_pagina()
                ultimo_texto = _mensagem_curta(texto)
                normalizado = _sem_acento(texto)
                if any(frase in normalizado for frase in FRASES_PAGINA_INICIAL):
                    log.info("pagina_inicial_pronta",
                             extra={"orgao": self.orgao,
                                    "em_s": round(time.monotonic() - inicio, 2)})
                    time.sleep(random.uniform(0.2, 0.6))
                    return True
                proxima_leitura_texto = time.monotonic() + 1.5

            time.sleep(0.2)

        log.warning("pagina_inicial_nao_apareceu",
                    extra={"orgao": self.orgao,
                           "esperou_s": self.tempo_pagina_inicial_s,
                           "texto": ultimo_texto[:300]})
        return False

    def _focar(self) -> None:
        if entrada_real.achar_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR) is None:
            log.warning("janela_do_edge_nao_encontrada_reabrindo")
            self._abrir_navegador()

        if not entrada_real.garantir_em_primeiro_plano(TITULO_JANELA,
                                                       EXECUTAVEL_NAVEGADOR):
            raise JanelaOcupada(
                "nao consegui trazer o navegador para a frente; alguem esta "
                "usando o computador. O robo cego precisa da tela so para ele."
            )

        self._garantir_janela_visivel()

    def _esperar_formulario(self, segundos: float) -> bool:
        inicio = time.monotonic()
        limite = inicio + segundos
        proxima_leitura_texto = inicio
        ultimo_texto = ""
        campo = botao = None
        while time.monotonic() < limite:
            agora = time.monotonic()
            imagem = tela.capturar()
            visivel, campo, botao = self._formulario_visivel(imagem)
            if visivel:
                log.info("formulario_pronto",
                         extra={"orgao": self.orgao,
                                "em_s": round(agora - inicio, 2)})
                time.sleep(random.uniform(0.15, 0.45))
                return True

            if agora >= proxima_leitura_texto:
                texto = self._texto_da_pagina()
                ultimo_texto = _mensagem_curta(texto)
                if _texto_indica_formulario(texto):
                    log.info("formulario_pronto_por_texto",
                             extra={"orgao": self.orgao,
                                    "em_s": round(time.monotonic() - inicio, 2)})
                    time.sleep(random.uniform(0.15, 0.45))
                    return True
                proxima_leitura_texto = agora + 1.0

            time.sleep(0.1)

        log.warning("formulario_nao_apareceu",
                    extra={"orgao": self.orgao, "esperou_s": segundos,
                           "campo": campo, "brilho_do_campo": (
                               round(tela.brilho(campo), 1) if campo else None),
                           "botao": botao,
                           "botao_passou": _parece_botao(botao) if botao
                           else None,
                           "texto": ultimo_texto[:300]})
        return False

    def _formulario_visivel(
        self, imagem
    ) -> tuple[bool, tuple[int, int, int], tuple[int, int, int]]:
        campo = tela.cor_media(imagem, *self._ponto("campo_documento"), raio=5)
        botao = tela.cor_media(imagem, *self._ponto("botao_emitir"), raio=5)
        return (
            tela.brilho(campo) > BRILHO_DO_CAMPO and _parece_botao(botao),
            campo,
            botao,
        )

    def _ponto_botao_emitir(self) -> tuple[int, int]:
        imagem = tela.capturar()
        return self._localizar_botao_emitir(imagem) or self._ponto("botao_emitir")

    def _localizar_botao_emitir(self, imagem) -> tuple[int, int] | None:
        """Acha o botao azul real, porque o Turnstile muda a altura do formulario."""
        janela = self._janela()
        _, _, largura, altura = janela
        campo_x, campo_y = self._ponto("campo_documento")
        x0 = max(janela[0], round(campo_x - largura * RAIO_X_SCAN_BOTAO))
        x1 = min(janela[0] + largura, round(campo_x + largura * RAIO_X_SCAN_BOTAO))
        y0 = max(janela[1], round(campo_y + altura * Y_SCAN_BOTAO_DEPOIS_DO_CAMPO[0]))
        y1 = min(janela[1] + altura,
                 round(campo_y + altura * Y_SCAN_BOTAO_DEPOIS_DO_CAMPO[1]))

        xs = []
        ys = []
        for y in range(y0, y1, PASSO_SCAN_BOTAO):
            for x in range(x0, x1, PASSO_SCAN_BOTAO):
                if _parece_botao(tela.cor_em(imagem, x, y)):
                    xs.append(x)
                    ys.append(y)

        if len(xs) < MINIMO_PONTOS_BOTAO:
            return None

        centro = (round((min(xs) + max(xs)) / 2), round((min(ys) + max(ys)) / 2))
        log.info("botao_emitir_localizado_por_cor",
                 extra={"orgao": self.orgao, "pontos": len(xs),
                        "centro": centro})
        return centro

    def _aguardar_reacao(
        self, doc: Documento, segundos: float
    ) -> tuple[str, Path | None]:
        inicio = time.monotonic()
        limite = inicio + segundos
        proximo_foco = 0.0
        proxima_leitura_texto = inicio + TEMPO_PRIMEIRA_LEITURA_TEXTO_S
        tentou_salvar_modal = False

        while time.monotonic() < limite:
            agora = time.monotonic()

            if agora >= proximo_foco:
                self._exigir_foco()
                proximo_foco = agora + 3.0

            imagem = tela.capturar()
            if self._tem_modal(imagem):
                if not tentou_salvar_modal:
                    tentou_salvar_modal = True
                    caminho = self._salvar_pdf_do_modal(doc)
                    if caminho is not None:
                        return "pdf", caminho

                texto = self._texto_da_pagina()
                if _turnstile_ainda_processando(texto):
                    self._ultimo_texto_portal = texto
                    return "turnstile", None
                if _texto_tem_resposta(texto):
                    self._ultimo_texto_portal = texto
                    return "texto", None
                return "modal", None

            tipo, cor_faixa = self._alerta_na_imagem(imagem)
            if tipo:
                log.warning("faixa_de_alerta_detectada",
                            extra={"orgao": self.orgao, "cor": cor_faixa,
                                   "tipo": tipo,
                                   "em_s": round(agora - inicio, 1)})
                return "bloqueio", None

            if agora >= proxima_leitura_texto:
                texto = self._texto_da_pagina()
                if _turnstile_ainda_processando(texto):
                    self._ultimo_texto_portal = texto
                    return "turnstile", None
                if _texto_tem_resposta(texto):
                    self._ultimo_texto_portal = texto
                    return "texto", None
                proxima_leitura_texto = agora + INTERVALO_LEITURA_TEXTO_S

            time.sleep(INTERVALO_REACAO_S)

        log.warning("sem_reacao_do_portal",
                    extra={"orgao": self.orgao, "esperou_s": segundos})
        return "nada", None

    def _tem_modal(self, imagem) -> bool:
        cores = [tela.cor_media(imagem, x, y, raio=10)
                 for x, y in self._pontos_do_modal()]
        return _tem_veu_modal(cores, self._calibragem.cor_fundo)

    def _pontos_do_modal(self) -> list[tuple[int, int]]:
        janela = self._janela()
        pontos = [self._ponto("fundo_pagina")]
        pontos.extend(
            _ponto_fracionario(janela, fx, fy) for fx, fy in PONTOS_VEU_MODAL
        )
        return pontos

    def _pontos_da_faixa_alerta(self) -> list[tuple[int, int]]:
        janela = self._janela()
        pontos = [self._ponto("faixa_alerta")]
        pontos.extend(
            _ponto_fracionario(janela, fx, fy) for fx, fy in PONTOS_FAIXA_ALERTA
        )
        return pontos

    def _alerta_na_imagem(self, imagem) -> tuple[str | None,
                                                 tuple[int, int, int] | None]:
        for cor_lida in (
            tela.cor_media(imagem, x, y, raio=10)
            for x, y in self._pontos_da_faixa_alerta()
        ):
            tipo = tela.parece_alerta(cor_lida)
            if tipo:
                return tipo, cor_lida

        contagem: dict[str, int] = {}
        primeira_cor: dict[str, tuple[int, int, int]] = {}
        janela = self._janela()
        for fx, fy in PONTOS_SCAN_FAIXA_ALERTA:
            x, y = _ponto_fracionario(janela, fx, fy)
            cor_lida = tela.cor_media(imagem, x, y, raio=10)
            tipo = tela.parece_alerta(cor_lida)
            if not tipo:
                continue
            contagem[tipo] = contagem.get(tipo, 0) + 1
            primeira_cor.setdefault(tipo, cor_lida)
            if contagem[tipo] >= MINIMO_PONTOS_FAIXA_ALERTA:
                return tipo, primeira_cor[tipo]
        return None, None

    def _texto_da_pagina(self) -> str:
        try:
            self._exigir_foco()
            entrada_real.limpar_area_transferencia()
            entrada_real.atalho(entrada_real.VK_CONTROL, entrada_real.VK_A)
            time.sleep(0.05)
            entrada_real.atalho(entrada_real.VK_CONTROL, entrada_real.VK_C)
            time.sleep(0.15)
            return entrada_real.texto_area_transferencia()
        except Exception as erro:
            log.warning("falha_ao_ler_texto_da_pagina",
                        extra={"erro": str(erro)})
            return ""
        finally:
            self._limpar_selecao_da_pagina()

    def _limpar_selecao_da_pagina(self) -> None:
        with contextlib.suppress(Exception):
            entrada_real.tecla(entrada_real.VK_ESCAPE)
            time.sleep(0.03)
            entrada_real.tecla(entrada_real.VK_RIGHT)

    def _fechar_aviso(self) -> None:
        with contextlib.suppress(Exception):
            entrada_real.tecla(entrada_real.VK_ESCAPE)
            time.sleep(0.2)

    def _salvar_pdf_do_modal(self, doc: Documento) -> Path | None:
        destino = self._novo_pdf_evidencia(doc)
        destino.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            destino.unlink()

        try:
            self._exigir_foco()
            self._clicar("visor_pdf", self._ponto("visor_pdf"))
            time.sleep(0.35)
            entrada_real.atalho(entrada_real.VK_CONTROL, VK_S)
            time.sleep(1.0)
            entrada_real.digitar(str(destino), minimo=0.003, maximo=0.012)
            time.sleep(0.2)
            entrada_real.tecla(entrada_real.VK_RETURN)
        except Exception as erro:
            log.warning("falha_ao_disparar_salvar_pdf",
                        extra={"orgao": self.orgao, "erro": str(erro)[:300]})
            return None

        if self._esperar_arquivo_pdf(destino):
            log.info("pdf_salvo_do_modal",
                     extra={"orgao": self.orgao, "arquivo": str(destino)})
            return destino

        log.warning("pdf_do_modal_nao_salvo",
                    extra={"orgao": self.orgao, "arquivo": str(destino)})
        return None

    def _novo_pdf_evidencia(self, doc: Documento) -> Path:
        marca = tempo.agora_iso()[:23].replace(":", "").replace(".", "")
        return self.cfg.pasta_evidencias / self.orgao / doc.documento / (
            f"{marca}-certidao.pdf"
        )

    def _esperar_arquivo_pdf(self, caminho: Path) -> bool:
        limite = time.monotonic() + self.tempo_salvar_pdf_s
        tamanho_anterior = -1
        estavel_desde = 0.0
        while time.monotonic() < limite:
            try:
                tamanho = caminho.stat().st_size
            except OSError:
                time.sleep(0.25)
                continue

            if tamanho <= 0:
                time.sleep(0.25)
                continue

            if tamanho != tamanho_anterior:
                tamanho_anterior = tamanho
                estavel_desde = time.monotonic()
                time.sleep(0.25)
                continue

            if time.monotonic() - estavel_desde < 0.4:
                time.sleep(0.15)
                continue

            with contextlib.suppress(OSError), caminho.open("rb") as arquivo:
                return arquivo.read(5).lstrip().startswith(b"%PDF")
            return False
        return False

    def _diagnosticar_falha(self, doc: Documento, reacao: str) -> ResultadoTentativa:
        evidencia = self._print(doc, "sem-pdf")
        texto = self._ultimo_texto_portal or self._texto_da_pagina()

        if _texto_tem_resposta(texto):
            desfecho = classificar_texto(texto)
            return ResultadoTentativa(
                desfecho,
                mensagem_portal=_mensagem(desfecho, texto),
                evidencia=evidencia,
            )

        if reacao == "bloqueio":
            return ResultadoTentativa(
                Desfecho.BLOQUEIO_TEMPORARIO,
                mensagem_portal=ERRO_BLOQUEIO,
                evidencia=evidencia,
            )

        if reacao == "modal":
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=ERRO_SALVAR_PDF,
                evidencia=evidencia,
            )

        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=ERRO_GENERICO,
            evidencia=evidencia,
        )

    def _classificar_pdf_salvo(
        self, caminho: Path, doc: Documento, mensagem: str
    ) -> ResultadoTentativa:
        resultado = ler_pdf(caminho, mensagem)
        if resultado.desfecho not in COM_PDF:
            return resultado

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, resultado.validade, doc.nome)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(caminho), str(destino))
        return replace(resultado, caminho_pdf=destino, evidencia=None)

    def _salvar_pdf(self, conteudo: bytes, doc: Documento,
                    mensagem: str) -> ResultadoTentativa:
        provisorio = self._novo_pdf_evidencia(doc)
        provisorio.parent.mkdir(parents=True, exist_ok=True)
        provisorio.write_bytes(conteudo)
        return self._classificar_pdf_salvo(provisorio, doc, mensagem)

    def _print(self, doc: Documento, motivo: str, imagem=None) -> Path | None:
        try:
            pasta = self.cfg.pasta_evidencias / self.orgao / doc.documento
            pasta.mkdir(parents=True, exist_ok=True)
            marca = time.strftime("%Y%m%d-%H%M%S")
            caminho = pasta / f"{marca}-{motivo}.png"
            (imagem or tela.capturar()).save(caminho)
            return caminho
        except Exception:
            log.exception("falha_ao_salvar_print")
            return None

    def _resolver_token_por_provider(self, pagina) -> bool:
        del pagina
        return False


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterSEFAZES:
    from cnd.infra.db import RAIZ_PROJETO

    urls = orgao.extras.get("urls", {})
    cego = orgao.extras.get("cego", {})
    return AdapterSEFAZES(
        orgao=orgao.codigo,
        cfg=cfg,
        caminho_calibragem=RAIZ_PROJETO / "data" / "calibragem" /
        f"{orgao.adapter}.json",
        url_consulta=str(urls.get("consulta", URL_CONSULTA)),
        url_emissao=str(urls.get("emissao", URL_EMISSAO)),
        tempo_pagina_inicial_s=float(
            cego.get("tempo_pagina_inicial_s", TEMPO_PAGINA_INICIAL_S)
        ),
        tempo_formulario_s=float(cego.get("tempo_formulario_s", TEMPO_FORMULARIO_S)),
        espera_turnstile_s=float(cego.get("espera_turnstile_s", TEMPO_TURNSTILE_S)),
        tempo_reacao_s=float(cego.get("tempo_reacao_s", TEMPO_REACAO_S)),
        tempo_salvar_pdf_s=float(cego.get("tempo_salvar_pdf_s", TEMPO_SALVAR_PDF_S)),
        tentativas_turnstile=int(
            cego.get("tentativas_turnstile", TENTATIVAS_TURNSTILE)
        ),
        captcha=ConfigCaptcha.de_config(orgao.extras.get("captcha", {})),
    )
