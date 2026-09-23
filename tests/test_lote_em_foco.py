"""Qual planilha a tela de operação mostra quando há mais de uma.

A tela mostra UMA planilha: a da vez, que é a que o robô está autorizado a
emitir agora (core/fila.lote_da_vez) — a mais antiga com item para pegar, ou
a que foi mandada "Rodar agora". Enquanto a regra era "a mais recente com
pendência", a tela anunciava um arquivo e o robô trabalhava em outro
(22/09/2026).

`escolhido` continua mandando: é como a Carteira abre um envio antigo.
"""
from __future__ import annotations

from cnd.core.modelos import Status
from cnd.web import consultas
from tests.conftest import criar_job


def _lote(conn, descricao: str, arquivo: str) -> int:
    return conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
        (descricao, arquivo),
    ).lastrowid


def _em_execucao(conn, job_id: int) -> None:
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.RUNNING, job_id))


def test_sem_lote_nenhum_devolve_none(conn):
    assert consultas.lote_em_foco(conn) is None


def test_com_um_lote_so_devolve_ele(conn):
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    criar_job(conn, antiga, "11222333000181")

    assert consultas.lote_em_foco(conn)["id"] == antiga


def test_a_planilha_da_vez_e_a_mais_antiga_com_fila(conn):
    """A antiga termina primeiro, e a tela acompanha o robô."""
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    criar_job(conn, antiga, "11222333000181")
    nova = _lote(conn, "nova", "nova.xlsx")
    criar_job(conn, nova, "04401250000194")

    foco = consultas.lote_em_foco(conn)

    assert foco["id"] == antiga
    assert foco["arquivo_origem"] == "antiga.xlsx"


def test_sem_pendencia_em_outra_planilha_usa_a_que_esta_rodando(conn):
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    rodando = criar_job(conn, antiga, "11222333000181")
    nova = _lote(conn, "nova", "nova.xlsx")
    novo_job = criar_job(conn, nova, "04401250000194")
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.DONE, novo_job))
    _em_execucao(conn, rodando)

    foco = consultas.lote_em_foco(conn)

    assert foco["id"] == antiga
    assert foco["arquivo_origem"] == "antiga.xlsx"


def test_planilha_terminada_cede_a_vez_para_a_seguinte(conn):
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    pronto = criar_job(conn, antiga, "11222333000181")
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.DONE, pronto))
    nova = _lote(conn, "nova", "nova.xlsx")
    criar_job(conn, nova, "04401250000194")

    assert consultas.lote_em_foco(conn)["id"] == nova


def test_escolha_da_tela_manda_mais_que_o_que_esta_rodando(conn):
    """Quem está conferindo uma planilha não pode ser jogado de volta para
    a que o robô está fazendo a cada recarga automática."""
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    rodando = criar_job(conn, antiga, "11222333000181")
    nova = _lote(conn, "nova", "nova.xlsx")
    criar_job(conn, nova, "04401250000194")
    _em_execucao(conn, rodando)

    assert consultas.lote_em_foco(conn, nova)["id"] == nova


def test_lote_em_execucao_aponta_a_planilha_do_item_que_roda(conn):
    """A tela usa isso para avisar quem está olhando outra planilha que o
    trabalho corre em outra — sem esse aviso, números parados parecem tela
    travada."""
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    rodando = criar_job(conn, antiga, "11222333000181")
    nova = _lote(conn, "nova", "nova.xlsx")
    criar_job(conn, nova, "04401250000194")

    assert consultas.lote_em_execucao(conn) is None

    _em_execucao(conn, rodando)

    assert consultas.lote_em_execucao(conn) == antiga


def test_lote_inexistente_cai_no_padrao_em_vez_de_quebrar(conn):
    """O id vem da URL: alguém pode digitar qualquer número, ou apontar
    para um lote de OUTRA máquina — os ids são numerados por máquina."""
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    criar_job(conn, antiga, "11222333000181")

    assert consultas.lote_em_foco(conn, 9999)["id"] == antiga


def test_fila_cancelada_nao_conta_como_pendencia(conn):
    """Fila cancelada tem item PENDING que ninguém vai pegar.

    Aconteceu em 16/09/2026: a planilha do dia terminou, a tela pulou para
    uma planilha cancelada na véspera e anunciou 94 pendentes — trabalho que
    o robô nunca ia fazer, porque `fila.reivindicar` filtra pela mesma regra.
    """
    from cnd.core import controle

    antiga = _lote(conn, "antiga", "antiga.xlsx")
    rodando = criar_job(conn, antiga, "11222333000181")
    cancelada = _lote(conn, "cancelada", "cancelada.xlsx")
    criar_job(conn, cancelada, "04401250000194", orgao="RFB_PJ")
    controle.definir(conn, cancelada, "RFB_PJ", controle.CANCELADA)
    _em_execucao(conn, rodando)

    assert consultas.lote_com_fila(conn) is None
    assert consultas.lote_em_foco(conn)["id"] == antiga


def test_fila_estacionada_tambem_nao_conta(conn):
    from cnd.core import controle

    parada = _lote(conn, "parada", "parada.xlsx")
    criar_job(conn, parada, "04401250000194", orgao="RFB_PJ")
    controle.definir(conn, parada, "RFB_PJ", controle.ESTACIONADA)

    assert consultas.lote_com_fila(conn) is None
