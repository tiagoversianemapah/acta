"""Identidade visual: as cores da Mapah e a marca do aplicativo.

As cores saem do logotipo da empresa — o azul-marinho do texto e o amarelo
da seta. Não são escolha estética: um sistema interno que usa outra paleta
parece software de terceiro, e este é da casa.

O branco domina; o azul aparece na barra lateral e nas ações principais; o
amarelo é reservado para uma coisa só, o que exige atenção. Cor que aparece
em tudo deixa de significar alguma coisa.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

# --- marca -------------------------------------------------------------
AZUL = "#2B2A6B"          # azul-marinho do logotipo
AZUL_CLARO = "#3E3D91"
AZUL_ESCURO = "#1E1D4E"
AMARELO = "#FCB817"       # a seta do logotipo
AMARELO_ESCURO = "#D99A0A"

# --- superfícies (tema claro) -----------------------------------------
BRANCO = "#FFFFFF"
FUNDO = "#FFFFFF"
PAPEL = "#F5F7FA"         # cartões e faixas
PAPEL_2 = "#EDF0F4"
BORDA = "#E4E8ED"

# --- texto -------------------------------------------------------------
TEXTO = "#171A21"
TEXTO_2 = "#59626F"
TEXTO_3 = "#8A94A2"
TEXTO_NA_BARRA = "#EDEEF5"
TEXTO_NA_BARRA_2 = "#9E9FC4"

# --- estados -----------------------------------------------------------
VERDE = "#1B7F4E"
VERDE_FUNDO = "#E9F5EE"
AMBAR = "#8A5D00"
AMBAR_FUNDO = "#FDF4E0"
VERMELHO = "#B02A1C"
VERMELHO_FUNDO = "#FBECEA"


def desenhar_marca(tamanho: int = 128, sobre_escuro: bool = False) -> Image.Image:
    """A seta da Mapah — o elemento gráfico que identifica a empresa.

    Desenhada em resolução alta e reduzida depois: é o que dá a borda lisa,
    sem depender do antisserrilhado do sistema, e mantém a marca nítida
    tanto no ícone de 16px da barra de tarefas quanto na tela em 150%.
    """
    escala = 8
    lado = tamanho * escala
    imagem = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    desenho = ImageDraw.Draw(imagem)

    # A seta é um triângulo com o lado de baixo escavado, o que lhe dá o
    # aspecto de cursor em movimento. As margens generosas evitam que a
    # ponta encoste na borda da imagem — encostando, o redimensionamento
    # corta o pixel da ponta e a seta sai truncada em tamanho pequeno.
    ponta = (lado * 0.82, lado * 0.20)
    base_baixo = (lado * 0.66, lado * 0.80)
    base_esquerda = (lado * 0.20, lado * 0.58)

    def curva(inicio, controle, fim, passos=48):
        saida = []
        for i in range(1, passos):
            t = i / passos
            um = 1 - t
            saida.append((
                um * um * inicio[0] + 2 * um * t * controle[0] + t * t * fim[0],
                um * um * inicio[1] + 2 * um * t * controle[1] + t * t * fim[1],
            ))
        return saida

    pontos = [ponta]
    pontos += curva(ponta, (lado * 0.80, lado * 0.58), base_baixo)
    pontos.append(base_baixo)
    pontos += curva(base_baixo, (lado * 0.50, lado * 0.58), base_esquerda)
    pontos.append(base_esquerda)

    desenho.polygon(pontos, fill=AMARELO)
    return imagem.resize((tamanho, tamanho), Image.LANCZOS)


def desenhar_icone_app(tamanho: int = 256) -> Image.Image:
    """Ícone do aplicativo: a seta amarela sobre o azul da marca."""
    escala = 4
    lado = tamanho * escala
    imagem = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    desenho = ImageDraw.Draw(imagem)

    desenho.rounded_rectangle([0, 0, lado, lado], radius=int(lado * 0.22),
                              fill=AZUL)
    seta = desenhar_marca(int(lado * 0.62))
    imagem.alpha_composite(seta, (int(lado * 0.19), int(lado * 0.19)))
    return imagem.resize((tamanho, tamanho), Image.LANCZOS)


def salvar_icone_janela(caminho) -> None:
    """Grava o .ico da janela e da barra de tarefas."""
    desenhar_icone_app(256).save(
        caminho, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
               (128, 128), (256, 256)],
    )
