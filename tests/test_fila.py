"""Testes da fila e da máquina de estados.

O que estes testes protegem: que dois workers nunca peguem o mesmo job,
que um crash não perca trabalho, e que retry e falha definitiva se
comportem como o documento 04 descreve.
"""
from __future__ import annotations

from cnd.core import fila, tempo
from cnd.core.modelos import Desfecho, ResultadoTentativa, Status
from tests.conftest import criar_job


def test_reivindicar_marca_running(conn, lote):
    criar_job(conn, lote)

    job = fila.reivindicar(conn, "FAKE")

    assert job is not None
    assert job.doc.documento == "11222333000181"
    status = conn.execute("SELECT status FROM job WHERE id = ?", (job.job_id,)).fetchone()
    assert status["status"] == Status.RUNNING


def test_um_job_nao_e_entregue_duas_vezes(conn, lote):
    criar_job(conn, lote)

    primeiro = fila.reivindicar(conn, "FAKE")
    segundo = fila.reivindicar(conn, "FAKE")

    assert primeiro is not None
    assert segundo is None, "o mesmo job não pode ser entregue a dois workers"


def test_orgaos_nao_se_misturam(conn, lote):
    criar_job(conn, lote, "11222333000181", orgao="FAKE")
    criar_job(conn, lote, "12ABC34501DE35", orgao="RFB_PJ")

    job = fila.reivindicar(conn, "RFB_PJ")

    assert job is not None
    assert job.doc.documento == "12ABC34501DE35"


def test_job_agendado_para_o_futuro_nao_sai(conn, lote):
    job_id = criar_job(conn, lote)
    conn.execute(
        "UPDATE job SET proxima_execucao_em = ? WHERE id = ?",
        (tempo.daqui_a(3600), job_id),
    )

    assert fila.reivindicar(conn, "FAKE") is None


def test_concluir_grava_desfecho(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.NEGATIVA))

    linha = conn.execute(
        "SELECT status, desfecho, tentativas FROM job WHERE id = ?", (job.job_id,)
    ).fetchone()
    assert linha["status"] == Status.DONE
    assert linha["desfecho"] == Desfecho.NEGATIVA
    assert linha["tentativas"] == 1


