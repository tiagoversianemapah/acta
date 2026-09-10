"""Ciclo de teste do adapter cego da SEFAZ-ES, com leitura do resultado.

    python ferramentas/testar_sefaz_es.py            # zera, importa, roda e le
    python ferramentas/testar_sefaz_es.py --so-ler   # so le a ultima execucao

Existe porque diagnosticar este adapter e caro: o portal do ES e instavel, uma
execucao leva minutos, e o que aconteceu so aparece cruzando o log estruturado
com o banco e com o print de evidencia. Fazer isso na mao a cada rodada e como
a gente perdeu boa parte de 08/09/2026.

Nao serve para producao - e ferramenta de banco de trabalho.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
CONFIG = RAIZ / "config.sefaz-es-teste.toml"
PLANILHA = RAIZ / "data/teste_sefaz_es/teste-es.xlsx"
BANCO = RAIZ / "data/teste_sefaz_es/cnd.db"
LOG = RAIZ / "data/teste_sefaz_es/logs/orquestrador.jsonl"

# Eventos que contam a historia. O resto e ruido de passo a passo.
MARCOS = (
    "reiniciando_site_sefaz_es", "reinsistindo_na_emissao",
    "formulario_pronto_por_texto", "pdf_salvo_do_modal",
    "pdf_salvo_com_outro_nome", "pdf_do_modal_nao_salvo",
    "faixa_de_alerta_detectada", "cookies_do_portal_limpos",
    "tentativa", "job_falhou", "breaker_aberto",
)


def _cnd(*argumentos: str) -> int:
    ambiente = {"PYTHONPATH": str(RAIZ / "src")}
    return subprocess.run([sys.executable, "-m", "cnd.cli", *argumentos],
                          cwd=RAIZ, env={**os.environ, **ambiente}).returncode


def preparar_fila() -> None:
    print("== zerando e reimportando ==")
    _cnd("zerar", "--config", str(CONFIG), "--sim")
    _cnd("importar", str(PLANILHA), "--abas", "ES", "--config", str(CONFIG))


def rodar() -> None:
    print("\n== rodando (pode levar varios minutos) ==")
    print("   nao mexa no mouse nem no teclado ate voltar o prompt\n")
    _cnd("rodar", "--config", str(CONFIG), "--limite", "1", "--ate-esvaziar")


def _eventos_da_ultima_execucao() -> list[dict]:
    if not LOG.exists():
        return []
    linhas = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    eventos = [json.loads(x) for x in linhas if x.strip().startswith("{")]
    inicios = [i for i, d in enumerate(eventos)
               if (d.get("event") or d.get("evento")) == "orquestrador_no_ar"]
    return eventos[inicios[-1]:] if inicios else eventos


def _nome(evento: dict) -> str:
    return evento.get("event") or evento.get("evento") or ""


def ler() -> None:
    eventos = _eventos_da_ultima_execucao()
    if not eventos:
        print("sem log de execucao ainda.")
        return

    print("\n== o que aconteceu ==")
    for nome, quantas in Counter(_nome(e) for e in eventos).most_common():
        print(f"  {quantas:3}  {nome}")

    print("\n== marcos ==")
    for evento in eventos:
        nome = _nome(evento)
        if nome in MARCOS:
            extra = {c: v for c, v in evento.items()
                     if c in ("motivo", "tentativa", "limite", "desfecho",
                              "duracao_s", "removidos", "arquivo", "encontrado")}
            print(f'  {str(evento.get("hora", ""))[11:19]}  {nome}  {extra or ""}')
        if evento.get("excecao"):
            ultima = evento["excecao"].strip().splitlines()[-1]
            print(f'  {str(evento.get("hora", ""))[11:19]}  EXCECAO  {ultima[:150]}')

    if not BANCO.exists():
        return
    conexao = sqlite3.connect(BANCO)
    conexao.row_factory = sqlite3.Row
    print("\n== desfecho ==")
    for linha in conexao.execute(
            "select numero, desfecho, mensagem_portal, evidencia"
            " from tentativa order by id desc limit 3"):
        for campo, valor in dict(linha).items():
            if valor:
                print(f"  {campo}: {str(valor)[:120]}")
        print()

    certidoes = list((RAIZ / "data/teste_sefaz_es/certidoes").rglob("*.pdf"))
    print(f"== certidoes entregues: {len(certidoes)} ==")
    for pdf in certidoes[-5:]:
        print(f"  {pdf.relative_to(RAIZ)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ciclo de teste do SEFAZ-ES")
    parser.add_argument("--so-ler", action="store_true",
                        help="nao roda nada, so le a ultima execucao")
    argumentos = parser.parse_args()
    if not argumentos.so_ler:
        preparar_fila()
        rodar()
    ler()
