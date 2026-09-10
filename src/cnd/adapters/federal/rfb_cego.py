"""Receita Federal PJ — adapter cego, sem automação de navegador.

A diferença para o `rfb_pj.py`: aqui **não existe Playwright**. Nenhuma
porta de depuração, nenhuma conexão com o navegador, nenhum
`navigator.webdriver`. O Edge é aberto como qualquer pessoa abriria, e o
robô só mexe no mouse e no teclado do Windows por cima dele.

Do ponto de vista do portal, não há nada para detectar: é um Edge comum
recebendo entrada do sistema operacional.

O preço é ficar cego: o robô não lê o HTML. Ele se orienta por três coisas:

  1. **Coordenadas calibradas** — onde ficam o campo e os botões, medidos
     uma vez com a janela maximizada (`cnd calibrar`).
  2. **Cor de pixels** — véu escuro denuncia janela modal aberta; faixa
     amarela ou vermelha no topo denuncia bloqueio do portal.
  3. **O PDF na pasta de downloads** — que é a fonte de verdade de qualquer
     jeito: é dele que já saíam o tipo da certidão, a validade e o código
     de controle.

Limitação assumida: a máquina fica ocupada enquanto roda (o mouse é um só)
e a calibragem depende da resolução da tela.
"""
from __future__ import annotations

import contextlib
import json
import random
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from cnd.adapters.federal import rfb_matriz
from cnd.adapters.federal.rfb_pdf import ler_pdf
from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra import entrada_real, perfil_edge, tela
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.rfb_cego")

URL_FORMULARIO = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj"
DOMINIO_PORTAL = "receitafederal.gov.br"

# A janela é localizada pelo PROGRAMA, não pelo título: o título de um
# navegador muda a cada página ("Serviços da Receita Federal" na home,
# "Certidão de Regularidade Fiscal" no formulário, "Resultado da Emissão"
# depois), e procurar por ele quebra no meio do fluxo.
EXECUTAVEL_NAVEGADOR = "msedge.exe"
TITULO_JANELA = "Receita"          # só desempate, quando há várias janelas

PONTOS_NECESSARIOS = ("campo_cnpj", "botao_emitir", "botao_emitir_nova",
                      "fundo_pagina", "faixa_alerta")

TEMPO_CARREGAR_S = 6.0
# Espera máxima pelo formulário. Era 4x o tempo de carregar (24s) e cada
# item pagava os 24 inteiros, porque a detecção falhava sempre e o fluxo
# seguia assim mesmo — 19 das 35 horas do lote eram esta espera. Não
# adianta esperar mais por um critério que nunca passa: se ele falhar, o
# passo seguinte confirma na prática, tentando digitar.
TEMPO_FORMULARIO_S = 8.0
# Brilho mínimo do campo de CNPJ para chamá-lo de "campo branco".
BRILHO_DO_CAMPO = 200
# O portal leva cerca de 12s para responder ao clique em emitir. Esperar
# menos que isso fazia o robô desistir antes de a janelinha aparecer.
TEMPO_REACAO_S = 45.0
INTERVALO_MODAL_S = 0.15
PAUSA_ANTES_EMITIR_NOVA_S = (0.20, 0.35)
TEMPO_PDF_S = 70.0
# Quanto esperar o Edge sumir da lista de processos depois do taskkill /F.
TEMPO_EDGE_MORRER_S = 15.0
# Quanto esperar a janela do Edge existir depois de mandar abrir.
TEMPO_JANELA_ABRIR_S = 25.0

# Quantas emissões uma janela do Edge aguenta antes de o portal recusar.
#
# Medido no log de 15/08/2026, com 30 itens: a 1ª consulta de cada sessão
# passou 17 vezes em 17; a 2ª falhou 4 vezes em 16; e a 3ª tomou o código
# 023 ("não foi possível concluir a ação para o contribuinte informado")
# 12 vezes em 12. Não é acaso — é o portal contando quantas emissões saíram
# daquela sessão.
#
# O robô pagava caro por descobrir isso item a item: ~13s numa consulta já
# condenada, mais 30 a 90 segundos de espera do micro-retry, e só então
# reabria o navegador e acertava. Reabrir ANTES troca tudo isso pelos ~10
# segundos de uma janela nova.
EMISSOES_POR_SESSAO = 1
TEMPO_PRIMEIRA_LEITURA_TEXTO_S = 5.0
INTERVALO_LEITURA_TEXTO_S = 2.0

