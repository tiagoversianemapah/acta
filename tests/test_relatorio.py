from openpyxl import load_workbook

from cnd.core import tempo
from cnd.core.modelos import Desfecho, Status
from cnd.web.relatorio import Recorte, gerar, mes_corrente
from tests.conftest import criar_job


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

    caminho = gerar(conn, Recorte(mes_corrente()),
                    tmp_path / "relatorio.xlsx")

    livro = load_workbook(caminho)
    assert "Auditoria" in livro.sheetnames
    planilha = livro["Auditoria"]
    assert planilha["A1"].value == "Tentativa"
    assert planilha["D2"].value == "11.222.333/0001-81"
    assert planilha["F2"].value == Status.DONE
    assert planilha["G2"].value == Desfecho.NEGATIVA
    assert planilha["K2"].value == Desfecho.NEGATIVA
    assert planilha["M2"].value == "emitida com sucesso"


def test_recorte_por_orgao_deixa_os_outros_de_fora(conn, lote, tmp_path):
    """Quem filtra "Receita Federal" e pede a planilha espera receber a
    Receita Federal — antes vinha o lote inteiro, sem aviso."""
    for documento, orgao, nome in (
        ("11222333000181", "RFB_PJ", "EMPRESA FEDERAL"),
        ("11444777000161", "SEFAZ_GO", "EMPRESA ESTADUAL"),
    ):
        job = criar_job(conn, lote, documento=documento, orgao=orgao,
                        nome=nome)
        conn.execute("UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
                     (Status.DONE, Desfecho.NEGATIVA, job))

    caminho = gerar(conn, Recorte(mes_corrente(), "RFB_PJ"),
                    tmp_path / "federal.xlsx")

    negativas = load_workbook(caminho)["Negativas"]
    nomes = [linha[0] for linha in negativas.iter_rows(min_row=2,
                                                       values_only=True)]
    assert "EMPRESA FEDERAL" in nomes
    assert "EMPRESA ESTADUAL" not in nomes


def test_arquivo_pdf_nao_vira_link_quebrado(conn, lote, tmp_path):
    job = criar_job(conn, lote, documento="11222333000181",
                    orgao="RFB_PJ", nome="EMPRESA COM PDF")
    pdf = tmp_path / "certidao.pdf"
    pdf.write_bytes(b"%PDF")
    conn.execute(
        "UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
        (Status.DONE, Desfecho.NEGATIVA, job),
    )
    conn.execute(
        """
        INSERT INTO certidao
            (job_id, tipo, emitida_em, valida_ate, codigo_controle,
             caminho_pdf, sha256)
        VALUES (?, ?, ?, '2027-02-03', 'ABC123', ?, 'x')
        """,
        (job, Desfecho.NEGATIVA, tempo.agora_iso(), str(pdf)),
    )

    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "pdf.xlsx")

    negativas = load_workbook(caminho)["Negativas"]
    assert negativas["G2"].value == "certidao.pdf"
    assert negativas["G2"].hyperlink is None


def test_sem_orgao_traz_todos(conn, lote, tmp_path):
    for documento, orgao in (("11222333000181", "RFB_PJ"),
                             ("11444777000161", "SEFAZ_GO")):
        job = criar_job(conn, lote, documento=documento, orgao=orgao,
                        nome=f"EMPRESA {orgao}")
        conn.execute("UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
                     (Status.DONE, Desfecho.NEGATIVA, job))

    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "tudo.xlsx")

    negativas = load_workbook(caminho)["Negativas"]
    assert negativas.max_row == 3, "cabeçalho + duas empresas"


def test_mes_anterior_fica_de_fora(conn, lote, tmp_path):
    """A emissão é mensal: a planilha de agosto não leva o que saiu em
    julho, senão o cliente recebe certidão vencida na conferência."""
    job = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PJ",
                    nome="EMPRESA ANTIGA")
    conn.execute(
        "UPDATE job SET status = ?, desfecho = ?, atualizado_em = ? WHERE id = ?",
        (Status.DONE, Desfecho.NEGATIVA, "2020-01-15T10:00:00.000Z", job))

    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "mes.xlsx")

    negativas = load_workbook(caminho)["Negativas"]
    assert negativas.max_row == 1, "só o cabeçalho"
