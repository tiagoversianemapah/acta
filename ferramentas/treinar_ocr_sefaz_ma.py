"""Treina o leitor de captcha da SEFAZ-MA nesta máquina.

    python ferramentas/treinar_ocr_sefaz_ma.py --semear 20   # rotula à mão
    python ferramentas/treinar_ocr_sefaz_ma.py --treinar 300 # cresce sozinho
    python ferramentas/treinar_ocr_sefaz_ma.py --aferir 100  # só mede o acerto

Por que existe. O portal do MA exige captcha em toda emissão, e o adapter lê
esse captcha localmente (Pillow puro, sem serviço externo — ver ADR-006). Para
ler, ele precisa de um banco de templates rotulados, que é o que esta
ferramenta constrói e guarda em `data/ocr/sefaz_ma.json`. É o análogo do
`cnd calibrar` do ES: uma preparação por máquina, feita uma vez.

Como funciona, e por que é seguro. O portal valida o palpite DE GRAÇA, sem
emitir certidão nenhuma: o AJAX do botão responde "arma / não arma". Então:

  --semear   baixa alguns captchas, abre a imagem e pergunta a você o código;
             só entra no banco o que o portal confirmar. É a partida a frio,
             quando o banco ainda está vazio e o robô não tem como chutar.
  --treinar  com o banco já semeado, o robô lê sozinho, confere no portal e
             aprende os que acertou — a leitura melhora a cada rodada.
  --aferir   só mede: lê e confere, sem gravar. Diz a taxa de acerto atual.

Nada disso emite certidão nem sai desta máquina (RNF-06). Ainda assim FALA COM
O PORTAL REAL: rode sob autorização, com o ritmo padrão (há uma pausa entre as
tentativas), e não em cima de um lote em produção.

Não serve para produção — é ferramenta de banco de trabalho.
"""
from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from PIL import Image  # noqa: E402

from cnd.adapters.estadual import sefaz_ma  # noqa: E402
from cnd.adapters.estadual.captcha_ma import ALFABETO, BancoCaptcha  # noqa: E402

BANCO = RAIZ / "data" / "ocr" / "sefaz_ma.json"
PAUSA_S = 1.5  # ritmo educado entre as idas ao portal


def cobertura(banco: BancoCaptcha) -> None:
    """Mostra o que já foi visto e o que ainda falta do alfabeto.

    É o que diz quando parar de semear: enquanto houver letra faltando, um
    captcha que a contenha é impossível de ler, e o robô vai ter de descartá-lo
    e pedir outro. Cobrir as 36 (a-z, 0-9) tira esse desperdício.
    """
    presentes = {c: len(banco.amostras.get(c, [])) for c in sorted(ALFABETO)}
    faltam = sorted(c for c, n in presentes.items() if n == 0)
    magros = sorted(c for c, n in presentes.items() if 0 < n < 3)
    print(f"  cobertura: {len(ALFABETO) - len(faltam)}/{len(ALFABETO)} "
          f"caracteres, {banco.total} exemplos")
    if faltam:
        print(f"  faltam ({len(faltam)}): {' '.join(faltam)}")
    if magros:
        print(f"  poucos exemplos (<3): {' '.join(magros)}")
    if not faltam and not magros:
        print("  alfabeto completo, com folga — pode partir para --treinar")


class Sessao:
    """Um cliente HTTP mínimo, só o que a rotina de treino precisa."""

    def __init__(self, url: str):
        self.url = url
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self.opener.addheaders = [("User-Agent", sefaz_ma.USER_AGENT)]

    def _abrir(self, req):
        # 500/4xx não podem derrubar o treino: devolve o corpo do erro, que a
        # validação simplesmente não vai reconhecer como "armado".
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as erro:
            return erro.read()

    def _post(self, dados: dict) -> bytes:
        corpo = urllib.parse.urlencode(dados, encoding="iso-8859-1").encode("iso-8859-1")
        req = urllib.request.Request(
            self.url, data=corpo,
            headers={"Referer": self.url,
                     "Content-Type": "application/x-www-form-urlencoded"
                                     "; charset=ISO-8859-1"})
        return self._abrir(req)

    def formulario(self):
        corpo = self._abrir(urllib.request.Request(
            self.url, headers={"Referer": self.url}))
        return sefaz_ma.ler_formulario(corpo)

    def imagem(self, src: str) -> Image.Image:
        url = urllib.parse.urljoin(self.url, src)
        corpo = self._abrir(urllib.request.Request(
            url, headers={"Referer": self.url}))
        return Image.open(BytesIO(corpo))

    def _trocar_para_cnpj(self, form) -> None:
        # Sem trocar para CPF/CNPJ, o campo do documento não existe na árvore
        # JSF e o portal responde 500 — foi o erro visto na 1ª semeadura.
        self._post({
            "AJAXREQUEST": form.container,
            "form1": "form1",
            "form1:tipoEmissao": "2",
            "form1:inscricaoEstadual": "",
            form.campo_captcha: "",
            "javax.faces.ViewState": form.viewstate,
            form.id_radio_cnpj: form.id_radio_cnpj,
        })

    def confere(self, form, codigo: str) -> bool:
        self._trocar_para_cnpj(form)
        resp = self._post({
            "AJAXREQUEST": form.container,
            "form1": "form1",
            "form1:tipoEmissao": "2",
            sefaz_ma.CAMPO_CNPJ_PADRAO: "11111111000191",
            form.campo_captcha: codigo.upper(),
            "javax.faces.ViewState": form.viewstate,
            form.id_validar: form.id_validar,
        })
        return sefaz_ma.RE_ARMADA in resp.decode("iso-8859-1", errors="replace")


