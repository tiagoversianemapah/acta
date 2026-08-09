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

# O produto se chama ACTA; `cnd` é o nome do pacote e da linha de comando.
# Fica num lugar só para que a janela, o executável e os atalhos não possam
# divergir entre si.
NOME_PRODUTO = "ACTA"
DESCRICAO_PRODUTO = "Emissão de Certidões"

# --- marca -------------------------------------------------------------
AZUL = "#2B2A6B"          # azul-marinho do logotipo: ações principais
AZUL_CLARO = "#3B3A8F"    # o mesmo azul sob o cursor
AZUL_ESCURO = "#242353"   # item selecionado na barra lateral
AZUL_PROFUNDO = "#17163C" # a própria barra lateral
AMARELO = "#FCB817"       # a seta do logotipo
AMARELO_ESCURO = "#D99A0A"

# --- superfícies -------------------------------------------------------
# A página é cinza e os cartões são brancos, não o contrário. É o que cria
# profundidade sem sombra nenhuma: cartão branco sobre cinza se destaca
# sozinho, enquanto branco sobre branco depende de uma borda que, fina o
# bastante para ser elegante, fica invisível na tela do escritório.
BRANCO = "#FFFFFF"        # cartões, campos, tabela
FUNDO = "#F5F7FA"         # a página
ZEBRA = "#FAFBFC"         # linha alternada da tabela — quase imperceptível
PAPEL = "#EFF3F8"         # sob o cursor, cabeçalho de tabela
PAPEL_2 = "#E3E9F1"       # trilho da barra de progresso
# Bordas claras de propósito. O que separa os blocos é o contraste entre o
# branco do cartão e o cinza da página; a borda só fecha a forma. Escura,
# ela vira grade e a tela fica pesada.
BORDA = "#E8ECF2"
BORDA_FORTE = "#D2D9E4"   # borda de campo, que precisa se ver

# O azul de leitura: números, links e o item selecionado. Mais vivo que o
# azul-marinho da marca, que é escuro demais para número grande e para
# texto pequeno — e continua da mesma família, então não briga com a logo.
AZUL_VIVO = "#2563EB"
AZUL_VIVO_FUNDO = "#EFF4FE"
AZUL_VIVO_BORDA = "#D3E1FB"

# --- barra lateral (clara) ---------------------------------------------
BARRA = "#FFFFFF"
BARRA_BORDA = "#E6E9EF"
BARRA_ATIVO = AZUL_VIVO_FUNDO

# --- texto -------------------------------------------------------------
TEXTO = "#0F172A"
TEXTO_2 = "#475569"
TEXTO_3 = "#94A3B8"
TEXTO_NA_BARRA = "#111827"
TEXTO_NA_BARRA_2 = "#5A6474"

# --- estados -----------------------------------------------------------
# Escolhidos para passar em contraste sobre branco (AA), porque são eles
# que dizem se a empresa está limpa — informação que não pode depender de
# quem enxerga bem.
VERDE = "#0F7A45"
VERDE_FUNDO = "#E6F4EC"
AMBAR = "#8A5A0B"
AMBAR_FUNDO = "#FDF3E0"
VERMELHO = "#B3261E"
VERMELHO_FUNDO = "#FBEBE9"


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


ARQUIVOS_DE_ICONE = ("segoeicons.ttf", "segmdl2.ttf")


def desenhar_glifo(codigo: str, cor: str, tamanho: int = 18) -> Image.Image | None:
    """Rende um ícone do Windows como imagem de fundo transparente.

    Rótulo de texto por cima de um botão carrega o próprio fundo, e ele
    aparece como um retângulo recortado assim que o botão muda de cor no
    hover ou na seleção. Imagem com canal alfa compõe sobre qualquer fundo.

    Devolve None quando a fonte de ícones não existe — Windows mais antigo,
    ou instalação sem ela. Aí o menu fica só com o texto, que é melhor do
    que um quadradinho de glifo ausente.
    """
    from pathlib import Path

    from PIL import ImageFont

    for nome in ARQUIVOS_DE_ICONE:
        caminho = Path("C:/Windows/Fonts") / nome
        if not caminho.exists():
            continue
        try:
            escala = 4
            fonte = ImageFont.truetype(str(caminho), tamanho * escala)
            lado = tamanho * escala
            imagem = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
            desenho = ImageDraw.Draw(imagem)
            caixa = desenho.textbbox((0, 0), codigo, font=fonte)
            desenho.text(((lado - caixa[2] - caixa[0]) / 2,
                          (lado - caixa[3] - caixa[1]) / 2),
                         codigo, font=fonte, fill=cor)
            return imagem.resize((tamanho, tamanho), Image.LANCZOS)
        except OSError:
            continue
    return None


def salvar_icone_janela(caminho) -> None:
    """Grava o .ico da janela e da barra de tarefas."""
    desenhar_icone_app(256).save(
        caminho, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
               (128, 128), (256, 256)],
    )
