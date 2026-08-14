"""A lista de "o que o robô acabou de fazer", da tela de máquinas."""
from __future__ import annotations

from datetime import UTC, datetime

from cnd.core import fila, tempo
from cnd.core.modelos import Desfecho, ResultadoTentativa
from cnd.web import consultas
from tests.conftest import criar_job


def _consultar(conn, lote_id: int, nome: str, documento: str,
               desfecho: Desfecho | None = Desfecho.NEGATIVA) -> int:
    """Roda uma consulta de ponta a ponta e devolve o id da tentativa.

    `desfecho=None` deixa a tentativa aberta, como acontece enquanto o robô
    ainda está no portal.
    """
    criar_job(conn, lote_id, documento=documento, nome=nome)
    job = fila.reivindicar(conn, "FAKE")
    tentativa_id = fila.abrir_tentativa(conn, job, worker=1)
    if desfecho is not None:
        fila.fechar_tentativa(conn, tentativa_id,
                              ResultadoTentativa(desfecho=desfecho))
        fila.concluir(conn, job, ResultadoTentativa(desfecho=desfecho))
    return tentativa_id


DOCUMENTOS = ["11222333000181", "11444777000161", "34028316000103"]


def test_vem_da_mais_recente_para_a_mais_antiga(conn, lote):
    for indice, documento in enumerate(DOCUMENTOS):
        _consultar(conn, lote, f"EMPRESA {indice}", documento)

    eventos = consultas.ultimas_tentativas(conn)
    assert [e["nome"] for e in eventos] == ["EMPRESA 2", "EMPRESA 1", "EMPRESA 0"]


def test_respeita_o_limite(conn, lote):
    for indice, documento in enumerate(DOCUMENTOS):
        _consultar(conn, lote, f"EMPRESA {indice}", documento)
    assert len(consultas.ultimas_tentativas(conn, limite=2)) == 2


def test_traz_a_empresa_e_o_desfecho(conn, lote):
    _consultar(conn, lote, "BRAVA ENGENHARIA", DOCUMENTOS[0], Desfecho.CPEN)

    evento = consultas.ultimas_tentativas(conn)[0]
    assert evento["nome"] == "BRAVA ENGENHARIA"
    assert evento["desfecho"] == Desfecho.CPEN
    assert evento["orgao"] == "FAKE"
    assert evento["quando"]
    assert not evento["em_curso"]
    assert not evento["interrompida"]


def test_hora_vem_no_fuso_local(conn, lote):
    tentativa_id = _consultar(conn, lote, "FUSO", DOCUMENTOS[0])
    conn.execute(
        "UPDATE tentativa SET finalizada_em = ? WHERE id = ?",
        (tempo.para_iso(datetime(2026, 8, 13, 19, 25, 36, tzinfo=UTC)),
         tentativa_id),
    )

    evento = consultas.ultimas_tentativas(conn)[0]

    assert evento["hora"] == "16:25:36"


def test_tentativa_aberta_agora_aparece_em_curso(conn, lote):
    _consultar(conn, lote, "EM ANDAMENTO", DOCUMENTOS[0], desfecho=None)

    evento = consultas.ultimas_tentativas(conn)[0]
    assert evento["em_curso"]
    assert not evento["interrompida"]


def test_tentativa_antiga_sem_fim_e_interrompida(conn, lote):
    """O robô encerrado no meio deixa a linha sem `finalizada_em` para
    sempre — `recuperar_orfaos` devolve o job à fila, mas não reescreve a
    tentativa. Chamar isso de "consultando" mostraria como atividade de
    agora uma consulta de horas atrás."""
    tentativa_id = _consultar(conn, lote, "MORREU NO MEIO", DOCUMENTOS[0],
                              desfecho=None)
    conn.execute("UPDATE tentativa SET iniciada_em = ? WHERE id = ?",
                 (tempo.daqui_a(-3600), tentativa_id))

    evento = consultas.ultimas_tentativas(conn)[0]
    assert not evento["em_curso"]
    assert evento["interrompida"]