PASTA_TEMP = RAIZ / "data" / "ocr" / "_captchas"


def _abrir_no_fotos(imagem: Image.Image, indice: int) -> Path:
    """Salva o captcha ampliado e o abre no app Fotos do Windows.

    Um nome de arquivo por captcha, para o Fotos nunca mostrar a imagem
    anterior em cache (o que acontece quando se reaproveita o mesmo nome)."""
    import os

    PASTA_TEMP.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_TEMP / f"captcha_{indice:02d}.png"
    imagem.resize((imagem.width * 6, imagem.height * 6)).save(caminho)
    abrir = getattr(os, "startfile", None)
    if abrir:  # Windows
        with contextlib.suppress(OSError):
            abrir(str(caminho))
    else:
        print(f"  abra a imagem: {caminho}")
    return caminho


def _fechar_fotos() -> None:
    """Fecha o app Fotos — é o "fecha sozinho depois".

    Encerra o processo do Fotos (fecha as janelas que ficaram abertas na
    semeadura). Se você tiver OUTRAS fotos abertas nele, elas fecham também —
    por isso rode a semeadura sem depender do Fotos para mais nada."""
    import subprocess

    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/IM", "Microsoft.Photos.exe", "/F"],
                       capture_output=True)


def _limpar_temp() -> None:
    with contextlib.suppress(Exception):
        for png in PASTA_TEMP.glob("captcha_*.png"):
            png.unlink()


def semear(sessao: Sessao, banco: BancoCaptcha, quantos: int) -> None:
    print(f"Rotulando {quantos} captchas. Cada um abre no app Fotos; digite "
          f"aqui o que você lê e Enter (vazio pula).")
    aprendidos = 0
    for i in range(quantos):
        form = sessao.formulario()
        if form is None:
            print("  formulário não veio; tentando de novo…")
            time.sleep(PAUSA_S)
            continue
        imagem = sessao.imagem(form.captcha_src)
        _fechar_fotos()  # fecha o captcha anterior antes de abrir o próximo
        _abrir_no_fotos(imagem, i + 1)
        codigo = input(f"[{i + 1}/{quantos}] digite o código: ").strip()
        if len(codigo) != 4:
            time.sleep(PAUSA_S)
            continue
        if sessao.confere(form, codigo):
            aprendidos += banco.aprender(imagem, codigo.lower())
            print("  ✓ confirmado pelo portal e aprendido")
        else:
            print("  ✗ o portal não aceitou esse código — descartado")
        time.sleep(PAUSA_S)
    _fechar_fotos()
    _limpar_temp()
    banco.salvar(BANCO)
    cobertura(banco)


def treinar(sessao: Sessao, banco: BancoCaptcha, rodadas: int,
            gravar: bool) -> None:
    if banco.vazio:
        sys.exit("Banco vazio: rode antes com --semear para a partida a frio.")
    acertos = tentativas = aprendidos = 0
    try:
        for i in range(rodadas):
            form = sessao.formulario()
            if form is None:
                time.sleep(PAUSA_S)
                continue
            imagem = sessao.imagem(form.captcha_src)
            palpite = banco.ler(imagem)
            tentativas += 1
            if palpite and sessao.confere(form, palpite):
                acertos += 1
                if gravar:
                    aprendidos += banco.aprender(imagem, palpite)
            if (i + 1) % 25 == 0:
                taxa = acertos / max(1, tentativas)
                print(f"  {i + 1:4d}  acerto={taxa:.0%}  banco={banco.total}")
                # Salva a cada checkpoint: um Ctrl+C no meio não joga fora o
                # que já foi aprendido nesta rodada.
                if gravar:
                    banco.salvar(BANCO)
            time.sleep(PAUSA_S)
    except KeyboardInterrupt:
        print("\n  interrompido — salvando o que já aprendi…")
    finally:
        if gravar:
            banco.salvar(BANCO)
    taxa = acertos / max(1, tentativas)
    print(f"Acerto: {acertos}/{tentativas} = {taxa:.0%}."
          + (f" Aprendidos {aprendidos} novos templates." if gravar else ""))


def main() -> None:
    p = argparse.ArgumentParser(description="Treina o leitor de captcha da SEFAZ-MA")
    p.add_argument("--semear", type=int, metavar="N",
                   help="rotula N captchas à mão (partida a frio)")
    p.add_argument("--treinar", type=int, metavar="N",
                   help="lê N captchas sozinho e aprende os que o portal confirmar")
    p.add_argument("--aferir", type=int, metavar="N",
                   help="lê N captchas e só mede o acerto, sem gravar")
    p.add_argument("--url", default=sefaz_ma.URL_CND,
                   help="endpoint do formulário (padrão: CND)")
    args = p.parse_args()

    banco = BancoCaptcha.carregar(BANCO)
    sessao = Sessao(args.url)
    print(f"Portal: {args.url}\nBanco:  {BANCO}")
    cobertura(banco)
    print()

    if args.semear:
        semear(sessao, banco, args.semear)
    elif args.treinar:
        treinar(sessao, banco, args.treinar, gravar=True)
    elif args.aferir:
        treinar(sessao, banco, args.aferir, gravar=False)
    else:
        p.error("escolha --semear, --treinar ou --aferir")


if __name__ == "__main__":
    main()
