"""OCR do captcha da SEFAZ-MA — Pillow puro, sem numpy nem binário externo.

Por que existe. O portal do Maranhão exige um captcha de imagem em TODA
emissão (é fixo, não heurístico — o oposto da Receita, que é o caso do
ADR-004). Mas ele é trivial: JPEG 100x25, quatro caracteres [a-z0-9], sem
linhas de ruído e sem distorção geométrica — só peso e itálico sorteados por
glifo. E, decisivo, uma validação vale para a SESSÃO inteira: acertar um
captcha arma a sessão para emitir a carteira toda (ver docs/fluxos/sefaz-ma.md
e ADR-006).

Consequência de projeto: o OCR NÃO precisa ser perfeito. O próprio portal
valida o palpite de graça, sem gastar emissão (o AJAX de validação responde
"arma / não arma"). Então o adapter lê, pergunta ao portal se acertou e, se
não, pede outra imagem e tenta de novo — poucas tentativas fecham, porque
basta acertar UMA vez por sessão. Este módulo é só a leitura; o laço de
tentar-e-conferir mora no adapter.

Por que Pillow puro. `pillow` já é dependência (ícone, marca, robô cego);
`numpy` e `tesseract` não são, e o `ACTA.exe` é empacotado com PyInstaller —
adicionar binário externo por causa de um estado pequeno não se paga. A
distância entre dois glifos é a de Hamming entre os bitmaps normalizados,
calculada como um XOR de inteiros (`int.bit_count()`), que é rápido o
bastante mesmo com o banco inteiro em memória: a leitura acontece uma vez por
sessão, não por documento.

O banco de templates é aprendido, não fixo: cada glifo que o portal CONFIRMA
entra no banco (`registrar`), então a leitura melhora sozinha na máquina onde
roda, sem nada sair dela (RNF-06). O banco semente é montado offline pela
ferramenta `ferramentas/treinar_ocr_sefaz_ma.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from cnd.infra.log import obter

log = obter("adapter.sefaz_ma.captcha")

# Lado da caixa em que cada glifo é normalizado antes de comparar. 20x20 = 400
# bits: fino o bastante para separar 'e' de 'c' e grosso o bastante para o
# ruído de reamostragem do JPEG não pesar. Mudar este número invalida um banco
# já gravado (os bitmaps teriam outro tamanho), por isso ele viaja DENTRO do
# arquivo do banco e é conferido na carga.
CAIXA = 20
BITS = CAIXA * CAIXA

# O alfabeto que o portal usa. Serve só de sanidade: um palpite com caractere
# fora daqui é a leitura tendo dado errado, e nem vale conferir no portal.
ALFABETO = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

# Teto de amostras por classe no banco. Sem isto, uma rodada longa deixaria o
# banco crescer sem limite e a leitura ficaria lenta à toa — passado o teto,
# mais exemplos da mesma letra quase não melhoram o acerto.
MAX_POR_CLASSE = 32

# Quantos caracteres o captcha tem. O campo do formulário aceita 4 (maxlength).
TAMANHO = 4


def _limiar(imagem: Image.Image) -> tuple[Image.Image, int, int]:
    cinza = imagem.convert("L")
    largura, altura = cinza.size
    return cinza, largura, altura


def segmentar(imagem: Image.Image) -> list[Image.Image]:
    """Recorta cada caractere por projeção vertical.

    O captcha do MA separa os quatro glifos com colunas em branco entre eles
    — conferido nas amostras, todas dão quatro blocos limpos. Então não é
    preciso componente conexo nem heurística de corte: onde não há tinta,
    corta. Cada recorte já vem no bounding box da tinta (sem margem morta),
    que é o que a normalização espera.
    """
    cinza, largura, altura = _limiar(imagem)
    px = cinza.load()
    coluna_tem_tinta = [
        any(px[x, y] < 128 for y in range(altura)) for x in range(largura)
    ]

    faixas: list[tuple[int, int]] = []
    inicio: int | None = None
    for x in range(largura):
        if coluna_tem_tinta[x] and inicio is None:
            inicio = x
        elif not coluna_tem_tinta[x] and inicio is not None:
            faixas.append((inicio, x))
            inicio = None
    if inicio is not None:
        faixas.append((inicio, largura))

    recortes: list[Image.Image] = []
    for a, b in faixas:
        ys = [y for y in range(altura) for x in range(a, b) if px[x, y] < 128]
        if not ys:
            continue
        recortes.append(cinza.crop((a, min(ys), b, max(ys) + 1)))
    return recortes


def normalizar(glifo: Image.Image) -> int:
    """Glifo -> um inteiro de BITS bits (1 = tinta), pronto para o XOR.

    Reamostra para CAIXA x CAIXA deformando o aspecto de propósito: é o que
    faz um 'o' fino (regular) e um 'o' gordo (bold) caírem quase no mesmo
    bitmap, que é o efeito desejado — o portal sorteia o peso, e não queremos
    uma classe por peso.
    """
    pequeno = glifo.resize((CAIXA, CAIXA), Image.LANCZOS)
    px = pequeno.load()
    mascara = 0
    for i, (x, y) in enumerate((x, y) for y in range(CAIXA) for x in range(CAIXA)):
        if px[x, y] < 128:
            mascara |= 1 << i
    return mascara


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass
class BancoCaptcha:
    """Templates rotulados, um punhado por classe."""

    # classe (um caractere) -> lista de bitmaps (inteiros)
    amostras: dict[str, list[int]] = field(default_factory=dict)

    @property
    def vazio(self) -> bool:
        return not self.amostras

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.amostras.values())

    def registrar(self, classe: str, mascara: int) -> None:
        """Guarda um glifo já rotulado, sem repetir bitmap idêntico."""
        if classe not in ALFABETO:
            return
        lista = self.amostras.setdefault(classe, [])
        if mascara in lista:
            return
        if len(lista) >= MAX_POR_CLASSE:
            return
        lista.append(mascara)

    def _classificar(self, mascara: int) -> tuple[str | None, int]:
        melhor: str | None = None
        menor = BITS + 1
        for classe, lista in self.amostras.items():
            for tmpl in lista:
                d = _hamming(mascara, tmpl)
                if d < menor:
                    menor = d
                    melhor = classe
        return melhor, menor

    def ler(self, imagem: Image.Image) -> str | None:
        """Lê o captcha inteiro. None quando não dá para arriscar um palpite.

        Devolve None — em vez de um palpite ruim — quando a segmentação não
        acha exatamente TAMANHO glifos ou quando o banco está vazio. Nesses
        casos não há o que conferir no portal, e o adapter pede outra imagem.
        """
        if self.vazio:
            return None
        recortes = segmentar(imagem)
        if len(recortes) != TAMANHO:
            return None
        saida = []
        for recorte in recortes:
            classe, _ = self._classificar(normalizar(recorte))
            if classe is None:
                return None
            saida.append(classe)
        return "".join(saida)

    def aprender(self, imagem: Image.Image, texto: str) -> int:
        """Adiciona ao banco os glifos de um captcha CONFIRMADO pelo portal.

        Só deve ser chamado com um `texto` que o portal aceitou — é o que
        garante que o rótulo está certo. Devolve quantos glifos entraram.
        """
        texto = texto.lower()
        if len(texto) != TAMANHO:
            return 0
        recortes = segmentar(imagem)
        if len(recortes) != TAMANHO:
            return 0
        antes = self.total
        for recorte, classe in zip(recortes, texto, strict=True):
            self.registrar(classe, normalizar(recorte))
        return self.total - antes

    # --- persistência -----------------------------------------------------

    def para_json(self) -> str:
        # Os bitmaps viram hexadecimal (compacto e estável); CAIXA viaja junto
        # para a carga recusar um banco de outro tamanho em vez de ler lixo.
        return json.dumps({
            "caixa": CAIXA,
            "amostras": {
                classe: [format(m, "x") for m in lista]
                for classe, lista in sorted(self.amostras.items())
            },
        })

    def salvar(self, caminho: Path) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        # Grava num temporário e renomeia: se a máquina cair no meio da
        # escrita, o banco velho continua íntegro em vez de virar meio-arquivo.
        temp = caminho.with_suffix(caminho.suffix + ".tmp")
        temp.write_text(self.para_json(), encoding="utf-8")
        temp.replace(caminho)

    @classmethod
    def de_json(cls, texto: str) -> BancoCaptcha:
        dados = json.loads(texto)
        if int(dados.get("caixa", CAIXA)) != CAIXA:
            raise ValueError(
                f"banco de captcha com caixa {dados.get('caixa')} != {CAIXA}: "
                f"foi gravado por outra versão e precisa ser retreinado"
            )
        amostras = {
            classe: [int(h, 16) for h in lista]
            for classe, lista in dados.get("amostras", {}).items()
        }
        return cls(amostras=amostras)

    @classmethod
    def carregar(cls, caminho: Path) -> BancoCaptcha:
        """Lê o banco do disco; banco ausente ou ilegível vira banco vazio.

        Vazio não é erro aqui: é o estado de uma máquina ainda não treinada.
        Quem decide o que fazer com isso é o adapter (ele recusa emitir e diz
        para rodar a ferramenta de treino), não a carga.
        """
        try:
            return cls.de_json(caminho.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (ValueError, json.JSONDecodeError) as erro:
            log.warning("banco_captcha_ilegivel",
                        extra={"arquivo": str(caminho), "erro": str(erro)[:200]})
            return cls()
