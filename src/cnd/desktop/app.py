"""Aplicativo de mesa do robô de certidões — Mapah.

Por que aplicativo e não página web: o robô cego precisa da tela só para
ele, e qualquer janela na frente atrapalha o clique. Um aplicativo pode se
minimizar sozinho ao começar o trabalho; um navegador aberto na mesma
máquina, não.

A interface não fala com o banco nem com o robô diretamente: tudo passa
por `estado.py`, que não conhece widget nenhum. Assim a lógica de "o que
mostrar" é testável sem abrir janela.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import TclError, filedialog, messagebox, simpledialog, ttk
from typing import ClassVar

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from cnd.core import tempo
from cnd.core.documentos import formatar
from cnd.desktop import acesso, marca, remoto
from cnd.desktop.estado import Robo, ler_panorama, listar_itens
from cnd.infra.config import carregar, nome_do_orgao
from cnd.infra.db import RAIZ_PROJETO
from cnd.infra.db import garantir as garantir_banco
from cnd.web.consultas import eta_horas
from cnd.web.relatorio import mes_corrente as relatorio_mes_corrente

ctk.set_appearance_mode("light")

def _versao() -> str:
    """A versão instalada. No executável empacotado o metadado não existe."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("cnd")
    except PackageNotFoundError:
        return "1.1.0"


FONTE = "Segoe UI"
VERSAO = _versao()
INTERVALO_ATUALIZACAO_MS = 2000
ID_DO_APLICATIVO = "Mapah.Acta.Certidoes"
# A tabela nativa aguenta a carteira inteira sem engasgar; o teto existe só
# para uma busca vazia não puxar o banco todo de uma vez.
LIMITE_DE_ITENS = 3000
# A cada quantos ciclos de 2s a tela de máquinas vai à rede de novo.
CICLOS_ENTRE_CONSULTAS_DE_REDE = 5
# Abaixo disso o disco é apertado o bastante para virar vermelho na tela.
LIMITE_DISCO_GB = 5.0
TODOS_OS_ORGAOS = "Todos os órgãos"

# Glifos do Segoe Fluent, escolhidos olhando: renderizei os candidatos numa
# folha e conferi o desenho de cada um antes de fixar o codigo.
ICONE_OK = "\ue930"          # circulo com check
ICONE_VAZIO = "\uea3a"       # circulo vazio: "nao da para saber"
ICONE_ALERTA = "\ue7ba"      # triangulo de atencao
ICONE_DISCO = "\ueda2"
ICONE_ABRIR_FORA = "\ue8a7"
ICONE_ATUALIZAR = "\ue72c"
TODOS_OS_MESES = "Todos os meses"
# Margem sobre o tamanho estimado do lote. Certidão que sai do portal e não
# encontra espaço é consulta gasta e documento perdido — vale pedir dobro.
FOLGA_DE_DISCO = 2.0

# O que a operação pergunta: a empresa está limpa ou não. Os nomes internos
# (CPEN, PENDENCIA_MANUAL) ficam no banco; na tela, português.
RESULTADOS = {
    "Todos os resultados": None,
    "Negativa": "NEGATIVA",
    "Com efeito de negativa": "CPEN",
    "Positiva": "POSITIVA",
    "Exige atendimento": "PENDENCIA_MANUAL",
    "Já emitida no mês": "APROVEITADA",
    "Portal recusou": "BLOQUEIO_TEMPORARIO",
    "Exigiu captcha": "CAPTCHA",
    "Erro técnico": "ERRO_TECNICO",
}

SITUACOES = {
    "Todas as situações": None,
    "Concluído": "DONE",
    "Falhou": "FAILED",
    "Na fila": "PENDING",
    "Processando": "RUNNING",
}

ROTULOS_DE_RESULTADO = {
    "NEGATIVA": ("Negativa", "#1B7F4E"),
    "CPEN": ("Com efeito de negativa", "#2B2A6B"),
    "POSITIVA": ("Positiva", "#8A5D00"),
    "PENDENCIA_MANUAL": ("Exige atendimento", "#8A5D00"),
    "APROVEITADA": ("Já emitida no mês", "#8A94A2"),
    "BLOQUEIO_TEMPORARIO": ("Portal recusou", "#B02A1C"),
    "CAPTCHA": ("Exigiu captcha", "#B02A1C"),
    "ERRO_TECNICO": ("Erro técnico", "#B02A1C"),
}

SITUACAO_SEM_RESULTADO = {
    "PENDING": "Na fila",
    "RUNNING": "Processando",
    "RETRY_WAIT": "Vai tentar de novo",
    "FAILED": "Falhou",
}


# Ícones do próprio Windows. Se a fonte não existir (Windows mais antigo),
# ICONES fica vazio e o menu mostra só o texto — glifo que vira quadradinho
# é pior que glifo nenhum.
FONTES_DE_ICONE = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICONES = ""


def _fonte_de_icone() -> str:
    """A primeira fonte de ícones instalada, ou vazio se não houver."""
    with contextlib.suppress(Exception):
        from tkinter import font as fontes

        disponiveis = set(fontes.families())
        return next((f for f in FONTES_DE_ICONE if f in disponiveis), "")
    return ""


MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro")


def _mes_por_extenso(mes: str) -> str:
    """'2026-08' vira 'agosto/2026'. Ninguém lê data no formato do banco."""
    try:
        ano, numero = mes.split("-")
        return f"{MESES[int(numero) - 1]}/{ano}"
    except (ValueError, IndexError):
        return mes


def _mes_do_rotulo(rotulo: str) -> str:
    """O caminho de volta, de 'agosto/2026' para '2026-08'."""
    try:
        nome, ano = rotulo.split("/")
        return f"{ano}-{MESES.index(nome) + 1:02d}"
    except ValueError:
        return rotulo


def _numero(valor: int) -> str:
    return f"{valor:,}".replace(",", ".")


def _resumo_da_maquina(estado) -> str:
    """A linha que responde 'como vai' — quanto, quão rápido e até quando."""
    partes = [f"{_numero(estado.concluidos)} de {_numero(estado.total)}"]

    por_hora = sum(o.get("por_hora") or 0 for o in estado.orgaos)
    if por_hora:
        partes.append(f"{_numero(round(por_hora))} por hora")

    # A previsão é a pergunta que mais cobram, e o cálculo já existia no
    # sistema sem nunca chegar à tela.
    horas = [o["eta_horas"] for o in estado.orgaos if o.get("eta_horas")]
    if horas:
        partes.append(f"faltam {_duracao(max(horas))}")

    partes.append(f"{_numero(estado.por_desfecho('NEGATIVA'))} negativas")
    partes.append(f"{_numero(estado.por_desfecho('CPEN'))} com efeito de negativa")
    if estado.falhados:
        partes.append(f"{_numero(estado.falhados)} exigem atendimento")
    return "   ·   ".join(partes)


def _detalhe_da_situacao(estado) -> str:
    """A segunda linha da situação — sempre com HÁ QUANTO TEMPO.

    Sem duração, "parada" e "sem resposta" não dizem se é o intervalo entre
    dois lotes ou um incidente de quatro horas que ninguém viu.
    """
    if not estado.online:
        return f"{estado.erro or 'sem resposta'} · último dado desconhecido"

    if estado.suspensa:
        return "o portal recusou várias consultas · retoma sozinho"

    parado_ha = estado.dados.get("ultimo_sinal_ha_s")
    if estado.robo_ativo:
        partes = [f"{_numero(estado.pendentes)} na fila"] if estado.pendentes \
            else []
        horas = [o["eta_horas"] for o in estado.orgaos if o.get("eta_horas")]
        if horas:
            partes.append(f"faltam {_duracao(max(horas))}")
        return "   ·   ".join(partes) or "trabalhando"

    if estado.pendentes:
        quando = (f" · sem sinal há {_duracao(parado_ha / 3600)}"
                  if parado_ha else "")
        return f"{_numero(estado.pendentes)} itens esperando{quando}"
    return "sem trabalho na fila"


def _preparo(estado) -> list[tuple[bool | None, str]]:
    """O checklist do que o robô precisa naquela máquina.

    `None` é "não dá para saber daqui" — é o caso da máquina muda, e mentir
    um ✓ ou um ! ali seria pior que admitir a ignorância.
    """
    if not estado.online:
        return [(None, "Calibração"), (None, "AnyDesk"), (None, "Painel"),
                (None, "Órgão")]

    calibragem = estado.dados.get("calibragem") or {}
    itens: list[tuple[bool | None, str]] = []
    if calibragem.get("pontos"):
        itens.append((True, f"Calibrada em {calibragem.get('quando', '—')}, "
                            f"{calibragem['pontos']} pontos"))
    elif estado.roda_robo:
        itens.append((False, "Nunca calibrada — o robô não roda"))
    else:
        itens.append((None, "Calibração não se aplica"))

    itens.append((True, "AnyDesk cadastrado") if estado.acessavel
                 else (False, "Sem AnyDesk cadastrado"))
    itens.append((True, "Painel respondendo"))

    rotulos = estado.rotulo_do_orgao
    itens.append((True, f"Órgão {rotulos}") if rotulos
                 else (False, "Nenhum órgão com trabalho"))
    return itens


def _linhas_de_disco(estado) -> list[str]:
    saude = estado.saude
    livre, total = saude["disco_livre_gb"], saude["disco_total_gb"]
    linhas = [f"{livre:.0f} GB livres de {total:.0f} GB"]

    certidoes = estado.dados.get("certidoes") or {}
    if certidoes.get("arquivos"):
        linhas.append(f"certidões: {certidoes['gb']:.1f} GB em "
                      f"{_numero(certidoes['arquivos'])} arquivos"
                      .replace(".", ",", 1))

    # A conta que evita o prejuízo: emitir a certidão e não ter onde salvar.
    if estado.pendentes and certidoes.get("media_kb"):
        precisa_mb = estado.pendentes * certidoes["media_kb"] / 1024
        cabe = (livre * 1024) > precisa_mb * FOLGA_DE_DISCO
        linhas.append(f"próximo lote precisa de ~{precisa_mb:.0f} MB — "
                      + ("cabe" if cabe else "NÃO CABE"))
    return linhas


def _ritmo_do_panorama(panorama) -> str:
    por_hora = sum(r.ritmo_por_hora or 0 for r in panorama.resumos)
    return f"{_numero(round(por_hora))} por hora" if por_hora else ""


def _veredito(panorama, rodando: bool) -> tuple[str, str]:
    """A frase que responde 'posso ir embora?'.

    Junta o que estava espalhado — se está de pé, a que velocidade e até
    quando — porque é assim que a pergunta é feita. Ordem de prioridade:
    o que impede o trabalho vem antes do que apenas o descreve.
    """
    suspensos = [r for r in panorama.resumos if r.breaker_estado == "ABERTO"]
    if suspensos:
        nomes = ", ".join(nome_do_orgao(r.orgao) for r in suspensos)
        return (f"{nomes} suspenso — o portal recusou várias consultas. "
                f"Retoma sozinho.", "vermelho")

    if not rodando and not panorama.robo_ativo:
        na_fila = panorama.total - panorama.concluidos - panorama.falhados
        if na_fila > 0:
            return (f"Robô parado com {_numero(na_fila)} itens na fila", "vermelho")
        return ("Robô parado · nada na fila", "cinza")

    partes = ["Trabalhando normal"]
    if ritmo := _ritmo_do_panorama(panorama):
        partes.append(ritmo)
    horas = [h for h in (eta_horas(r) for r in panorama.resumos) if h]
    if horas:
        partes.append(f"faltam {_duracao(max(horas))}")
    return ("   ·   ".join(partes), "verde")


