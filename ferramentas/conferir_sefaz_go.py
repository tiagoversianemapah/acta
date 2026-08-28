"""Confere, contra o portal de verdade, o que o adapter da SEFAZ-GO enxerga.

Existe para a subida de um adapter novo, que é quando o risco é maior e a
suíte não ajuda: os testes são todos offline, e o que ninguém conferiu
ainda é se o PORTAL escreve o que o código espera ler. Um desfecho errado
aqui não quebra nada — ele entrega a certidão positiva de um cliente como
se fosse negativa.

Por isso a saída não é só o desfecho: é o desfecho **e a prova**. Qual
marcador casou, que título veio no PDF, e o trecho do texto em volta. É o
que permite consertar o marcador sem ter de adivinhar o que o portal
mandou.

    python ferramentas/conferir_sefaz_go.py 11222333000181
    python ferramentas/conferir_sefaz_go.py --planilha CARTEIRA.xlsx --aba GO --limite 15
    python ferramentas/conferir_sefaz_go.py --pdf data/certidoes/1/SEFAZ_GO/x.pdf

Caçar "um CNPJ de cada tipo" antes de rodar não funciona: ninguém sabe quem
tem débito estadual até perguntar. Com `--planilha` você manda uma AMOSTRA da
sua própria lista, e negativa, positiva e CPEN aparecem sozinhas — o resumo
do fim agrupa por desfecho e mostra qual CNPJ deu o quê.

Com `--pdf` não há rede: reclassifica um PDF já baixado, que é como se
confere uma mudança de marcador sem gastar consulta no portal.

Uma consulta por CNPJ, e nada é gravado no banco — isto não cria lote nem
job. Os PDFs caem em `data/evidencias/SEFAZ_GO/`.

As consultas saem ESPAÇADAS. Quem espaça no sistema é o orquestrador, e
esta ferramenta não passa por ele: chamar `emitir` em laço dispararia tudo
colado contra um portal que tem resposta pronta para isso ("a requisição
foi bloqueada pela política de segurança"). O intervalo padrão é o mesmo
`pacing.intervalo_inicial_s` do config do órgão.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from cnd.adapters import sefaz_go  # noqa: E402
from cnd.core.documentos import formatar, limpar  # noqa: E402
from cnd.core.modelos import COM_PDF, Documento  # noqa: E402
from cnd.infra.config import carregar  # noqa: E402

LARGURA = 78


def _regua(titulo: str = "") -> None:
    print(f"\n{titulo}\n" + "-" * LARGURA if titulo else "-" * LARGURA)


def _marcadores(texto_pdf: str) -> list[tuple[str, bool]]:
    """Quais sinais o classificador viu. É a prova por trás do desfecho."""
    normalizado = " ".join(sefaz_go._sem_acento(texto_pdf).split())
    return [
        ("titulo NEGATIVA", bool(sefaz_go.RE_TITULO_NEGATIVA.search(normalizado))),
        ("titulo POSITIVA", bool(sefaz_go.RE_TITULO_POSITIVA.search(normalizado))),
        ("'positiva com efeito' (CPEN)", "positiva com efeito" in normalizado),
        ("prosa 'nao consta debito'", "nao consta debito" in normalizado),
        ("prosa 'consta debito'",
         bool(sefaz_go.RE_CONSTA_DEBITO.search(normalizado))),
    ]


def _trecho_do_titulo(texto_pdf: str) -> str:
    """A linha que deveria trazer NEGATIVA ou POSITIVA, como ela veio."""
    for linha in texto_pdf.splitlines():
        if "CERTID" in linha.upper():
            return " ".join(linha.split())[:160]
    return "(nenhuma linha com 'CERTID' — o PDF pode estar sem texto extraível)"


def _relatar(rotulo: str, resultado, texto_pdf: str | None) -> None:
    _regua(rotulo)
    print(f"  desfecho ........ {resultado.desfecho}")
    print(f"  entregue ao cliente? {'SIM' if resultado.desfecho in COM_PDF else 'nao'}")
    print(f"  validade ........ {resultado.validade or '(nao extraida)'}")
    print(f"  codigo .......... {resultado.codigo_controle or '(nao extraido)'}")
    if resultado.caminho_pdf:
        print(f"  PDF (entregavel)  {resultado.caminho_pdf}")
    if resultado.evidencia:
        print(f"  evidencia ....... {resultado.evidencia}")
    if resultado.mensagem_portal:
        print(f"  portal disse .... {resultado.mensagem_portal[:200]}")

    if texto_pdf is None:
        print("\n  (sem PDF — o desfecho veio da tela, nao do documento)")
        return

    print(f"\n  titulo lido ..... {_trecho_do_titulo(texto_pdf)}")
    print("  marcadores:")
    for nome, casou in _marcadores(texto_pdf):
        print(f"    [{'x' if casou else ' '}] {nome}")


def _resumir(vistos: list[tuple[str, object]]) -> None:
    """Agrupa por desfecho. É o que responde "consegui um de cada tipo?"."""
    if not vistos:
        return
    _regua("RESUMO")
    por_desfecho: dict[str, list[str]] = {}
    for documento, resultado in vistos:
        por_desfecho.setdefault(str(resultado.desfecho), []).append(documento)

    for desfecho, documentos in sorted(por_desfecho.items()):
        print(f"  {desfecho:20} {len(documentos):>3}  "
              f"{', '.join(formatar(d) for d in documentos[:4])}"
              f"{' ...' if len(documentos) > 4 else ''}")

    faltando = [d for d in ("NEGATIVA", "POSITIVA", "CPEN")
                if d not in por_desfecho]
    print()
    if faltando:
        print(f"  ainda sem exemplo de: {', '.join(faltando)}"
              f" - rode mais alguns CNPJs")
    else:
        print("  os tres tipos apareceram.")


def _documentos_da_planilha(caminho: Path, aba: str, limite: int) -> list[str]:
    """Uma amostra da carteira, sem tocar no banco.

    `ler` devolve válidos e rejeitados e não grava nada — é a mesma leitura
    que a importação usa, então o que chega aqui é exatamente o que entraria
    na fila.
    """
    from cnd.ingestao.planilha import ler

    leitura = ler(caminho, [aba] if aba else None, "SEFAZ_GO")
    if leitura.rejeitados:
        print(f"  ({len(leitura.rejeitados)} linha(s) rejeitada(s) na leitura, "
              f"ignoradas aqui)")
    return [item.documento for item in leitura.itens[:limite]]


def conferir_pdf(caminho: Path) -> int:
    if not caminho.exists():
        print(f"nao achei {caminho}")
        return 1
    try:
        texto = sefaz_go._texto_pdf(caminho)
    except Exception as erro:
        print(f"PDF ilegivel: {erro}")
        return 1
    _relatar(f"PDF: {caminho.name}", sefaz_go.ler_pdf(caminho, "conferencia"), texto)
    return 0


def conferir_cnpj(documento: str, cfg):
    limpo = limpar(documento)
    adapter = sefaz_go.criar(cfg.orgaos["SEFAZ_GO"], cfg)
    adapter.preparar()
    try:
        doc = Documento(empresa_id=0, documento=limpo, tipo="CNPJ",
                        nome="CONFERENCIA", lote_id=0)
        resultado = adapter.emitir(doc)
    finally:
        adapter.encerrar()

    caminho = resultado.caminho_pdf or resultado.evidencia
    texto = None
    if caminho and caminho.suffix.lower() == ".pdf" and caminho.exists():
        try:
            texto = sefaz_go._texto_pdf(caminho)
        except Exception:
            texto = None
    _relatar(f"CNPJ {formatar(limpo)}", resultado, texto)
    return resultado


def _esperar(segundos: float, jitter: float) -> None:
    """Mesma ideia do ritmo do orquestrador: intervalo com variação.

    Espera igualzinha a cada consulta é padrão de robô — o jitter existe
    para o intervalo não virar assinatura.
    """
    if segundos <= 0:
        return
    real = segundos * (1 + random.uniform(-jitter, jitter))
    print(f"  (aguardando {real:.0f}s antes da proxima)")
    time.sleep(max(0.0, real))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Confere o que o adapter da SEFAZ-GO enxerga.")
    parser.add_argument("documentos", nargs="*",
                        help="um ou mais CNPJs (uma consulta cada)")
    parser.add_argument("--pdf", type=Path,
                        help="reclassifica um PDF ja baixado, sem tocar na rede")
    parser.add_argument("--planilha", type=Path,
                        help="tira a amostra da sua carteira, sem tocar no banco")
    parser.add_argument("--aba", default="GO",
                        help="aba da planilha com os CNPJs de Goias (padrao: GO)")
    parser.add_argument("--limite", type=int, default=10,
                        help="quantos CNPJs consultar da planilha (padrao: 10)")
    parser.add_argument("--intervalo", type=float, default=None,
                        help="segundos entre consultas "
                             "(padrao: o pacing do orgao no config)")
    args = parser.parse_args()

    if args.pdf:
        return conferir_pdf(args.pdf)
    documentos = list(args.documentos)
    if args.planilha:
        if not args.planilha.exists():
            print(f"nao achei {args.planilha}")
            return 1
        documentos += _documentos_da_planilha(args.planilha, args.aba, args.limite)
    if not documentos:
        parser.print_help()
        return 2

    cfg = carregar()
    if "SEFAZ_GO" not in cfg.orgaos:
        print("SEFAZ_GO nao esta no config.toml desta maquina. "
              "Copie a secao [orgaos.SEFAZ_GO] do config.exemplo.toml.")
        return 1

    pacing = cfg.orgaos["SEFAZ_GO"].pacing
    intervalo = (args.intervalo if args.intervalo is not None
                 else pacing.intervalo_inicial_s)

    print(f"Consultando o portal da SEFAZ-GO — {len(documentos)} consulta(s), "
          f"~{intervalo:.0f}s entre elas.")
    print("Os PDFs caem em data/evidencias/SEFAZ_GO/. Nada vai para o banco.")
    vistos = []
    for indice, documento in enumerate(documentos):
        if indice:
            _esperar(intervalo, pacing.jitter)
        vistos.append((limpar(documento), conferir_cnpj(documento, cfg)))
    _resumir(vistos)
    _regua()
    return 0


if __name__ == "__main__":
    sys.exit(main())
