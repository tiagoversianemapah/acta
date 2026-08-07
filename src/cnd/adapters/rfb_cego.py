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
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra import entrada_real, tela
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.rfb_cego")

URL_FORMULARIO = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj"

# A janela é localizada pelo PROGRAMA, não pelo título: o título de um
# navegador muda a cada página ("Serviços da Receita Federal" na home,
# "Certidão de Regularidade Fiscal" no formulário, "Resultado da Emissão"
# depois), e procurar por ele quebra no meio do fluxo.
EXECUTAVEL_NAVEGADOR = "msedge.exe"
TITULO_JANELA = "Receita"          # só desempate, quando há várias janelas

PONTOS_NECESSARIOS = ("campo_cnpj", "botao_emitir", "botao_emitir_nova",
                      "fundo_pagina", "faixa_alerta")

TEMPO_CARREGAR_S = 6.0
# O portal leva cerca de 12s para responder ao clique em emitir. Esperar
# menos que isso fazia o robô desistir antes de a janelinha aparecer.
TEMPO_REACAO_S = 45.0
INTERVALO_MODAL_S = 0.15
PAUSA_ANTES_EMITIR_NOVA_S = (0.20, 0.35)
TEMPO_PDF_S = 70.0

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

    def conferir(self, janela_atual: tuple[int, int, int, int] | None = None) -> None:
        faltando = [p for p in PONTOS_NECESSARIOS if p not in self.pontos]
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
    _calibragem: Calibragem | None = field(default=None, repr=False)

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
        subprocess.Popen([_achar_edge(), "--start-maximized", URL_FORMULARIO])
        time.sleep(TEMPO_CARREGAR_S + 3)
        entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        log.info("navegador_aberto_sem_automacao",
                 extra={"orgao": self.orgao, "janela": self._janela()})

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
        """Fecha e reabre o navegador (Alt+F4 na janela, depois abre de novo)."""
        log.info("reiniciando_sessao", extra={"orgao": self.orgao})
        self.encerrar()
        time.sleep(2)
        self._abrir_navegador()

    def encerrar(self) -> None:
        # Fecha com educação (WM_CLOSE), não com taskkill /F: matar à força
        # marca o perfil como travado e o Edge volta com a bolha "Restaurar
        # páginas", que rouba o foco e cobre a tela.
        entrada_real.fechar_janelas(EXECUTAVEL_NAVEGADOR)
        subprocess.run(["taskkill", "/IM", "msedge.exe"],
                       capture_output=True, check=False)
        time.sleep(1.5)

    # ------------------------------------------------------------------
    # Fluxo
    # ------------------------------------------------------------------
    def emitir(self, doc: Documento) -> ResultadoTentativa:
        try:
            self._focar()
            entrada_real.ir_para_url(URL_FORMULARIO)
            self._esperar_formulario()

            self._limpar_downloads_antigos(doc.documento)

            # Digitar o CNPJ
            self._exigir_foco()
            entrada_real.clicar(*self._ponto("campo_cnpj"))
            time.sleep(random.uniform(0.2, 0.5))
            entrada_real.limpar_campo()
            entrada_real.digitar(doc.documento)
            time.sleep(random.uniform(0.7, 1.8))     # confere o que digitou

            # Enviar
            self._exigir_foco()
            entrada_real.clicar(*self._ponto("botao_emitir"))

            reacao, caminho_baixado = self._aguardar_reacao(doc.documento)

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
                    doc.documento, aceitar_modal=False, segundos=TEMPO_PDF_S)

            if caminho_baixado is not None:
                return self._ler_pdf(caminho_baixado, doc)

            return self._diagnosticar_falha(doc)

        except Exception as erro:
            log.exception("falha_no_fluxo_cego", extra={"documento": doc.documento})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=f"{type(erro).__name__}: {erro}"[:400],
                evidencia=self._print(doc, "erro"),
            )

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
        if atual and abs(atual[2] - esperada[2]) > 40:
            log.warning("janela_fora_do_tamanho_remaximizando",
                        extra={"agora": atual, "calibrada": esperada})
            entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
            time.sleep(1.0)
            entrada_real.garantir_em_primeiro_plano(TITULO_JANELA,
                                                    EXECUTAVEL_NAVEGADOR)

    def _esperar_formulario(self, segundos: float = TEMPO_CARREGAR_S * 4) -> bool:
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
        while time.monotonic() < limite:
            imagem = tela.capturar()
            campo = tela.cor_media(imagem, *self._ponto("campo_cnpj"), raio=5)
            botao = tela.cor_media(imagem, *self._ponto("botao_emitir"), raio=5)
            if tela.brilho(campo) > 200 and _parece_botao(botao):
                log.info("formulario_pronto",
                         extra={"orgao": self.orgao,
                                "em_s": round(time.monotonic() - inicio, 2)})
                time.sleep(random.uniform(0.15, 0.45))   # respiro humano
                return True
            time.sleep(0.1)

        log.warning("formulario_nao_apareceu",
                    extra={"orgao": self.orgao, "esperou_s": segundos})
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

        Devolve ('modal' | 'pdf' | 'bloqueio' | 'nada', caminho_do_pdf).
        """
        inicio = time.monotonic()
        limite = inicio + segundos
        proximo_foco = 0.0

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

            # 3. Faixa de aviso no topo: o portal nos barrou.
            cor_faixa = tela.cor_media(imagem, *self._ponto("faixa_alerta"), raio=10)
            if tela.parece_alerta(cor_faixa):
                log.warning("faixa_de_alerta_detectada",
                            extra={"orgao": self.orgao, "cor": cor_faixa,
                                   "em_s": round(agora - inicio, 1)})
                return "bloqueio", None

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

    def _limpar_downloads_antigos(self, documento: str) -> None:
        """Um PDF da tentativa anterior faria o robô achar que deu certo."""
        for antigo in self.pasta_downloads.glob(f"Certidao-{documento}*.pdf"):
            with contextlib.suppress(OSError):
                antigo.unlink()

    def _diagnosticar_falha(self, doc: Documento) -> ResultadoTentativa:
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
        cor = tela.cor_media(imagem, *self._ponto("faixa_alerta"), raio=10)
        tipo = tela.parece_alerta(cor)
        evidencia = self._print(doc, "sem-pdf", imagem)

        if tipo:
            log.warning("bloqueio_detectado_por_cor",
                        extra={"documento": doc.documento, "tipo": tipo, "cor": cor})
            return ResultadoTentativa(
                Desfecho.BLOQUEIO_TEMPORARIO,
                mensagem_portal=f"faixa de {tipo} no topo da página (cor {cor})",
                evidencia=evidencia,
            )

        # Sem faixa e sem PDF: pode ser "informações insuficientes", pode ser
        # outra coisa. Cego, não dá para afirmar — quem olha o print decide.
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal="não veio PDF e não há faixa de aviso — ver evidência",
            evidencia=evidencia,
        )

    def _ler_pdf(self, baixado: Path, doc: Documento) -> ResultadoTentativa:
        """Move o PDF para o acervo e classifica pelo conteúdo.

        Reaproveita a leitura já validada contra um PDF real da Receita.
        """
        from cnd.adapters.rfb_pj import _ler_pdf

        destino = caminho_certidao(self.cfg.pasta_certidoes, doc.lote_id,
                                   self.orgao, doc.documento, nome=doc.nome)
        shutil.move(str(baixado), str(destino))
        return _ler_pdf(destino, "PDF baixado (adapter cego)")

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
    )
