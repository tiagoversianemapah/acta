"""Mede e treina o leitor de captcha da Prefeitura de Sao Luis.

    python ferramentas/treinar_ocr_sao_luis.py --aferir 100   # so mede
    python ferramentas/treinar_ocr_sao_luis.py --treinar 300  # cresce o banco
    python ferramentas/treinar_ocr_sao_luis.py --semear 20    # rotula a mao

Por que existe. O adapter ja sai com um banco SEMEADO no pacote
(`src/cnd/adapters/municipal/captcha_sao_luis.json`), montado em 24/09/2026
so com glifos que o portal confirmou - entao ninguem precisa rodar isto para
ligar o orgao. A ferramenta serve para medir o acerto, crescer o banco de uma
maquina e, se a Prefeitura trocar a fonte do captcha, refazer a semente:

    --semente   grava no arquivo semente, e nao em data/ocr/sao_luis.json

Por que e seguro. O portal confere o codigo DE GRACA: o "Emitir" sem CNPJ
volta recusado por falta dele e, na mesma resposta, diz se o codigo estava
errado (`Portal.conferir_captcha`). Nenhuma certidao e emitida. Ainda assim
FALA COM O PORTAL REAL: ha uma pausa entre as idas, e nao rode em cima de um
lote em producao.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from cnd.adapters.estadual.captcha_ma import ALFABETO, BancoCaptcha  # noqa: E402
from cnd.adapters.municipal import sao_luis  # noqa: E402

PAUSA_S = 1.0          # ritmo educado entre as idas ao portal
POR_SESSAO = 25        # imagens por sessao antes de abrir outra
PASTA_TEMP = RAIZ / "data" / "ocr" / "_captchas_sao_luis"
# O portal nao usa o zero (conferido em 24/09/2026: "O" e sempre letra).
ALFABETO_SAO_LUIS = ALFABETO - {"0"}


def cobertura(banco: BancoCaptcha) -> None:
    faltam = sorted(c for c in ALFABETO_SAO_LUIS if not banco.amostras.get(c))
    print(f"  cobertura: {len(ALFABETO_SAO_LUIS) - len(faltam)}/"
          f"{len(ALFABETO_SAO_LUIS)} caracteres, {banco.total} exemplos")
    if faltam:
        print(f"  faltam ({len(faltam)}): {' '.join(faltam)}")


class Sessao:
    """Uma sessao do portal que se renova a cada POR_SESSAO imagens."""

    def __init__(self, url: str):
        self.url = url
        self.usos = 0
        self.portal: sao_luis.Portal | None = None
        self.formulario: sao_luis.Formulario | None = None

    def imagem(self):
        if self.formulario is None or self.usos >= POR_SESSAO:
            self.portal = sao_luis.Portal(self.url)
            self.formulario = self.portal.abrir()
            self.usos = 0
            if self.formulario is None:
                return None
        self.usos += 1
        return self.portal.captcha(self.formulario)

    def confere(self, codigo: str) -> bool:
        return self.portal.conferir_captcha(self.formulario, codigo)


def _mostrar(imagem, indice: int) -> None:
    PASTA_TEMP.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_TEMP / f"captcha_{indice:02d}.png"
    imagem.resize((imagem.width * 6, imagem.height * 6)).save(caminho)
    abrir = getattr(os, "startfile", None)
    if abrir:
        with contextlib.suppress(OSError):
            abrir(str(caminho))
    else:
        print(f"  abra a imagem: {caminho}")


def semear(sessao: Sessao, banco: BancoCaptcha, destino: Path,
           quantos: int) -> None:
    print(f"Rotulando {quantos} captchas: cada um abre ampliado; digite o "
          f"codigo e Enter (vazio pula).")
    for i in range(quantos):
        imagem = sessao.imagem()
        if imagem is None:
            time.sleep(PAUSA_S)
            continue
        _mostrar(imagem, i + 1)
        codigo = input(f"[{i + 1}/{quantos}] codigo: ").strip()
        if len(codigo) != 4:
            continue
        if sessao.confere(codigo):
            banco.aprender(imagem, codigo)
            print("  confirmado pelo portal e aprendido")
        else:
            print("  o portal nao aceitou - descartado")
        time.sleep(PAUSA_S)
    banco.salvar(destino)
    cobertura(banco)


def treinar(sessao: Sessao, banco: BancoCaptcha, destino: Path,
            rodadas: int, gravar: bool) -> None:
    if banco.vazio:
        sys.exit("Banco vazio: rode antes com --semear.")
    acertos = tentativas = sem_leitura = 0
    try:
        for i in range(rodadas):
            imagem = sessao.imagem()
            palpite = banco.ler(imagem) if imagem is not None else None
            if not palpite:
                # Glifos grudados: o adapter tambem descarta e pede outra.
                sem_leitura += 1
            else:
                tentativas += 1
                if sessao.confere(palpite):
                    acertos += 1
                    if gravar:
                        banco.aprender(imagem, palpite)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1:4d}  acerto={acertos}/{tentativas}  "
                      f"sem leitura={sem_leitura}  banco={banco.total}")
                if gravar:
                    banco.salvar(destino)
            time.sleep(PAUSA_S)
    except KeyboardInterrupt:
        print("\n  interrompido")
    finally:
        if gravar:
            banco.salvar(destino)
    taxa = acertos / max(1, tentativas)
    print(f"Acerto: {acertos}/{tentativas} = {taxa:.0%} das imagens lidas; "
          f"{sem_leitura} descartadas sem leitura.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Mede e treina o leitor de captcha de Sao Luis")
    p.add_argument("--aferir", type=int, metavar="N",
                   help="le N captchas e so mede o acerto, sem gravar")
    p.add_argument("--treinar", type=int, metavar="N",
                   help="le N captchas e aprende os que o portal confirmar")
    p.add_argument("--semear", type=int, metavar="N",
                   help="rotula N captchas a mao")
    p.add_argument("--semente", action="store_true",
                   help="le e grava a semente do pacote, e nao a da maquina")
    p.add_argument("--url", default=sao_luis.URL_CONSULTA)
    args = p.parse_args()

    if args.semente:
        destino = sao_luis.SEMENTE
        banco = BancoCaptcha.carregar(destino)
    else:
        destino = sao_luis.BANCO_PADRAO
        banco = BancoCaptcha.carregar(destino)
        if banco.vazio:
            banco = BancoCaptcha.carregar(sao_luis.SEMENTE)
    sessao = Sessao(args.url)
    print(f"Portal: {args.url}\nBanco:  {destino}")
    cobertura(banco)
    print()

    if args.semear:
        semear(sessao, banco, destino, args.semear)
    elif args.treinar:
        treinar(sessao, banco, destino, args.treinar, gravar=True)
    elif args.aferir:
        treinar(sessao, banco, destino, args.aferir, gravar=False)
    else:
        p.error("escolha --aferir, --treinar ou --semear")


if __name__ == "__main__":
    main()
