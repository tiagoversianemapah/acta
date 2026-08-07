from openpyxl import load_workbook
from tests.conftest import criar_job

from cnd.core import tempo
from cnd.core.modelos import Desfecho, Status
from cnd.web.relatorio import gerar


def test_relatorio_tem_aba_de_auditoria(conn, lote, tmp_path):
    job_id = criar_job(
        conn,
        lote,
        documento="11222333000181",
        orgao="RFB_PJ",
        nome="EMPRESA AUDITADA",
    )
    conn.execute(
        "UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
        (Status.DONE, Desfecho.NEGATIVA, job_id),
    )
    conn.execute(
        """
        INSERT INTO tentativa
            (job_id, numero, iniciada_em, finalizada_em, desfecho,
             mensagem_portal, evidencia, worker)
        VALUES (?, 1, ?, ?, ?, ?, ?, 0)
        """,
        (
            job_id,
            tempo.agora_iso(),
            tempo.agora_iso(),
            Desfecho.NEGATIVA,
            "emitida com sucesso",
            "evidencia.png",
        ),
    )

    caminho = gerar(conn, lote, tmp_path / "relatorio.xlsx")

    livro = load_workbook(caminho)
    assert "Auditoria" in livro.sheetnames
    planilha = livro["Auditoria"]
    assert planilha["A1"].value == "Tentativa"
    assert planilha["D2"].value == "11.222.333/0001-81"
    assert planilha["F2"].value == Status.DONE
    assert planilha["G2"].value == Desfecho.NEGATIVA
    assert planilha["K2"].value == Desfecho.NEGATIVA
    assert planilha["M2"].value == "emitida com sucesso"
