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
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from cnd.core.documentos import formatar
from cnd.desktop import acesso, marca, remoto
from cnd.desktop.estado import Robo, estado_dos_orgaos, ler_panorama, listar_itens
from cnd.infra.config import carregar
from cnd.infra.db import RAIZ_PROJETO
from cnd.infra.db import garantir as garantir_banco

ctk.set_appearance_mode("light")

FONTE = "Segoe UI"
INTERVALO_ATUALIZACAO_MS = 2000
ID_DO_APLICATIVO = "Mapah.Acta.Certidoes"
# A tabela nativa aguenta a carteira inteira sem engasgar; o teto existe só
# para uma busca vazia não puxar o banco todo de uma vez.
LIMITE_DE_ITENS = 3000

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
        self.cfg = carregar()
        self.robo = Robo(RAIZ_PROJETO)
        self.secao_atual = "inicio"
        self._pontos: dict[str, ImageTk.PhotoImage] = {}

        self.title(f"{marca.NOME_PRODUTO} — {marca.DESCRICAO_PRODUTO}")
        self.geometry("1200x760")
        self.minsize(980, 640)
        self._por_icone()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._montar_lateral()
        self._montar_conteudo()
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
        lateral = ctk.CTkFrame(self, width=236, corner_radius=0, fg_color=marca.AZUL)
        lateral.grid(row=0, column=0, sticky="nsew")
        lateral.grid_rowconfigure(5, weight=1)
        lateral.grid_propagate(False)

        topo = ctk.CTkFrame(lateral, fg_color="transparent")
        topo.grid(row=0, column=0, sticky="ew", padx=22, pady=(26, 30))

        self._marca = ctk.CTkImage(marca.desenhar_marca(96), size=(26, 26))
        ctk.CTkLabel(topo, image=self._marca, text="").grid(row=0, column=0,
                                                            rowspan=2, padx=(0, 12))
        ctk.CTkLabel(topo, text=marca.NOME_PRODUTO, font=(FONTE, 20, "bold"),
                     text_color=marca.TEXTO_NA_BARRA).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(topo, text="Certidões · Mapah", font=(FONTE, 11),
                     text_color=marca.TEXTO_NA_BARRA_2).grid(row=1, column=1,
                                                             sticky="w")

        self.botoes_menu: dict[str, ctk.CTkButton] = {}
        for indice, (chave, rotulo) in enumerate(
            [("inicio", "Início"), ("maquinas", "Máquinas"),
             ("itens", "Consultar itens"), ("registro", "Registro"),
             ("ajustes", "Ajustes")], start=1
        ):
            botao = ctk.CTkButton(
                lateral, text=rotulo, anchor="w", height=40, corner_radius=9,
                font=(FONTE, 13), fg_color="transparent",
                hover_color=marca.AZUL_CLARO, text_color=marca.TEXTO_NA_BARRA_2,
                command=lambda c=chave: self.mostrar(c),
            )
            botao.grid(row=indice, column=0, sticky="ew", padx=14, pady=2)
            self.botoes_menu[chave] = botao

        rodape = ctk.CTkFrame(lateral, fg_color=marca.AZUL_ESCURO, corner_radius=10)
        rodape.grid(row=6, column=0, sticky="ew", padx=14, pady=18)
        self.pastilha = ctk.CTkLabel(rodape, text="●", font=(FONTE, 15),
                                     text_color=marca.TEXTO_NA_BARRA_2)
        self.pastilha.grid(row=0, column=0, padx=(13, 8), pady=13)
        self.rotulo_situacao = ctk.CTkLabel(rodape, text="Verificando...",
                                            font=(FONTE, 12),
                                            text_color=marca.TEXTO_NA_BARRA)
        self.rotulo_situacao.grid(row=0, column=1, sticky="w", pady=13)

    def mostrar(self, chave: str) -> None:
        self.secao_atual = chave
        for nome, botao in self.botoes_menu.items():
            ativo = nome == chave
            botao.configure(
                fg_color=marca.AZUL_ESCURO if ativo else "transparent",
                text_color=marca.BRANCO if ativo else marca.TEXTO_NA_BARRA_2,
                font=(FONTE, 13, "bold" if ativo else "normal"),
            )
        for nome, quadro in self.secoes.items():
            if nome == chave:
                quadro.grid(row=0, column=0, sticky="nsew")
            else:
                quadro.grid_remove()
        if chave == "itens":
            self._recarregar_itens()
        elif chave == "maquinas":
            self._recarregar_maquinas()

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
        ctk.CTkLabel(quadro, text=texto, font=(FONTE, 25, "bold"),
                     text_color=marca.TEXTO, anchor="w").grid(row=0, column=0,
                                                              sticky="w")
        ctk.CTkLabel(quadro, text=subtitulo, font=(FONTE, 12),
                     text_color=marca.TEXTO_3, anchor="w").grid(row=1, column=0,
                                                                sticky="w",
                                                                pady=(3, 0))
        return quadro

    def _cartao(self, pai) -> ctk.CTkFrame:
        return ctk.CTkFrame(pai, fg_color=marca.BRANCO, corner_radius=12,
                            border_width=1, border_color=marca.BORDA)

    # ---------------- Início ----------------
    def _secao_inicio(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkScrollableFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_columnconfigure(0, weight=1)

        self._titulo(quadro, "Início", "Situação do lote em andamento"
                     ).grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 22))

        acoes = ctk.CTkFrame(quadro, fg_color="transparent")
        acoes.grid(row=1, column=0, sticky="ew", padx=34, pady=(0, 22))

        self.botao_robo = ctk.CTkButton(
            acoes, text="Iniciar robô", height=44, width=170, corner_radius=22,
            font=(FONTE, 13, "bold"), fg_color=marca.AZUL,
            hover_color=marca.AZUL_CLARO, text_color=marca.BRANCO,
            command=self._alternar_robo)
        self.botao_robo.grid(row=0, column=0, padx=(0, 10))

        self.botao_importar = ctk.CTkButton(
            acoes, text="Importar planilha", height=44, width=168, corner_radius=22,
            font=(FONTE, 13), fg_color=marca.BRANCO, hover_color=marca.PAPEL,
            text_color=marca.TEXTO, border_width=1, border_color=marca.BORDA,
            command=self._importar)
        self.botao_importar.grid(row=0, column=1, padx=(0, 10))

        for coluna, (rotulo, acao) in enumerate(
            [("Exportar planilha", self._exportar_planilha),
             ("Baixar certidões", self._baixar_zip)], start=2
        ):
            ctk.CTkButton(acoes, text=rotulo, height=44, width=158,
                          corner_radius=22, font=(FONTE, 13),
                          fg_color=marca.BRANCO, hover_color=marca.PAPEL,
                          text_color=marca.TEXTO, border_width=1,
                          border_color=marca.BORDA, command=acao
                          ).grid(row=0, column=coluna, padx=(0, 10))

        cartao = self._cartao(quadro)
        cartao.grid(row=2, column=0, sticky="ew", padx=34, pady=(0, 16))
        cartao.grid_columnconfigure(0, weight=1)

        cabecalho = ctk.CTkFrame(cartao, fg_color="transparent")
        cabecalho.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 0))
        cabecalho.grid_columnconfigure(1, weight=1)

        self.rotulo_lote = ctk.CTkLabel(cabecalho, text="Nenhum lote",
                                        font=(FONTE, 15, "bold"),
                                        text_color=marca.TEXTO, anchor="w")
        self.rotulo_lote.grid(row=0, column=0, sticky="w")
        self.rotulo_percentual = ctk.CTkLabel(cabecalho, text="", font=(FONTE, 13),
                                              text_color=marca.TEXTO_2, anchor="e")
        self.rotulo_percentual.grid(row=0, column=1, sticky="e")

        self.barra = ctk.CTkProgressBar(cartao, height=8, corner_radius=4,
                                        progress_color=marca.AZUL,
                                        fg_color=marca.PAPEL_2)
        self.barra.grid(row=1, column=0, sticky="ew", padx=24, pady=(15, 5))
        self.barra.set(0)

        self.rotulo_restante = ctk.CTkLabel(cartao, text="", font=(FONTE, 11),
                                            text_color=marca.TEXTO_3, anchor="w")
        self.rotulo_restante.grid(row=2, column=0, sticky="w", padx=24, pady=(0, 22))

        numeros = ctk.CTkFrame(quadro, fg_color="transparent")
        numeros.grid(row=3, column=0, sticky="ew", padx=34)
        for coluna in range(4):
            numeros.grid_columnconfigure(coluna, weight=1)

        self.metricas: dict[str, ctk.CTkLabel] = {}
        for coluna, (chave, rotulo, cor) in enumerate([
            ("NEGATIVA", "Negativas", marca.VERDE),
            ("CPEN", "Com efeito de negativa", marca.AZUL),
            ("PENDENTES", "Com pendência", marca.AMBAR),
            ("FALHAS", "Falhas", marca.VERMELHO),
        ]):
            caixa = self._cartao(numeros)
            caixa.grid(row=0, column=coluna, sticky="ew",
                       padx=(0 if coluna == 0 else 9, 0))
            valor = ctk.CTkLabel(caixa, text="0", font=(FONTE, 30, "bold"),
                                 text_color=cor)
            valor.grid(row=0, column=0, sticky="w", padx=20, pady=(18, 0))
            ctk.CTkLabel(caixa, text=rotulo, font=(FONTE, 11),
                         text_color=marca.TEXTO_3, anchor="w", wraplength=160
                         ).grid(row=1, column=0, sticky="w", padx=20, pady=(3, 18))
            self.metricas[chave] = valor

        self.faixa_aviso = ctk.CTkFrame(quadro, fg_color=marca.AMBAR_FUNDO,
                                        corner_radius=10)
        self.rotulo_aviso = ctk.CTkLabel(self.faixa_aviso, text="",
                                         font=(FONTE, 12), text_color=marca.AMBAR,
                                         anchor="w", justify="left", wraplength=740)
        self.rotulo_aviso.grid(row=0, column=0, sticky="w", padx=18, pady=13)
        return quadro

    # ---------------- Máquinas ----------------
    def _secao_maquinas(self, pai) -> ctk.CTkFrame:
        quadro = ctk.CTkScrollableFrame(pai, fg_color=marca.FUNDO)
        quadro.grid_columnconfigure(0, weight=1)

        cabecalho = ctk.CTkFrame(quadro, fg_color="transparent")
        cabecalho.grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))
        cabecalho.grid_columnconfigure(0, weight=1)

        self._titulo(cabecalho, "Máquinas",
                     "Cada computador roda o robô de um órgão. "
                     "Clique no nome para acessá-lo pelo AnyDesk."
                     ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(cabecalho, text="Atualizar", height=38, width=112,
                      corner_radius=19, font=(FONTE, 12), fg_color=marca.BRANCO,
                      hover_color=marca.PAPEL, text_color=marca.TEXTO,
                      border_width=1, border_color=marca.BORDA,
                      command=self._recarregar_maquinas).grid(row=0, column=1,
                                                              sticky="e")

        self.painel_maquinas = ctk.CTkFrame(quadro, fg_color="transparent")
        self.painel_maquinas.grid(row=1, column=0, sticky="ew", padx=34,
                                  pady=(0, 28))
        self.painel_maquinas.grid_columnconfigure(0, weight=1)
        return quadro

    def _recarregar_maquinas(self) -> None:
        for filho in self.painel_maquinas.winfo_children():
            filho.destroy()
        ctk.CTkLabel(self.painel_maquinas, text="Consultando as máquinas...",
                     font=(FONTE, 12), text_color=marca.TEXTO_3).grid(row=0,
                                                                      column=0,
                                                                      pady=30)

        def trabalho():
            estados = remoto.consultar_todas(self.cfg)
            self.after(0, lambda: self._desenhar_maquinas(estados))

        self._em_segundo_plano(trabalho, "consultar as máquinas")

    def _desenhar_maquinas(self, estados: list) -> None:
        for filho in self.painel_maquinas.winfo_children():
            filho.destroy()

        cores = {"verde": marca.VERDE, "vermelho": marca.VERMELHO,
                 "cinza": marca.TEXTO_3}

        for indice, estado in enumerate(estados):
            cartao = self._cartao(self.painel_maquinas)
            cartao.grid(row=indice, column=0, sticky="ew", pady=(0, 12))
            cartao.grid_columnconfigure(0, weight=1)

            topo = ctk.CTkFrame(cartao, fg_color="transparent")
            topo.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 0))
            topo.grid_columnconfigure(1, weight=1)

            texto_situacao, cor = estado.situacao
            ctk.CTkLabel(topo, text="●", font=(FONTE, 14),
                         text_color=cores[cor]).grid(row=0, column=0, rowspan=2,
                                                     padx=(0, 10))
            # O nome é o próprio acesso: clicar nele abre o AnyDesk já
            # apontado para aquela máquina. Azul e sublinhado porque é
            # assim que se lê "isto leva a algum lugar" — botão separado
            # obrigaria a procurar onde clicar.
            titulo = ctk.CTkLabel(topo, text=estado.rotulo.upper(),
                                  font=(FONTE, 15, "bold"), anchor="w",
                                  text_color=marca.AZUL if estado.acessavel
                                  else marca.TEXTO)
            titulo.grid(row=0, column=1, sticky="w")
            if estado.acessavel:
                self._transformar_em_link(titulo, estado)
            ctk.CTkLabel(topo, text=texto_situacao, font=(FONTE, 12),
                         text_color=cores[cor]).grid(row=0, column=2, sticky="e")
            if estado.subtitulo:
                ctk.CTkLabel(topo, text=estado.subtitulo, font=(FONTE, 11),
                             text_color=marca.TEXTO_3, anchor="w").grid(
                    row=1, column=1, columnspan=2, sticky="w")

            linha = 1
            if not estado.online:
                ctk.CTkLabel(cartao, text=estado.erro or "sem resposta",
                             font=(FONTE, 11), text_color=marca.TEXTO_3,
                             anchor="w").grid(row=linha, column=0, sticky="w",
                                              padx=22, pady=(10, 0))
                linha += 1
            else:
                def n(valor: int) -> str:
                    return f"{valor:,}".replace(",", ".")

                ctk.CTkLabel(cartao,
                             text=f"Lote #{estado.lote_id} · {estado.lote_nome}"
                                  if estado.lote_id else "Nenhum lote importado",
                             font=(FONTE, 12), text_color=marca.TEXTO_2,
                             anchor="w").grid(row=linha, column=0, sticky="w",
                                              padx=22, pady=(12, 0))
                linha += 1

                barra = ctk.CTkProgressBar(cartao, height=7, corner_radius=4,
                                           progress_color=marca.AZUL,
                                           fg_color=marca.PAPEL_2)
                barra.grid(row=linha, column=0, sticky="ew", padx=22, pady=(10, 4))
                barra.set(estado.percentual / 100)
                linha += 1

                resumo = (f"{n(estado.concluidos)} de {n(estado.total)}"
                          f"   ({estado.percentual:.1f}%)"
                          f"   ·   {n(estado.por_desfecho('NEGATIVA'))} negativas"
                          f"   ·   {n(estado.por_desfecho('CPEN'))} com efeito"
                          f" de negativa")
                if estado.falhados:
                    resumo += f"   ·   {n(estado.falhados)} falhas"
                ctk.CTkLabel(cartao, text=resumo, font=(FONTE, 11),
                             text_color=marca.TEXTO_3, anchor="w").grid(
                    row=linha, column=0, sticky="w", padx=22, pady=(0, 6))
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

            acoes = ctk.CTkFrame(cartao, fg_color="transparent")
            acoes.grid(row=linha, column=0, sticky="w", padx=22, pady=(12, 18))

            botoes = []
            if estado.online and estado.lote_id and estado.maquina.url:
                botoes += [
                    ("Baixar planilha",
                     lambda e=estado: self._baixar_de(
                         e, f"/relatorio/{e.lote_id}.xlsx", ".xlsx")),
                    ("Baixar certidões",
                     lambda e=estado: self._baixar_de(
                         e, f"/relatorio/{e.lote_id}.zip", ".zip")),
                ]

            for coluna, (rotulo, acao) in enumerate(botoes):
                ctk.CTkButton(
                    acoes, text=rotulo, height=34, width=152,
                    corner_radius=17, font=(FONTE, 12), fg_color=marca.BRANCO,
                    hover_color=marca.PAPEL, text_color=marca.AZUL,
                    border_width=1, border_color=marca.BORDA, command=acao,
                ).grid(row=0, column=coluna, padx=(0, 9))
            if not botoes:
                acoes.grid_remove()

    def _transformar_em_link(self, rotulo, estado) -> None:
        """Deixa o texto com cara e comportamento de link.

        O CustomTkinter não tem widget de link, e o sublinhado do Tk vive na
        fonte — daí trocar a fonte no hover em vez de uma propriedade de
        estilo.
        """
        normal = (FONTE, 15, "bold")
        sobre = (FONTE, 15, "bold underline")

        rotulo.configure(cursor="hand2")
        rotulo.bind("<Enter>", lambda _e: rotulo.configure(font=sobre))
        rotulo.bind("<Leave>", lambda _e: rotulo.configure(font=normal))
        rotulo.bind("<Button-1>", lambda _e: self._acessar(estado))

    def _acessar(self, estado) -> None:
        """Abre o AnyDesk já apontado para aquela máquina."""
        try:
            acesso.abrir(estado.maquina.anydesk)
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
                                  width=250, height=38, corner_radius=19,
                                  fg_color=marca.BRANCO, border_color=marca.BORDA,
                                  text_color=marca.TEXTO, font=(FONTE, 12))
        self.busca.grid(row=0, column=0, padx=(0, 9))
        self.busca.bind("<Return>", lambda _: self._recarregar_itens())

        def seletor(valores: list[str], largura: int) -> ctk.CTkOptionMenu:
            return ctk.CTkOptionMenu(
                filtros, width=largura, height=38, corner_radius=19,
                values=valores, fg_color=marca.BRANCO, button_color=marca.PAPEL,
                button_hover_color=marca.PAPEL_2, text_color=marca.TEXTO,
                dropdown_fg_color=marca.BRANCO, dropdown_text_color=marca.TEXTO,
                dropdown_hover_color=marca.PAPEL, font=(FONTE, 12),
                command=lambda _: self._recarregar_itens())

        # Filtrar por RESULTADO é o que a operação pede na prática: separar
        # quem está limpa de quem tem pendência. Situação (na fila, falhou)
        # interessa a quem acompanha o processamento, não o resultado.
        self.filtro_resultado = seletor(list(RESULTADOS), 212)
        self.filtro_resultado.grid(row=0, column=1, padx=(0, 9))

        self.filtro_situacao = seletor(list(SITUACOES), 170)
        self.filtro_situacao.grid(row=0, column=2, padx=(0, 9))

        ctk.CTkButton(filtros, text="Atualizar", height=38, width=104,
                      corner_radius=19, font=(FONTE, 12), fg_color=marca.BRANCO,
                      hover_color=marca.PAPEL, text_color=marca.TEXTO,
                      border_width=1, border_color=marca.BORDA,
                      command=self._recarregar_itens).grid(row=0, column=3)

        self.contador = ctk.CTkLabel(filtros, text="", font=(FONTE, 12),
                                     text_color=marca.TEXTO_3)
        self.contador.grid(row=0, column=4, padx=(14, 0))

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
            quadro, fg_color=marca.PAPEL, corner_radius=12, border_width=1,
            border_color=marca.BORDA, font=("Consolas", 11),
            text_color=marca.TEXTO_2, wrap="none")
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
            ctk.CTkButton(cartao, text=rotulo, height=36, width=142,
                          corner_radius=18, font=(FONTE, 12),
                          fg_color=marca.BRANCO, hover_color=marca.PAPEL,
                          text_color=marca.AZUL, border_width=1,
                          border_color=marca.BORDA, command=acao).grid(
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

    def _baixar_zip(self) -> None:
        panorama = ler_panorama(self.cfg)
        if not panorama.lote_id:
            messagebox.showwarning("Sem lote", "Importe uma planilha primeiro.")
            return

        somente = messagebox.askyesno(
            "Quais certidões incluir",
            "Incluir apenas as NEGATIVAS?\n\n"
            "Sim — só empresas totalmente limpas.\n"
            "Não — negativas e também as positivas com efeito de negativa "
            "(débito parcelado), que valem como negativa.")
        destino = filedialog.asksaveasfilename(
            title="Salvar pacote de certidões", defaultextension=".zip",
            initialfile=f"certidoes_lote_{panorama.lote_id}.zip",
            filetypes=[("Pacote ZIP", "*.zip")])
        if not destino:
            return

        def trabalho():
            from cnd.infra.db import conectar_leitura
            from cnd.web.relatorio import zipar_pdfs

            conn = conectar_leitura(self.cfg.banco)
            try:
                dados = zipar_pdfs(conn, panorama.lote_id, somente)
            finally:
                conn.close()
            Path(destino).write_bytes(dados)
            self.after(0, lambda: messagebox.showinfo(
                "Pacote salvo", f"Arquivo gerado em:\n{destino}"))

        self._em_segundo_plano(trabalho, "montar o pacote")

    def _calibrar(self) -> None:
        messagebox.showinfo(
            "Calibragem",
            "A calibragem é feita no terminal, porque você precisa apontar o "
            "mouse para os campos do portal — e esta janela na frente "
            "atrapalharia a medição.\n\n"
            "Abra o Prompt de Comando na pasta do projeto e rode:\n\n"
            "    python -m cnd.cli calibrar")

    def _conferir_calibragem(self) -> None:
        messagebox.showinfo(
            "Conferir calibragem",
            "Rode no terminal:\n\n    python -m cnd.cli calibrar --conferir\n\n"
            "Ele salva uma imagem com as marcas desenhadas sobre a tela do "
            "portal, na pasta data\\calibragem.")

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
        self.after(INTERVALO_ATUALIZACAO_MS, self._ciclo)

    def _atualizar_situacao(self) -> None:
        panorama = ler_panorama(self.cfg)
        rodando = self.robo.rodando

        texto, cor = panorama.situacao
        if rodando and not panorama.robo_ativo:
            texto, cor = "Iniciando...", "ambar"
        cores = {"verde": "#4ADE80", "vermelho": "#F87171",
                 "ambar": marca.AMARELO, "cinza": marca.TEXTO_NA_BARRA_2}
        self.pastilha.configure(text_color=cores[cor])
        self.rotulo_situacao.configure(text=texto)

        self.botao_robo.configure(
            text="Parar robô" if rodando else "Iniciar robô",
            fg_color=marca.VERMELHO if rodando else marca.AZUL,
            hover_color="#C9412F" if rodando else marca.AZUL_CLARO)
        self.botao_importar.configure(state="disabled" if rodando else "normal")

        def numero(valor: int) -> str:
            return f"{valor:,}".replace(",", ".")

        if panorama.lote_id:
            self.rotulo_lote.configure(
                text=f"Lote #{panorama.lote_id} · {panorama.lote_nome}")
            self.rotulo_percentual.configure(
                text=f"{numero(panorama.concluidos)} de {numero(panorama.total)}"
                     f"   ({panorama.percentual:.1f}%)")
            self.barra.set(panorama.percentual / 100)
            restam = panorama.total - panorama.concluidos - panorama.falhados
            self.rotulo_restante.configure(text=f"{numero(restam)} restantes na fila")
        else:
            self.rotulo_lote.configure(text="Nenhum lote importado")
            self.rotulo_percentual.configure(text="")
            self.rotulo_restante.configure(
                text="Use o botão Importar planilha para começar")
            self.barra.set(0)

        self.metricas["NEGATIVA"].configure(text=numero(panorama.por_desfecho("NEGATIVA")))
        self.metricas["CPEN"].configure(text=numero(panorama.por_desfecho("CPEN")))
        self.metricas["PENDENTES"].configure(
            text=numero(panorama.por_desfecho("POSITIVA")
                        + panorama.por_desfecho("PENDENCIA_MANUAL")))
        self.metricas["FALHAS"].configure(text=numero(panorama.falhados))

        suspensos = [codigo for codigo, estado in estado_dos_orgaos(self.cfg).items()
                     if estado == "ABERTO"]
        if suspensos:
            self.rotulo_aviso.configure(
                text=f"{', '.join(suspensos)} suspenso — o portal recusou várias "
                     f"consultas seguidas. O robô retoma sozinho.")
            self.faixa_aviso.grid(row=4, column=0, sticky="ew", padx=34,
                                  pady=(18, 28))
        else:
            self.faixa_aviso.grid_remove()

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
        itens = listar_itens(
            self.cfg,
            status=SITUACOES.get(self.filtro_situacao.get()),
            desfecho=RESULTADOS.get(self.filtro_resultado.get()),
            busca=self.busca.get().strip() or None,
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
