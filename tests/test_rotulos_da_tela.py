"""Todo desfecho e todo status têm nome em português na tela.

Sem isto, acrescentar um desfecho novo no `core` faz a tela mostrar "—" no
lugar do resultado — e ninguém percebe até alguém perguntar por que uma
empresa está sem situação.

Os testes importam só os dicionários do módulo da janela; nenhuma janela é
aberta, então rodam no mesmo lugar que o resto da suíte.
"""
from __future__ import annotations

import pytest

from cnd.core.modelos import CONCLUSIVOS, RETENTAVEIS, Desfecho, Status


@pytest.fixture(scope="module")
def tela():
    return pytest.importorskip("cnd.desktop.app")


def test_todo_desfecho_tem_rotulo(tela):
    faltando = [d for d in Desfecho if d not in tela.ROTULOS_DE_RESULTADO]
    assert not faltando, f"sem rótulo na tela: {faltando}"


def test_todo_desfecho_tem_cor(tela):
    for desfecho in Desfecho:
        _, cor = tela.ROTULOS_DE_RESULTADO[desfecho]
        assert cor.startswith("#") and len(cor) == 7, f"{desfecho}: {cor!r}"


def test_o_filtro_oferece_todo_desfecho_conclusivo(tela):
    """O que a operação separa é resultado de negócio: limpa ou não."""
    oferecidos = {v for v in tela.RESULTADOS.values() if v}
    faltando = {d for d in CONCLUSIVOS if d not in oferecidos}
    assert not faltando, f"não dá para filtrar por: {faltando}"


def test_o_filtro_oferece_todo_desfecho_retentavel(tela):
    """Quem acompanha o processamento precisa achar o que travou."""
    oferecidos = {v for v in tela.RESULTADOS.values() if v}
    faltando = {d for d in RETENTAVEIS if d not in oferecidos}
    assert not faltando, f"não dá para filtrar por: {faltando}"


def test_o_filtro_de_situacao_so_usa_status_que_existem(tela):
    validos = set(Status)
    usados = {v for v in tela.SITUACOES.values() if v}
    assert usados <= validos, f"status inexistente no filtro: {usados - validos}"


def test_status_sem_resultado_so_usa_status_que_existem(tela):
    validos = set(Status)
    assert set(tela.SITUACAO_SEM_RESULTADO) <= validos


def test_todo_status_nao_concluido_tem_texto(tela):
    """Item na fila ou em execução ainda não tem desfecho; a tela cai no
    texto da situação, e ele precisa existir para os dois casos."""
    for status in (Status.PENDING, Status.RUNNING, Status.RETRY_WAIT,
                   Status.FAILED):
        assert tela.SITUACAO_SEM_RESULTADO.get(status)
