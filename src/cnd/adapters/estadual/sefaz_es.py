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

from cnd.adapters.federal.rfb.cego import (
    Calibragem,
    CalibragemAusente,
    JanelaOcupada,
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
# Este portal devolve 400 "request header or cookie too large" quando o
# cookie cresce, e a pagina de erro vem SEM o menu lateral - o robo fica
# clicando num item que nao existe mais. Ver reiniciar_sessao.
DOMINIO_PORTAL = "sefaz.es.gov.br"

EXECUTAVEL_NAVEGADOR = "msedge.exe"
TITULO_JANELA = "Certid"
# Tamanho de celular, e nao maximizado. Em janela larga o portal sobe inerte:
# uma folha de estilo pendura, os scripts nao executam, `abreTela` nunca e
# definida e a barra lateral fica colapsada. Estreitando a janela o mesmo
# portal carrega inteiro - medido em 09/09/2026, no layout responsivo. Nao e o
# User-Agent: basta a largura. Valores de um iPhone 16 Pro Max.
LARGURA_JANELA = 430
ALTURA_JANELA = 932
# Pisos: abaixo disto nao ha layout de celular que preste, e insistir numa
# janela menor que isso e pior do que aceitar a tela pequena que se tem.
LARGURA_MINIMA_ESTREITA = 360
ALTURA_MINIMA_ESTREITA = 480
# Tempo que a pagina fica carregando ESTREITA antes de o robo
# maximizar. E na largura que o portal decide se carrega; depois de
# carregado, os scripts ja rodaram e a janela pode crescer - o que
# permite manter a calibragem do layout largo, de sempre.
# Teto, nao espera fixa: o robo segue assim que a tela para de mudar.
# Generoso de proposito - so custa tempo se a pagina ainda estiver
# se montando, que e justamente quando esperar vale a pena.
ESPERA_CARGA_ESTREITA_S = 20.0
# Amostras espalhadas pela janela para saber se a tela ainda esta mudando.
# Nao interessa o QUE mudou: enquanto muda, a pagina ainda esta se montando.
PONTOS_ASSENTAMENTO = tuple(
    (fx, fy)
    for fy in (0.15, 0.35, 0.55, 0.75)
    for fx in (0.20, 0.50, 0.80)
)
INTERVALO_ASSENTAMENTO_S = 0.6
QUADROS_ESTAVEIS_PARA_ASSENTAR = 3
TOLERANCIA_COR_ASSENTAMENTO = 6

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
PONTOS_SCAN_BOTAO_AVISO_MODAL = tuple(
    (fx, fy)
    for fy in (0.46, 0.50, 0.54, 0.58, 0.62, 0.66, 0.70, 0.74, 0.78)
    for fx in (0.34, 0.38, 0.42, 0.46, 0.50, 0.54, 0.58, 0.62, 0.66)
)
MINIMO_PONTOS_BOTAO_AVISO_MODAL = 3
PONTOS_SCAN_ICONE_ERRO_MODAL = tuple(
    (fx, fy)
    for fy in (0.20, 0.24, 0.28, 0.32, 0.36, 0.40, 0.44)
    for fx in (0.38, 0.42, 0.46, 0.50, 0.54, 0.58, 0.62)
)
MINIMO_PONTOS_ICONE_ERRO_MODAL = 3
AREA_SCAN_BOTAO_AVISO_MODAL = (0.30, 0.52, 0.70, 0.82)
AREA_SCAN_ICONE_ERRO_MODAL = (0.34, 0.22, 0.66, 0.52)
PASSO_SCAN_AVISO_MODAL = 10
MINIMO_PIXELS_BOTAO_AVISO_MODAL = 15
MINIMO_PIXELS_ICONE_ERRO_MODAL = 8
OFFSET_X_ICONE_MENU_CND = 48
TEMPO_MENU_CND_FALLBACK_S = 6.0
# Dez, e nao duas. O portal do ES e intermitente: as vezes uma folha de
# estilo pendura, os scripts nao executam e a pagina fica inerte - o menu
# nao abre formulario nenhum porque a funcao que ele chama nem existe. Nao
# temos como consertar o servidor da SEFAZ, e nao adianta diagnosticar: o
# que funciona e recarregar ate cair uma carga boa. Custa tempo so no
# caminho de falha, que ja estava perdido de qualquer jeito.
TENTATIVAS_ABRIR_FORMULARIO = 10
# Espera antes de cada recarga, crescendo: recarregar na hora costuma cair
# no mesmo estado, e martelar o portal e a pior resposta possivel.
ESPERA_ENTRE_RECARGAS_S = 3.0
ESPERA_MAXIMA_ENTRE_RECARGAS_S = 20.0
RAIO_X_SCAN_BOTAO = 0.18
Y_SCAN_BOTAO_DEPOIS_DO_CAMPO = (0.03, 0.25)
PASSO_SCAN_BOTAO = 8
MINIMO_PONTOS_BOTAO = 8

VK_S = 0x53
PORTA_CDP_EDGE = 9222
URL_CDP_EDGE = f"http://127.0.0.1:{PORTA_CDP_EDGE}"
OFFSET_X_CAIXA_TURNSTILE_FRACAO = 0.055
OFFSET_X_CAIXA_TURNSTILE_MIN = 110
OFFSET_X_CAIXA_TURNSTILE_MAX = 190
FATOR_Y_CAIXA_TURNSTILE = 0.55
OFFSET_Y_CAIXA_TURNSTILE_MIN = 70
OFFSET_Y_CAIXA_TURNSTILE_MAX = 170

RE_VALIDADE = re.compile(
    r"(?:valid[ao]\s+ate|validade)[:\s]*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
RE_VALIDA_POR = re.compile(r"valid[ao]\s+por\s+(\d+)\s+dias", re.IGNORECASE)
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
# "Nao foi possivel emitir A certidao negativa para o CNPJ ..." - o artigo
# esta no texto real do portal (visto em 10/09/2026) e faltava no padrao fixo,
# que por isso nunca casava. Como e esta frase que diz POSITIVA, o modal virava
# erro tecnico retentavel: 4 reinsistencias e mais 3 reagendamentos do job numa
# empresa que nunca teria negativa. Regex em vez de substring para o artigo (e
# um eventual complemento) nao poder quebrar de novo.
RE_SEM_NEGATIVA = re.compile(
    r"nao foi possivel emitir\s+(?:a\s+)?certidao\s+negativa")

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


def _arquivo_parece_pdf(caminho: Path) -> bool:
    with contextlib.suppress(OSError), caminho.open("rb") as arquivo:
        return arquivo.read(5).lstrip().startswith(b"%PDF")
    return False


def _pdf_do_data_uri(valor: str) -> bytes | None:
    bruto = (valor or "").strip()
    if bruto.startswith("data:application/pdf;base64,"):
        bruto = bruto.split(",", 1)[1].strip()
    else:
        return None
    if not bruto:
        return None
    try:
        pdf = base64.b64decode(bruto, validate=True)
    except (binascii.Error, ValueError):
        return None
    return pdf if pdf.lstrip().startswith(b"%PDF") else None


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


def _parece_icone_erro_modal(cor: tuple[int, int, int]) -> bool:
    r, g, b = cor
    return r > 200 and 70 <= g <= 175 and 70 <= b <= 175 and r > max(g, b) + 35


def _texto_tem_resposta(texto: str) -> bool:
    t = _sem_acento(texto)
    if RE_SEM_NEGATIVA.search(t):
        return True
    return any(frase in t for frase in FRASES_RESPOSTA_TELA)


def _texto_indica_formulario(texto: str) -> bool:
    t = _sem_acento(texto)
    tem_campo = "cpf / cnpj" in t or "cpf/cnpj" in t
    tem_acao = "emitir certidao" in t or "digite o cpf" in t
    return tem_campo and tem_acao


def _turnstile_ainda_processando(texto: str) -> bool:
    return "complete a verificacao" in _sem_acento(texto)


# O portal poe um modal "CARREGANDO - POR FAVOR, AGUARDE" no MESMO lugar e com
# o MESMO veu escuro do visualizador do PDF. O robo via o veu 1,4 s depois de
# clicar em Emitir, concluia que a certidao ja estava na tela e disparava
# Ctrl+S em cima do aviso de espera (09/09/2026). Nao e desfecho nenhum: e o
# portal trabalhando. A resposta certa e continuar esperando.
def _portal_carregando(texto: str) -> bool:
    t = _sem_acento(texto)
    return "carregando" in t or "por favor, aguarde" in t


# "Ocorreu um erro ao processar a solicitacao" nao e resposta do orgao sobre o
# contribuinte: e o portal falhando por conta propria, com o Turnstile ja
# aprovado e o CNPJ ja preenchido. Tratar como desfecho definitivo joga fora
# uma emissao que costuma sair na tentativa seguinte - basta fechar o aviso e
# clicar em Emitir de novo (visto em 08/09/2026).
def _erro_transitorio_do_portal(texto: str) -> bool:
    return "ocorreu um erro ao processar" in _sem_acento(texto)


def _mensagem_curta(texto: str) -> str:
    return " ".join((texto or "").split())[:500]


# Respostas que falam do CNPJ consultado, e nao da sessao: o formulario
# continua intacto atras do modal, entao ESC devolve a tela pronta e o proximo
# documento entra direto no campo. Captcha e erro de sessao (cookie estourado,
# 400) ficam de fora de proposito - ali a sessao esta suja e reaproveita-la
# repetiria o problema no documento seguinte.
DESFECHOS_QUE_PRESERVAM_A_SESSAO = frozenset({
    Desfecho.POSITIVA,
    Desfecho.PENDENCIA_MANUAL,
})


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
    if RE_SEM_NEGATIVA.search(t):
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


@dataclass
class AdapterSEFAZES:
    orgao: str
    cfg: Config
    caminho_calibragem: Path = Path("data/calibragem/sefaz_es.json")
    url_consulta: str = URL_CONSULTA
    tempo_pagina_inicial_s: float = TEMPO_PAGINA_INICIAL_S
    tempo_formulario_s: float = TEMPO_FORMULARIO_S
    espera_turnstile_s: float = TEMPO_TURNSTILE_S
    tempo_reacao_s: float = TEMPO_REACAO_S
    tempo_salvar_pdf_s: float = TEMPO_SALVAR_PDF_S
    tentativas_turnstile: int = TENTATIVAS_TURNSTILE
    tentativas_abrir_formulario: int = TENTATIVAS_ABRIR_FORMULARIO
    largura_janela: int = LARGURA_JANELA
    espera_carga_estreita_s: float = ESPERA_CARGA_ESTREITA_S
    altura_janela: int = ALTURA_JANELA
    _calibragem: Calibragem | None = field(default=None, repr=False)
    _formulario_pronto: bool = field(default=False, repr=False)
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
        self._formulario_pronto = False
        self._limpar_cookies_do_portal()
        self._abrir_navegador()

    def _limpar_cookies_do_portal(self) -> None:
        """Apaga do disco os cookies do portal do ES.

        Sem isto o cookie cresce a cada sessao ate o portal responder 400, e
        a pagina de erro vem sem menu lateral - o sintoma e "o site bugou",
        nao um erro legivel. Diferente da Receita, aqui nao ha sessao a
        preservar: nao tem login, entao limpar sempre sai mais barato que
        detectar o estouro depois.

        Mata o Edge antes: com o processo vivo o banco de cookies nao abre,
        e a limpeza sai com removidos=0 (licao do rfb_cego, 15/08/2026).
        """
        self._matar_edge()
        removidos = perfil_edge.limpar_cookies(
            DOMINIO_PORTAL, self._raiz_perfil_edge())
        log.info("cookies_do_portal_limpos",
                 extra={"orgao": self.orgao, "dominio": DOMINIO_PORTAL,
                        "removidos": removidos})

    def _raiz_perfil_edge(self) -> Path:
        return self.cfg.pasta_evidencias.parent / "edge-sefaz-es"

    def encerrar(self) -> None:
        self._matar_edge()

    def _esperar_pagina_assentar(self) -> bool:
        """Espera a tela parar de mudar, em vez de dormir um tempo fixo.

        Tempo fixo erra dos dois lados: sobra quando a pagina sobe rapido e
        falta quando o portal esta lento. Aqui o robo tira fotos da janela e
        compara: enquanto os pixels mudam, a pagina ainda esta se montando;
        quando param, ela assentou. Funciona em qualquer layout, porque nao
        depende de saber ONDE as coisas ficam.

        `espera_carga_estreita_s` deixa de ser a espera e passa a ser o TETO.
        """
        limite = time.monotonic() + max(1.0, self.espera_carga_estreita_s)
        anterior: list[tuple[int, int, int]] | None = None
        estaveis = 0

        while time.monotonic() < limite:
            time.sleep(INTERVALO_ASSENTAMENTO_S)
            janela = entrada_real.retangulo_janela(TITULO_JANELA,
                                                   EXECUTAVEL_NAVEGADOR)
            if janela is None:
                continue
            imagem = tela.capturar()
            atual = [tela.cor_media(imagem, *_ponto_fracionario(janela, fx, fy))
                     for fx, fy in PONTOS_ASSENTAMENTO]

            if anterior is not None and all(
                    abs(a - b) <= TOLERANCIA_COR_ASSENTAMENTO
                    for cor_a, cor_b in zip(atual, anterior, strict=True)
                    for a, b in zip(cor_a, cor_b, strict=True)):
                estaveis += 1
                if estaveis >= QUADROS_ESTAVEIS_PARA_ASSENTAR:
                    log.info("pagina_assentou",
                             extra={"orgao": self.orgao,
                                    "em_s": round(
                                        max(1.0, self.espera_carga_estreita_s)
                                        - (limite - time.monotonic()), 1)})
                    return True
            else:
                estaveis = 0
            anterior = atual

        log.info("pagina_nao_assentou_no_teto",
                 extra={"orgao": self.orgao,
                        "teto_s": self.espera_carga_estreita_s})
        return False

    def _abrir_navegador(self) -> None:
        """Fecha o Edge e abre de novo, ja no portal.

        E o UNICO jeito de carregar a pagina neste adapter, e e de proposito.
        Abrir aba nova e fechar a antiga carregava o portal duas vezes por
        sessao - o Edge ja nasce na URL - e deixava a janela num estado que o
        robo nao sabia descrever. Recarregar aqui e fechar tudo e comecar
        limpo, que e o que uma pessoa faria.
        """
        self.encerrar()
        # Janela ESTREITA, e nunca maximizada: e a largura que decide se este
        # portal carrega ou fica inerte (ver LARGURA_JANELA). Maximizar aqui
        # desfaria justamente o que faz ele funcionar.
        #
        # Volta a chamar o msedge.exe com flags porque --window-size nao passa
        # pelo shell. O caminho do shell foi testado em 08/09/2026 e nao trouxe
        # vantagem nenhuma.
        subprocess.Popen([
            _achar_edge(), "--new-window", "--disable-extensions",
            "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={self._raiz_perfil_edge()}",
            f"--remote-debugging-port={PORTA_CDP_EDGE}",
            "--remote-debugging-address=127.0.0.1",
            "--window-size={},{}".format(*self._tamanho_da_janela()),
            self.url_consulta,
        ])
        if not self._esperar_janela():
            log.warning("janela_do_edge_nao_apareceu",
                        extra={"orgao": self.orgao,
                               "esperou_s": TEMPO_JANELA_ABRIR_S})
        # Forcar o tamanho, e nao confiar so no --window-size: o Edge lembra
        # o estado da janela do perfil e reabre maximizada se assim ficou da
        # ultima vez, ignorando a flag. Janela larga = portal inerte.
        self._estreitar_janela()
        self._esperar_pagina_assentar()
        self._posicionar_janela_calibrada()
        entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        self._formulario_pronto = False
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

    def _tamanho_da_janela(self) -> tuple[int, int]:
        """Tamanho pedido, limitado ao que cabe na tela desta maquina.

        932 e a altura de um iPhone 16 Pro Max e nao cabe num monitor de
        1024x768: a janela nasceria mais alta que a area util, o rodape
        ficaria atras da barra de tarefas e - num robo que mede tudo em
        FRACAO da janela - os pontos calibrados apontariam para fora do
        visivel. Melhor uma janela mais baixa que uma janela mentirosa.
        """
        _x, _y, largura_util, altura_util = entrada_real.area_util()
        largura = min(self.largura_janela,
                      max(LARGURA_MINIMA_ESTREITA, largura_util))
        altura = min(self.altura_janela,
                     max(ALTURA_MINIMA_ESTREITA, altura_util))
        if (largura, altura) != (self.largura_janela, self.altura_janela):
            log.info("janela_estreita_ajustada_a_tela",
                     extra={"orgao": self.orgao,
                            "pedido": [self.largura_janela, self.altura_janela],
                            "usado": [largura, altura],
                            "area_util": [largura_util, altura_util]})
        return largura, altura

    def _estreitar_janela(self) -> None:
        """Poe a janela no tamanho de celular, desmaximizando se preciso."""
        atual = entrada_real.retangulo_janela(TITULO_JANELA,
                                              EXECUTAVEL_NAVEGADOR)
        origem = (atual[0], atual[1]) if atual else (0, 0)
        tamanho = self._tamanho_da_janela()
        if not entrada_real.posicionar_janela(
                TITULO_JANELA, EXECUTAVEL_NAVEGADOR, (*origem, *tamanho)):
            log.warning("janela_nao_estreitou",
                        extra={"orgao": self.orgao, "tamanho": list(tamanho)})

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
            perfil_edge.marcar_saida_limpa(self._raiz_perfil_edge())
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

        # Uma sessao serve varios CNPJs. Recarregar o portal a cada documento
        # abre aba nova, refaz a carga estreita e gasta meio minuto por item -
        # tudo isso para chegar no mesmo formulario que ja esta na tela. Aqui
        # o robo so recarrega quando o formulario nao esta mais utilizavel; se
        # der erro, quem reabre o Edge inteiro e o reiniciar_sessao.
        if not self._formulario_na_tela():
            self._abrir_navegador()
            if not self._abrir_formulario():
                self._formulario_pronto = False
                return "nada", None
        self._formulario_pronto = True

        time.sleep(max(0.0, self.espera_turnstile_s))

        self._exigir_foco()
        self._clicar("campo_documento", self._ponto("campo_documento"))
        time.sleep(random.uniform(0.2, 0.5))
        entrada_real.limpar_campo()
        entrada_real.digitar(doc.documento)
        time.sleep(random.uniform(0.5, 1.2))

        for tentativa in range(1, max(1, self.tentativas_turnstile) + 1):
            self._ultimo_texto_portal = None
            self._exigir_foco()
            self._clicar("botao_emitir", self._ponto_botao_emitir())
            reacao, caminho = self._aguardar_reacao(doc, self.tempo_reacao_s)
            if reacao not in ("turnstile", "erro_transitorio"):
                # ESC fecha o modal e o formulario volta para a tela, pronto
                # para o proximo CNPJ - vale para o visor do PDF e tambem para
                # o aviso de "nao foi possivel emitir a certidao negativa",
                # que e resposta sobre a empresa e nao avaria da sessao.
                # Recarregar o portal custa cerca de um minuto (janela
                # estreita, espera de assentamento, maximizar), e paga-se isso
                # so quando o estado e mesmo desconhecido.
                # `_formulario_na_tela` confere antes de usar: a marca aqui e
                # so a aposta, a tela continua sendo a verdade.
                self._formulario_pronto = reacao == "pdf" or (
                    reacao == "texto"
                    and classificar_texto(self._ultimo_texto_portal or "")
                    in DESFECHOS_QUE_PRESERVAM_A_SESSAO
                )
                if self._formulario_pronto:
                    self._fechar_aviso()
                return reacao, caminho

            log.info("reinsistindo_na_emissao",
                     extra={"orgao": self.orgao, "motivo": reacao,
                            "tentativa": tentativa,
                            "limite": self.tentativas_turnstile})
            if reacao == "turnstile":
                self._clicar_caixa_turnstile()
            else:
                self._fechar_aviso()
            time.sleep(max(1.0, self.espera_turnstile_s))

        return reacao, None

    def _formulario_na_tela(self) -> bool:
        """O formulario esta ali agora, pronto para o proximo documento?

        Nao basta a marca da ultima vez: a tela e a verdade. Um modal que nao
        fechou, uma sessao que expirou ou o portal que se perdeu sozinho
        deixam a marca mentindo.
        """
        if not self._formulario_pronto:
            return False
        return self._esperar_formulario(segundos=2.0)

    def _abrir_formulario(self) -> bool:
        for tentativa_site in range(1, self.tentativas_abrir_formulario + 1):
            if self._esperar_formulario(segundos=3.0):
                return True

            if not self._esperar_pagina_inicial():
                if tentativa_site < self.tentativas_abrir_formulario:
                    self._reiniciar_site("pagina_inicial_nao_carregou", tentativa_site)
                    continue
                return False

            if self._esperar_formulario(segundos=0.5):
                return True

            if self._clicar_menu_cnd_e_esperar_formulario():
                return True

            if tentativa_site < self.tentativas_abrir_formulario:
                self._reiniciar_site("menu_cnd_nao_abriu_formulario", tentativa_site)

        return False

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
                           "limite": self.tentativas_abrir_formulario})
        self._abrir_navegador()
        espera = min(ESPERA_MAXIMA_ENTRE_RECARGAS_S,
                     ESPERA_ENTRE_RECARGAS_S * tentativa)
        time.sleep(random.uniform(espera * 0.8, espera * 1.2))

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

    def _ponto_caixa_turnstile(self, imagem) -> tuple[int, int]:
        """Ponto provavel da caixinha do Turnstile, sem nova calibragem.

        O desafio aparece entre o campo CPF/CNPJ e o botao Emitir. A caixa
        fica no lado esquerdo do widget; usar proporcao da janela preserva o
        mesmo chute em resolucoes diferentes.
        """
        janela = self._janela()
        _x, _y, largura, _altura = janela
        campo_x, campo_y = self._ponto("campo_documento")
        _botao_x, botao_y = self._localizar_botao_emitir(imagem) or self._ponto(
            "botao_emitir")
        distancia_y = max(0, botao_y - campo_y)
        offset_x = max(
            OFFSET_X_CAIXA_TURNSTILE_MIN,
            min(OFFSET_X_CAIXA_TURNSTILE_MAX,
                round(largura * OFFSET_X_CAIXA_TURNSTILE_FRACAO)),
        )
        offset_y = max(
            OFFSET_Y_CAIXA_TURNSTILE_MIN,
            min(OFFSET_Y_CAIXA_TURNSTILE_MAX,
                round(distancia_y * FATOR_Y_CAIXA_TURNSTILE)),
        )
        return round(campo_x - offset_x), round(campo_y + offset_y)

    def _clicar_caixa_turnstile(self) -> bool:
        with contextlib.suppress(Exception):
            imagem = tela.capturar()
            ponto = self._ponto_caixa_turnstile(imagem)
            log.info("clicando_caixa_turnstile",
                     extra={"orgao": self.orgao, "ponto": ponto})
            self._clicar("caixa_turnstile", ponto)
            return True
        log.warning("caixa_turnstile_nao_clicada",
                    extra={"orgao": self.orgao})
        return False

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
        avisou_carregando = False

        while time.monotonic() < limite:
            agora = time.monotonic()

            if agora >= proximo_foco:
                self._exigir_foco()
                proximo_foco = agora + 3.0

            imagem = tela.capturar()
            if self._tem_modal(imagem):
                # Ler ANTES de tentar salvar. Nem todo modal e o visualizador
                # do PDF: o portal usa o mesmo veu para o aviso de erro e para
                # o Turnstile. Disparar Ctrl+S num modal de erro salvava a
                # PAGINA (o foco esta nela, nao num visor), e a caixa "Salvar
                # como" ficava aberta por cima roubando os cliques seguintes -
                # o robo entrava num ciclo de erro que so acabava no limite.
                if self._modal_parece_aviso(imagem):
                    texto = self._texto_da_pagina(limpar=False)
                    self._ultimo_texto_portal = texto
                    if _portal_carregando(texto):
                        if not avisou_carregando:
                            avisou_carregando = True
                            log.info("portal_carregando",
                                     extra={"orgao": self.orgao,
                                            "em_s": round(agora - inicio, 1)})
                        time.sleep(INTERVALO_REACAO_S)
                        continue
                    if _turnstile_ainda_processando(texto):
                        return "turnstile", None
                    if _erro_transitorio_do_portal(texto):
                        return "erro_transitorio", None
                    if _texto_tem_resposta(texto):
                        return "texto", None
                    log.info("modal_de_aviso_sem_pdf",
                             extra={"orgao": self.orgao,
                                    "texto": _mensagem_curta(texto)})
                    return "erro_transitorio", None

                texto = self._texto_da_pagina(limpar=False)
                # Antes de qualquer coisa: o portal ainda esta trabalhando?
                # O modal de espera usa o mesmo veu do visor do PDF, e tratar
                # um como o outro custava a certidao inteira.
                if _portal_carregando(texto):
                    if not avisou_carregando:
                        avisou_carregando = True
                        log.info("portal_carregando",
                                 extra={"orgao": self.orgao,
                                        "em_s": round(agora - inicio, 1)})
                    time.sleep(INTERVALO_REACAO_S)
                    continue

                if _turnstile_ainda_processando(texto):
                    self._ultimo_texto_portal = texto
                    return "turnstile", None
                if _erro_transitorio_do_portal(texto):
                    self._ultimo_texto_portal = texto
                    return "erro_transitorio", None

                if not tentou_salvar_modal:
                    tentou_salvar_modal = True
                    log.info("modal_tratado_como_visualizador_pdf",
                             extra={"orgao": self.orgao,
                                    "texto": _mensagem_curta(texto)})
                    caminho = self._salvar_pdf_do_modal(doc)
                    if caminho is not None:
                        return "pdf", caminho

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
                if _erro_transitorio_do_portal(texto):
                    self._ultimo_texto_portal = texto
                    return "erro_transitorio", None
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

    def _modal_parece_aviso(self, imagem) -> bool:
        """SweetAlert de aviso: e resposta do portal, nao visor de PDF."""
        with contextlib.suppress(Exception):
            janela = self._janela()
            azuis = 0
            for fx, fy in PONTOS_SCAN_BOTAO_AVISO_MODAL:
                x, y = _ponto_fracionario(janela, fx, fy)
                if _parece_botao(tela.cor_media(imagem, x, y, raio=4)):
                    azuis += 1
                    if azuis >= MINIMO_PONTOS_BOTAO_AVISO_MODAL:
                        log.info("modal_parece_aviso",
                                 extra={"orgao": self.orgao,
                                        "sinal": "botao_ok",
                                        "pontos": azuis})
                        return True
            vermelhos = 0
            for fx, fy in PONTOS_SCAN_ICONE_ERRO_MODAL:
                x, y = _ponto_fracionario(janela, fx, fy)
                if _parece_icone_erro_modal(tela.cor_media(imagem, x, y, raio=4)):
                    vermelhos += 1
                    if vermelhos >= MINIMO_PONTOS_ICONE_ERRO_MODAL:
                        log.info("modal_parece_aviso",
                                 extra={"orgao": self.orgao,
                                        "sinal": "icone_erro",
                                        "pontos": vermelhos})
                        return True
            if self._modal_tem_cor_na_area(
                    imagem, AREA_SCAN_BOTAO_AVISO_MODAL, _parece_botao,
                    MINIMO_PIXELS_BOTAO_AVISO_MODAL):
                log.info("modal_parece_aviso",
                         extra={"orgao": self.orgao, "sinal": "botao_ok_area"})
                return True
            if self._modal_tem_cor_na_area(
                    imagem, AREA_SCAN_ICONE_ERRO_MODAL, _parece_icone_erro_modal,
                    MINIMO_PIXELS_ICONE_ERRO_MODAL):
                log.info("modal_parece_aviso",
                         extra={"orgao": self.orgao, "sinal": "icone_erro_area"})
                return True
        return False

    def _modal_tem_cor_na_area(
        self,
        imagem,
        area: tuple[float, float, float, float],
        predicado,
        minimo: int,
    ) -> bool:
        janela = self._janela()
        x0, y0 = _ponto_fracionario(janela, area[0], area[1])
        x1, y1 = _ponto_fracionario(janela, area[2], area[3])
        encontrados = 0
        for y in range(min(y0, y1), max(y0, y1) + 1, PASSO_SCAN_AVISO_MODAL):
            for x in range(min(x0, x1), max(x0, x1) + 1, PASSO_SCAN_AVISO_MODAL):
                if predicado(tela.cor_em(imagem, x, y)):
                    encontrados += 1
                    if encontrados >= minimo:
                        return True
        return False

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

    def _texto_da_pagina(self, *, limpar: bool = True) -> str:
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
            if limpar:
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
        arquivos_antes = self._estado_arquivos_da_pasta(destino.parent)

        if (extraido := self._salvar_pdf_do_dom(destino)) is not None:
            return extraido

        try:
            self._exigir_foco()
            self._clicar("visor_pdf", self._ponto("visor_pdf"))
            time.sleep(0.35)
            entrada_real.atalho(entrada_real.VK_CONTROL, VK_S)
            time.sleep(1.0)
            # COLAR, nao digitar. A caixa "Salvar como" do Windows perde
            # caractere quando se digita caminho longo: em 08/09/2026 ela
            # recebeu o caminho cortado no meio, e o PDF foi salvo com outro
            # nome - falha silenciosa, a pior de todas aqui, porque a certidao
            # ja tinha sido emitida. Colar e atomico.
            entrada_real.limpar_campo()
            if not entrada_real.colar(str(destino)):
                log.warning("clipboard_indisponivel_digitando_o_caminho",
                            extra={"orgao": self.orgao})
                entrada_real.digitar(str(destino), minimo=0.012, maximo=0.030)
            time.sleep(0.3)
            entrada_real.tecla(entrada_real.VK_RETURN)
        except Exception as erro:
            log.warning("falha_ao_disparar_salvar_pdf",
                        extra={"orgao": self.orgao, "erro": str(erro)[:300]})
            self._fechar_caixa_salvar(destino, arquivos_antes)
            return None

        if self._esperar_arquivo_pdf(destino):
            log.info("pdf_salvo_do_modal",
                     extra={"orgao": self.orgao, "arquivo": str(destino)})
            return destino

        # Rede de seguranca: se o nome saiu diferente do pedido - uma tecla
        # perdida na caixa "Salvar como" basta - o PDF existe mas com outro
        # nome. Perder uma certidao ja emitida por causa disso e caro: o
        # portal cobra outra emissao e o Turnstile de novo.
        if (salvo := self._pdf_recem_criado(destino)) is not None:
            log.warning("pdf_salvo_com_outro_nome",
                        extra={"orgao": self.orgao, "pedido": str(destino),
                               "encontrado": str(salvo)})
            return salvo

        log.warning("pdf_do_modal_nao_salvo",
                    extra={"orgao": self.orgao, "arquivo": str(destino)})
        self._fechar_caixa_salvar(destino, arquivos_antes)
        return None

    def _salvar_pdf_do_dom(self, destino: Path) -> Path | None:
        """Extrai o PDF do `<object data="data:application/pdf;base64,...">`.

        O portal ja colocou os bytes no DOM quando o visualizador abre. O
        atalho Ctrl+S depende do foco interno do visualizador PDF do Edge, que
        falha em iframe/modal. Ler o `data:` evita teclado, caixa "Salvar como"
        e download de pagina HTML com extensao .pdf.
        """
        if not self._cdp_disponivel():
            log.info("cdp_edge_indisponivel_para_pdf_dom",
                     extra={"orgao": self.orgao, "url": URL_CDP_EDGE})
            return None
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except Exception as erro:
            log.info("playwright_indisponivel_para_pdf_dom",
                     extra={"orgao": self.orgao, "erro": str(erro)[:200]})
            return None

        try:
            with sync_playwright() as playwright:
                navegador = playwright.chromium.connect_over_cdp(
                    URL_CDP_EDGE, timeout=3000)
                try:
                    for contexto in navegador.contexts:
                        for pagina in contexto.pages:
                            if DOMINIO_PORTAL not in pagina.url:
                                continue
                            if self._extrair_data_uri_pdf_da_pagina(pagina, destino):
                                return destino
                finally:
                    navegador.close()
        except PlaywrightError as erro:
            log.info("pdf_dom_indisponivel",
                     extra={"orgao": self.orgao, "erro": str(erro)[:200]})
        except Exception as erro:
            log.warning("falha_ao_extrair_pdf_dom",
                        extra={"orgao": self.orgao, "erro": str(erro)[:300]})
        return None

    def _cdp_disponivel(self) -> bool:
        with (
            contextlib.suppress(Exception),
            urllib.request.urlopen(f"{URL_CDP_EDGE}/json/version",
                                   timeout=0.4) as resposta,
        ):
            return resposta.status == 200
        return False

    def _extrair_data_uri_pdf_da_pagina(self, pagina, destino: Path) -> bool:
        script = """
        () => {
          const candidatos = [];
          const add = (valor) => {
            if (typeof valor === 'string' && valor) candidatos.push(valor);
          };
          for (const el of document.querySelectorAll('object, embed, iframe')) {
            add(el.getAttribute('data'));
            add(el.getAttribute('src'));
            add(el.data);
            add(el.src);
          }
          for (const el of document.querySelectorAll('[data], [src]')) {
            add(el.getAttribute('data'));
            add(el.getAttribute('src'));
          }
          return candidatos.find((valor) =>
            valor.startsWith('data:application/pdf;base64,')) || '';
        }
        """
        for frame in [pagina.main_frame, *pagina.frames]:
            with contextlib.suppress(Exception):
                data_uri = frame.evaluate(script)
                if not isinstance(data_uri, str):
                    continue
                pdf = _pdf_do_data_uri(data_uri)
                if pdf is None:
                    continue
                temporario = destino.with_suffix(destino.suffix + ".tmp")
                temporario.write_bytes(pdf)
                if not _arquivo_parece_pdf(temporario):
                    temporario.unlink(missing_ok=True)
                    continue
                temporario.replace(destino)
                log.info("pdf_salvo_do_dom",
                         extra={"orgao": self.orgao, "arquivo": str(destino),
                                "bytes": len(pdf)})
                return True
        return False

    def _fechar_caixa_salvar(
        self,
        destino: Path | None = None,
        arquivos_antes: dict[Path, tuple[int, int]] | None = None,
    ) -> None:
        """Fecha a caixa "Salvar como" que tenha ficado aberta.

        Se o salvamento falha e a caixa fica de pe, ela cobre a pagina e rouba
        todo clique seguinte: o robo tenta Emitir de novo e acerta o dialogo.
        Foi assim que uma falha virava um ciclo de erros ate o limite.
        """
        if destino is not None and self._arquivo_de_salvamento_aparecendo(
                destino, arquivos_antes or {}):
            log.info("caixa_salvar_nao_fechada_download_em_andamento",
                     extra={"orgao": self.orgao, "arquivo": str(destino)})
            return

        with contextlib.suppress(Exception):
            entrada_real.tecla(entrada_real.VK_ESCAPE)
            time.sleep(0.25)
            entrada_real.tecla(entrada_real.VK_ESCAPE)

    def _estado_arquivos_da_pasta(self, pasta: Path) -> dict[Path, tuple[int, int]]:
        estados: dict[Path, tuple[int, int]] = {}
        with contextlib.suppress(OSError):
            for arquivo in pasta.iterdir():
                with contextlib.suppress(OSError):
                    if arquivo.is_file():
                        info = arquivo.stat()
                        estados[arquivo] = (info.st_size, info.st_mtime_ns)
        return estados

    def _arquivo_de_salvamento_aparecendo(
        self,
        destino: Path,
        antes: dict[Path, tuple[int, int]],
    ) -> bool:
        """Arquivo novo ou mudando no destino: nao apertar Escape.

        No Edge, Escape pode cancelar um download que ja saiu da caixa
        "Salvar como". Se algo apareceu ou mudou na pasta depois do Ctrl+S,
        o estado correto e deixar o Windows/Edge terminar sozinho.
        """
        agora = self._estado_arquivos_da_pasta(destino.parent)
        return any(antes.get(arquivo) != estado for arquivo, estado in agora.items())

    def _pdf_recem_criado(self, pedido: Path) -> Path | None:
        """PDF que apareceu na pasta agora, com nome diferente do pedido."""
        candidatos: list[Path] = []
        with contextlib.suppress(OSError):
            for arquivo in pedido.parent.glob("*.pdf"):
                if arquivo == pedido:
                    continue
                info = arquivo.stat()
                if info.st_size <= 0:
                    continue
                if time.time() - info.st_mtime >= self.tempo_salvar_pdf_s:
                    continue
                if _arquivo_parece_pdf(arquivo):
                    candidatos.append(arquivo)
                else:
                    log.warning("arquivo_recente_nao_e_pdf",
                                extra={"orgao": self.orgao,
                                       "arquivo": str(arquivo)})
        if not candidatos:
            return None
        return max(candidatos, key=lambda a: a.stat().st_mtime)

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

            return _arquivo_parece_pdf(caminho)
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

