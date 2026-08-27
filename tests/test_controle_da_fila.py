"""Estacionar, cancelar e furar a fila.

A fila era única e ordenada por id: a planilha que chegava depois esperava
a anterior terminar. Com RFB e CRF no mesmo arquivo, faltava poder dizer
"para essa e faz aquela agora".
"""
from __future__ import annotations

import pytest

from cnd.core import controle, fila
from cnd.core.modelos import Status
from tests.conftest import criar_job


@pytest.fixture
def outro_lote(conn):
    """Uma segunda planilha, para exercitar a ordem entre elas."""
    cursor = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) "
        "VALUES ('segunda', 'segunda.xlsx')")
    return cursor.lastrowid


def _pegar(conn, orgao="RFB_PJ"):
    return fila.reivindicar(conn, orgao)


def test_planilha_ativa_e_entregue(conn, lote):
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    assert _pegar(conn) is not None, "sem linha de controle, vale ATIVA"


def test_estacionada_nao_e_entregue(conn, lote):
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    controle.definir(conn, lote, "RFB_PJ", controle.ESTACIONADA)
    assert _pegar(conn) is None


def test_cancelada_nao_e_entregue(conn, lote):
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    controle.definir(conn, lote, "RFB_PJ", controle.CANCELADA)
    assert _pegar(conn) is None


def test_cancelar_nao_apaga_o_item(conn, lote):
    """A conta do relatório precisa continuar fechando."""
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    controle.definir(conn, lote, "RFB_PJ", controle.CANCELADA)
    n = conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
    assert n == 1, "cancelar tira da fila, não do banco"


def test_estacionar_e_reversivel(conn, lote):
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    controle.definir(conn, lote, "RFB_PJ", controle.ESTACIONADA)
    assert _pegar(conn) is None
    controle.definir(conn, lote, "RFB_PJ", controle.ATIVA)
    assert _pegar(conn) is not None, "retomar devolve de onde parou"


def test_estacionar_uma_automacao_nao_para_a_outra(conn, lote):
    """O caso que motivou tudo: RFB e CRF na MESMA planilha."""
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    criar_job(conn, lote, documento="00000000000002", orgao="CRF")
    controle.definir(conn, lote, "RFB_PJ", controle.ESTACIONADA)

    assert _pegar(conn, "RFB_PJ") is None
    assert _pegar(conn, "CRF") is not None


def test_rodar_agora_fura_a_fila(conn, lote, outro_lote):
    """A planilha nova espera a antiga — a não ser que se mande o contrário."""
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    criar_job(conn, outro_lote, documento="00000000000002", orgao="RFB_PJ")

    controle.priorizar(conn, outro_lote, "RFB_PJ")
    pego = _pegar(conn)
    assert pego.lote_id == outro_lote, "a priorizada vem primeiro"


def test_rodar_agora_reativa_o_que_estava_estacionado(conn, lote):
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    controle.definir(conn, lote, "RFB_PJ", controle.ESTACIONADA)
    controle.priorizar(conn, lote, "RFB_PJ")
    assert _pegar(conn) is not None, "mandar rodar tira do estacionamento"


def test_situacao_nao_escreve(conn, lote):
    """O painel lê isto numa conexão sem permissão de escrita."""
    controle.situacao(conn, lote, "CRF")
    n = conn.execute("SELECT COUNT(*) AS n FROM fila_controle").fetchone()["n"]
    assert n == 0


def test_situacao_invalida_e_recusada(conn, lote):
    with pytest.raises(ValueError):
        controle.definir(conn, lote, "RFB_PJ", "INVENTADA")


def test_dois_workers_nao_pegam_o_mesmo_item(conn, lote):
    """A trava contra corrida não pode ter sido perdida no caminho.

    É o risco real desta mudança: errar aqui não dá erro na tela, dá
    certidão duplicada descoberta semanas depois no relatório.
    """
    criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
    primeiro = _pegar(conn)
    segundo = _pegar(conn)
    assert primeiro is not None
    assert segundo is None, "o item já está RUNNING; ninguém mais o pega"
    situacao_job = conn.execute("SELECT status FROM job").fetchone()["status"]
    assert situacao_job == Status.RUNNING


def test_oito_workers_disputando_nao_quebram_nem_duplicam(tmp_path):
    """Fumaça, não prova: oito conexões disputando a mesma linha.

    Vale pelo que exercita — o `reivindicar` novo, com o LEFT JOIN no
    controle, sob concorrência real e sem quebrar. NÃO vale como prova da
    trava: trocando `BEGIN IMMEDIATE` por `BEGIN DEFERRED` este teste
    continua passando, então ele não distingue trava boa de trava ruim.

    Fica registrado para ninguém confiar demais nele. Provar a corrida
    exigiria controlar o instante entre o SELECT e o UPDATE dos dois
    lados, o que pede um ponto de pausa dentro da função — e um teste que
    obriga a mexer no código que ele testa costuma sair caro. Se um dia
    aparecer certidão duplicada, é aqui que se olha primeiro.
    """
    import sqlite3
    import threading

    from cnd.infra.db import conectar, garantir

    banco = tmp_path / "cnd.db"
    garantir(banco)
    preparo = conectar(banco)
    try:
        lote_id = preparo.execute(
            "INSERT INTO lote (descricao, arquivo_origem) "
            "VALUES ('corrida', 'c.xlsx')").lastrowid
        criar_job(preparo, lote_id, documento="00000000000001", orgao="RFB_PJ")
        preparo.commit()
    finally:
        preparo.close()

    pegos, erros = [], []
    largada = threading.Barrier(8)

    def worker():
        conexao = conectar(banco)
        try:
            largada.wait()
            for _ in range(40):
                try:
                    job = fila.reivindicar(conexao, "RFB_PJ")
                except sqlite3.OperationalError:
                    continue          # banco ocupado: tenta de novo
                if job is not None:
                    pegos.append(job.job_id)
                return
        except Exception as erro:
            erros.append(erro)
        finally:
            conexao.close()

    fios = [threading.Thread(target=worker) for _ in range(8)]
    for f in fios:
        f.start()
    for f in fios:
        f.join(timeout=30)

    assert not erros, f"nenhum worker pode quebrar: {erros}"
    assert len(pegos) == 1, f"o item foi entregue {len(pegos)} vezes"


def test_estacionar_no_meio_da_corrida_para_de_entregar(tmp_path):
    """Estacionar vale a partir do próximo item, não do meio de um."""
    from cnd.infra.db import conectar, garantir

    banco = tmp_path / "cnd.db"
    garantir(banco)
    conexao = conectar(banco)
    try:
        lote_id = conexao.execute(
            "INSERT INTO lote (descricao, arquivo_origem) "
            "VALUES ('x', 'x.xlsx')").lastrowid
        for i in range(3):
            criar_job(conexao, lote_id, documento=f"{i:014d}", orgao="RFB_PJ")
        conexao.commit()

        assert fila.reivindicar(conexao, "RFB_PJ") is not None
        controle.definir(conexao, lote_id, "RFB_PJ", controle.ESTACIONADA)
        conexao.commit()
        assert fila.reivindicar(conexao, "RFB_PJ") is None, "os demais param"
    finally:
        conexao.close()
