"""Calibragem do adapter cego: ensinar ao robô onde ficam as coisas.

Como o robô cego não lê o HTML, alguém precisa medir uma vez onde estão o
campo de CNPJ e os botões. O jeito mais simples e à prova de erro é você
apontar com o mouse: o programa lê a posição do cursor.

A leitura não usa contagem regressiva, e sim **parada do cursor**: você
leva o mouse até o alvo e segura parado; quando ele fica imóvel por alguns
segundos, a posição é registrada. Assim ninguém corre contra o relógio, e
não é preciso tocar no teclado (o que traria outra janela para a frente e
estragaria a medição).

As coordenadas são absolutas, com a janela maximizada. Por isso a
calibragem só vale enquanto a resolução da tela não mudar — o adapter
confere isso na subida e recusa rodar com medidas velhas.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from cnd.adapters.rfb_cego import (
    EXECUTAVEL_NAVEGADOR,
    TITULO_JANELA,
    URL_FORMULARIO,
    Calibragem,
    _achar_edge,
)
from cnd.infra import entrada_real, tela

SEGUNDOS_IMOVEL = 2.5
TOLERANCIA_PX = 4

PASSOS = [
    ("campo_cnpj",
     "o CAMPO branco onde se digita o CNPJ (escrito 'Informe o CNPJ')"),
    ("botao_emitir",
     "o botão azul 'EMITIR CERTIDÃO', no canto inferior direito"),
    ("fundo_pagina",
     "uma área BRANCA e vazia da página (a margem ao lado do formulário)"),
    ("faixa_alerta",
     "o topo da página, na altura de 'Serviços da Receita Federal' —\n"
     "     é onde a faixa de aviso aparece quando o portal recusa"),
]


def _ler_ponto(rotulo: str, descricao: str) -> tuple[int, int]:
    """Espera o cursor se mover e depois parar em cima do alvo."""
    print()
    print("-" * 70)
    print(f"  APONTE PARA: {descricao}")
    print("  Leve o mouse até lá e SEGURE PARADO. Não clique.")
    print()

    entrada_real.trazer_para_frente(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)

    ultimo = entrada_real.posicao()
    imovel_desde = time.monotonic()
    ja_moveu = False

    while True:
        time.sleep(0.12)
        atual = entrada_real.posicao()
        distancia = max(abs(atual[0] - ultimo[0]), abs(atual[1] - ultimo[1]))

        if distancia > TOLERANCIA_PX:
            ja_moveu = True
            ultimo = atual
            imovel_desde = time.monotonic()
            print(f"    cursor em {atual}  — pare em cima do alvo        ",
                  end="\r", flush=True)
            continue

        if not ja_moveu:
            print("    aguardando você mover o mouse...                 ",
                  end="\r", flush=True)
            continue

        parado_ha = time.monotonic() - imovel_desde
        if parado_ha >= SEGUNDOS_IMOVEL:
            print(f"    {rotulo}: {atual}                                ")
            return atual

        print(f"    parado em {atual} — registrando em "
              f"{SEGUNDOS_IMOVEL - parado_ha:.1f}s   ", end="\r", flush=True)


def _ler_ponto_valido(rotulo: str, descricao: str,
                      janela: tuple[int, int, int, int] | None,
                      ja_medidos: dict[str, tuple[int, int]]) -> tuple[int, int]:
    """Lê um ponto e confere na hora. Se estiver errado, pede de novo.

    Validar no fim obrigaria a refazer os cinco por causa de um. Aqui o erro
    aparece no momento em que acontece, e você repete só aquele.
    """
    while True:
        ponto = _ler_ponto(rotulo, descricao)
        problema = None

        if janela:
            x, y, largura, altura = janela
            if not (x <= ponto[0] <= x + largura and y <= ponto[1] <= y + altura):
                problema = (f"esse ponto caiu FORA da janela do Edge "
                            f"(que vai de ({x}, {y}) a ({x + largura}, {y + altura})).\n"
                            f"     O cursor precisa estar em cima da página, "
                            f"não de outra janela.")

        if not problema:
            for outro, coords in ja_medidos.items():
                if max(abs(coords[0] - ponto[0]), abs(coords[1] - ponto[1])) <= 5:
                    problema = (f"é o mesmo lugar de '{outro}' {coords}.\n"
                                f"     Mova o mouse para o alvo certo desta vez.")
                    break

        if not problema:
            return ponto

        print()
        print(f"  >>> PROBLEMA: {problema}")
        print("  Vamos repetir este ponto.")


def _medir_cor_do_fundo(ponto: tuple[int, int]) -> tuple[int, int, int]:
    """Mede a cor da página com o Edge garantidamente na frente.

    Tenta algumas vezes: trazer janela para frente no Windows nem sempre
    funciona de primeira, e uma foto tirada com o editor por cima devolve
    a cor errada — foi o que estragou a primeira calibragem.
    """
    for tentativa in range(5):
        entrada_real.trazer_para_frente(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
        time.sleep(0.6)
        if not entrada_real.em_primeiro_plano(TITULO_JANELA, EXECUTAVEL_NAVEGADOR):
            print(f"    (a janela do Edge não veio para a frente, "
                  f"tentativa {tentativa + 1}/5)")
            continue
        cor = tela.cor_media(tela.capturar(), *ponto)
        if tela.brilho(cor) >= 150:
            return cor
        print(f"    (a cor saiu escura {cor}, tentando de novo)")
    return cor


def _validar(absolutos: dict, cor_fundo: tuple,
             janela: tuple[int, int, int, int] | None) -> list[str]:
    """Erros que só apareceriam no meio do lote — melhor gritar agora."""
    problemas = []

    for a in absolutos:
        for b in absolutos:
            if a < b and max(abs(absolutos[a][0] - absolutos[b][0]),
                             abs(absolutos[a][1] - absolutos[b][1])) <= 5:
                problemas.append(
                    f"'{a}' e '{b}' estão no mesmo lugar {absolutos[a]} — "
                    f"um dos dois não chegou a ser medido"
                )

    if tela.brilho(cor_fundo) < 150:
        problemas.append(
            f"a cor de fundo saiu escura {cor_fundo} — a página da Receita é "
            f"branca, então a foto pegou outra janela por cima"
        )

    if janela is None:
        problemas.append("não consegui medir a janela do Edge")
        return problemas

    x, y, largura, altura = janela
    for nome, (px, py) in absolutos.items():
        if not (x <= px <= x + largura and y <= py <= y + altura):
            problemas.append(
                f"'{nome}' {px, py} caiu FORA da janela do Edge "
                f"(que vai de {x, y} até {x + largura, y + altura})"
            )

    return problemas


CORES_ESPERADAS = {
    "campo_cnpj": ("claro (campo branco)", lambda c: tela.brilho(c) > 200),
    "botao_emitir": ("azul do botão", lambda c: c[2] > c[0] + 40 and c[2] > 90),
    "fundo_pagina": ("claro (página)", lambda c: tela.brilho(c) > 200),
    "botao_emitir_nova": ("azul do botão", lambda c: c[2] > c[0] + 40 and c[2] > 90),
}


def conferir(origem: Path, destino_imagem: Path) -> None:
    """Abre o portal e desenha os pontos calibrados sobre uma foto da tela.

    É a única forma honesta de verificar um robô cego: em vez de acreditar
    nas coordenadas, você olha a imagem e vê se as marcas caíram em cima do
    campo e dos botões.
    """
    from PIL import ImageDraw

    calibragem = Calibragem.carregar(origem)

    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"],
                   capture_output=True, check=False)
    time.sleep(2)
    subprocess.Popen([_achar_edge(), "--start-maximized", URL_FORMULARIO])
    print("  abrindo o portal...", flush=True)
    time.sleep(10)
    entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
    time.sleep(1)

    janela = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
    if janela is None:
        raise RuntimeError("não achei a janela do Edge")

    imagem = tela.capturar().convert("RGB")
    desenho = ImageDraw.Draw(imagem)

    print()
    print(f"  janela agora: {janela[2]}x{janela[3]}")
    print()
    for nome in calibragem.pontos:
        x, y = calibragem.ponto(nome, janela)
        cor = tela.cor_media(imagem, x, y, raio=4)

        rotulo, teste = CORES_ESPERADAS.get(nome, ("", lambda _: True))
        ok = teste(cor)
        marca = "ok " if ok else "??"
        if nome == "botao_emitir_nova":
            marca = "-- "        # só existe com a janelinha aberta
            rotulo += " (só aparece com a janelinha aberta)"

        print(f"  {marca} {nome:20s} ({x:5d},{y:5d})  cor {cor!s:18s} "
              f"esperado: {rotulo}")

        contorno = (255, 0, 0) if not ok else (0, 170, 0)
        desenho.ellipse([x - 26, y - 26, x + 26, y + 26], outline=contorno, width=5)
        desenho.line([x - 40, y, x + 40, y], fill=contorno, width=2)
        desenho.line([x, y - 40, x, y + 40], fill=contorno, width=2)
        desenho.text((x + 32, y - 46), nome, fill=contorno)

    destino_imagem.parent.mkdir(parents=True, exist_ok=True)
    imagem.save(destino_imagem)
    print()
    print(f"  imagem salva em: {destino_imagem}")
    print("  ABRA ESSA IMAGEM e veja se os círculos caíram em cima dos alvos.")


def calibrar(destino: Path) -> Calibragem:
    print()
    print("=" * 70)
    print("  CALIBRAGEM DO ROBÔ CEGO")
    print("=" * 70)
    print()
    print("  Vou abrir o portal da Receita maximizado.")
    print("  Para cada item, leve o mouse até o alvo e segure parado.")
    print("  Não clique em nada e não mexa no tamanho da janela.")
    print()
    print("  Tenha à mão o CNPJ 32874104000111 — você vai precisar dele no fim.")
    print()
    input("  Enter para começar... ")

    # Fecha o Edge antes: com ele já aberto, o --start-maximized é ignorado
    # e a janela fica de qualquer tamanho.
    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"],
                   capture_output=True, check=False)
    time.sleep(2)

    subprocess.Popen([_achar_edge(), "--start-maximized", URL_FORMULARIO])
    print("\n  abrindo o Edge maximizado...", flush=True)
    time.sleep(9)
    entrada_real.maximizar(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
    time.sleep(1)

    janela = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR)
    if janela:
        print(f"  janela do Edge: {janela[2]}x{janela[3]} "
              f"em ({janela[0]}, {janela[1]})", flush=True)
    else:
        print("  AVISO: não localizei a janela do Edge pelo título.", flush=True)

    pontos: dict[str, tuple[int, int]] = {}
    cor_fundo: tuple[int, int, int] | None = None

    for rotulo, descricao in PASSOS:
        pontos[rotulo] = _ler_ponto_valido(rotulo, descricao, janela, pontos)

        # A cor é medida logo depois de marcar a área branca, com o Edge na
        # frente. Medir isso no fim daria errado: qualquer Enter no terminal
        # traz o editor para a frente e a foto sai da janela errada.
        if rotulo == "fundo_pagina":
            while True:
                print("\n  medindo a cor da página...", flush=True)
                cor_fundo = _medir_cor_do_fundo(pontos["fundo_pagina"])
                print(f"  cor da página: {cor_fundo} "
                      f"(brilho {tela.brilho(cor_fundo):.0f})")
                if tela.brilho(cor_fundo) >= 150:
                    break
                print()
                print("  >>> PROBLEMA: a cor saiu escura. A foto pegou outra")
                print("      janela por cima do Edge, ou o ponto não está numa")
                print("      área branca da página.")
                print("      Vamos remarcar a área branca.")
                pontos[rotulo] = _ler_ponto_valido(rotulo, descricao, janela,
                                                   {k: v for k, v in pontos.items()
                                                    if k != rotulo})

    # O botão "Emitir Nova Certidão" só existe com a janela modal aberta.
    print()
    print("-" * 70)
    print("  FALTA UM: o botão 'EMITIR NOVA CERTIDÃO'.")
    print("  Ele só aparece quando a empresa já tem certidão válida.")
    print()
    print("  Faça agora, com a sua mão, na janela do Edge:")
    print("    1. digite  32874104000111  no campo")
    print("    2. clique em 'Emitir Certidão'")
    print("    3. espere a janelinha 'Certidão Válida Encontrada' abrir")
    print()
    print("  NÃO clique em 'Emitir Nova Certidão' — só vamos apontar para ele.")
    print()
    input("  Enter quando a janelinha estiver aberta... ")

    pontos["botao_emitir_nova"] = _ler_ponto_valido(
        "botao_emitir_nova", "o botão azul 'EMITIR NOVA CERTIDÃO' da janelinha",
        janela, pontos)

    janela = entrada_real.retangulo_janela(TITULO_JANELA, EXECUTAVEL_NAVEGADOR) or janela
    problemas = _validar(pontos, cor_fundo, janela)

    print()
    print("=" * 70)
    if problemas:
        print("  CALIBRAGEM COM PROBLEMA — não vou salvar:")
        for problema in problemas:
            print(f"    - {problema}")
        print()
        print("  Rode `cnd calibrar` de novo.")
        print("=" * 70)
        raise SystemExit(1)

    calibragem = Calibragem.de_absolutos(janela, pontos, cor_fundo)
    calibragem.salvar(destino)

    print(f"  CALIBRADO. Salvo em {destino}")
    print(f"  janela do Edge: {janela[2]}x{janela[3]} em ({janela[0]}, {janela[1]})")
    print(f"  cor de fundo  : {cor_fundo} (brilho {tela.brilho(cor_fundo):.0f})")
    print()
    print("  medidas guardadas como proporção da janela — funcionam em")
    print("  qualquer resolução de tela parecida:")
    for rotulo, (fx, fy) in calibragem.pontos.items():
        px, py = pontos[rotulo]
        print(f"    {rotulo:20s} {fx * 100:5.1f}% x {fy * 100:5.1f}%   "
              f"(aqui: {px}, {py})")
    print("=" * 70)
    return calibragem
