"""Receita de empacotamento do ACTA.

Gera DOIS executáveis que dividem a mesma pasta de bibliotecas:

  ACTA.exe   sem console — é o que vai no atalho
  cnd.exe    com console — a linha de comando (importar, painel, calibrar)
             e o processo do robô, cuja saída a janela lê

Modo pasta (onedir) e não arquivo único: o arquivo único se descompacta
inteiro num diretório temporário a cada abertura, o que custa segundos de
espera e faz o antivírus corporativo olhar torto. A pasta abre instantâneo
e o operador enxerga o config.toml ao lado do programa, que é onde ele
espera encontrar.

Rodar por:  python empacotar/construir.py
"""
# ruff: noqa: F821  — Analysis, PYZ, EXE, COLLECT e SPECPATH são injetados
# pelo PyInstaller no momento de ler a receita; não existem como import.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

RAIZ = Path(SPECPATH).parent
FONTE = RAIZ / "src" / "cnd"

# CustomTkinter carrega temas de arquivos .json em tempo de execução; sem
# arrastar os dados junto, a janela abre sem cor nenhuma.
ctk_datas, ctk_binarios, ctk_ocultos = collect_all("customtkinter")

# O metadado do proprio `cnd`, para `importlib.metadata.version` responder
# dentro do executavel. Sem ele a versao volta vazia numa instalacao por
# copia de pasta - que e como o ACTA se instala -, e "qual versao esta nesta
# maquina?" fica sem resposta justamente quando importa: depois de uma
# atualizacao pela rede, para saber se ela pegou.
cnd_metadados = copy_metadata("cnd")

# O Playwright entra por causa do CRF da Caixa. Ele não traz navegador: o
# adapter usa `channel="msedge"`, o Edge que toda máquina já tem. O que
# vem junto é o driver dele (node + protocolo), ~100 MB, e é isso que
# permite automação por elemento em vez de coordenada de tela.
#
# Opcional na construção porque é opcional na instalação: ele mora no
# extra `navegador` do pyproject, e `pip install -e ".[dev]"` — o que o
# README manda rodar — não o traz. Exigir aqui fazia a construção
# documentada morrer num checkout limpo, com um ImportError do
# PyInstaller que não diz o que fazer. Sem ele o pacote sai menor e roda
# a Receita normalmente; o que não roda é o CRF, e o aviso diz isso.
try:
    pw_datas, pw_binarios, pw_ocultos = collect_all("playwright")
except Exception:
    pw_datas, pw_binarios, pw_ocultos = [], [], []
    print("  AVISO  playwright ausente: o pacote sai SEM o adapter do CRF.")
    print('         Para incluí-lo:  pip install -e ".[dev,navegador]"')

dados = [
    *ctk_datas,
    *cnd_metadados,
    *pw_datas,
    (str(FONTE / "infra" / "schema.sql"), "cnd/infra"),
    (str(FONTE / "web" / "templates"), "cnd/web/templates"),
    (str(FONTE / "web" / "static"), "cnd/web/static"),
]

ocultos = [
    *ctk_ocultos,
    # Escolhidos pelo config.toml e importados por nome — o PyInstaller não
    # tem como enxergar isso lendo o código.
    "cnd.adapters.federal.rfb.cego",
    "cnd.adapters.federal.rfb.cego_pf",
    "cnd.adapters.federal.crf",
    "cnd.adapters.estadual.sefaz_go",
    "cnd.adapters.estadual.sefaz_es",
    # sefaz_ma importa captcha_ma estaticamente, então basta ele aqui.
    "cnd.adapters.estadual.sefaz_ma",
    "cnd.adapters.estadual.sefaz_mt",
    "cnd.adapters.municipal.goiania",
    "cnd.adapters.municipal.vitoria",
    "cnd.adapters.fake",
    *pw_ocultos,
    # Cinto e suspensório: o painel também é alcançado por nome em alguns
    # caminhos, e sem ele o executável sobe e morre ao abrir o servidor.
    "cnd.web.app",
    # O uvicorn monta o servidor por nome de módulo, pelo mesmo motivo.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.on",
]

analise = Analysis(
    [str(FONTE / "lancador.py")],
    pathex=[str(RAIZ / "src")],
    binaries=[*ctk_binarios, *pw_binarios],
    datas=dados,
    hiddenimports=ocultos,
    # rfb_pj é o adapter de navegador DA RECEITA, que continua desligado:
    # aquele portal detecta automação (teste A/B em 07/08/2026) e quem
    # atende a Receita é o rfb_cego. O Playwright em si deixou de ser
    # excluído por causa do CRF da Caixa, que não detecta.
    excludes=["cnd.adapters.federal.rfb.pj", "pytest", "matplotlib",
              "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(analise.pure)

janela = EXE(
    pyz, analise.scripts, [],
    exclude_binaries=True,
    name="ACTA",
    console=False,
    icon=str(RAIZ / "empacotar" / "acta.ico"),
    version=str(RAIZ / "empacotar" / "versao.txt"),
)

terminal = EXE(
    pyz, analise.scripts, [],
    exclude_binaries=True,
    name="cnd",
    console=True,
    icon=str(RAIZ / "empacotar" / "acta.ico"),
    version=str(RAIZ / "empacotar" / "versao.txt"),
)

COLLECT(
    janela, terminal,
    analise.binaries, analise.datas,
    name="ACTA",
)