FRASE_INSUFICIENTE = "sao insuficientes para emitir a certidao pela internet"
# "Inscrição no CNPJ ... Inapta - Omissão de declarações, emissão de
# certidão não permitida." Vista em 17/08/2026 em 4 empresas do lote 1.
# Vem numa caixa branca comum, sem faixa amarela nem vermelha, então a
# heurística de cor não a enxerga: só o texto denuncia. Antes de existir
# esta frase, essas 4 caíam no diagnóstico final como ERRO_TECNICO e
# gastavam 3 tentativas contra uma tela que nunca mudaria sozinha.
FRASE_INAPTA = "inapta"
FRASE_INAPTA_MOTIVO = "emissao de certidao nao permitida"
FRASE_RETORNE_RESULTADO = "retorne em alguns minutos para o resultado"
FRASE_SERVICO_INDISPONIVEL = (
    "servico de emissao de certidao esta temporariamente indisponivel"
)
FRASES_RETENTAR_TEXTO = (
    FRASE_RETORNE_RESULTADO,
    FRASE_SERVICO_INDISPONIVEL,
)
FRASES_BLOQUEIO_TEXTO = (
    "tente novamente em alguns minutos",
    "nao foi possivel emitir a certidao",
    "nao foi possivel concluir a acao para o contribuinte informado",
)
# Página do nginx, servida antes de a aplicação do portal rodar. Não é
# recado da Receita sobre a empresa: é o cabeçalho de cookies da NOSSA
# sessão passando do limite do servidor. Some ao apagar os cookies do
# domínio — ver infra/perfil_edge.py.
FRASES_COOKIE_GRANDE = (
    "request header or cookie too large",
    "400 bad request",
)
# O portal carimba o código junto da data: "033 - 17/08/2026 15:18:08".
# Exigir esse formato não é preciosismo — sem a data, `\b033\b` casava com
# os três dígitos DENTRO DO CNPJ. A ANGONESE (17.406.033/0001-39) recebeu
# "são insuficientes para emitir a certidão", que é POSITIVA, e foi
# classificada como resultado pendente por causa do próprio número dela
# (17/08/2026). Vale para qualquer CNPJ com .033/ ou .033. no meio.
RE_CODIGO_033 = re.compile(r"\b033\s*-\s*\d{2}/\d{2}/\d{4}")

# Pontos fora do centro da tela. O modal branco costuma cobrir o centro; o
# veu escuro aparece melhor nas laterais.
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


class CalibragemAusente(RuntimeError):
    pass


class JanelaOcupada(RuntimeError):
    """Não foi possível dar o foco ao navegador — alguém está usando a máquina."""


def _ponto_fracionario(janela: tuple[int, int, int, int],
                       fx: float, fy: float) -> tuple[int, int]:
    x, y, largura, altura = janela
    return round(x + fx * largura), round(y + fy * altura)


def _parece_botao(cor: tuple[int, int, int]) -> bool:
    """O azul do botão de ação do portal (#1351B4 e vizinhos)."""
    r, _, b = cor
    return b > r + 40 and b > 90


def _tem_veu_modal(cores: list[tuple[int, int, int]],
                   referencia: tuple[int, int, int]) -> bool:
    escuros = sum(1 for cor in cores if tela.escurecida(cor, referencia))
    return escuros >= MINIMO_PONTOS_VEU_MODAL


def _normalizar_texto(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    ascii_puro = sem_acento.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_puro.lower().split())


def _mensagem_curta(texto: str) -> str:
    return " ".join((texto or "").split())[:400]


def _classificar_texto_portal(texto: str) -> str | None:
    normalizado = _normalizar_texto(texto)
    # Testada primeiro: nesta tela o portal nem chegou a rodar, então nada
    # do que vem depois pode estar escrito nela. É falha nossa de sessão, e
    # tem conserto — não é resposta sobre a empresa.
    if any(frase in normalizado for frase in FRASES_COOKIE_GRANDE):
        return "cookies"
    # Testada antes das outras: a faixa que pede a matriz é amarela igual à
    # do bloqueio, e confundi-las faria o robô recuar e retentar quando o
    # portal só estava dizendo qual CNPJ digitar.
    if rfb_matriz.exige_matriz(normalizado):
        return "matriz"
    if (any(frase in normalizado for frase in FRASES_RETENTAR_TEXTO)
            or RE_CODIGO_033.search(normalizado)):
        return "retentar"
    # Exige as DUAS partes: "inapta" sozinha é palavra comum demais para
    # decidir o destino de uma empresa, e o motivo sozinho poderia vir de
    # outra recusa que ainda não conhecemos.
    if FRASE_INAPTA in normalizado and FRASE_INAPTA_MOTIVO in normalizado:
        return "inapta"
    if FRASE_INSUFICIENTE in normalizado:
        return "insuficiente"
    if any(frase in normalizado for frase in FRASES_BLOQUEIO_TEXTO):
        return "bloqueio"
    return None


