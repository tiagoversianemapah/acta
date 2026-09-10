"""Calibragem dos adapters cegos.

Um adapter cego nao le HTML. Ele precisa que alguem ensine, uma vez, onde
ficam os alvos na tela. A posicao e salva como proporcao da janela do Edge,
entao a mesma calibragem continua valendo em resolucoes parecidas.
"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cnd.adapters.federal.rfb.cego import Calibragem, _achar_edge
from cnd.infra import entrada_real, tela

SEGUNDOS_IMOVEL = 2.5
TOLERANCIA_PX = 4

Cor = tuple[int, int, int]
TesteCor = Callable[[Cor], bool]


@dataclass(frozen=True)
class PerfilCalibragem:
    codigo: str
    nome: str
    url: str
    titulo_janela: str
    executavel: str
    passos: tuple[tuple[str, str], ...]
    pontos_necessarios: tuple[str, ...]
    cores_esperadas: dict[str, tuple[str, TesteCor]]
    pontos_condicionais: dict[str, str]
    # (largura, altura) da janela, ou None para maximizar. O SEFAZ-ES so
    # carrega em janela estreita, e calibrar num tamanho e rodar noutro poe
    # todos os pontos no lugar errado.
    janela: tuple[int, int] | None = None


def _parece_botao(cor: Cor) -> bool:
    r, _, b = cor
    return b > r + 40 and b > 90


def _claro(cor: Cor) -> bool:
    return tela.brilho(cor) > 200


def _qualquer(_cor: Cor) -> bool:
    return True


PASSOS_RFB = (
    ("campo_cnpj",
     "o CAMPO branco onde se digita o CNPJ (escrito 'Informe o CNPJ')"),
    ("botao_emitir",
     "o botao azul 'EMITIR CERTIDAO', no canto inferior direito"),
    ("fundo_pagina",
     "uma area BRANCA e vazia da pagina (a margem ao lado do formulario)"),
    ("faixa_alerta",
     "o topo da pagina, na altura de 'Servicos da Receita Federal' -\n"
     "     e onde a faixa de aviso aparece quando o portal recusa"),
)

PASSOS_SEFAZ_ES = (
    ("menu_cnd",
     "o item do menu lateral 'Certidao Negativa de Debito'"),
    ("campo_documento",
     "o CAMPO branco onde se digita CPF / CNPJ"),
    ("botao_emitir",
     "o botao azul 'Emitir Certidao'"),
    ("fundo_pagina",
     "uma area CLARA e vazia da pagina, fora do formulario"),
    ("faixa_alerta",
     "o topo da pagina, onde alertas e mensagens ficam visiveis"),
)


def _perfil(orgao: str | None = None) -> PerfilCalibragem:
    chave = (orgao or "rfb_cego").strip().lower().replace("-", "_")

    if chave in {"rfb", "rfb_pj", "rfb_cego", "receita"}:
        from cnd.adapters.federal.rfb import cego as rfb_cego

        return PerfilCalibragem(
            codigo="rfb_cego",
            nome="Receita Federal",
            url=rfb_cego.URL_FORMULARIO,
            titulo_janela=rfb_cego.TITULO_JANELA,
            executavel=rfb_cego.EXECUTAVEL_NAVEGADOR,
            passos=PASSOS_RFB,
            pontos_necessarios=rfb_cego.PONTOS_NECESSARIOS,
            cores_esperadas={
                "campo_cnpj": ("claro (campo branco)", _claro),
                "botao_emitir": ("azul do botao", _parece_botao),
                "fundo_pagina": ("claro (pagina)", _claro),
                "botao_emitir_nova": ("azul do botao", _parece_botao),
            },
            pontos_condicionais={
                "botao_emitir_nova": "so aparece com a janelinha aberta",
            },
        )

    if chave in {"sefaz_es", "sefazespiritosanto", "es"}:
        from cnd.adapters.estadual import sefaz_es

        return PerfilCalibragem(
            codigo="sefaz_es",
            nome="SEFAZ-ES",
            url=sefaz_es.URL_CONSULTA,
            titulo_janela=sefaz_es.TITULO_JANELA,
            executavel=sefaz_es.EXECUTAVEL_NAVEGADOR,
            passos=PASSOS_SEFAZ_ES,
            janela=(sefaz_es.LARGURA_JANELA, sefaz_es.ALTURA_JANELA),
            pontos_necessarios=sefaz_es.PONTOS_NECESSARIOS,
            cores_esperadas={
                "menu_cnd": ("menu lateral", _qualquer),
                "campo_documento": ("claro (campo branco)", _claro),
                "botao_emitir": ("azul do botao", _parece_botao),
                "fundo_pagina": ("claro (pagina)", _claro),
                "visor_pdf": ("area do PDF", _qualquer),
            },
            pontos_condicionais={
                "visor_pdf": "so aparece depois de emitir uma certidao",
            },
        )

    raise ValueError(
        f"Orgao sem perfil de calibragem: {orgao}. Use RFB_CEGO ou SEFAZ_ES."
    )


def _ler_ponto(perfil: PerfilCalibragem, rotulo: str,
               descricao: str) -> tuple[int, int]:
    """Espera o cursor se mover e depois parar em cima do alvo."""
    print()
    print("-" * 70)
    print(f"  APONTE PARA: {descricao}")
    print("  Leve o mouse ate la e SEGURE PARADO. Nao clique.")
    print()

    entrada_real.trazer_para_frente(perfil.titulo_janela, perfil.executavel)

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
            print(f"    cursor em {atual}  - pare em cima do alvo        ",
                  end="\r", flush=True)
            continue

        if not ja_moveu:
            print("    aguardando voce mover o mouse...                 ",
                  end="\r", flush=True)
            continue

        parado_ha = time.monotonic() - imovel_desde
        if parado_ha >= SEGUNDOS_IMOVEL:
            print(f"    {rotulo}: {atual}                                ")
            return atual

        print(f"    parado em {atual} - registrando em "
              f"{SEGUNDOS_IMOVEL - parado_ha:.1f}s   ", end="\r", flush=True)


def _ler_ponto_valido(
    perfil: PerfilCalibragem,
    rotulo: str,
    descricao: str,
    janela: tuple[int, int, int, int] | None,
    ja_medidos: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    """Le um ponto e confere na hora. Se estiver errado, pede de novo."""
    while True:
        ponto = _ler_ponto(perfil, rotulo, descricao)
        problema = None

        if janela:
            x, y, largura, altura = janela
            if not (x <= ponto[0] <= x + largura and y <= ponto[1] <= y + altura):
                problema = (f"esse ponto caiu FORA da janela do Edge "
                            f"(que vai de ({x}, {y}) a ({x + largura}, "
                            f"{y + altura})).\n"
                            f"     O cursor precisa estar em cima da pagina, "
                            f"nao de outra janela.")

        if not problema:
            for outro, coords in ja_medidos.items():
                if max(abs(coords[0] - ponto[0]), abs(coords[1] - ponto[1])) <= 5:
                    problema = (f"e o mesmo lugar de '{outro}' {coords}.\n"
                                f"     Mova o mouse para o alvo certo desta vez.")
                    break

        if not problema:
            return ponto

        print()
        print(f"  >>> PROBLEMA: {problema}")
        print("  Vamos repetir este ponto.")


def _medir_cor_do_fundo(perfil: PerfilCalibragem,
                        ponto: tuple[int, int]) -> Cor:
    """Mede a cor da pagina com o Edge garantidamente na frente."""
    cor = (0, 0, 0)
    for tentativa in range(5):
        entrada_real.trazer_para_frente(perfil.titulo_janela, perfil.executavel)
        time.sleep(0.6)
        if not entrada_real.em_primeiro_plano(perfil.titulo_janela,
                                              perfil.executavel):
            print(f"    (a janela do Edge nao veio para a frente, "
                  f"tentativa {tentativa + 1}/5)")
            continue
        cor = tela.cor_media(tela.capturar(), *ponto)
        if tela.brilho(cor) >= 150:
            return cor
        print(f"    (a cor saiu escura {cor}, tentando de novo)")
    return cor


def _validar(
    absolutos: dict[str, tuple[int, int]],
    cor_fundo: Cor,
    janela: tuple[int, int, int, int] | None,
    pontos_necessarios: tuple[str, ...],
    pontos_condicionais: tuple[str, ...] = (),
) -> list[str]:
    """Erros que so apareceriam no meio do lote."""
    problemas = []

    faltando = [p for p in pontos_necessarios if p not in absolutos]
    if faltando:
        problemas.append(f"faltam pontos obrigatorios: {', '.join(faltando)}")

    for a in absolutos:
        for b in absolutos:
            if a in pontos_condicionais or b in pontos_condicionais:
                continue
            if a < b and max(abs(absolutos[a][0] - absolutos[b][0]),
                             abs(absolutos[a][1] - absolutos[b][1])) <= 5:
                problemas.append(
                    f"'{a}' e '{b}' estao no mesmo lugar {absolutos[a]} - "
                    f"um dos dois nao chegou a ser medido"
                )

    if tela.brilho(cor_fundo) < 150:
        problemas.append(
            f"a cor de fundo saiu escura {cor_fundo} - a foto pegou outra "
            f"janela por cima do Edge, ou o ponto nao esta numa area clara"
        )

    if janela is None:
        problemas.append("nao consegui medir a janela do Edge")
        return problemas

    x, y, largura, altura = janela
    for nome, (px, py) in absolutos.items():
        if not (x <= px <= x + largura and y <= py <= y + altura):
            problemas.append(
                f"'{nome}' {px, py} caiu FORA da janela do Edge "
                f"(que vai de {x, y} ate {x + largura, y + altura})"
            )

    return problemas


def _abrir_portal(perfil: PerfilCalibragem) -> tuple[int, int, int, int] | None:
    subprocess.run(["taskkill", "/IM", perfil.executavel, "/F"],
                   capture_output=True, check=False)
    time.sleep(2)

    if perfil.janela:
        largura, altura = perfil.janela
        subprocess.Popen([_achar_edge(), "--new-window",
                          f"--window-size={largura},{altura}", perfil.url])
        print(f"\n  abrindo o Edge em {largura}x{altura}...",
              flush=True)
        time.sleep(9)
        entrada_real.trazer_para_frente(perfil.titulo_janela, perfil.executavel)
        # O Edge ignora --window-size quando reabre no estado maximizado que
        # ficou salvo no perfil. Forcar aqui, senao a calibragem sai do
        # tamanho errado e todos os pontos ficam inuteis.
        atual = entrada_real.retangulo_janela(perfil.titulo_janela,
                                              perfil.executavel)
        origem = (atual[0], atual[1]) if atual else (0, 0)
        entrada_real.posicionar_janela(perfil.titulo_janela, perfil.executavel,
                                       (*origem, largura, altura))
        # E ENTAO maximiza, igual ao adapter faz. A janela estreita serve so
        # para a pagina CARREGAR; depois de carregada ela pode crescer, e e na
        # janela grande que o robo vai clicar. Calibrar estreito e trabalhar
        # maximizado poria todos os pontos no lugar errado.
        print("  pagina carregada; maximizando para medir...", flush=True)
        time.sleep(3)
        entrada_real.maximizar(perfil.titulo_janela, perfil.executavel)
    else:
        subprocess.Popen([_achar_edge(), "--start-maximized", perfil.url])
        print("\n  abrindo o Edge maximizado...", flush=True)
        time.sleep(9)
        entrada_real.maximizar(perfil.titulo_janela, perfil.executavel)
    time.sleep(1)

    janela = entrada_real.retangulo_janela(perfil.titulo_janela,
                                           perfil.executavel)
    if janela:
        print(f"  janela do Edge: {janela[2]}x{janela[3]} "
              f"em ({janela[0]}, {janela[1]})", flush=True)
    else:
        print("  AVISO: nao localizei a janela do Edge pelo titulo.", flush=True)
    return janela


def _preparar_formulario_sefaz_es(
    perfil: PerfilCalibragem,
    janela: tuple[int, int, int, int] | None,
    pontos: dict[str, tuple[int, int]],
) -> None:
    if perfil.codigo != "sefaz_es" or "menu_cnd" not in pontos:
        return
    print()
    print("  Abrindo o formulario da SEFAZ-ES pelo menu medido...")
    entrada_real.trazer_para_frente(perfil.titulo_janela, perfil.executavel)
    entrada_real.clicar(*pontos["menu_cnd"])
    time.sleep(5)
    nova = entrada_real.retangulo_janela(perfil.titulo_janela, perfil.executavel)
    if nova:
        janela = nova
    del janela


def _medir_ponto_condicional(
    perfil: PerfilCalibragem,
    janela: tuple[int, int, int, int] | None,
    pontos: dict[str, tuple[int, int]],
) -> None:
    if perfil.codigo == "rfb_cego":
        print()
        print("-" * 70)
        print("  FALTA UM: o botao 'EMITIR NOVA CERTIDAO'.")
        print("  Ele so aparece quando a empresa ja tem certidao valida.")
        print()
        print("  Faca agora, com a sua mao, na janela do Edge:")
        print("    1. digite  32874104000111  no campo")
        print("    2. clique em 'Emitir Certidao'")
        print("    3. espere a janelinha 'Certidao Valida Encontrada' abrir")
        print()
        print("  NAO clique em 'Emitir Nova Certidao' - so vamos apontar para ele.")
        input("  Enter quando a janelinha estiver aberta... ")

        pontos["botao_emitir_nova"] = _ler_ponto_valido(
            perfil,
            "botao_emitir_nova",
            "o botao azul 'EMITIR NOVA CERTIDAO' da janelinha",
            janela,
            pontos,
        )
        return

    if perfil.codigo == "sefaz_es":
        print()
        print("-" * 70)
        print("  FALTA UM: um ponto DENTRO do visualizador de PDF.")
        print("  O portal so mostra esse visor depois de uma emissao real.")
        print()
        print("  Faca agora, com a sua mao, na janela do Edge:")
        print("    1. digite um CNPJ que emita certidao no ES")
        print("    2. aguarde a verificacao de seguranca passar")
        print("    3. clique em 'Emitir Certidao'")
        print("    4. espere o modal com o PDF abrir")
        print()
        print("  NAO feche o modal - so vamos apontar para dentro do PDF.")
        input("  Enter quando o PDF estiver visivel... ")

        pontos["visor_pdf"] = _ler_ponto_valido(
            perfil,
            "visor_pdf",
            "um ponto dentro do visualizador de PDF, em cima da pagina do PDF",
            janela,
            pontos,
        )


def _orgao_por_destino(destino: Path) -> str:
    return destino.stem if destino and destino.stem else "rfb_cego"


def conferir(origem: Path, destino_imagem: Path,
             orgao: str | None = None) -> None:
    """Abre o portal e desenha os pontos calibrados sobre uma foto da tela."""
    from PIL import ImageDraw

    perfil = _perfil(orgao or _orgao_por_destino(origem))
    calibragem = Calibragem.carregar(origem)
    calibragem.conferir(pontos_necessarios=perfil.pontos_necessarios)

    janela = _abrir_portal(perfil)
    if janela is None:
        raise RuntimeError("nao achei a janela do Edge")

    if perfil.codigo == "sefaz_es" and "menu_cnd" in calibragem.pontos:
        entrada_real.clicar(*calibragem.ponto("menu_cnd", janela))
        time.sleep(5)
        janela = entrada_real.retangulo_janela(perfil.titulo_janela,
                                               perfil.executavel) or janela

    imagem = tela.capturar().convert("RGB")
    desenho = ImageDraw.Draw(imagem)

    print()
    print(f"  perfil: {perfil.nome}")
    print(f"  janela agora: {janela[2]}x{janela[3]}")
    print()
    for nome in calibragem.pontos:
        x, y = calibragem.ponto(nome, janela)
        cor = tela.cor_media(imagem, x, y, raio=4)

        rotulo, teste = perfil.cores_esperadas.get(nome, ("", _qualquer))
        ok = teste(cor)
        marca = "ok " if ok else "??"
        if nome in perfil.pontos_condicionais:
            marca = "-- "
            rotulo += f" ({perfil.pontos_condicionais[nome]})"

        print(f"  {marca} {nome:20s} ({x:5d},{y:5d})  cor {cor!s:18s} "
              f"esperado: {rotulo}")

        contorno = (255, 0, 0) if not ok else (0, 170, 0)
        desenho.ellipse([x - 26, y - 26, x + 26, y + 26],
                        outline=contorno, width=5)
        desenho.line([x - 40, y, x + 40, y], fill=contorno, width=2)
        desenho.line([x, y - 40, x, y + 40], fill=contorno, width=2)
        desenho.text((x + 32, y - 46), nome, fill=contorno)

    destino_imagem.parent.mkdir(parents=True, exist_ok=True)
    imagem.save(destino_imagem)
    print()
    print(f"  imagem salva em: {destino_imagem}")
    print("  ABRA ESSA IMAGEM e veja se os circulos cairam em cima dos alvos.")


def calibrar(destino: Path, orgao: str | None = None) -> Calibragem:
    perfil = _perfil(orgao or _orgao_por_destino(destino))

    print()
    print("=" * 70)
    print(f"  CALIBRAGEM DO ROBO CEGO - {perfil.nome}")
    print("=" * 70)
    print()
    if perfil.janela:
        largura, altura = perfil.janela
        print(f"  Vou abrir o portal de {perfil.nome} numa janela estreita")
        print(f"  ({largura}x{altura}) e so DEPOIS maximizar. Nao estranhe: e")
        print("  na largura pequena que este portal carrega inteiro, e e na")
        print("  janela grande que o robo vai clicar - por isso medimos nela.")
    else:
        print(f"  Vou abrir o portal de {perfil.nome} maximizado.")
    print("  Para cada item, leve o mouse ate o alvo e segure parado.")
    print("  Nao clique em nada e nao mexa no tamanho da janela.")
    print()
    if perfil.codigo == "rfb_cego":
        print("  Tenha a mao o CNPJ 32874104000111 - voce vai precisar dele no fim.")
    elif perfil.codigo == "sefaz_es":
        print("  Tenha a mao um CNPJ que emita certidao no ES.")
        print("  A etapa final abre uma emissao real para medir o visor do PDF.")
    print()
    input("  Enter para comecar... ")

    janela = _abrir_portal(perfil)

    pontos: dict[str, tuple[int, int]] = {}
    cor_fundo: Cor | None = None

    for rotulo, descricao in perfil.passos:
        pontos[rotulo] = _ler_ponto_valido(rotulo=rotulo,
                                           descricao=descricao,
                                           perfil=perfil,
                                           janela=janela,
                                           ja_medidos=pontos)

        if rotulo == "menu_cnd":
            _preparar_formulario_sefaz_es(perfil, janela, pontos)

        if rotulo == "fundo_pagina":
            while True:
                print("\n  medindo a cor da pagina...", flush=True)
                cor_fundo = _medir_cor_do_fundo(perfil, pontos["fundo_pagina"])
                print(f"  cor da pagina: {cor_fundo} "
                      f"(brilho {tela.brilho(cor_fundo):.0f})")
                if tela.brilho(cor_fundo) >= 150:
                    break
                print()
                print("  >>> PROBLEMA: a cor saiu escura. A foto pegou outra")
                print("      janela por cima do Edge, ou o ponto nao esta numa")
                print("      area clara da pagina.")
                print("      Vamos remarcar a area clara.")
                pontos[rotulo] = _ler_ponto_valido(
                    perfil,
                    rotulo,
                    descricao,
                    janela,
                    {k: v for k, v in pontos.items() if k != rotulo},
                )

    _medir_ponto_condicional(perfil, janela, pontos)

    janela = entrada_real.retangulo_janela(perfil.titulo_janela,
                                           perfil.executavel) or janela
    problemas = _validar(pontos, cor_fundo or (0, 0, 0), janela,
                         perfil.pontos_necessarios,
                         tuple(perfil.pontos_condicionais))

    print()
    print("=" * 70)
    if problemas:
        print("  CALIBRAGEM COM PROBLEMA - nao vou salvar:")
        for problema in problemas:
            print(f"    - {problema}")
        print()
        print("  Rode `cnd calibrar` de novo.")
        print("=" * 70)
        raise SystemExit(1)

    calibragem = Calibragem.de_absolutos(janela, pontos, cor_fundo or (0, 0, 0))
    calibragem.salvar(destino)

    print(f"  CALIBRADO. Salvo em {destino}")
    print(f"  janela do Edge: {janela[2]}x{janela[3]} em ({janela[0]}, {janela[1]})")
    print(f"  cor de fundo  : {cor_fundo} (brilho {tela.brilho(cor_fundo):.0f})")
    print()
    print("  medidas guardadas como proporcao da janela:")
    for rotulo, (fx, fy) in calibragem.pontos.items():
        px, py = pontos[rotulo]
        print(f"    {rotulo:20s} {fx * 100:5.1f}% x {fy * 100:5.1f}%   "
              f"(aqui: {px}, {py})")
    print("=" * 70)
    return calibragem
