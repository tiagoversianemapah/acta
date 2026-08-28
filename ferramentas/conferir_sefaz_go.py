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
job. Os PDFs caem todos em `data/conferencia/`, e NÃO em `data/certidoes/`:
conferência não é entrega, e arquivo de teste na pasta de onde sai o pacote
do cliente é o tipo de coisa que alguém acaba mandando por engano.

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
        print(f"  ainda sem exemplo de: {', '.join(faltando)}")
        print("  continue de onde parou com --pular, para nao repetir consulta:")
        print(f"     ... --limite {len(vistos)} --pular {len(vistos)}")
    else:
        print("  os tres tipos apareceram.")


def _documentos_da_planilha(caminho: Path, aba: str, limite: int,
                            pular: int = 0) -> list[str]:
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
    fatia = leitura.itens[pular:pular + limite]
    print(f"  (aba {aba}: {len(leitura.itens)} CNPJs; "
          f"consultando do {pular + 1} ao {pular + len(fatia)})")
    return [item.documento for item in fatia]


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


def _cfg_de_conferencia(cfg):
    """Manda tudo para `data/conferencia/`, longe da pasta de entrega.

    O adapter move a negativa para `pasta_certidoes` — e faz certo, porque
    e de la que o pacote do cliente e montado. So que aqui nao ha lote, nao
    ha banco e nao ha entrega: o arquivo ficava em `data/certidoes/0/` com
    o nome CONFERENCIA, no meio das certidoes de verdade.
    """
    from dataclasses import replace

    base = cfg.banco.parent / "conferencia"
    return replace(cfg,
                   pasta_certidoes=base / "certidoes",
                   pasta_evidencias=base / "evidencias")


def conferir_cnpj(documento: str, cfg):
    limpo = limpar(documento)
    cfg = _cfg_de_conferencia(cfg)
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


def _carregar_config(caminho: Path | None):
    """O config, ou um recado que diz o que fazer.

    Traceback de FileNotFoundError nao ajuda ninguem: `config.toml` e da
    INSTALACAO e nao vai para o controle de versao, entao num checkout do
    repositorio ele legitimamente nao existe. Para esta conferencia, que
    nao toca no banco, o exemplo versionado serve.
    """
    from cnd.infra.config import CAMINHO_PADRAO

    # CAMINHO_PADRAO ja resolve CND_CONFIG, mas na IMPORTACAO do modulo:
    # `--config` chega depois disso, entao ele vai por argumento para
    # `carregar`, que aceita um caminho explicito.
    alvo = caminho or CAMINHO_PADRAO
    if not alvo.exists():
        exemplo = CAMINHO_PADRAO.parent / "config.exemplo.toml"
        print(f"Nao achei o config em {alvo}.")
        print()
        print("Ele e da INSTALACAO e nao vai para o controle de versao, entao")
        print("num checkout do repositorio ele nao existe mesmo. Para esta")
        print("conferencia, que nao toca no banco, o exemplo serve:")
        print()
        print(f"    python ferramentas/conferir_sefaz_go.py --config {exemplo.name} ...")
        print()
        print("ou copie de vez:  copy config.exemplo.toml config.toml")
        return None

    cfg = carregar(alvo)
    if "SEFAZ_GO" not in cfg.orgaos:
        print(f"{alvo} nao tem a secao [orgaos.SEFAZ_GO].")
        print("Copie-a do config.exemplo.toml. Nao precisa ficar ativo = true:")
        print("esta ferramenta nao passa pelo orquestrador.")
        return None
    return cfg


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
    parser.add_argument("--config", type=Path, default=None,
                        help="outro arquivo de config (ex.: config.exemplo.toml)")
    parser.add_argument("--pular", type=int, default=0,
                        help="quantos CNPJs do inicio ignorar, para uma segunda "
                             "rodada continuar de onde a primeira parou")
    parser.add_argument("--intervalo", type=float, default=None,
                        help="segundos entre consultas "
                             "(padrao: o pacing do orgao no config)")
    args = parser.parse_args()

    if args.pdf:
        return conferir_pdf(args.pdf)

    # O config vem ANTES de ler a planilha: descobrir que falta configuracao
    # depois de abrir um arquivo de 2.600 linhas e trabalho jogado fora.
    cfg = _carregar_config(args.config)
    if cfg is None:
        return 1

    documentos = list(args.documentos)
    if args.planilha:
        if not args.planilha.exists():
            print(f"nao achei {args.planilha}")
            return 1
        documentos += _documentos_da_planilha(
            args.planilha, args.aba, args.limite, args.pular)
    if not documentos:
        parser.print_help()
        return 2

    pacing = cfg.orgaos["SEFAZ_GO"].pacing
    intervalo = (args.intervalo if args.intervalo is not None
                 else pacing.intervalo_inicial_s)

    print(f"Consultando o portal da SEFAZ-GO — {len(documentos)} consulta(s), "
          f"~{intervalo:.0f}s entre elas.")
    print("Os PDFs caem em data/conferencia/. Nada vai para o banco,")
    print("e nada entra em data/certidoes/, que e a pasta de entrega.")
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
