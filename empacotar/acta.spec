# -*- mode: python ; coding: utf-8 -*-
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

from PyInstaller.utils.hooks import collect_all

RAIZ = Path(SPECPATH).parent
FONTE = RAIZ / "src" / "cnd"

# CustomTkinter carrega temas de arquivos .json em tempo de execução; sem
# arrastar os dados junto, a janela abre sem cor nenhuma.
ctk_datas, ctk_binarios, ctk_ocultos = collect_all("customtkinter")

dados = ctk_datas + [
    (str(FONTE / "infra" / "schema.sql"), "cnd/infra"),
    (str(FONTE / "web" / "templates"), "cnd/web/templates"),
]

ocultos = ctk_ocultos + [
    # Escolhidos pelo config.toml e importados por nome — o PyInstaller não
    # tem como enxergar isso lendo o código.
    "cnd.adapters.rfb_cego",
    "cnd.adapters.fake",
    # Cinto e suspensório: o painel também é alcançado por nome em alguns
    # caminhos, e sem ele o executável sobe e morre ao abrir o servidor.
    "cnd.web.app",
    # O uvicorn monta o servidor por nome de módulo, pelo mesmo motivo.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

analise = Analysis(
    [str(FONTE / "lancador.py")],
    pathex=[str(RAIZ / "src")],
    binaries=ctk_binarios,
    datas=dados,
    hiddenimports=ocultos,
    # O adapter do Playwright está desligado (o portal o detecta) e traz
    # junto ~100 MB de navegador que não seria usado.
    excludes=["playwright", "cnd.adapters.rfb_pj", "pytest", "matplotlib",
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
