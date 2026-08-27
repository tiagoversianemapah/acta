"""Qual planilha a tela de operação mostra quando há mais de uma.

Enviar uma planilha nova não interrompe a que está rodando: a fila é única
e ordenada por id. Se a tela mostrasse sempre a última enviada, ela ficaria
zerada — "0 de 2.829, 0%" — justamente enquanto o robô emite certidões da
anterior. Foi o que aconteceu em 14/08/2026, e é o que estes testes travam.
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


def test_planilha_em_processamento_vence_a_recem_enviada(conn):
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    rodando = criar_job(conn, antiga, "11222333000181")
    nova = _lote(conn, "nova", "nova.xlsx")
    criar_job(conn, nova, "04401250000194")
    _em_execucao(conn, rodando)

    foco = consultas.lote_em_foco(conn)

    assert foco["id"] == antiga
    assert foco["arquivo_origem"] == "antiga.xlsx"


def test_sem_nada_em_execucao_vale_a_mais_recente(conn):
    antiga = _lote(conn, "antiga", "antiga.xlsx")
    criar_job(conn, antiga, "11222333000181")
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
