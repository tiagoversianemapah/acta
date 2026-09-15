"""O painel relê o config.toml quando ele muda.

Ler uma vez só na importação fazia o painel discordar do robô: em
15/09/2026, o SEFAZ-ES ficou com o ritmo preso em 300s porque o arquivo
já dizia 10s e o processo do painel ainda respondia pelos valores
velhos — inclusive na normalização do ritmo do início manual.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

BASE = """
[geral]
banco = "data/cnd.db"

[rede]
nome = "teste"
senha = "segredo"

[orgaos.SEFAZ_ES]
ativo = true
adapter = "sefaz_es"

[orgaos.SEFAZ_ES.pacing]
intervalo_inicial_s = {ritmo}
intervalo_piso_s    = {ritmo}

[orgaos.SEFAZ_ES.breaker]
captchas_para_abrir = {captchas}
"""


@pytest.fixture
def painel(monkeypatch, tmp_path):
    """O módulo do painel apontado para um config.toml descartável."""
    arquivo = tmp_path / "config.toml"
    arquivo.write_text(BASE.format(ritmo=10.0, captchas=1), encoding="utf-8")
    monkeypatch.setenv("CND_CONFIG", str(arquivo))

    from cnd.infra import config as modulo_config
    importlib.reload(modulo_config)
    from cnd.web import app as modulo
    importlib.reload(modulo)
    yield modulo, arquivo
    # Devolve os módulos ao estado do resto da suíte.
    monkeypatch.undo()
    importlib.reload(modulo_config)
    importlib.reload(modulo)


def _ritmo(modulo) -> float:
    return modulo.config_atual().orgaos["SEFAZ_ES"].pacing.intervalo_inicial_s


def test_editar_o_arquivo_muda_o_que_o_painel_responde(painel):
    modulo, arquivo = painel
    assert _ritmo(modulo) == 10.0

    arquivo.write_text(BASE.format(ritmo=7.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 7.0


def test_dois_salvamentos_no_mesmo_instante_nao_passam_batido(painel):
    """A data do arquivo não serve de marca: dois writes seguidos saem com
    `st_mtime_ns` idêntico no Windows, que é o caso de um editor que grava
    um temporário e renomeia por cima."""
    modulo, arquivo = painel
    arquivo.write_text(BASE.format(ritmo=7.0, captchas=1), encoding="utf-8")
    assert _ritmo(modulo) == 7.0

    # Sem pausa nenhuma entre um e outro.
    arquivo.write_text(BASE.format(ritmo=3.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 3.0


def test_config_quebrada_mantem_a_anterior_e_nao_derruba_o_painel(painel):
    modulo, arquivo = painel
    arquivo.write_text(BASE.format(ritmo=7.0, captchas=1), encoding="utf-8")
    assert _ritmo(modulo) == 7.0

    arquivo.write_text("isto [ nao e toml", encoding="utf-8")

    assert _ritmo(modulo) == 7.0


def test_config_consertada_volta_a_valer(painel):
    modulo, arquivo = painel
    arquivo.write_text("isto [ nao e toml", encoding="utf-8")
    assert _ritmo(modulo) == 10.0

    arquivo.write_text(BASE.format(ritmo=5.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 5.0


def test_arquivo_sumido_mantem_a_ultima_config_boa(painel):
    modulo, arquivo = painel
    assert _ritmo(modulo) == 10.0

    Path(arquivo).unlink()

    assert _ritmo(modulo) == 10.0