@dataclass
class Calibragem:
    """Onde ficam as coisas na tela, em PROPORÇÕES da janela do navegador.

    Guardar pixels absolutos amarraria a calibragem a um monitor só — e o
    servidor tem outra tela. Guardando fração (0 a 1) da largura e da
    altura da janela, as mesmas medidas valem em qualquer resolução, desde
    que a proporção da tela seja parecida.
    """

    janela: tuple[int, int, int, int]          # x, y, largura, altura na medição
    pontos: dict[str, tuple[float, float]]     # frações 0..1 dentro da janela
    cor_fundo: tuple[int, int, int]

    @property
    def proporcao(self) -> float:
        _, _, largura, altura = self.janela
        return largura / altura if altura else 0.0

    @classmethod
    def de_absolutos(cls, janela: tuple[int, int, int, int],
                     absolutos: dict[str, tuple[int, int]],
                     cor_fundo: tuple[int, int, int]) -> Calibragem:
        x, y, largura, altura = janela
        return cls(
            janela=janela,
            pontos={nome: ((px - x) / largura, (py - y) / altura)
                    for nome, (px, py) in absolutos.items()},
            cor_fundo=cor_fundo,
        )

    def ponto(self, nome: str, janela_atual: tuple[int, int, int, int]
              ) -> tuple[int, int]:
        """Converte a proporção guardada em pixel na janela de agora."""
        fx, fy = self.pontos[nome]
        x, y, largura, altura = janela_atual
        return round(x + fx * largura), round(y + fy * altura)

    @classmethod
    def carregar(cls, caminho: Path) -> Calibragem:
        if not caminho.exists():
            raise CalibragemAusente(
                f"Falta calibrar o adapter cego: rode `cnd calibrar`.\n"
                f"(esperado em {caminho})"
            )
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        return cls(
            janela=tuple(dados["janela"]),
            pontos={k: tuple(v) for k, v in dados["pontos"].items()},
            cor_fundo=tuple(dados["cor_fundo"]),
        )

    def salvar(self, caminho: Path) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps({
            "janela": list(self.janela),
            "pontos": {k: [round(v[0], 5), round(v[1], 5)]
                       for k, v in self.pontos.items()},
            "cor_fundo": list(self.cor_fundo),
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    def conferir(
        self,
        janela_atual: tuple[int, int, int, int] | None = None,
        pontos_necessarios: tuple[str, ...] = PONTOS_NECESSARIOS,
    ) -> None:
        faltando = [p for p in pontos_necessarios if p not in self.pontos]
        if faltando:
            raise CalibragemAusente(
                f"Calibragem incompleta, faltam: {', '.join(faltando)}. "
                f"Rode `cnd calibrar` de novo."
            )

        for nome, (fx, fy) in self.pontos.items():
            if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
                raise CalibragemAusente(
                    f"O ponto '{nome}' ficou fora da janela do navegador "
                    f"({fx:.2f}, {fy:.2f}). Rode `cnd calibrar` de novo."
                )

        if janela_atual is None:
            return

        # Resolução diferente tudo bem — proporção diferente, não. Numa tela
        # muito mais larga ou mais estreita o site reorganiza o layout e as
        # frações deixam de apontar para os mesmos elementos.
        _, _, largura, altura = janela_atual
        atual = largura / altura if altura else 0.0
        if self.proporcao and abs(atual - self.proporcao) / self.proporcao > 0.08:
            raise CalibragemAusente(
                f"A janela mudou de proporção ({self.proporcao:.2f} para "
                f"{atual:.2f}). O site reorganiza o layout e as medidas não "
                f"valem mais — rode `cnd calibrar` nesta máquina."
            )


@dataclass
class AdapterRFBCego:
    orgao: str
    cfg: Config
    pasta_downloads: Path
    caminho_calibragem: Path
    # 5ª posição continua sendo a calibragem: os testes a passam sem nome.
    _calibragem: Calibragem | None = field(default=None, repr=False)
    emissoes_por_sessao: int = EMISSOES_POR_SESSAO
    _emissoes_na_sessao: int = field(default=0, repr=False)
    _ultimo_texto_portal: str | None = field(default=None, repr=False)
    # Ligado quando o portal recusou por cabeçalho grande: a próxima
    # reabertura de sessão precisa apagar os cookies do domínio, senão o
    # navegador volta com o mesmo cabeçalho e toma o mesmo 400.
    _cookies_estourados: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def preparar(self) -> None:
        self._calibragem = Calibragem.carregar(self.caminho_calibragem)
        self._calibragem.conferir()
        self._abrir_navegador()
        self._calibragem.conferir(self._janela())

    def _abrir_navegador(self) -> None:
        """Abre o Edge como uma pessoa abriria: só o executável e uma URL.

        Fecha o que estiver aberto antes: com o Edge já rodando, a opção
        `--start-maximized` é ignorada (ele só abre uma aba na janela
        existente) e a janela fica de qualquer tamanho — o que faz todos os
        cliques calibrados caírem no lugar errado.
        """
        self.encerrar()
        self._emissoes_na_sessao = 0
        subprocess.Popen([_achar_edge(), "--new-window", "--start-maximized",
                          URL_FORMULARIO])
        # Espera a janela existir, em vez de dormir um tempo fixo. Eram 9
        # segundos por abertura, e com uma abertura por item isso sozinho
        # respondia por quase 3 horas num lote de 2.800.
        if not self._esperar_janela():
            log.warning("janela_do_edge_nao_apareceu",
                        extra={"orgao": self.orgao,
                               "esperou_s": TEMPO_JANELA_ABRIR_S})
        self._posicionar_janela_calibrada()
        entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        log.info("navegador_aberto_sem_automacao",
                 extra={"orgao": self.orgao, "janela": self._janela()})

    def _esperar_janela(self, segundos: float = TEMPO_JANELA_ABRIR_S) -> bool:
        """Espera a janela DE VERDADE do Edge aparecer.

        O Edge cria janelinhas auxiliares em segundo plano (uma delas mede
        516x249). Exigir largura próxima da calibrada evita seguir em frente
        com o handle de uma delas e posicionar a janela errada.
        """
        largura_minima = self._calibragem.janela[2] * 0.6 if self._calibragem else 0
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            caixa = entrada_real.retangulo_janela(TITULO_JANELA,
                                                  EXECUTAVEL_NAVEGADOR)
            if caixa is not None and caixa[2] >= largura_minima:
                time.sleep(0.6)          # deixa ele terminar de desenhar
                return True
            time.sleep(0.3)
        return False

    def _posicionar_janela_calibrada(self) -> None:
        """Leva o Edge para o monitor/tamanho usados na calibragem."""
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

    def _exigir_foco(self) -> None:
        """Confere o foco imediatamente antes de digitar ou clicar.

        Entre um passo e outro alguém pode ter clicado em outra janela; sem
        esta checagem, o resto da sequência iria para o programa errado.
        """
        if (not entrada_real.em_primeiro_plano(TITULO_JANELA,
                                               EXECUTAVEL_NAVEGADOR)
                and not entrada_real.garantir_em_primeiro_plano(
                    TITULO_JANELA, EXECUTAVEL_NAVEGADOR)):
            raise JanelaOcupada(
                "o navegador perdeu o foco no meio da consulta — "
                "alguém mexeu no computador"
            )

    def _janela(self) -> tuple[int, int, int, int]:
        caixa = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        if caixa is None:
            raise RuntimeError("janela do Edge não encontrada")
        return caixa

    def _ponto(self, nome: str) -> tuple[int, int]:
        """Converte a proporção calibrada em pixel na janela de agora."""
        return self._calibragem.ponto(nome, self._janela())

    def reiniciar_sessao(self) -> None:
        """Sessão nova: mata o Edge e abre outra janela.

        Acontece a cada item, e não só depois de falha — ver
        `EMISSOES_POR_SESSAO`. Por isso não há espera fixa nenhuma aqui: o
        que precisa ser esperado (o processo morrer, a janela aparecer) é
        esperado pelo que aconteceu, não pelo relógio.
        """
        log.info("reiniciando_sessao", extra={"orgao": self.orgao})
        if self._cookies_estourados:
            self._limpar_cookies_do_portal()   # já mata o Edge
        self._abrir_navegador()                # mata de novo, se preciso

    def _limpar_cookies_do_portal(self) -> None:
        """Apaga do disco os cookies da Receita.

        Mata o Edge antes, e confere que ele morreu: com o processo vivo o
        banco de cookies não abre, e a primeira versão disto saiu com
        "removidos: 0" enquanto o portal seguia devolvendo 400 (15/08/2026).
        """
        self._cookies_estourados = False
        self._matar_edge()
        try:
            removidos = perfil_edge.limpar_cookies(DOMINIO_PORTAL)
        except Exception:
            log.exception("falha_ao_apagar_cookies", extra={"orgao": self.orgao})
            return
        if removidos:
            log.warning("cookies_do_portal_apagados",
                        extra={"orgao": self.orgao, "dominio": DOMINIO_PORTAL,
                               "removidos": removidos})
        else:
            # Sem isto, "não removi nada" e "não tinha nada para remover"
            # ficam indistinguíveis — e são problemas opostos.
            log.error("nenhum_cookie_removido",
                      extra={"orgao": self.orgao, "dominio": DOMINIO_PORTAL,
                             "dica": "conferir se o msedge.exe morreu e se o "
                                     "perfil é o do usuário que roda o robô"})

    def _recomecar_sem_cookies(self) -> None:
        """Sessão nova e limpa, no meio da tentativa.

        Vale interromper o item para fazer isto: enquanto o cabeçalho
        estiver grande, o portal responde 400 a TODAS as consultas — e cada
        uma gastaria 65 segundos para terminar sem resposta.
        """
        self._limpar_cookies_do_portal()      # já encerra o navegador
        self._abrir_navegador()

    def encerrar(self) -> None:
        self._matar_edge()

    def _matar_edge(self) -> bool:
        """Encerra o Edge e CONFERE que ele morreu.

        Esta função já fechou com educação (WM_CLOSE, sem `/F`), porque
        matar à força faz o Edge voltar com a bolha "Restaurar páginas" —
        que rouba o foco e cobre a tela do robô cego. Duas coisas mostraram
        que educação não bastava, e as duas custaram caro:

        - **Cookies.** O processo de rede sobrevive alguns segundos ao
          fechamento da janela e segura o banco; a limpeza saía com
          "unable to open database file" e removidos: 0 (15/08/2026).
        - **Sessão.** Com o processo vivo, abrir "outra" janela do Edge só
          cria uma aba na mesma instância — mesma sessão, mesmo 023. A
          renovação de sessão a cada item, que é o que evita o bloqueio,
          seria um teatro.

        A bolha volta a ser problema nosso, e é desfeita marcando a saída
        como limpa no perfil.
        """
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
        self._ultimo_texto_portal = None
        try:
            doc_consulta, aviso_matriz = rfb_matriz.documento_para_consulta(doc)
            documento = doc_consulta.documento

            reacao, caminho_baixado = self._submeter(documento)

            # 400 do nginx por cabeçalho grande. Cookie acumulado é problema
            # nosso e tem conserto imediato: limpa, reabre e refaz a consulta
            # na hora, em vez de devolver o item para a fila e esperar.
            if reacao == "cookies":
                log.warning("portal_recusou_por_cookie_grande",
                            extra={"orgao": self.orgao,
                                   "documento": documento,
                                   "texto": _mensagem_curta(
                                       self._ultimo_texto_portal or "")})
                self._recomecar_sem_cookies()
                reacao, caminho_baixado = self._submeter(documento)

            # O portal pediu a matriz. Não é bloqueio nem pendência: é ele
            # dizendo qual CNPJ digitar, e o número vem escrito na faixa.
            # Refazemos a consulta com o número dele — uma vez só.
            if reacao == "matriz":
                matriz = rfb_matriz.matriz_exigida_no_texto(
                    self._ultimo_texto_portal or ""
                )
                if matriz and matriz != documento:
                    log.info("portal_pediu_o_cnpj_da_matriz",
                             extra={"orgao": self.orgao, "digitado": documento,
                                    "matriz": matriz})
                    aviso_matriz = rfb_matriz.mensagem_de_matriz(
                        doc.documento, matriz)
                    documento = matriz
                    reacao, caminho_baixado = self._submeter(documento)

            # Insistiu, ou não deu para ler qual é a matriz: repetir de novo
            # só gastaria tentativa com a mesma resposta.
            if reacao == "matriz":
                return rfb_matriz.anotar_matriz(
                    ResultadoTentativa(
                        Desfecho.PENDENCIA_MANUAL,
                        mensagem_portal=(
                            _mensagem_curta(self._ultimo_texto_portal or "")
                            or "o portal exige emitir pelo CNPJ da matriz"
                        ),
                        evidencia=self._print(doc, "exige-matriz"),
                    ),
                    aviso_matriz,
                )

            # Limpar os cookies não resolveu. Vira erro técnico — que é
            # retentável — em vez de resposta sobre a empresa: o portal não
            # chegou a olhar o CNPJ.
            if reacao == "cookies":
                self._cookies_estourados = True
                return rfb_matriz.anotar_matriz(
                    ResultadoTentativa(
                        Desfecho.ERRO_TECNICO,
                        mensagem_portal=(
                            "o portal recusou a requisição por cabeçalho de "
                            "cookies grande, mesmo com a sessão limpa: "
                            + (_mensagem_curta(self._ultimo_texto_portal or "")
                               or "400 Bad Request")
                        )[:400],
                        evidencia=self._print(doc, "cookie-grande"),
                    ),
                    aviso_matriz,
                )

            if caminho_baixado is not None:
                return rfb_matriz.anotar_matriz(
                    self._ler_pdf(caminho_baixado, doc), aviso_matriz
                )

            return rfb_matriz.anotar_matriz(
                self._diagnosticar_falha(
                    doc, bloqueio_ja_visto=reacao == "bloqueio"
                ),
                aviso_matriz,
            )

        except Exception as erro:
            log.exception("falha_no_fluxo_cego", extra={"documento": doc.documento})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=f"{type(erro).__name__}: {erro}"[:400],
                evidencia=self._print(doc, "erro"),
            )

    # ------------------------------------------------------------------
    def _submeter(self, documento: str) -> tuple[str, Path | None]:
        """Um ciclo completo do formulário: digita o CNPJ, emite e espera.

        Separado do `emitir` porque o portal pode mandar refazer a consulta
        com outro número — o da matriz —, e refazer significa voltar ao
        formulário do zero, não reaproveitar a tela de resultado.
        """
        self._ultimo_texto_portal = None
        self._renovar_sessao_se_gasta()
        self._focar()
        entrada_real.ir_para_url(URL_FORMULARIO)
        if not self._esperar_formulario() and self._recusado_por_cookies():
            # Sem esta saída, o robô digitaria o CNPJ na página de erro do
            # nginx e esperaria 45 segundos por um PDF impossível — foi o que
            # transformou um lote inteiro em "não consegui ler a tela".
            return "cookies", None

        self._limpar_downloads_antigos(documento)

        # Digitar o CNPJ
        self._exigir_foco()
        entrada_real.clicar(*self._ponto("campo_cnpj"))
        time.sleep(random.uniform(0.2, 0.5))
        entrada_real.limpar_campo()
        entrada_real.digitar(documento)
        time.sleep(random.uniform(0.7, 1.8))     # confere o que digitou

        # Enviar
        self._exigir_foco()
        # Conta antes da resposta: recusada ou atendida, a sessão gastou uma.
        self._emissoes_na_sessao += 1
        entrada_real.clicar(*self._ponto("botao_emitir"))

        reacao, caminho_baixado = self._aguardar_reacao(documento)

        if reacao == "modal":
            # Regra de negócio: sempre emitir nova. A certidão vale 180
            # dias, mas quem recebe exige emissão do mês corrente.
            log.info("modal_certidao_vigente", extra={"orgao": self.orgao})
            time.sleep(random.uniform(*PAUSA_ANTES_EMITIR_NOVA_S))
            self._exigir_foco()
            entrada_real.clicar(*self._ponto("botao_emitir_nova"))
            # Depois da janelinha já sabemos que a emissão está em curso:
            # vale esperar mais, porque o processamento é assíncrono e o
            # download vem só no fim.
            reacao, caminho_baixado = self._aguardar_reacao(
                documento, aceitar_modal=False, segundos=TEMPO_PDF_S,
            )

        return reacao, caminho_baixado

    def _renovar_sessao_se_gasta(self) -> None:
        """Janela nova quando a atual já emitiu o que o portal tolera.

        É mais barato do que descobrir pelo 023: aquela consulta seria
        perdida de qualquer forma, e ainda pagaríamos a espera do micro-retry
        antes de fazer exatamente isto aqui.
        """
        if self._emissoes_na_sessao < self.emissoes_por_sessao:
            return
        log.info("sessao_gasta_reabrindo",
                 extra={"orgao": self.orgao,
                        "emissoes": self._emissoes_na_sessao,
                        "limite": self.emissoes_por_sessao})
        self.reiniciar_sessao()

    # ------------------------------------------------------------------
    def _focar(self) -> None:
        """Garante que o Edge está na frente e maximizado, ou desiste.

        Duas garantias, e as duas são obrigatórias:

        - **Foco de verdade.** Enquanto alguém usa o computador, o Windows
          recusa a troca de foco. Sem conferir, o robô digitaria o CNPJ e
          clicaria dentro de outro programa — foi o que aconteceu em
          07/08/2026: o print de evidência mostrou o editor de código, não o
          navegador. Melhor abortar a tentativa do que clicar às cegas.

        - **Tamanho.** A calibragem é proporcional à janela; se ela encolher
          no meio do lote, os cliques passam a cair fora dos alvos.
        """
        if entrada_real.achar_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR) is None:
            log.warning("janela_do_edge_nao_encontrada_reabrindo")
            self._abrir_navegador()

        if not entrada_real.garantir_em_primeiro_plano(TITULO_JANELA,
                                                       EXECUTAVEL_NAVEGADOR):
            raise JanelaOcupada(
                "não consegui trazer o navegador para a frente — alguém está "
                "usando o computador. O robô cego precisa da tela só para ele."
            )

        atual = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        esperada = self._calibragem.janela
        if atual and self._janela_desalinhada(atual, esperada):
            log.warning("janela_fora_do_tamanho_remaximizando",
                        extra={"agora": atual, "calibrada": esperada})
            self._posicionar_janela_calibrada()
            entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
            time.sleep(1.0)
            entrada_real.garantir_em_primeiro_plano(TITULO_JANELA,
                                                    EXECUTAVEL_NAVEGADOR)

    def _esperar_formulario(self, segundos: float = TEMPO_FORMULARIO_S) -> bool:
        """Espera o formulário aparecer, em vez de dormir um tempo fixo.

        Antes havia uma espera cega de ~7 segundos depois de digitar a URL.
        A página quase sempre carrega bem antes disso, e o resto era tempo
        morto — multiplicado por 10 mil consultas, mais de 10 horas jogadas
        fora no lote inteiro.

        A prontidão é medida na própria tela: o campo de CNPJ branco e o
        botão de emitir azul, os dois renderizados nos lugares calibrados.
        """
        inicio = time.monotonic()
        limite = inicio + segundos
        campo = botao = None
        while time.monotonic() < limite:
            imagem = tela.capturar()
            campo = tela.cor_media(imagem, *self._ponto("campo_cnpj"), raio=5)
            botao = tela.cor_media(imagem, *self._ponto("botao_emitir"), raio=5)
            if tela.brilho(campo) > BRILHO_DO_CAMPO and _parece_botao(botao):
                log.info("formulario_pronto",
                         extra={"orgao": self.orgao,
                                "em_s": round(time.monotonic() - inicio, 2)})
                time.sleep(random.uniform(0.15, 0.45))   # respiro humano
                return True
            time.sleep(0.1)

        # As cores medidas vão no log de propósito. Quando isto dispara em
        # TODOS os itens e a emissão funciona logo depois, o formulário
        # estava lá e quem errou foi o critério — sem os números medidos,
        # não há como saber QUAL dos dois critérios falhou nem por quanto.
        log.warning("formulario_nao_apareceu",
                    extra={"orgao": self.orgao, "esperou_s": segundos,
                           "campo": campo, "brilho_do_campo": (
                               round(tela.brilho(campo), 1) if campo else None),
                           "minimo_esperado": BRILHO_DO_CAMPO,
                           "botao": botao,
                           "botao_passou": _parece_botao(botao) if botao
                           else None})
        return False

    def _aguardar_reacao(self, documento: str, aceitar_modal: bool = True,
                         segundos: float = TEMPO_REACAO_S
                         ) -> tuple[str, Path | None]:
        """Espera o que vier primeiro depois do clique em emitir.

        Três coisas podem acontecer, e não dá para esperá-las em fila. O
        portal leva cerca de 12 segundos para responder: a versão anterior
        deste código dava 4 segundos para a janelinha aparecer, desistia e
        ia esperar um PDF que nunca viria — enquanto a janelinha abria no
        segundo 10 e ficava lá, parada. Era o "não clica em nova certidão".

        Olhar as três ao mesmo tempo também evita o desperdício oposto:
        empresas sem certidão vigente não pagam a espera da janelinha.

        Devolve ('modal' | 'pdf' | 'bloqueio' | 'matriz' | 'texto' | 'nada',
        caminho_do_pdf).
        """
        inicio = time.monotonic()
        limite = inicio + segundos
        proximo_foco = 0.0
        proxima_leitura_texto = inicio + TEMPO_PRIMEIRA_LEITURA_TEXTO_S

        while time.monotonic() < limite:
            agora = time.monotonic()

            # 1. O PDF é o sinal mais confiável de sucesso.
            baixado = self._pdf_pronto(documento)
            if baixado is not None:
                log.info("pdf_baixado", extra={"orgao": self.orgao,
                                               "em_s": round(agora - inicio, 1)})
                return "pdf", baixado

            if agora >= proximo_foco:
                self._exigir_foco()
                proximo_foco = agora + 3.0

            imagem = tela.capturar()

            # 2. Janelinha de certidão vigente: véu escuro sobre a página.
            if aceitar_modal:
                cores = [tela.cor_media(imagem, x, y, raio=10)
                         for x, y in self._pontos_do_modal()]
                if _tem_veu_modal(cores, self._calibragem.cor_fundo):
                    log.info("modal_detectado",
                             extra={"orgao": self.orgao,
                                    "em_s": round(agora - inicio, 1)})
                    return "modal", None

            # 3. Faixa de aviso no topo: o portal nos barrou — ou está
            #    pedindo o CNPJ da matriz. As duas faixas são amarelas e a
            #    cor não as separa; só o texto separa. Ler antes de concluir
            #    evita punir o ritmo por uma instrução de negócio.
            tipo, cor_faixa = self._alerta_na_imagem(imagem)
            if tipo:
                texto = self._texto_da_pagina()
                if _classificar_texto_portal(texto) == "matriz":
                    self._ultimo_texto_portal = texto
                    log.info("faixa_pede_o_cnpj_da_matriz",
                             extra={"orgao": self.orgao,
                                    "em_s": round(agora - inicio, 1),
                                    "texto": _mensagem_curta(texto)})
                    return "matriz", None
                log.warning("faixa_de_alerta_detectada",
                            extra={"orgao": self.orgao, "cor": cor_faixa,
                                   "tipo": tipo,
                                   "em_s": round(agora - inicio, 1)})
                return "bloqueio", None

            if agora >= proxima_leitura_texto:
                texto = self._texto_da_pagina()
                tipo_texto = _classificar_texto_portal(texto)
                if tipo_texto:
                    self._ultimo_texto_portal = texto
                    log.warning("texto_do_portal_detectado",
                                extra={"orgao": self.orgao,
                                       "tipo": tipo_texto,
                                       "em_s": round(agora - inicio, 1),
                                       "texto": _mensagem_curta(texto)})
                    if tipo_texto in ("matriz", "cookies"):
                        return tipo_texto, None
                    return "texto", None
                proxima_leitura_texto = agora + INTERVALO_LEITURA_TEXTO_S

            time.sleep(INTERVALO_MODAL_S)

        log.warning("sem_reacao_do_portal",
                    extra={"orgao": self.orgao, "esperou_s": segundos})
        return "nada", None

    def _pdf_pronto(self, documento: str) -> Path | None:
        """PDF já terminado de baixar, ou None."""
        for arquivo in self.pasta_downloads.glob(f"Certidao-{documento}*.pdf"):
            try:
                tamanho = arquivo.stat().st_size
            except OSError:
                continue
            if tamanho <= 0:
                continue
            time.sleep(0.4)
            try:
                if arquivo.stat().st_size == tamanho:
                    return arquivo
            except OSError:
                continue
        return None

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

    def _limpar_downloads_antigos(self, documento: str) -> None:
        """Um PDF da tentativa anterior faria o robô achar que deu certo."""
        for antigo in self.pasta_downloads.glob(f"Certidao-{documento}*.pdf"):
            with contextlib.suppress(OSError):
                antigo.unlink()

    def _alerta_na_imagem(self, imagem) -> tuple[str | None,
                                                 tuple[int, int, int] | None]:
        """Procura a faixa de erro/aviso do portal na regiao visivel."""
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

    def _recusado_por_cookies(self) -> bool:
        """A tela é a página de erro do nginx, e não o formulário?

        Só é chamada quando o formulário não apareceu, porque ler a página
        custa um Ctrl+A/Ctrl+C — e o formulário aparecendo é o caso comum.
        """
        texto = self._texto_da_pagina()
        if _classificar_texto_portal(texto) != "cookies":
            return False
        self._ultimo_texto_portal = texto
        self._cookies_estourados = True
        return True

    def _texto_da_pagina(self) -> str:
        """Seleciona/copia a pagina para ler mensagens sem automacao do browser."""
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
        """Tira o azul do Ctrl+A para o proximo clique cair numa tela limpa."""
        with contextlib.suppress(Exception):
            entrada_real.tecla(entrada_real.VK_ESCAPE)
            time.sleep(0.03)
            entrada_real.tecla(entrada_real.VK_RIGHT)

    def _resultado_por_texto(
        self, doc: Documento, texto: str, evidencia: Path | None
    ) -> ResultadoTentativa | None:
        tipo = _classificar_texto_portal(texto)
        if tipo == "cookies":
            # Erro técnico, não resposta do órgão: o portal recusou a
            # requisição antes de olhar o CNPJ. A sessão seguinte já nasce
            # sem os cookies que estouraram o cabeçalho.
            self._cookies_estourados = True
            log.warning("cookie_grande_detectado_no_diagnostico",
                        extra={"documento": doc.documento,
                               "texto": _mensagem_curta(texto)})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=(
                    "o portal recusou a requisição por cabeçalho de cookies "
                    "grande; a sessão será limpa antes da próxima tentativa"
                ),
                evidencia=evidencia,
            )
        if tipo == "bloqueio":
            mensagem = _mensagem_curta(texto) or (
                "portal pediu para tentar novamente em alguns minutos"
            )
            log.warning("bloqueio_detectado_por_texto",
                        extra={"documento": doc.documento,
                               "mensagem": mensagem[:250]})
            return ResultadoTentativa(
                Desfecho.BLOQUEIO_TEMPORARIO,
                mensagem_portal=mensagem,
                evidencia=evidencia,
            )
        if tipo == "matriz":
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=_mensagem_curta(texto) or (
                    "o portal exige emitir pelo CNPJ da matriz"
                ),
                evidencia=evidencia,
            )
        if tipo == "insuficiente":
            # "As informações disponíveis ... são insuficientes para emitir a
            # certidão pela Internet" é como o portal recusa quem tem débito:
            # é POSITIVA, e não pendência de cadastro. A diferença importa na
            # entrega — positiva é o resultado que IMPEDE mandar ao cliente,
            # enquanto pendência manual só pede que alguém vá ao e-CAC.
            return ResultadoTentativa(
                Desfecho.POSITIVA,
                mensagem_portal=_mensagem_curta(texto) or (
                    "informações insuficientes para emitir a certidão pela internet"
                ),
                evidencia=evidencia,
            )
        if tipo == "inapta":
            return ResultadoTentativa(
                Desfecho.INAPTA,
                mensagem_portal=_mensagem_curta(texto) or (
                    "CNPJ inapto por omissão de declarações; emissão de "
                    "certidão não permitida"
                ),
                evidencia=evidencia,
            )
        if tipo == "retentar":
            return ResultadoTentativa(
                Desfecho.RESULTADO_PENDENTE,
                mensagem_portal=_mensagem_curta(texto) or (
                    "portal ainda processando a emissao; tentar novamente depois"
                ),
                evidencia=evidencia,
            )
        return None

    def _voltar_para_topo(self) -> None:
        """O erro 106 pode rolar a pagina; a faixa sai dos pontos medidos."""
        self._exigir_foco()
        entrada_real.atalho(entrada_real.VK_CONTROL, entrada_real.VK_HOME)
        time.sleep(0.35)

    def _resultado_bloqueio(self, doc: Documento, tipo: str | None,
                            cor: tuple[int, int, int] | None,
                            evidencia: Path | None,
                            presumido: bool = False) -> ResultadoTentativa:
        if presumido:
            mensagem = (
                "faixa de alerta detectada durante a espera, mas a pagina "
                "saiu da posicao antes da evidencia; sessao sera reiniciada"
            )
        else:
            mensagem = f"faixa de {tipo} no topo da pagina (cor {cor})"
        log.warning("bloqueio_detectado_por_cor",
                    extra={"documento": doc.documento, "tipo": tipo,
                           "cor": cor, "presumido": presumido})
        return ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal=mensagem,
            evidencia=evidencia,
        )

    def _diagnosticar_falha(self, doc: Documento,
                            bloqueio_ja_visto: bool = False
                            ) -> ResultadoTentativa:
        """Sem PDF. A cor da faixa no topo diz se foi bloqueio do portal."""
        try:
            self._exigir_foco()
        except JanelaOcupada as erro:
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=str(erro),
                evidencia=self._print(doc, "janela-sem-foco"),
            )

        imagem = tela.capturar()
        tipo, cor = self._alerta_na_imagem(imagem)
        if not tipo:
            self._voltar_para_topo()
            imagem_topo = tela.capturar()
            tipo, cor = self._alerta_na_imagem(imagem_topo)
            if tipo:
                imagem = imagem_topo
        evidencia = self._print(doc, "sem-pdf", imagem)

        if tipo:
            return self._resultado_bloqueio(doc, tipo, cor, evidencia)

        texto = self._ultimo_texto_portal or self._texto_da_pagina()
        if resultado := self._resultado_por_texto(doc, texto, evidencia):
            return resultado

        if bloqueio_ja_visto:
            return self._resultado_bloqueio(
                doc, tipo, cor, evidencia, presumido=True)

        # Sem PDF, sem faixa e sem texto legível: o robô não sabe o que a
        # tela mostrava. Não vira POSITIVA — dizer que um cliente tem débito
        # com base em chute é o erro mais caro que este programa pode
        # cometer — nem conclui como pendência manual.
        #
        # Era PENDENCIA_MANUAL até 15/08/2026, sob o argumento de que
        # insistir repetiria a mesma tela. O lote daquele dia mostrou o
        # contrário: dezenas de itens seguidos caíram aqui porque o portal
        # devolvia 400 do nginx, e uma tentativa com sessão nova resolvia
        # todos. Como erro técnico, o item volta para a fila (5s, 30s, 120s)
        # e a sessão é reiniciada entre as tentativas; se as três falharem,
        # aí sim ele aparece no painel para alguém olhar.
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=(
                "sem PDF e sem faixa de alerta; não foi possível ler a tela "
                "do portal para classificar"
            ),
            evidencia=evidencia,
        )

    def _ler_pdf(self, baixado: Path, doc: Documento) -> ResultadoTentativa:
        """Move o PDF para o acervo e classifica pelo conteúdo.

        Reaproveita a leitura já validada contra um PDF real da Receita.
        """
        destino = caminho_certidao(self.cfg.pasta_certidoes, doc.lote_id,
                                   self.orgao, doc.documento, nome=doc.nome)
        shutil.move(str(baixado), str(destino))
        return ler_pdf(destino, "PDF baixado (adapter cego)")

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