def _duracao(horas: float) -> str:
    if horas < 1:
        return f"{round(horas * 60)} min"
    if horas < 24:
        return f"{horas:.0f}h".replace(".", ",")
    return f"{horas / 24:.0f} dias"


def _registrar_no_windows() -> None:
    """Faz o Windows tratar isto como aplicativo próprio.

    Sem isto, a barra de tarefas mostra o ícone do Python e agrupa a janela
    com qualquer outro script — porque, para o sistema, quem está rodando é
    o interpretador. Declarar uma identidade própria resolve.
    """
    with contextlib.suppress(Exception):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ID_DO_APLICATIVO)


class Aplicativo(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=marca.FUNDO)
        global ICONES
        ICONES = _fonte_de_icone()      # só dá para perguntar com o Tk de pé
        self.cfg = carregar()
        self.robo = Robo(RAIZ_PROJETO)
        self.secao_atual = "inicio"
        self._pontos: dict[str, ImageTk.PhotoImage] = {}
        self._glifos: dict[tuple, object] = {}
        self._ciclos_ate_renovar = 0
        # O que as máquinas responderam por último. O bloco do mês soma
        # daqui, e não do banco local — no computador que só acompanha, o
        # banco local está vazio e mostraria zero com quatro robôs rodando.
        self._maquinas: list = []
        self._orgaos_no_filtro: list[str] = []
        self._meses_no_filtro: list[str] = []

        self.title(f"{marca.NOME_PRODUTO} — {marca.DESCRICAO_PRODUTO}")
        self.geometry("1200x760")
        self.minsize(980, 640)
        self._por_icone()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._montar_lateral()
        self._montar_conteudo()
        self._montar_rodape()
        self.mostrar("inicio")

        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)
        self.after(300, self._ciclo)

    def _por_icone(self) -> None:
        try:
            destino = Path(tempfile.gettempdir()) / "cnd_mapah.ico"
            marca.salvar_icone_janela(destino)
            self.iconbitmap(default=str(destino))
        except Exception:
            pass    # ícone nunca deve impedir o programa de abrir

    # ------------------------------------------------------------------
    # Barra lateral
    # ------------------------------------------------------------------
    def _montar_lateral(self) -> None:
        lateral = ctk.CTkFrame(self, width=248, corner_radius=0,
                               fg_color=marca.BARRA, border_width=0)
        lateral.grid(row=0, column=0, sticky="nsew")
        lateral.grid_rowconfigure(6, weight=1)
        lateral.grid_columnconfigure(0, weight=1)
        lateral.grid_propagate(False)

        # Fio de separação no lugar de uma faixa escura: a barra clara faz a
        # janela inteira respirar, e a divisão continua legível.
        ctk.CTkFrame(self, width=1, corner_radius=0,
                     fg_color=marca.BARRA_BORDA).grid(row=0, column=0,
                                                      sticky="nse")

        topo = ctk.CTkFrame(lateral, fg_color="transparent")
        topo.grid(row=0, column=0, sticky="ew", padx=24, pady=(26, 24))

        self._marca = ctk.CTkImage(marca.desenhar_marca(128), size=(26, 26))
        ctk.CTkLabel(topo, image=self._marca, text="").grid(row=0, column=0,
                                                            rowspan=2,
                                                            padx=(0, 11))
        ctk.CTkLabel(topo, text=marca.NOME_PRODUTO, font=(FONTE, 20, "bold"),
                     text_color=marca.AZUL).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(topo, text="Certidões · Mapah", font=(FONTE, 11),
                     text_color=marca.TEXTO_3).grid(row=1, column=1, sticky="w")

        self.botoes_menu: dict[str, ctk.CTkButton] = {}
        self.marcadores_menu: dict[str, ctk.CTkFrame] = {}
        self.icones_menu: dict[str, tuple] = {}
        for indice, (chave, rotulo, icone) in enumerate(
            [("inicio", "Início", ""),
             ("maquinas", "Máquinas", ""),
             ("itens", "Consultar itens", ""),
             ("registro", "Registro", ""),
             ("ajustes", "Ajustes", "")], start=1
        ):
            item = ctk.CTkFrame(lateral, fg_color="transparent")
            item.grid(row=indice, column=0, sticky="ew", padx=(0, 14), pady=1)
            item.grid_columnconfigure(1, weight=1)

            # Altura explícita: um CTkFrame sem altura declarada assume 200px,
            # e com grid_propagate desligado ele impõe isso à linha inteira.
            marcador = ctk.CTkFrame(item, width=3, height=20, corner_radius=2,
                                    fg_color="transparent")
            marcador.grid(row=0, column=0, padx=(0, 12))
            marcador.grid_propagate(False)

            # O ícone entra como IMAGEM do botão, não como rótulo por cima:
            # rótulo carrega o próprio fundo e vira um retângulo recortado
            # assim que o botão muda de cor no hover ou na seleção.
            apagado = self._glifo(icone, marca.TEXTO_3)
            aceso = self._glifo(icone, marca.AZUL_VIVO)

            botao = ctk.CTkButton(
                item, text=rotulo, anchor="w", height=38, corner_radius=8,
                font=(FONTE, 13), fg_color="transparent",
                hover_color=marca.PAPEL, text_color=marca.TEXTO_2,
                image=apagado, compound="left", command=lambda c=chave:
                self.mostrar(c),
            )
            botao.grid(row=0, column=1, sticky="ew")

            self.botoes_menu[chave] = botao
            self.marcadores_menu[chave] = marcador
            if apagado is not None:
                self.icones_menu[chave] = (apagado, aceso)

        rodape = ctk.CTkFrame(lateral, fg_color=marca.BRANCO, corner_radius=10,
                              border_width=1, border_color=marca.BORDA)
        rodape.grid(row=7, column=0, sticky="ew", padx=18, pady=18)
        rodape.grid_columnconfigure(1, weight=1)

        self.rotulo_situacao = ctk.CTkLabel(rodape, text="Verificando...",
                                            font=(FONTE, 12, "bold"),
                                            text_color=marca.TEXTO, anchor="w")
        self.rotulo_situacao.grid(row=0, column=0, sticky="w", padx=(16, 6),
                                  pady=(14, 0))
        self.pastilha = ctk.CTkLabel(rodape, text="●", font=(FONTE, 12),
                                     text_color=marca.TEXTO_3)
        self.pastilha.grid(row=0, column=1, sticky="w", pady=(14, 0))
        self.rotulo_detalhe = ctk.CTkLabel(rodape, text="", font=(FONTE, 11),
                                           text_color=marca.TEXTO_3, anchor="w",
                                           justify="left")
        self.rotulo_detalhe.grid(row=1, column=0, columnspan=2, sticky="w",
                                 padx=16, pady=(2, 14))

    def _glifo(self, codigo: str, cor: str, tamanho: int = 17):
        """Ícone do Windows como CTkImage, ou None se a fonte não existir.

        Guardado em cache e preso a `self`: o Tk não segura a imagem, e sem
        alguém guardando a referência ela some no coletor de lixo e o ícone
        aparece em branco.
        """
        chave = (codigo, cor, tamanho)
        if chave not in self._glifos:
            imagem = marca.desenhar_glifo(codigo, cor, tamanho)
            self._glifos[chave] = (
                ctk.CTkImage(light_image=imagem, size=(tamanho, tamanho))
                if imagem is not None else None)
        return self._glifos[chave]

    def _montar_rodape(self) -> None:
        """A faixa de baixo: versão, papel da máquina e última leitura.

        A última leitura é a que importa: sem ela, uma tela congelada por
        falha de rede é indistinguível de uma tela em que nada mudou.
        """
        rodape = ctk.CTkFrame(self, height=38, corner_radius=0,
                              fg_color=marca.BRANCO, border_width=0)
        rodape.grid(row=1, column=0, columnspan=2, sticky="ew")
        rodape.grid_columnconfigure(2, weight=1)
        rodape.grid_propagate(False)

        ctk.CTkFrame(rodape, height=1, corner_radius=0,
                     fg_color=marca.BARRA_BORDA).grid(row=0, column=0,
                                                      columnspan=4, sticky="ew")

        papel = ("Console — acompanha as máquinas" if self.cfg.rede.maquinas
                 else "Máquina de robô")
        for coluna, texto in enumerate([f"v{VERSAO}", papel], start=0):
            ctk.CTkLabel(rodape, text=texto, font=(FONTE, 11),
                         text_color=marca.TEXTO_3).grid(
                row=1, column=coluna, padx=(24 if not coluna else 22, 0),
                pady=(0, 2))

        self.rotulo_sincronia = ctk.CTkLabel(rodape, text="", font=(FONTE, 11),
                                             text_color=marca.TEXTO_3,
                                             anchor="e")
        self.rotulo_sincronia.grid(row=1, column=3, sticky="e", padx=(0, 24),
                                   pady=(0, 2))

    def mostrar(self, chave: str) -> None:
        self.secao_atual = chave
        for nome, botao in self.botoes_menu.items():
            ativo = nome == chave
            botao.configure(
                fg_color=marca.BARRA_ATIVO if ativo else "transparent",
                text_color=marca.AZUL_VIVO if ativo else marca.TEXTO_2,
                font=(FONTE, 13, "bold" if ativo else "normal"),
            )
            self.marcadores_menu[nome].configure(
                fg_color=marca.AZUL_VIVO if ativo else "transparent")
            if nome in self.icones_menu:
                apagado, aceso = self.icones_menu[nome]
                botao.configure(image=aceso if ativo else apagado)
        for nome, quadro in self.secoes.items():
            if nome == chave:
                quadro.grid(row=0, column=0, sticky="nsew")
            else:
                quadro.grid_remove()
        if chave == "itens":
            self._recarregar_itens()
        elif chave == "inicio":
            self._recarregar_maquinas()
        elif chave == "maquinas":
            self._recarregar_saude()

    # ------------------------------------------------------------------
    # Conteúdo
    # ------------------------------------------------------------------
    def _montar_conteudo(self) -> None:
        area = ctk.CTkFrame(self, fg_color=marca.FUNDO, corner_radius=0)
        area.grid(row=0, column=1, sticky="nsew")
        area.grid_rowconfigure(0, weight=1)
        area.grid_columnconfigure(0, weight=1)

        self.secoes = {
            "inicio": self._secao_inicio(area),
            "maquinas": self._secao_maquinas(area),
            "itens": self._secao_itens(area),
            "registro": self._secao_registro(area),
            "ajustes": self._secao_ajustes(area),
        }

    def _titulo(self, pai, texto: str, subtitulo: str) -> ctk.CTkFrame:
        quadro = ctk.CTkFrame(pai, fg_color="transparent")
        ctk.CTkLabel(quadro, text=texto, font=(FONTE, 22, "bold"),
                     text_color=marca.TEXTO, anchor="w").grid(row=0, column=0,
                                                              sticky="w")
        ctk.CTkLabel(quadro, text=subtitulo, font=(FONTE, 12),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=1, column=0,
                                                                sticky="w",
                                                                pady=(4, 0))
        return quadro

    def _cartao(self, pai) -> ctk.CTkFrame:
        return ctk.CTkFrame(pai, fg_color=marca.BRANCO, corner_radius=12,
                            border_width=1, border_color=marca.BORDA)

    # Cada situação tem um par de cores: a do texto e a do fundo da
    # etiqueta. Texto colorido solto se perde no meio do cartão; com o fundo
    # tingido, a situação da máquina é a primeira coisa que se enxerga.
    CORES_DE_SITUACAO: ClassVar[dict[str, tuple[str, str]]] = {
        "verde": (marca.VERDE, marca.VERDE_FUNDO),
        "vermelho": (marca.VERMELHO, marca.VERMELHO_FUNDO),
        "ambar": (marca.AMBAR, marca.AMBAR_FUNDO),
        "cinza": (marca.TEXTO_2, marca.PAPEL),
    }

    def _etiqueta(self, pai, texto: str, cor: str) -> ctk.CTkFrame:
        """Pastilha de situação: texto curto sobre fundo tingido."""
        frente, fundo = self.CORES_DE_SITUACAO[cor]
        quadro = ctk.CTkFrame(pai, fg_color=fundo, corner_radius=11, height=22)
        ctk.CTkLabel(quadro, text=texto, font=(FONTE, 11, "bold"),
                     text_color=frente).grid(row=0, column=0, padx=11, pady=4)
        return quadro

    def _botao_secundario(self, pai, texto: str, acao, largura: int = 160):
        """Ação de apoio: contorno, não preenchimento.

        Só uma ação por tela é preenchida de azul. Quando tudo é destacado,
        nada é — e quem chega na tela não sabe por onde começar.
        """
        return ctk.CTkButton(
            pai, text=texto, height=40, width=largura, corner_radius=8,
            font=(FONTE, 13), fg_color=marca.BRANCO, hover_color=marca.PAPEL,
            text_color=marca.TEXTO_2, border_width=1,
            border_color=marca.BORDA_FORTE, command=acao)

    # ---------------- Início ----------------
    def _secao_inicio(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkScrollableFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_columnconfigure(0, weight=1)

        cabecalho = ctk.CTkFrame(quadro, fg_color="transparent")
        cabecalho.grid(row=0, column=0, sticky="ew", padx=30, pady=(26, 14))
        cabecalho.grid_columnconfigure(0, weight=1)
        self._titulo(cabecalho, "Início", "Visão geral da emissão de certidões"
                     ).grid(row=0, column=0, sticky="w")

        # Iniciar o robô e importar planilha são ações da máquina em que
        # este aplicativo está. No computador que só acompanha, elas não
        # existem — ele não roda robô nenhum, e o botão só confundiria.
        self.acoes_locais = ctk.CTkFrame(cabecalho, fg_color="transparent")
        self.botao_robo = ctk.CTkButton(
            self.acoes_locais, text="Iniciar robô", height=38, width=136,
            corner_radius=8, font=(FONTE, 13, "bold"), fg_color=marca.AZUL_VIVO,
            hover_color=marca.AZUL, text_color=marca.BRANCO,
            command=self._alternar_robo)
        self.botao_robo.grid(row=0, column=0, padx=(0, 8))
        self.botao_importar = self._botao_secundario(
            self.acoes_locais, "Importar planilha", self._importar, largura=148)
        self.botao_importar.grid(row=0, column=1)
        if self.cfg.rede.roda_robo:
            self.acoes_locais.grid(row=0, column=1, sticky="e")

        self._montar_veredito(quadro).grid(row=1, column=0, sticky="ew",
                                           padx=30, pady=(0, 12))
        self._montar_progresso(quadro).grid(row=2, column=0, sticky="ew",
                                            padx=30, pady=(0, 12))

        self.faixa_aviso = ctk.CTkFrame(quadro, fg_color=marca.AMBAR_FUNDO,
                                        corner_radius=10, border_width=1,
                                        border_color="#F0DFBA")
        self.faixa_aviso.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.faixa_aviso, text="⚠", font=(FONTE, 15),
                     text_color=marca.AMBAR).grid(row=0, column=0,
                                                  padx=(18, 12), pady=13)
        self.rotulo_aviso = ctk.CTkLabel(self.faixa_aviso, text="",
                                         font=(FONTE, 12), text_color=marca.AMBAR,
                                         anchor="w", justify="left",
                                         wraplength=620)
        self.rotulo_aviso.grid(row=0, column=1, sticky="w")
        acoes_aviso = ctk.CTkFrame(self.faixa_aviso, fg_color="transparent")
        acoes_aviso.grid(row=0, column=2, sticky="e", padx=(12, 14), pady=10)
        self._botao_secundario(acoes_aviso, "Ver os itens",
                               self._ver_pendencias, largura=124).grid(row=0,
                                                                       column=0)
        ctk.CTkButton(acoes_aviso, text="Tentar de novo", height=40, width=136,
                      corner_radius=8, font=(FONTE, 13, "bold"),
                      fg_color=marca.AZUL_VIVO, hover_color=marca.AZUL,
                      text_color=marca.BRANCO,
                      command=self._reenfileirar).grid(row=0, column=1,
                                                       padx=(8, 0))

        self._montar_entrega(quadro).grid(row=4, column=0, sticky="ew",
                                          padx=30, pady=(0, 16))

        ctk.CTkLabel(quadro, text="Máquinas", font=(FONTE, 14, "bold"),
                     text_color=marca.AZUL_VIVO, anchor="w").grid(
            row=5, column=0, sticky="w", padx=30, pady=(0, 8))
        self.painel_maquinas = ctk.CTkFrame(quadro, fg_color="transparent")
        self.painel_maquinas.grid(row=6, column=0, sticky="ew", padx=30,
                                  pady=(0, 24))
        self.painel_maquinas.grid_columnconfigure(0, weight=1)
        return quadro

    # ---------------- Máquinas ----------------
    def _secao_maquinas(self, pai) -> ctk.CTkFrame:
        """A saúde dos computadores: memória, disco e há quanto tempo ligados.

        Separada do Início de propósito. O Início responde "como vai a
        emissão"; aqui a pergunta é outra — "a máquina aguenta?" — e é o
        lugar de onde se acessa cada uma pelo AnyDesk.
        """
        quadro = ctk.CTkScrollableFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_columnconfigure(0, weight=1)

        cabecalho = ctk.CTkFrame(quadro, fg_color="transparent")
        cabecalho.grid(row=0, column=0, sticky="ew", padx=30, pady=(26, 14))
        cabecalho.grid_columnconfigure(0, weight=1)
        self._titulo(cabecalho, "Máquinas",
                     "Estado das máquinas e robôs registrados"
                     ).grid(row=0, column=0, sticky="w")
        self._botao_secundario(cabecalho, "Atualizar agora",
                               self._recarregar_saude, largura=136).grid(
            row=0, column=1, sticky="e")

        # Resumo em uma linha: com quatro máquinas, é o que se lê antes de
        # olhar cartão por cartão.
        faixa = ctk.CTkFrame(quadro, fg_color="transparent")
        faixa.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 14))
        self.icone_resumo = ctk.CTkLabel(faixa, text="", width=20)
        self.icone_resumo.grid(row=0, column=0, padx=(0, 8))
        self.resumo_maquinas = ctk.CTkLabel(faixa, text="", font=(FONTE, 12),
                                            text_color=marca.TEXTO_2,
                                            anchor="w")
        self.resumo_maquinas.grid(row=0, column=1, sticky="w")

        # Cabeçalho das colunas, fora dos cartões: repetir os títulos em
        # cada cartão custaria quatro linhas de ruído por máquina.
        titulos = ctk.CTkFrame(quadro, fg_color="transparent")
        titulos.grid(row=2, column=0, sticky="ew", padx=52, pady=(0, 6))
        for coluna, (texto, peso) in enumerate([
            ("ÓRGÃO / MÁQUINA", 0), ("SITUAÇÃO", 1), ("PREPARO", 1),
            ("DISCO", 1), ("AÇÕES", 0),
        ]):
            titulos.grid_columnconfigure(coluna, weight=peso,
                                         minsize=210 if not coluna else 0)
            ctk.CTkLabel(titulos, text=texto, font=(FONTE, 10, "bold"),
                         text_color=marca.TEXTO_3, anchor="w").grid(
                row=0, column=coluna, sticky="w", padx=(0, 26))

        self.painel_saude = ctk.CTkFrame(quadro, fg_color="transparent")
        self.painel_saude.grid(row=3, column=0, sticky="ew", padx=30,
                               pady=(0, 24))
        self.painel_saude.grid_columnconfigure(0, weight=1)
        return quadro

    def _recarregar_saude(self) -> None:
        for filho in self.painel_saude.winfo_children():
            filho.destroy()
        ctk.CTkLabel(self.painel_saude, text="Consultando as máquinas...",
                     font=(FONTE, 12), text_color=marca.TEXTO_3).grid(
            row=0, column=0, pady=30)

        def trabalho():
            estados = remoto.consultar_todas(self.cfg)
            self.after(0, lambda: self._desenhar_saude(estados))

        self._em_segundo_plano(trabalho, "consultar as máquinas")

    def _desenhar_saude(self, estados: list) -> None:
        for filho in self.painel_saude.winfo_children():
            filho.destroy()

        # Problema primeiro. Com quatro máquinas, a quebrada não pode ficar
        # em terceiro por ordem alfabética: ela é o motivo de abrir a tela.
        estados = sorted(estados, key=lambda e: (e.gravidade, e.rotulo))
        self._resumir_maquinas(estados)

        for indice, estado in enumerate(estados):
            cartao = self._cartao(self.painel_saude)
            cartao.grid(row=indice, column=0, sticky="ew", pady=(0, 12))
            cartao.grid_columnconfigure(1, weight=1)
            cartao.grid_columnconfigure(2, weight=1)

            self._coluna_identidade(cartao, estado).grid(
                row=0, column=0, sticky="nsw", padx=(22, 26), pady=(18, 16))
            self._coluna_preparo(cartao, estado).grid(
                row=0, column=1, sticky="nsw", padx=(0, 26), pady=(18, 16))
            self._coluna_disco(cartao, estado).grid(
                row=0, column=2, sticky="nsew", padx=(0, 26), pady=(18, 16))
            self._coluna_acoes(cartao, estado).grid(
                row=0, column=3, sticky="ne", padx=(0, 22), pady=(18, 16))

            if rodape := self._rodape_da_maquina(estado):
                risco = ctk.CTkFrame(cartao, height=1, corner_radius=0,
                                     fg_color=marca.BORDA)
                risco.grid(row=1, column=0, columnspan=4, sticky="ew")
                ctk.CTkLabel(cartao, text=rodape, font=(FONTE, 11),
                             text_color=marca.TEXTO_3, anchor="w").grid(
                    row=2, column=0, columnspan=4, sticky="w", padx=22,
                    pady=(12, 14))

    def _resumir_maquinas(self, estados: list) -> None:
        prontas = sum(1 for e in estados if e.online)
        mudas = len(estados) - prontas
        texto = f"{prontas} de {len(estados)} respondendo"
        if mudas:
            texto += f"   ·   {mudas} sem resposta"
        travadas = [e for e in estados if e.online and e.pendentes
                    and not e.robo_ativo]
        if travadas:
            texto += f"   ·   {len(travadas)} com fila parada"
        self.resumo_maquinas.configure(text=texto)

        tudo_certo = not mudas and not travadas
        self.icone_resumo.configure(image=self._glifo(
            ICONE_OK if tudo_certo else ICONE_ALERTA,
            marca.VERDE if tudo_certo else marca.AMBAR, 16))

    def _coluna_identidade(self, pai, estado) -> ctk.CTkFrame:
        """Quem é a máquina e como ela está, com duração.

        A duração é o que faltava: parada há 40 segundos entre lotes e
        parada há 4 horas sem ninguém notar são situações opostas, e a tela
        antiga mostrava as duas igual.
        """
        caixa = ctk.CTkFrame(pai, fg_color="transparent")

        titulo = ctk.CTkLabel(caixa, text=estado.rotulo,
                              font=(FONTE, 14, "bold"), anchor="w",
                              text_color=marca.AZUL_VIVO if estado.acessavel
                              else marca.TEXTO)
        titulo.grid(row=0, column=0, sticky="w")
        if estado.acessavel:
            self._transformar_em_link(titulo, estado, tamanho=14)

        ctk.CTkLabel(caixa, text=estado.subtitulo, font=(FONTE, 11),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=1, column=0,
                                                                sticky="w",
                                                                pady=(2, 0))
        versao = estado.dados.get("versao") or "versão desconhecida"
        ctk.CTkLabel(caixa, text=f"ACTA {versao}", font=(FONTE, 11),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=2, column=0,
                                                                sticky="w")

        texto, cor = estado.situacao
        frente, _ = self.CORES_DE_SITUACAO[cor]
        linha = ctk.CTkFrame(caixa, fg_color="transparent")
        linha.grid(row=3, column=0, sticky="w", pady=(12, 0))
        ctk.CTkLabel(linha, text="●", font=(FONTE, 12),
                     text_color=frente).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(linha, text=texto, font=(FONTE, 12, "bold"),
                     text_color=frente, anchor="w").grid(row=0, column=1,
                                                         sticky="w")
        ctk.CTkLabel(caixa, text=_detalhe_da_situacao(estado),
                     font=(FONTE, 11), text_color=marca.TEXTO_3, anchor="w",
                     justify="left", wraplength=210).grid(row=4, column=0,
                                                          sticky="w",
                                                          pady=(3, 0))
        return caixa

    def _coluna_preparo(self, pai, estado) -> ctk.CTkFrame:
        """A máquina consegue trabalhar se eu mandar agora?

        A calibragem é o item crítico: o robô cego não roda sem ela, e ela
        quebra quando alguém muda a resolução do monitor. Sem esta coluna,
        isso só se descobre errando um lote inteiro.
        """
        caixa = ctk.CTkFrame(pai, fg_color="transparent")
        ctk.CTkLabel(caixa, text="PREPARO", font=(FONTE, 10, "bold"),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=0, column=0,
                                                                columnspan=2,
                                                                sticky="w",
                                                                pady=(0, 8))
        for linha, (ok, texto) in enumerate(_preparo(estado), start=1):
            codigo, cor = ((ICONE_OK, marca.VERDE) if ok is True else
                           (ICONE_VAZIO, marca.TEXTO_3) if ok is None else
                           (ICONE_ALERTA, marca.AMBAR))
            marca_visual = ctk.CTkLabel(caixa, text="", width=18,
                                        image=self._glifo(codigo, cor, 14))
            marca_visual.grid(row=linha, column=0, sticky="w", pady=1)
            ctk.CTkLabel(caixa, text=texto, font=(FONTE, 11),
                         text_color=marca.TEXTO_2 if ok is not None
                         else marca.TEXTO_3, anchor="w").grid(row=linha,
                                                              column=1,
                                                              sticky="w",
                                                              pady=1)
        return caixa

    def _coluna_disco(self, pai, estado) -> ctk.CTkFrame:
        """Espaço, e o número que decide: o próximo lote cabe?

        A porcentagem sozinha é genérica. O que evita o prejuízo é saber
        antes de começar — disco cheio faz o robô emitir a certidão no
        portal e não conseguir salvar o PDF, com a consulta já gasta.
        """
        caixa = ctk.CTkFrame(pai, fg_color="transparent")
        caixa.grid_columnconfigure(0, weight=1)
        saude = estado.saude

        titulo = ctk.CTkFrame(caixa, fg_color="transparent")
        titulo.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ctk.CTkLabel(titulo, text="", width=18,
                     image=self._glifo(ICONE_DISCO, marca.TEXTO_3, 14)).grid(
            row=0, column=0)
        ctk.CTkLabel(titulo, text="DISCO", font=(FONTE, 10, "bold"),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=0, column=1,
                                                                sticky="w")
        if not saude:
            ctk.CTkLabel(caixa, text="último dado desconhecido",
                         font=(FONTE, 11), text_color=marca.TEXTO_3,
                         anchor="w").grid(row=1, column=0, sticky="w")
            return caixa

        livre, total = saude["disco_livre_gb"], saude["disco_total_gb"]
        fracao = (total - livre) / total if total else 0
        apertado = livre < LIMITE_DISCO_GB

        barra = ctk.CTkProgressBar(caixa, height=6, corner_radius=3,
                                   progress_color=marca.VERMELHO if apertado
                                   else marca.AZUL_VIVO,
                                   fg_color=marca.PAPEL_2)
        barra.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        barra.set(min(max(fracao, 0.0), 1.0))
        ctk.CTkLabel(caixa, text=f"{fracao * 100:.0f}%", font=(FONTE, 11),
                     text_color=marca.VERMELHO if apertado
                     else marca.TEXTO_3).grid(row=1, column=1, sticky="e")

        for linha, texto in enumerate(_linhas_de_disco(estado), start=2):
            ctk.CTkLabel(caixa, text=texto, font=(FONTE, 11),
                         text_color=marca.TEXTO_3, anchor="w").grid(
                row=linha, column=0, columnspan=2, sticky="w", pady=(4, 0))
        return caixa

    def _coluna_acoes(self, pai, estado) -> ctk.CTkFrame:
        """Só o acesso remoto.

        O botão de abrir o painel dela no navegador saiu: mostra a mesma
        coisa que esta janela já mostra, e ação que duplica outra só divide
        a atenção. Quando algo precisa de mão humana, o que se quer é entrar
        na máquina — e é isso que sobrou aqui.
        """
        caixa = ctk.CTkFrame(pai, fg_color="transparent")
        if not estado.acessavel:
            return caixa
        ctk.CTkButton(
            caixa, text="Acessar AnyDesk", height=36, width=158,
            corner_radius=8, font=(FONTE, 12, "bold"), fg_color=marca.BRANCO,
            hover_color=marca.PAPEL, text_color=marca.AZUL_VIVO,
            border_width=1, border_color=marca.BORDA_FORTE,
            image=self._glifo(ICONE_ABRIR_FORA, marca.AZUL_VIVO, 13),
            compound="right", anchor="w",
            command=lambda e=estado: self._acessar(e)).grid(row=0, column=0)
        return caixa

    def _rodape_da_maquina(self, estado) -> str:
        """Histórico curto: já rodou este mês? quando foi o último lote?

        No ritmo mensal essa é a pergunta do dia 1, e nenhuma outra tela
        responde.
        """
        if not estado.online:
            return ""
        partes = []
        if estado.lote_id:
            partes.append(f"Último lote: #{estado.lote_id} · "
                          f"{_numero(estado.total)} itens")
        emitidas = estado.por_desfecho("NEGATIVA") + estado.por_desfecho("CPEN")
        partes.append(f"{_numero(emitidas)} certidões emitidas")
        if (ligada := estado.saude.get("ligada_ha_h")) is not None:
            partes.append(f"ligada há {_duracao(ligada)}")
        partes.append("emite certidões" if estado.roda_robo else "só acompanha")
        return "   ·   ".join(partes)

    def _medidor(self, pai, titulo: str, detalhe: str, fracao: float):
        """Barra de uso com legenda. Vermelha quando aperta.

        O limiar é 90%: abaixo disso a máquina só está usando o que tem;
        acima, é o ponto em que o Windows começa a paginar e o robô cego
        passa a clicar atrasado.
        """
        caixa = ctk.CTkFrame(pai, fg_color="transparent")
        caixa.grid_columnconfigure(0, weight=1)
        apertado = fracao >= 0.9

        ctk.CTkLabel(caixa, text=titulo, font=(FONTE, 11, "bold"),
                     text_color=marca.TEXTO_2, anchor="w").grid(row=0, column=0,
                                                                sticky="w")
        ctk.CTkLabel(caixa, text=f"{fracao * 100:.0f}%", font=(FONTE, 11),
                     text_color=marca.VERMELHO if apertado else marca.TEXTO_3
                     ).grid(row=0, column=1, sticky="e")
        barra = ctk.CTkProgressBar(caixa, height=6, corner_radius=3,
                                   progress_color=marca.VERMELHO if apertado
                                   else marca.AZUL_VIVO,
                                   fg_color=marca.PAPEL_2)
        barra.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        barra.set(min(max(fracao, 0.0), 1.0))
        ctk.CTkLabel(caixa, text=detalhe, font=(FONTE, 11),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=2, column=0,
                                                                columnspan=2,
                                                                sticky="w")
        return caixa

    def _montar_veredito(self, pai) -> ctk.CTkFrame:
        """A frase que responde 'posso ir embora?' sem fazer conta.

        Junta o que hoje está espalhado — se está rodando, a que velocidade
        e até quando — numa linha só, que é como a pergunta é feita.
        """
        cartao = self._cartao(pai)
        cartao.grid_columnconfigure(1, weight=1)
        self.icone_veredito = ctk.CTkLabel(cartao, text="●", font=(FONTE, 16),
                                           text_color=marca.TEXTO_3)
        self.icone_veredito.grid(row=0, column=0, padx=(18, 12), pady=14)
        self.rotulo_veredito = ctk.CTkLabel(cartao, text="Verificando...",
                                            font=(FONTE, 13, "bold"),
                                            text_color=marca.TEXTO, anchor="w")
        self.rotulo_veredito.grid(row=0, column=1, sticky="w", padx=(0, 18))
        return cartao

    def _montar_progresso(self, pai) -> ctk.CTkFrame:
        """Quanto do mês já saiu, somando todas as máquinas.

        O recorte é o mês porque é o que se entrega. O lote saiu da frente:
        cada máquina numera o dela por conta, e o número não diz nada a
        quem olha.
        """
        cartao = self._cartao(pai)
        cartao.grid_columnconfigure(0, weight=1)

        topo = ctk.CTkFrame(cartao, fg_color="transparent")
        topo.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 0))
        topo.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(topo, text="EMISSÃO DO MÊS", font=(FONTE, 10, "bold"),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=0, column=0,
                                                                columnspan=2,
                                                                sticky="w")
        self.rotulo_mes = ctk.CTkLabel(topo, text="", font=(FONTE, 16, "bold"),
                                       text_color=marca.TEXTO, anchor="w")
        self.rotulo_mes.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # O filtro de órgão comanda a tela inteira: percentual, números e o
        # pacote. A federal costuma fechar antes das estaduais, e olhar o
        # total somado esconde exatamente essa diferença.
        self.filtro_orgao = ctk.CTkOptionMenu(
            topo, width=190, height=30, corner_radius=8,
            values=[TODOS_OS_ORGAOS], fg_color=marca.PAPEL,
            button_color=marca.PAPEL, button_hover_color=marca.PAPEL_2,
            text_color=marca.TEXTO, dropdown_fg_color=marca.BRANCO,
            dropdown_text_color=marca.TEXTO, dropdown_hover_color=marca.PAPEL,
            font=(FONTE, 12), dropdown_font=(FONTE, 12),
            command=lambda _: self._atualizar_situacao())
        self.filtro_orgao.grid(row=1, column=1, sticky="e", padx=(12, 12))

        self.rotulo_percentual = ctk.CTkLabel(topo, text="",
                                              font=(FONTE, 18, "bold"),
                                              text_color=marca.AZUL_VIVO)
        self.rotulo_percentual.grid(row=1, column=2, sticky="e")

        self.barra = ctk.CTkProgressBar(cartao, height=6, corner_radius=3,
                                        progress_color=marca.AZUL_VIVO,
                                        fg_color=marca.PAPEL_2)
        self.barra.grid(row=1, column=0, sticky="ew", padx=22, pady=(10, 8))
        self.barra.set(0)

        self.rotulo_restante = ctk.CTkLabel(cartao, text="", font=(FONTE, 11),
                                            text_color=marca.TEXTO_3, anchor="w")
        self.rotulo_restante.grid(row=2, column=0, sticky="w", padx=22,
                                  pady=(0, 14))

        # Métricas em linha, com fio entre elas, em vez de quatro caixas: o
        # que se compara aqui são valores do mesmo conjunto, e caixa
        # separada sugere que são coisas independentes.
        numeros = ctk.CTkFrame(cartao, fg_color="transparent")
        numeros.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 18))

        self.metricas: dict[str, ctk.CTkLabel] = {}
        for coluna, (chave, rotulo) in enumerate([
            ("NEGATIVA", "Negativas"),
            ("CPEN", "Com efeito de negativa"),
            ("POSITIVA", "Positivas"),
            ("APROVEITADA", "Já emitidas no mês"),
        ]):
            numeros.grid_columnconfigure(coluna * 2, weight=1, uniform="m")
            if coluna:
                ctk.CTkFrame(numeros, width=1, height=38, corner_radius=0,
                             fg_color=marca.BORDA).grid(row=0,
                                                        column=coluna * 2 - 1,
                                                        sticky="ns", padx=16)
            caixa = ctk.CTkFrame(numeros, fg_color="transparent")
            caixa.grid(row=0, column=coluna * 2, sticky="w")
            ctk.CTkLabel(caixa, text=rotulo, font=(FONTE, 11),
                         text_color=marca.TEXTO_2, anchor="w").grid(row=0,
                                                                    column=0,
                                                                    sticky="w")
            valor = ctk.CTkLabel(caixa, text="0", font=(FONTE, 20, "bold"),
                                 text_color=marca.AZUL_VIVO, anchor="w")
            valor.grid(row=1, column=0, sticky="w", pady=(2, 0))
            self.metricas[chave] = valor
        return cartao

    def _montar_entrega(self, pai) -> ctk.CTkFrame:
        """O bloco de entrega: o pacote do mês, de todas as máquinas juntas.

        Por mês e não por lote porque é assim que o cliente recebe — e
        porque cada máquina numera os lotes por conta, então "lote 7" não
        quer dizer nada fora dela.
        """
        cartao = ctk.CTkFrame(pai, fg_color=marca.AZUL_VIVO_FUNDO,
                              corner_radius=10, border_width=1,
                              border_color="#D5E1F8")
        cartao.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(cartao, text="↓", font=(FONTE, 15, "bold"),
                     text_color=marca.AZUL_VIVO).grid(row=0, column=0,
                                                      padx=(18, 12), pady=13)
        self.rotulo_entrega = ctk.CTkLabel(
            cartao, text="", font=(FONTE, 12), text_color=marca.TEXTO_2,
            anchor="w", justify="left")
        self.rotulo_entrega.grid(row=0, column=1, sticky="w", padx=(0, 16))

        acoes = ctk.CTkFrame(cartao, fg_color="transparent")
        acoes.grid(row=0, column=2, sticky="e", padx=(12, 14), pady=10)

        self.seletor_mes = ctk.CTkOptionMenu(
            acoes, width=124, height=40, corner_radius=8,
            values=[_mes_por_extenso(relatorio_mes_corrente())],
            fg_color=marca.BRANCO, button_color=marca.BRANCO,
            button_hover_color=marca.PAPEL, text_color=marca.TEXTO,
            dropdown_fg_color=marca.BRANCO, dropdown_text_color=marca.TEXTO,
            dropdown_hover_color=marca.PAPEL, font=(FONTE, 12),
            dropdown_font=(FONTE, 12))
        self.seletor_mes.grid(row=0, column=0, padx=(0, 8))

        ctk.CTkButton(
            acoes, text="Baixar certidões (ZIP)", height=40, width=178,
            corner_radius=8, font=(FONTE, 13, "bold"), fg_color=marca.AZUL_VIVO,
            hover_color=marca.AZUL, text_color=marca.BRANCO,
            command=self._baixar_certidoes_do_mes).grid(row=0, column=1,
                                                        padx=(0, 8))
        self._botao_secundario(acoes, "Exportar planilha",
                               self._exportar_planilha, largura=150).grid(
            row=0, column=2)
        return cartao

    # ---------------- Máquinas (dentro do Início) ----------------
    def _recarregar_maquinas(self, silencioso: bool = False) -> None:
        # Na renovação automática não se limpa a tela antes: piscar
        # "Consultando..." a cada dez segundos, por cima do que a pessoa
        # está lendo, é pior que esperar a resposta chegar.
        if not silencioso:
            for filho in self.painel_maquinas.winfo_children():
                filho.destroy()
            ctk.CTkLabel(self.painel_maquinas,
                         text="Consultando as máquinas...", font=(FONTE, 12),
                         text_color=marca.TEXTO_3).grid(row=0, column=0,
                                                        pady=30)

        def trabalho():
            estados = remoto.consultar_todas(self.cfg)
            self.after(0, lambda: self._desenhar_maquinas(estados))

        self._em_segundo_plano(trabalho, "consultar as máquinas")

    def _desenhar_maquinas(self, estados: list) -> None:
        for filho in self.painel_maquinas.winfo_children():
            filho.destroy()

        # Guardado porque o bloco do mês soma daqui, e não do banco local:
        # no computador que só acompanha, o banco local está vazio.
        self._maquinas = estados
        self._atualizar_filtro_de_orgao()
        self.rotulo_sincronia.configure(
            text=f"Última leitura: {self._hora(tempo.agora_iso())}")

        for indice, estado in enumerate(estados):
            cartao = self._cartao(self.painel_maquinas)
            cartao.grid(row=indice, column=0, sticky="ew", pady=(0, 12))
            cartao.grid_columnconfigure(0, weight=1)

            topo = ctk.CTkFrame(cartao, fg_color="transparent")
            topo.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 0))
            topo.grid_columnconfigure(0, weight=1)

            # O nome é o próprio acesso: clicar nele abre o AnyDesk já
            # apontado para aquela máquina. Azul e sublinhado porque é
            # assim que se lê "isto leva a algum lugar" — botão separado
            # obrigaria a procurar onde clicar.
            titulo = ctk.CTkLabel(topo, text=estado.rotulo.upper(),
                                  font=(FONTE, 15, "bold"), anchor="w",
                                  text_color=marca.AZUL if estado.acessavel
                                  else marca.TEXTO)
            titulo.grid(row=0, column=0, sticky="w")
            if estado.acessavel:
                self._transformar_em_link(titulo, estado)

            self._etiqueta(topo, *estado.situacao).grid(row=0, column=1,
                                                        sticky="e")
            if estado.subtitulo:
                ctk.CTkLabel(topo, text=estado.subtitulo, font=(FONTE, 11),
                             text_color=marca.TEXTO_3, anchor="w").grid(
                    row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

            linha = 1
            if not estado.online:
                ctk.CTkLabel(cartao, text=estado.erro or "sem resposta",
                             font=(FONTE, 11), text_color=marca.TEXTO_3,
                             anchor="w").grid(row=linha, column=0, sticky="w",
                                              padx=22, pady=(10, 0))
                linha += 1
            else:
                progresso = ctk.CTkFrame(cartao, fg_color="transparent")
                progresso.grid(row=linha, column=0, sticky="ew", padx=22,
                               pady=(14, 0))
                progresso.grid_columnconfigure(0, weight=1)
                linha += 1

                barra = ctk.CTkProgressBar(progresso, height=7, corner_radius=4,
                                           progress_color=marca.AZUL,
                                           fg_color=marca.PAPEL_2)
                barra.grid(row=0, column=0, sticky="ew", padx=(0, 14))
                barra.set(estado.percentual / 100)
                ctk.CTkLabel(progresso, text=f"{estado.percentual:.0f}%",
                             font=(FONTE, 14, "bold"),
                             text_color=marca.AZUL).grid(row=0, column=1)

                ctk.CTkLabel(cartao, text=_resumo_da_maquina(estado),
                             font=(FONTE, 11), text_color=marca.TEXTO_3,
                             anchor="w", justify="left", wraplength=820).grid(
                    row=linha, column=0, sticky="w", padx=22, pady=(9, 0))
                linha += 1

                self._desenhar_atividade(cartao, estado).grid(
                    row=linha, column=0, sticky="ew", padx=22, pady=(10, 2))
                linha += 1

                if estado.suspensos:
                    ctk.CTkLabel(cartao,
                                 text=f"{', '.join(estado.suspensos)} suspenso — "
                                      f"o portal recusou várias consultas. "
                                      f"Retoma sozinho.",
                                 font=(FONTE, 11), text_color=marca.AMBAR,
                                 anchor="w", wraplength=680).grid(
                        row=linha, column=0, sticky="w", padx=22, pady=(0, 6))
                    linha += 1

            # Sem botão de baixar por máquina: a entrega é uma só, do mês
            # inteiro, e está no bloco de cima. Espalhar downloads por
            # cartão devolveria o problema do pacote picado que a mudança
            # para "por mês" veio resolver.
            ctk.CTkFrame(cartao, fg_color="transparent", height=6).grid(
                row=linha, column=0)

    def _transformar_em_link(self, rotulo, estado, tamanho: int = 15) -> None:
        """Deixa o texto com cara e comportamento de link.

        O CustomTkinter não tem widget de link, e o sublinhado do Tk vive na
        fonte — daí trocar a fonte no hover em vez de uma propriedade de
        estilo.
        """
        normal = (FONTE, tamanho, "bold")
        sobre = (FONTE, tamanho, "bold underline")

        rotulo.configure(cursor="hand2")
        rotulo.bind("<Enter>", lambda _e: rotulo.configure(font=sobre))
        rotulo.bind("<Leave>", lambda _e: rotulo.configure(font=normal))
        rotulo.bind("<Button-1>", lambda _e: self._acessar(estado))

    def _desenhar_atividade(self, pai, estado) -> ctk.CTkFrame:
        """O que aquela máquina acabou de fazer, linha a linha.

        É a diferença entre saber que ela está em 42% e saber que ela está
        viva: o percentual demora minutos para mudar, mas a última empresa
        consultada muda a cada consulta.
        """
        painel = ctk.CTkFrame(pai, fg_color=marca.FUNDO, corner_radius=8)
        painel.grid_columnconfigure(1, weight=1)

        if not estado.atividade:
            ctk.CTkLabel(painel, text="Nenhuma consulta registrada ainda",
                         font=(FONTE, 11), text_color=marca.TEXTO_3,
                         anchor="w").grid(row=0, column=0, columnspan=3,
                                          sticky="w", padx=14, pady=11)
            return painel

        for indice, evento in enumerate(estado.atividade):
            if evento.get("em_curso"):
                rotulo, cor = "consultando...", marca.AZUL
            elif evento.get("interrompida"):
                rotulo, cor = "interrompida", marca.TEXTO_3
            else:
                rotulo, cor = ROTULOS_DE_RESULTADO.get(
                    evento.get("desfecho"), ("—", marca.TEXTO_3))

            ctk.CTkLabel(painel, text=self._hora(evento.get("quando")),
                         font=("Consolas", 10), text_color=marca.TEXTO_3,
                         anchor="w").grid(row=indice, column=0, sticky="w",
                                          padx=(14, 12),
                                          pady=(9 if indice == 0 else 2,
                                                9 if indice == len(estado.atividade) - 1 else 2))
            ctk.CTkLabel(painel, text=(evento.get("nome") or "")[:52],
                         font=(FONTE, 11), text_color=marca.TEXTO_2,
                         anchor="w").grid(row=indice, column=1, sticky="w")
            ctk.CTkLabel(painel, text=rotulo, font=(FONTE, 11, "bold"),
                         text_color=cor, anchor="e").grid(row=indice, column=2,
                                                          sticky="e", padx=(12, 14))
        return painel

    @staticmethod
    def _hora(momento: str | None) -> str:
        """Só a hora do carimbo ISO — a data polui e quase sempre é hoje."""
        if not momento or "T" not in momento:
            return "--:--:--"
        return momento.split("T", 1)[1][:8]

    def _acessar(self, estado) -> None:
        """Abre o AnyDesk já apontado para aquela máquina."""
        try:
            acesso.abrir(estado.anydesk)
        except RuntimeError as erro:
            messagebox.showwarning(f"Acessar {estado.rotulo}", str(erro))

    def _baixar_de(self, estado, rota: str, extensao: str) -> None:
        """Traz o arquivo da outra máquina.

        Quem gera é ela, no momento do pedido — este computador não precisa
        de acesso ao disco dela nem de pasta compartilhada.
        """
        destino = filedialog.asksaveasfilename(
            title=f"Salvar de {estado.nome}", defaultextension=extensao,
            initialfile=f"{estado.nome.replace(' ', '_')}_lote_"
                        f"{estado.lote_id}{extensao}")
        if not destino:
            return

        def trabalho():
            remoto.baixar(estado.maquina, rota, Path(destino),
                          self.cfg.rede.senha)
            self.after(0, lambda: messagebox.showinfo(
                "Arquivo salvo", f"Trazido de {estado.nome}:\n{destino}"))

        self._em_segundo_plano(trabalho, f"baixar de {estado.nome}")

    # ---------------- Itens ----------------
    def _secao_itens(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_rowconfigure(2, weight=1)
        quadro.grid_columnconfigure(0, weight=1)

        self._titulo(quadro, "Consultar itens",
                     "Cada linha é uma empresa em um órgão"
                     ).grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))

        filtros = ctk.CTkFrame(quadro, fg_color="transparent")
        filtros.grid(row=1, column=0, sticky="ew", padx=34, pady=(0, 14))

        self.busca = ctk.CTkEntry(filtros, placeholder_text="CNPJ ou razão social",
                                  width=250, height=40, corner_radius=8,
                                  fg_color=marca.BRANCO,
                                  border_color=marca.BORDA_FORTE,
                                  text_color=marca.TEXTO, font=(FONTE, 12))
        self.busca.grid(row=0, column=0, padx=(0, 8))
        self.busca.bind("<Return>", lambda _: self._recarregar_itens())

        def seletor(valores: list[str], largura: int) -> ctk.CTkOptionMenu:
            # A seta na mesma cor do campo: com cor própria, o CustomTkinter
            # a desenha como uma pastilha colada ao lado, e o filtro parece
            # dois controles em vez de um.
            moldura = ctk.CTkFrame(filtros, fg_color=marca.BORDA_FORTE,
                                   corner_radius=8)
            menu = ctk.CTkOptionMenu(
                moldura, width=largura, height=38, corner_radius=7,
                values=valores, fg_color=marca.BRANCO,
                button_color=marca.BRANCO, button_hover_color=marca.PAPEL,
                text_color=marca.TEXTO_2, dropdown_fg_color=marca.BRANCO,
                dropdown_text_color=marca.TEXTO, dropdown_hover_color=marca.PAPEL,
                font=(FONTE, 12), dropdown_font=(FONTE, 12),
                command=lambda _: self._recarregar_itens())
            menu.grid(row=0, column=0, padx=1, pady=1)
            menu.moldura = moldura      # quem posiciona é a moldura
            return menu

        # Filtrar por RESULTADO é o que a operação pede na prática: separar
        # quem está limpa de quem tem pendência. Situação (na fila, falhou)
        # interessa a quem acompanha o processamento, não o resultado.
        self.filtro_resultado = seletor(list(RESULTADOS), 210)
        self.filtro_resultado.moldura.grid(row=0, column=1, padx=(0, 8))

        self.filtro_situacao = seletor(list(SITUACOES), 168)
        self.filtro_situacao.moldura.grid(row=0, column=2, padx=(0, 8))

        # Sem recorte de mês, a lista mistura agosto com julho e junho — e a
        # emissão é mensal, então "o que saiu neste mês" é a pergunta que a
        # tela existe para responder.
        self.filtro_mes = seletor([TODOS_OS_MESES], 150)
        self.filtro_mes.moldura.grid(row=0, column=3, padx=(0, 8))

        self._botao_secundario(filtros, "Atualizar", self._recarregar_itens,
                               largura=104).grid(row=0, column=4)

        self.contador = ctk.CTkLabel(filtros, text="", font=(FONTE, 12),
                                     text_color=marca.TEXTO_3)
        self.contador.grid(row=0, column=5, padx=(14, 0))

        moldura = self._cartao(quadro)
        moldura.grid(row=2, column=0, sticky="nsew", padx=34, pady=(0, 28))
        moldura.grid_rowconfigure(0, weight=1)
        moldura.grid_columnconfigure(0, weight=1)

        # Tabela nativa, e não uma pilha de widgets num quadro rolável: com
        # milhares de itens, o quadro rolável cria um widget por célula, e o
        # Windows não repinta todos a tempo quando a rolagem é rápida — as
        # linhas antigas ficam na tela por cima das novas. O Treeview desenha
        # só o que está visível e rola liso com a lista inteira.
        self._preparar_estilo_da_tabela()

        # A coluna da árvore (#0) guarda o ponto colorido do resultado. É o
        # único lugar do Treeview que aceita cor por célula — etiqueta pinta
        # a linha inteira, e a razão social sairia verde ou vermelha junto.
        self.tabela = ttk.Treeview(
            moldura, style="Acta.Treeview", show="tree headings",
            selectmode="browse", columns=("empresa", "documento", "resultado"),
        )
        self.tabela.column("#0", width=34, minwidth=34, stretch=False)
        self.tabela.heading("#0", text="")
        for chave, titulo, largura, minimo in (
            ("empresa", "EMPRESA", 440, 220),
            ("documento", "DOCUMENTO", 190, 160),
            ("resultado", "RESULTADO", 220, 150),
        ):
            self.tabela.heading(chave, text=titulo, anchor="w")
            self.tabela.column(chave, width=largura, minwidth=minimo,
                               stretch=(chave == "empresa"), anchor="w")
        self.tabela.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)

        rolagem = ctk.CTkScrollbar(moldura, command=self.tabela.yview,
                                   button_color=marca.BORDA,
                                   button_hover_color=marca.TEXTO_3,
                                   fg_color="transparent", width=16)
        rolagem.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=6)
        self.tabela.configure(yscrollcommand=rolagem.set)

        self.tabela.tag_configure("par", background=marca.BRANCO)
        self.tabela.tag_configure("impar", background=marca.PAPEL)
        return quadro

    def _ponto(self, cor: str):
        """Bolinha colorida do resultado, desenhada e guardada em cache.

        Guardar a referência é obrigatório: o Tk não segura a imagem, e sem
        alguém guardando ela some no coletor de lixo e a linha aparece vazia.
        """
        if cor not in self._pontos:
            lado, escala = 12, 4
            imagem = Image.new("RGBA", (lado * escala,) * 2, (0, 0, 0, 0))
            ImageDraw.Draw(imagem).ellipse(
                [escala, escala, lado * escala - escala, lado * escala - escala],
                fill=cor)
            self._pontos[cor] = ImageTk.PhotoImage(
                imagem.resize((lado, lado), Image.LANCZOS))
        return self._pontos[cor]

    def _preparar_estilo_da_tabela(self) -> None:
        """Veste o Treeview com as cores da marca.

        O tema `clam` é a base porque é o único em que o Tk no Windows
        respeita cor de fundo e de cabeçalho; o tema nativo ignora boa parte
        do que se configura. O estilo tem nome próprio para não afetar
        nenhum outro widget da janela.
        """
        estilo = ttk.Style()
        with contextlib.suppress(Exception):
            estilo.theme_use("clam")

        estilo.configure(
            "Acta.Treeview", font=(FONTE, 12), rowheight=34,
            background=marca.BRANCO, fieldbackground=marca.BRANCO,
            foreground=marca.TEXTO, borderwidth=0, relief="flat",
        )
        estilo.configure(
            "Acta.Treeview.Heading", font=(FONTE, 10, "bold"),
            background=marca.PAPEL, foreground=marca.TEXTO_3,
            relief="flat", borderwidth=0, padding=(14, 10),
        )
        estilo.map("Acta.Treeview.Heading",
                   background=[("active", marca.PAPEL_2)])
        # Seleção no azul da marca, em vez do azul do sistema.
        estilo.map("Acta.Treeview",
                   background=[("selected", marca.AZUL)],
                   foreground=[("selected", marca.BRANCO)])
        estilo.layout("Acta.Treeview", [
            ("Acta.Treeview.treearea", {"sticky": "nswe"}),
        ])

    # ---------------- Registro ----------------
    def _secao_registro(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_rowconfigure(1, weight=1)
        quadro.grid_columnconfigure(0, weight=1)

        self._titulo(quadro, "Registro", "O que o robô está fazendo agora"
                     ).grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))

        self.caixa_log = ctk.CTkTextbox(
            quadro, fg_color=marca.BRANCO, corner_radius=12, border_width=1,
            border_color=marca.BORDA, font=("Consolas", 11),
            text_color=marca.TEXTO_2, wrap="none",
            scrollbar_button_color=marca.BORDA,
            scrollbar_button_hover_color=marca.TEXTO_3)
        self.caixa_log.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 28))
        self.caixa_log.insert("end",
                              "O registro aparece aqui quando o robô estiver rodando.\n")
        self.caixa_log.configure(state="disabled")
        return quadro

    # ---------------- Ajustes ----------------
    def _secao_ajustes(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkScrollableFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_columnconfigure(0, weight=1)

        self._titulo(quadro, "Ajustes", "Preparo da máquina e avisos"
                     ).grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))

        for linha, (titulo, descricao, rotulo, acao) in enumerate([
            ("Calibrar a tela",
             "Ensina ao robô onde ficam o campo de CNPJ e os botões do portal. "
             "Refazer sempre que mudar a resolução do monitor.",
             "Calibrar", self._calibrar),
            ("Conferir a calibragem",
             "Abre o portal e desenha as marcas sobre uma foto da tela, para "
             "você ver se caíram nos lugares certos.",
             "Conferir", self._conferir_calibragem),
            ("Número do AnyDesk desta máquina",
             "Guarda o número aqui para que o computador que acompanha "
             "consiga acessá-la clicando no nome dela.",
             "Cadastrar", self._cadastrar_anydesk),
            ("Testar avisos",
             "Publica uma mensagem de teste no canal do Teams.",
             "Enviar teste", self._testar_alerta),
            ("Painel no navegador",
             "A mesma informação em página web, para acessar de outro "
             "computador da rede.",
             "Abrir painel", self._abrir_painel),
        ], start=1):
            cartao = self._cartao(quadro)
            cartao.grid(row=linha, column=0, sticky="ew", padx=34, pady=(0, 11))
            cartao.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(cartao, text=titulo, font=(FONTE, 14, "bold"),
                         text_color=marca.TEXTO, anchor="w").grid(
                row=0, column=0, sticky="w", padx=22, pady=(18, 3))
            ctk.CTkLabel(cartao, text=descricao, font=(FONTE, 11),
                         text_color=marca.TEXTO_3, anchor="w", justify="left",
                         wraplength=580).grid(row=1, column=0, sticky="w",
                                              padx=22, pady=(0, 18))
            self._botao_secundario(cartao, rotulo, acao, largura=140).grid(
                row=0, column=1, rowspan=2, padx=22, pady=18)
        return quadro

    # ------------------------------------------------------------------
    # Ações
    # ------------------------------------------------------------------
    def _importar(self) -> None:
        caminho = filedialog.askopenfilename(
            title="Escolha a planilha de CNPJs",
            filetypes=[("Planilha do Excel", "*.xlsx *.xlsm"), ("Todos", "*.*")])
        if not caminho:
            return

        def trabalho():
            from cnd.infra.db import conectar, criar_schema
            from cnd.ingestao.planilha import importar

            conn = conectar(self.cfg.banco)
            criar_schema(conn)
            try:
                lote_id, leitura = importar(conn, Path(caminho),
                                            f"Importação de {Path(caminho).name}",
                                            ["RFB"])
            finally:
                conn.close()

            detalhe = ""
            if leitura.rejeitados:
                exemplos = "\n".join(
                    f"  linha {r.linha}: {r.valor_original} — {r.motivo}"
                    for r in leitura.rejeitados[:8])
                detalhe = (f"\n\n{len(leitura.rejeitados)} não entraram "
                           f"(documento inválido ou repetido):\n{exemplos}")
            self.after(0, lambda: messagebox.showinfo(
                "Planilha importada",
                f"Lote #{lote_id} criado com {len(leitura.itens)} empresas.{detalhe}"))

        self._em_segundo_plano(trabalho, "importar a planilha")

    def _alternar_robo(self) -> None:
        if self.robo.rodando:
            self.robo.parar()
            return

        if not messagebox.askyesno(
            "Iniciar o robô",
            "O robô vai assumir o mouse e o teclado desta máquina.\n\n"
            "Não use o computador enquanto ele trabalha — qualquer clique seu "
            "desvia a automação.\n\nEsta janela será minimizada. Iniciar agora?",
        ):
            return

        self.robo.iniciar()
        self.mostrar("registro")
        self.after(1200, self.iconify)

    def _exportar_planilha(self) -> None:
        panorama = ler_panorama(self.cfg)
        if not panorama.lote_id:
            messagebox.showwarning("Sem lote", "Importe uma planilha primeiro.")
            return

        destino = filedialog.asksaveasfilename(
            title="Salvar planilha do lote", defaultextension=".xlsx",
            initialfile=f"relatorio_lote_{panorama.lote_id}.xlsx",
            filetypes=[("Planilha do Excel", "*.xlsx")])
        if not destino:
            return

        def trabalho():
            from cnd.infra.db import conectar_leitura
            from cnd.web.relatorio import gerar

            conn = conectar_leitura(self.cfg.banco)
            try:
                gerar(conn, panorama.lote_id, Path(destino))
            finally:
                conn.close()
            self.after(0, lambda: messagebox.showinfo(
                "Planilha salva", f"Arquivo gerado em:\n{destino}"))

        self._em_segundo_plano(trabalho, "gerar a planilha")

    def _baixar_certidoes_do_mes(self) -> None:
        """Um pacote só, do mês, com as certidões de todas as máquinas."""
        mes = _mes_do_rotulo(self.seletor_mes.get())

        somente = messagebox.askyesno(
            "Quais certidões incluir",
            "Incluir apenas as NEGATIVAS?\n\n"
            "Sim — só empresas totalmente limpas.\n"
            "Não — negativas e também as positivas com efeito de negativa "
            "(débito parcelado), que valem como negativa.")
        destino = filedialog.asksaveasfilename(
            title="Salvar pacote de certidões", defaultextension=".zip",
            initialfile=f"certidoes_{mes}.zip",
            filetypes=[("Pacote ZIP", "*.zip")])
        if not destino:
            return

        # O pacote respeita o filtro da tela: a federal costuma fechar antes
        # das estaduais, e não faz sentido segurar a entrega dela.
        escolhido = self.filtro_orgao.get()
        codigo = next((o["orgao"] for o in self._orgaos_visiveis()), None) \
            if escolhido != TODOS_OS_ORGAOS else None

        def trabalho():
            entrega = remoto.baixar_certidoes(self.cfg, mes, Path(destino),
                                              somente, orgao=codigo)
            self.after(0, lambda: self._avisar_entrega(entrega, destino))

        self._em_segundo_plano(trabalho, "montar o pacote de certidões")

    def _avisar_entrega(self, entrega, destino: str) -> None:
        """Diz o que entrou e, principalmente, o que ficou de fora.

        Máquina fora do ar não impede a entrega — mas entregar um pacote
        incompleto sem saber disso é o pior desfecho possível, então a
        falha é dita em voz alta.
        """
        corpo = [f"{entrega.arquivos} certidões salvas em:\n{destino}"]
        if entrega.por_maquina:
            corpo.append("\n" + entrega.resumo)
        if entrega.falhas:
            faltando = "\n".join(f"{nome}: {motivo}"
                                 for nome, motivo in entrega.falhas.items())
            messagebox.showwarning(
                "Pacote incompleto",
                "\n".join(corpo) + "\n\nNÃO respondeu, e ficou de fora:\n"
                + faltando)
            return
        messagebox.showinfo("Pacote salvo", "\n".join(corpo))

    def _atualizar_filtro_de_mes(self) -> None:
        """Oferece só os meses que existem no banco desta máquina."""
        from cnd.desktop.estado import ler_meses_de_itens

        valores = [TODOS_OS_MESES,
                   *(_mes_por_extenso(m) for m in ler_meses_de_itens(self.cfg))]
        if valores == self._meses_no_filtro:
            return
        self._meses_no_filtro = valores
        atual = self.filtro_mes.get()
        self.filtro_mes.configure(values=valores)
        self.filtro_mes.set(atual if atual in valores else TODOS_OS_MESES)

    def _ver_pendencias(self) -> None:
        """Leva à lista já filtrada — a pergunta seguinte é sempre 'quais?'."""
        self.mostrar("itens")
        self.filtro_situacao.set("Falhou")
        self.filtro_resultado.set("Todos os resultados")
        self.busca.delete(0, "end")
        self._recarregar_itens()

    def _reenfileirar(self) -> None:
        """Devolve à fila o que esgotou as tentativas.

        Zera o contador: são itens que falharam por motivo já resolvido —
        portal fora do ar, máquina reiniciada — e merecem as três tentativas
        de novo, não a última que sobrou.
        """
        if not messagebox.askyesno(
            "Tentar de novo",
            "Devolver à fila os itens que esgotaram as tentativas?\n\n"
            "Eles voltam a ser consultados na próxima execução do robô, "
            "com o contador de tentativas zerado."
        ):
            return

        def trabalho():
            from cnd.core.fila import reenfileirar_falhados
            from cnd.infra.db import conectar

            conn = conectar(self.cfg.banco)
            try:
                quantidade = reenfileirar_falhados(conn)
            finally:
                conn.close()
            self.after(0, lambda: messagebox.showinfo(
                "De volta à fila",
                f"{_numero(quantidade)} itens voltaram para a fila."))

        self._em_segundo_plano(trabalho, "devolver os itens à fila")

    def _calibrar(self) -> None:
        """Abre a calibragem numa janela de console e sai da frente.

        O aplicativo se minimiza porque a calibragem pede que você aponte o
        mouse para os campos do portal — com esta janela na frente, os
        pontos medidos seriam os dela.
        """
        if not messagebox.askyesno(
            "Calibrar a tela",
            "Vou abrir uma janela de comando com as instruções e minimizar "
            "este aplicativo.\n\n"
            "Deixe o portal da Receita aberto no Edge, em tela cheia, e siga "
            "o que a janela pedir: apontar o mouse para cada campo e ficar "
            "parado até ele registrar.\n\nComeçar agora?"
        ):
            return
        self.iconify()
        self._abrir_no_console(["calibrar"])

    def _conferir_calibragem(self) -> None:
        """Desenha as marcas salvas sobre uma foto da tela e abre a imagem."""
        destino = (RAIZ_PROJETO / "data" / "calibragem" /
                   "rfb_cego-conferencia.png")
        if not messagebox.askyesno(
            "Conferir a calibragem",
            "Vou tirar uma foto da tela e desenhar em cima os pontos "
            "gravados, para você ver se caíram nos lugares certos.\n\n"
            "Deixe o portal aberto no Edge como o robô vai encontrá-lo. "
            "Este aplicativo se minimiza durante a foto.\n\nContinuar?"
        ):
            return

        self.iconify()

        def trabalho():
            from cnd.adapters.calibragem import conferir

            time.sleep(1.2)     # tempo de a janela sumir da foto
            conferir(RAIZ_PROJETO / "data" / "calibragem" / "rfb_cego.json",
                     destino)
            self.after(0, lambda: self._mostrar_conferencia(destino))

        self._em_segundo_plano(trabalho, "conferir a calibragem")

    def _mostrar_conferencia(self, destino: Path) -> None:
        self.deiconify()
        if messagebox.askyesno(
            "Conferência pronta",
            f"Imagem salva em:\n{destino}\n\nAbrir agora?"
        ):
            with contextlib.suppress(OSError):
                os.startfile(destino)

    def _cadastrar_anydesk(self) -> None:
        """Guarda o número do AnyDesk desta máquina no config.toml.

        Mora no config da própria máquina, e não na lista do console: quem
        sabe o número é quem está na frente dela, na hora de instalar. O
        console lê pela rede e não precisa que ninguém redigite.
        """
        from cnd.infra import ajustes

        atual = ajustes.ler_valor("rede", "anydesk")
        numero = simpledialog.askstring(
            "Número do AnyDesk",
            "Abra o AnyDesk nesta máquina e copie o número que aparece em "
            '"Este computador".\n\nPode colar com os espaços.',
            initialvalue=atual, parent=self)
        if numero is None:
            return

        limpo = acesso.normalizar(numero)
        if numero.strip() and not limpo:
            messagebox.showwarning(
                "Número inválido",
                "Não reconheci um número do AnyDesk aí. Ele tem só dígitos "
                "(ex.: 123 456 789) ou é um apelido com @ (ex.: acta@ad).")
            return

        ajustes.gravar_valor("rede", "anydesk", limpo)
        self.cfg = carregar()
        messagebox.showinfo(
            "Número guardado",
            f"AnyDesk desta máquina: {limpo or '(nenhum)'}\n\n"
            "O computador que acompanha vai enxergar assim que consultar "
            "esta máquina de novo.")

    def _abrir_no_console(self, argumentos: list[str]) -> None:
        """Roda um comando do cnd numa janela de console visível.

        Visível de propósito: são comandos que conversam com quem está na
        frente da máquina — pedem para apontar o mouse, esperam confirmação.
        Escondê-los deixaria a pessoa esperando um robô que espera por ela.
        """
        from cnd.desktop.estado import _comando_base

        subprocess.Popen(_comando_base() + argumentos, cwd=str(RAIZ_PROJETO),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE",
                                               0))

    def _testar_alerta(self) -> None:
        def trabalho():
            from cnd.infra import alertas

            enviado = alertas.enviar(
                self.cfg.alertas, "Teste de configuração", "",
                dados={"Origem": "Aplicativo de mesa"},
                severidade="ok", forcar=True)
            self.after(0, lambda: messagebox.showinfo(
                "Teste de aviso",
                "Mensagem publicada no canal do Teams." if enviado else
                "Não foi enviado. Confira o webhook em config.toml — o motivo "
                "está registrado na pasta data\\logs."))

        self._em_segundo_plano(trabalho, "enviar o teste")

    def _abrir_painel(self) -> None:
        webbrowser.open("http://127.0.0.1:8000")

    def _em_segundo_plano(self, funcao, descricao: str) -> None:
        """Trabalho pesado fora da thread da interface, para a janela não
        congelar — e erro vira caixa de aviso, não travamento silencioso."""
        def envolver():
            try:
                funcao()
            except Exception as erro:
                # `erro` é apagado ao fim do except — o lambda só roda
                # depois, na thread da interface, e encontraria o nome
                # vazio. Amarrar no parâmetro guarda o valor agora. Sem
                # isso, a caixa de aviso morria calada e a falha sumia.
                #
                # O suppress cobre a janela já fechada: aí não há laço de
                # eventos, e até perguntar se ela existe estoura. Fechar o
                # aplicativo no meio de um download não é incidente.
                with contextlib.suppress(RuntimeError, TclError):
                    self.after(0, lambda e=erro: messagebox.showerror(
                        "Não deu certo", f"Falha ao {descricao}:\n\n{e}"))

        threading.Thread(target=envolver, daemon=True).start()

    # ------------------------------------------------------------------
    # Atualização periódica
    # ------------------------------------------------------------------
    def _ciclo(self) -> None:
        with contextlib.suppress(Exception):
            self._atualizar_situacao()
            self._drenar_registro()
            self._ciclo_das_maquinas()
        self.after(INTERVALO_ATUALIZACAO_MS, self._ciclo)

    def _ciclo_das_maquinas(self) -> None:
        """Renova a tela de máquinas enquanto ela estiver aberta.

        Mais devagar que o resto de propósito: cada renovação é uma ida à
        rede por máquina, e o que a pessoa acompanha ali — a última empresa
        consultada — muda na casa das dezenas de segundos, não dos dois.
        """
        if self.secao_atual != "inicio":
            self._ciclos_ate_renovar = 0
            return

        self._ciclos_ate_renovar -= 1
        if self._ciclos_ate_renovar <= 0:
            self._ciclos_ate_renovar = CICLOS_ENTRE_CONSULTAS_DE_REDE
            self._recarregar_maquinas(silencioso=True)

    def _atualizar_situacao(self) -> None:
        panorama = ler_panorama(self.cfg)
        rodando = self.robo.rodando

        texto, cor = panorama.situacao
        if rodando and not panorama.robo_ativo:
            texto, cor = "Iniciando...", "ambar"
        cores = {"verde": marca.VERDE, "vermelho": marca.VERMELHO,
                 "ambar": marca.AMBAR, "cinza": marca.TEXTO_3}
        self.pastilha.configure(text_color=cores[cor])
        self.rotulo_situacao.configure(text=texto)
        self.rotulo_detalhe.configure(text=_ritmo_do_panorama(panorama)
                                      or "sem trabalho na fila")

        self.botao_robo.configure(
            text="Parar robô" if rodando else "Iniciar robô",
            fg_color=marca.VERMELHO if rodando else marca.AZUL_VIVO,
            hover_color="#93201A" if rodando else marca.AZUL)
        self.botao_importar.configure(state="disabled" if rodando else "normal")

        veredito, cor_veredito = _veredito(panorama, rodando)
        self.icone_veredito.configure(text="●", text_color=cores[cor_veredito])
        self.rotulo_veredito.configure(text=veredito,
                                       text_color=cores[cor_veredito]
                                       if cor_veredito != "cinza"
                                       else marca.TEXTO_2)

        falhados = self._atualizar_mes()

        # A faixa só existe quando há o que fazer. Cartão marcando "0
        # pendências" ocupa espaço para dizer que não há nada a dizer.
        if falhados:
            self.rotulo_aviso.configure(
                text=f"{_numero(falhados)} itens esgotaram as "
                     f"{self._max_tentativas()} tentativas e precisam de "
                     f"conferência manual")
            self.faixa_aviso.grid(row=3, column=0, sticky="ew", padx=30,
                                  pady=(0, 12))
        else:
            self.faixa_aviso.grid_remove()

    def _max_tentativas(self) -> int:
        ativos = self.cfg.ativos()
        return ativos[0].retry.max_tentativas if ativos else 3

    def _atualizar_mes(self) -> None:
        """O bloco do mês, somando TODAS as máquinas e respeitando o filtro.

        A soma vem dos órgãos que as máquinas informaram, e não do banco
        desta máquina. No computador que só acompanha, o banco local está
        vazio — ele mostraria zero enquanto quatro robôs trabalham.
        """
        mes = relatorio_mes_corrente()
        resumos = self._orgaos_visiveis()

        titulo = _mes_por_extenso(mes).capitalize()
        if (escolhido := self.filtro_orgao.get()) != TODOS_OS_ORGAOS:
            titulo += f"  ·  {escolhido}"
        self.rotulo_mes.configure(text=titulo)

        total = sum(o["total"] for o in resumos)
        concluidos = sum(o["concluidos"] for o in resumos)
        falhados = sum(o["falhados"] for o in resumos)
        percentual = (concluidos / total * 100) if total else 0.0

        self.rotulo_percentual.configure(
            text=f"{percentual:.1f}%".replace(".", ","))
        self.barra.set(percentual / 100)

        partes = [f"{_numero(concluidos)} de {_numero(total)}"]
        if (restam := total - concluidos - falhados) > 0:
            partes.append(f"{_numero(restam)} na fila")
        self.rotulo_restante.configure(text="   ·   ".join(partes))

        def por_desfecho(chave: str) -> int:
            return sum(o["por_desfecho"].get(chave, 0) for o in resumos)

        for chave in self.metricas:
            self.metricas[chave].configure(text=_numero(por_desfecho(chave)))

        com_certidao = por_desfecho("NEGATIVA") + por_desfecho("CPEN")
        sem_certidao = por_desfecho("POSITIVA") + por_desfecho("PENDENCIA_MANUAL")
        alcance = ("uma pasta por órgão dentro do pacote"
                   if escolhido == TODOS_OS_ORGAOS else f"só de {escolhido}")
        self.rotulo_entrega.configure(
            text=f"{_numero(com_certidao)} com certidão  ·  "
                 f"{_numero(sem_certidao)} sem certidão\n{alcance}")
        return falhados

    def _orgaos_visiveis(self) -> list[dict]:
        """Os órgãos das máquinas, filtrados pelo que está selecionado."""
        escolhido = self.filtro_orgao.get()
        return [o for e in self._maquinas for o in e.orgaos
                if escolhido in (TODOS_OS_ORGAOS,
                                 o.get("rotulo") or o["orgao"])]

    def _atualizar_filtro_de_orgao(self) -> None:
        """Mantém a lista do filtro igual ao que as máquinas informam."""
        vistos = {o.get("rotulo") or o["orgao"]
                  for e in self._maquinas for o in e.orgaos}
        valores = [TODOS_OS_ORGAOS, *sorted(vistos)]
        if valores == self._orgaos_no_filtro:
            return
        self._orgaos_no_filtro = valores
        atual = self.filtro_orgao.get()
        self.filtro_orgao.configure(values=valores)
        self.filtro_orgao.set(atual if atual in valores else TODOS_OS_ORGAOS)

    def _drenar_registro(self) -> None:
        linhas = self.robo.drenar()
        if not linhas:
            return
        self.caixa_log.configure(state="normal")
        for linha in linhas:
            self.caixa_log.insert("end", linha + "\n")
        if float(self.caixa_log.index("end-1c").split(".")[0]) > 2000:
            self.caixa_log.delete("1.0", "800.0")
        self.caixa_log.see("end")
        self.caixa_log.configure(state="disabled")

    def _recarregar_itens(self) -> None:
        self._atualizar_filtro_de_mes()
        escolhido = self.filtro_mes.get()
        itens = listar_itens(
            self.cfg,
            status=SITUACOES.get(self.filtro_situacao.get()),
            desfecho=RESULTADOS.get(self.filtro_resultado.get()),
            busca=self.busca.get().strip() or None,
            mes=None if escolhido == TODOS_OS_MESES
            else _mes_do_rotulo(escolhido),
            limite=LIMITE_DE_ITENS,
        )

        self.tabela.delete(*self.tabela.get_children())
        self.contador.configure(
            text=f"{len(itens)} item(ns)"
                 + (" (máximo)" if len(itens) >= LIMITE_DE_ITENS else ""))

        if not itens:
            # Tabela vazia sem explicação parece tela quebrada.
            self.tabela.insert("", "end", tags=("par",),
                               values=("Nenhum item com esses filtros", "", ""))
            return

        for indice, item in enumerate(itens):
            rotulo, cor = ROTULOS_DE_RESULTADO.get(
                item["desfecho"], (SITUACAO_SEM_RESULTADO.get(item["status"], "—"),
                                   marca.TEXTO_3))
            self.tabela.insert(
                "", "end", image=self._ponto(cor),
                tags=("impar" if indice % 2 else "par",),
                values=(item["nome"], formatar(item["documento"]), rotulo),
            )

    # ------------------------------------------------------------------
    def _ao_fechar(self) -> None:
        if self.robo.rodando and not messagebox.askyesno(
            "O robô está trabalhando",
            "Fechar agora vai interromper o robô no meio do lote.\n\n"
            "Os itens já concluídos ficam salvos e o restante continua na fila "
            "para a próxima execução.\n\nFechar mesmo assim?",
        ):
            return
        self.robo.parar()
        self.destroy()


def main() -> int:
    _registrar_no_windows()
    garantir_banco()        # primeira abertura numa máquina nova
    Aplicativo().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
