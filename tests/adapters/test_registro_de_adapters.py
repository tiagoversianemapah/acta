"""O mapa nome-do-config -> modulo tem de apontar para modulo que existe.

Este arquivo existe por causa de um risco que nenhum outro teste pega: o
adapter e escolhido por NOME, vindo do config.toml, e importado por
`importlib`. Renomear ou mover um adapter nao quebra import nenhum no
codigo-fonte - quebra na hora em que o operador liga o orgao, na maquina do
robo, longe daqui. E o `acta.spec` sofre do mesmo: o PyInstaller nao enxerga
esses imports lendo o codigo, entao cada um precisa estar nos `hiddenimports`
a mao, escrito certo.
"""
from __future__ import annotations

import importlib
import importlib.util

import pytest

from cnd.adapters import base
from tests.conftest import RAIZ

SPEC = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")


@pytest.mark.parametrize("curto,modulo", sorted(base.MODULOS_POR_ADAPTER.items()))
def test_o_modulo_do_mapa_existe(curto, modulo):
    assert importlib.util.find_spec(modulo) is not None, (
        f"o config aceita adapter = {curto!r}, mas {modulo} nao existe"
    )


@pytest.mark.parametrize("curto", sorted(base.MODULOS_POR_ADAPTER))
def test_adapter_existe_responde_pelo_nome_curto(curto):
    assert base.adapter_existe(curto)


def test_todo_adapter_de_orgao_expoe_criar():
    """`matriz` e `pdf` sao leitura compartilhada, nao orgao: nao tem criar()."""
    sem_criar = {"rfb_matriz", "rfb_pdf"}
    for curto, modulo in base.MODULOS_POR_ADAPTER.items():
        tem = hasattr(importlib.import_module(modulo), "criar")
        if curto in sem_criar:
            assert not tem, f"{curto} nao devia ser instanciavel como orgao"
        else:
            assert tem, f"{modulo} precisa expor criar(orgao, cfg)"


@pytest.mark.parametrize("modulo", [
    "cnd.adapters.federal.rfb.cego",
    "cnd.adapters.federal.rfb.cego_pf",
    "cnd.adapters.federal.crf",
    "cnd.adapters.estadual.sefaz_go",
    "cnd.adapters.estadual.sefaz_es",
    "cnd.adapters.estadual.sefaz_ma",
    "cnd.adapters.fake",
])
def test_o_empacotador_leva_os_adapters_ligaveis(modulo):
    """Sem a linha no .spec o executavel sobe e morre ao ligar o orgao."""
    assert f'"{modulo}"' in SPEC, f"falta {modulo} nos hiddenimports do acta.spec"
    assert importlib.util.find_spec(modulo) is not None


def test_o_que_o_spec_exclui_tambem_e_modulo_de_verdade():
    """Exclude com nome velho nao da erro - so deixa de excluir, em silencio."""
    assert '"cnd.adapters.federal.rfb.pj"' in SPEC
    assert importlib.util.find_spec("cnd.adapters.federal.rfb.pj") is not None