def _achar_edge() -> str:
    candidatos = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for caminho in candidatos:
        if Path(caminho).exists():
            return caminho
    raise RuntimeError("Nao achei o msedge.exe nos caminhos padrao do Windows")


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
        tempo_pagina_inicial_s=float(
            cego.get("tempo_pagina_inicial_s", TEMPO_PAGINA_INICIAL_S)
        ),
        tempo_formulario_s=float(cego.get("tempo_formulario_s", TEMPO_FORMULARIO_S)),
        espera_turnstile_s=float(cego.get("espera_turnstile_s", TEMPO_TURNSTILE_S)),
        tempo_reacao_s=float(cego.get("tempo_reacao_s", TEMPO_REACAO_S)),
        tempo_salvar_pdf_s=float(cego.get("tempo_salvar_pdf_s", TEMPO_SALVAR_PDF_S)),
        tentativas_abrir_formulario=int(
            cego.get("tentativas_abrir_formulario",
                     TENTATIVAS_ABRIR_FORMULARIO)),
        largura_janela=int(cego.get("largura_janela", LARGURA_JANELA)),
        espera_carga_estreita_s=float(
            cego.get("espera_carga_estreita_s", ESPERA_CARGA_ESTREITA_S)),
        altura_janela=int(cego.get("altura_janela", ALTURA_JANELA)),
        tentativas_turnstile=int(
            cego.get("tentativas_turnstile", TENTATIVAS_TURNSTILE)
        ),
    )