def test_reagendar_deixa_aguardando_retry(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    fila.reagendar(conn, job, Desfecho.CAPTCHA, espera_s=0)

    linha = conn.execute(
        "SELECT status, tentativas FROM job WHERE id = ?", (job.job_id,)
    ).fetchone()
    assert linha["status"] == Status.RETRY_WAIT
    assert linha["tentativas"] == 1
    assert fila.reivindicar(conn, "FAKE") is not None


def test_retry_futuro_nao_sai_antes_da_hora(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    fila.reagendar(conn, job, Desfecho.CAPTCHA, espera_s=3600)

    assert fila.reivindicar(conn, "FAKE") is None


def test_devolver_nao_conta_tentativa(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    fila.devolver(conn, job)

    linha = conn.execute(
        "SELECT status, tentativas, desfecho FROM job WHERE id = ?", (job.job_id,)
    ).fetchone()
    assert linha["status"] == Status.PENDING
    assert linha["tentativas"] == 0
    assert linha["desfecho"] is None
    assert fila.reivindicar(conn, "FAKE") is not None


def test_falhar_e_terminal(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    fila.falhar(conn, job, Desfecho.CAPTCHA)

    linha = conn.execute("SELECT status FROM job WHERE id = ?", (job.job_id,)).fetchone()
    assert linha["status"] == Status.FAILED
    assert fila.reivindicar(conn, "FAKE") is None


def test_crash_nao_perde_job(conn, lote):
    """Um job em RUNNING quando a máquina cai precisa voltar para a fila."""
    criar_job(conn, lote)
    fila.reivindicar(conn, "FAKE")   # simula o worker que morreu aqui

    recuperados = fila.recuperar_orfaos(conn)

    assert recuperados == 1
    assert fila.reivindicar(conn, "FAKE") is not None


def test_reenfileirar_falhados_zera_tentativas(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")
    fila.falhar(conn, job, Desfecho.ERRO_TECNICO)

    quantidade = fila.reenfileirar_falhados(conn, "FAKE")

    linha = conn.execute(
        "SELECT status, tentativas FROM job WHERE id = ?", (job.job_id,)
    ).fetchone()
    assert quantidade == 1
    assert linha["status"] == Status.PENDING
    assert linha["tentativas"] == 0


def test_reenfileirar_falhados_respeita_lote(conn, lote):
    outro_lote = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('outro', 'outro.xlsx')"
    ).lastrowid
    job_do_lote = criar_job(conn, lote, documento="11222333000181", orgao="FAKE")
    job_do_outro = criar_job(conn, outro_lote, documento="11444777000161", orgao="FAKE")
    conn.execute(
        "UPDATE job SET status = ?, desfecho = ?, tentativas = 3 WHERE id IN (?, ?)",
        (Status.FAILED, Desfecho.ERRO_TECNICO, job_do_lote, job_do_outro),
    )

    quantidade = fila.reenfileirar_falhados(conn, "FAKE", lote)

    status_lote = conn.execute(
        "SELECT status FROM job WHERE id = ?", (job_do_lote,)
    ).fetchone()["status"]
    status_outro = conn.execute(
        "SELECT status FROM job WHERE id = ?", (job_do_outro,)
    ).fetchone()["status"]
    assert quantidade == 1
    assert status_lote == Status.PENDING
    assert status_outro == Status.FAILED


def test_ha_trabalho(conn, lote):
    assert fila.ha_trabalho(conn, "FAKE") is False

    criar_job(conn, lote)
    assert fila.ha_trabalho(conn, "FAKE") is True

    job = fila.reivindicar(conn, "FAKE")
    fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.NEGATIVA))
    assert fila.ha_trabalho(conn, "FAKE") is False


def _guardar_certidao(conn, job_id: int, emitida_em: str, valida_ate: str):
    conn.execute(
        "INSERT INTO certidao (job_id, tipo, emitida_em, valida_ate, caminho_pdf, sha256) "
        "VALUES (?, 'NEGATIVA', ?, ?, 'x.pdf', 'abc')",
        (job_id, emitida_em, valida_ate),
    )


def test_certidao_do_mes_e_reaproveitada(conn, lote):
    job_id = criar_job(conn, lote)
    empresa_id = conn.execute(
        "SELECT empresa_id FROM job WHERE id = ?", (job_id,)
    ).fetchone()["empresa_id"]
    hoje = tempo.agora_iso()
    _guardar_certidao(conn, job_id, hoje, "2099-12-31")

    assert fila.certidao_do_mes(conn, empresa_id, "FAKE") is not None


def test_certidao_do_mes_passado_nao_serve(conn, lote):
    """Ainda válida (180 dias), mas de outro mês: quem recebe recusa.
    Reaproveitá-la marcaria o job como concluído sem entregar nada útil."""
    job_id = criar_job(conn, lote)
    empresa_id = conn.execute(
        "SELECT empresa_id FROM job WHERE id = ?", (job_id,)
    ).fetchone()["empresa_id"]
    _guardar_certidao(conn, job_id, "2020-01-15T10:00:00.000Z", "2099-12-31")

    assert fila.certidao_do_mes(conn, empresa_id, "FAKE") is None


def test_certidao_de_outra_planilha_nao_serve(conn, lote):
    """Duas remessas são trabalhos separados, ainda que tragam os mesmos
    CNPJs. Sem o filtro por lote, reenviar a planilha fechava a segunda
    inteira como APROVEITADA e não gerava um PDF novo — enquanto quem
    mandou de novo queria exatamente certidões novas."""
    job_id = criar_job(conn, lote)
    empresa_id = conn.execute(
        "SELECT empresa_id FROM job WHERE id = ?", (job_id,)
    ).fetchone()["empresa_id"]
    _guardar_certidao(conn, job_id, tempo.agora_iso(), "2099-12-31")
    outro_lote = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('2a', 'igual.xlsx')"
    ).lastrowid

    assert fila.certidao_do_mes(conn, empresa_id, "FAKE", lote) is not None
    assert fila.certidao_do_mes(conn, empresa_id, "FAKE", outro_lote) is None


def test_certidao_de_outro_orgao_nao_serve(conn, lote):
    job_id = criar_job(conn, lote, orgao="FAKE")
    empresa_id = conn.execute(
        "SELECT empresa_id FROM job WHERE id = ?", (job_id,)
    ).fetchone()["empresa_id"]
    _guardar_certidao(conn, job_id, tempo.agora_iso(), "2099-12-31")

    assert fila.certidao_do_mes(conn, empresa_id, "RFB_PJ") is None


def test_tentativa_registra_historico(conn, lote):
    criar_job(conn, lote)
    job = fila.reivindicar(conn, "FAKE")

    tentativa_id = fila.abrir_tentativa(conn, job, worker=0)
    fila.fechar_tentativa(conn, tentativa_id, ResultadoTentativa(
        desfecho=Desfecho.CAPTCHA, mensagem_portal="desafio exibido"))

    linha = conn.execute("SELECT * FROM tentativa WHERE id = ?", (tentativa_id,)).fetchone()
    assert linha["numero"] == 1
    assert linha["desfecho"] == Desfecho.CAPTCHA
    assert linha["mensagem_portal"] == "desafio exibido"
    assert linha["finalizada_em"] is not None