def _edge_rodando() -> bool:
    """Ainda há msedge.exe vivo? Inclui os processos sem janela."""
    saida = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {EXECUTAVEL_NAVEGADOR}", "/NH"],
        capture_output=True, text=True, check=False,
    )
    return EXECUTAVEL_NAVEGADOR in (saida.stdout or "").lower()


def _achar_edge() -> str:
    candidatos = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for caminho in candidatos:
        if Path(caminho).exists():
            return caminho
    raise RuntimeError("Não achei o msedge.exe nos caminhos padrão do Windows")


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterRFBCego:
    from cnd.infra.db import RAIZ_PROJETO

    pasta = orgao.extras.get("pasta_downloads")
    return AdapterRFBCego(
        orgao=orgao.codigo,
        cfg=cfg,
        pasta_downloads=Path(pasta) if pasta else (Path.home() / "Downloads"),
        # A calibragem pertence ao ADAPTER (é o layout da tela dele), não ao
        # órgão: o mesmo órgão pode trocar de adapter sem perder as medidas.
        caminho_calibragem=RAIZ_PROJETO / "data" / "calibragem" /
                           f"{orgao.adapter}.json",
        emissoes_por_sessao=max(
            1, int(orgao.extras.get("emissoes_por_sessao", EMISSOES_POR_SESSAO))
        ),
    )
