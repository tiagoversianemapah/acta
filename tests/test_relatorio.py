from openpyxl import load_workbook

from cnd.core import tempo
from cnd.core.modelos import Desfecho, Status
from cnd.web.relatorio import Recorte, gerar, mes_corrente
from tests.conftest import criar_job


def test_planilha_tem_quatro_abas(conn, lote, tmp_path):
    """Uma aba por pergunta da operação, e nada além.

    Eram nove — quatro delas com colunas idênticas, mudando só o desfecho,
    mais a Auditoria (uma linha por tentativa do robô), que é diagnóstico
    técnico e não relatório.
    """
    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "abas.xlsx")

    livro = load_workbook(caminho)
    assert livro.sheetnames == ["Resumo", "Certidões", "Pendências", "Erros"]


def test_desfecho_vira_coluna_com_nome_legivel(conn, lote, tmp_path):
    """Com a aba juntando desfechos, o desfecho tem de aparecer na linha —
    e escrito em português, não como 'PENDENCIA_MANUAL'."""
    for documento, desfecho, nome in (
        ("11222333000181", Desfecho.CPEN, "EMPRESA PARCELADA"),
        ("11444777000161", Desfecho.PENDENCIA_MANUAL, "EMPRESA SEM DADOS"),
    ):
        job = criar_job(conn, lote, documento=documento, orgao="RFB_PJ",
                        nome=nome)
        conn.execute("UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
                     (Status.DONE, desfecho, job))

    livro = load_workbook(
        gerar(conn, Recorte(mes_corrente()), tmp_path / "colunas.xlsx"))

    certidoes = livro["Certidões"]
    assert certidoes["A1"].value == "Empresa" and certidoes["D1"].value == "Tipo"
    assert certidoes["A2"].value == "EMPRESA PARCELADA"
    assert certidoes["D2"].value == "Positiva c/ efeito de negativa"

    pendencias = livro["Pendências"]
    assert pendencias["D1"].value == "Situação"
    assert pendencias["A2"].value == "EMPRESA SEM DADOS"
    assert pendencias["D2"].value == "Informações insuficientes"


def test_aproveitada_entra_com_a_certidao_que_reusou(conn, lote, tmp_path):
    """A APROVEITADA vale como certidão em mãos e traz emissão, validade,
    código e arquivo — a do job reenfileirado, que não reemitiu.

    Ela ficava numa aba própria, no formato das pendências: sem nenhuma
    dessas colunas. Quem somava o que tinha para entregar deixava de
    contá-la, embora o PDF já estivesse no pacote."""
    pdf = tmp_path / "certidao.pdf"
    pdf.write_bytes(b"%PDF")
    job = criar_job(conn, lote, documento="11222333000181",
                    orgao="RFB_PJ", nome="EMPRESA REPETIDA")
    conn.execute(
        """
        INSERT INTO certidao
            (job_id, tipo, emitida_em, valida_ate, codigo_controle,
             caminho_pdf, sha256)
        VALUES (?, ?, ?, '2027-02-03', 'ABC123', ?, 'x')
        """,
        (job, Desfecho.NEGATIVA, tempo.agora_iso(), str(pdf)),
    )
    conn.execute("UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
                 (Status.DONE, Desfecho.APROVEITADA, job))

    livro = load_workbook(
        gerar(conn, Recorte(mes_corrente()), tmp_path / "aproveitada.xlsx"))

    certidoes = livro["Certidões"]
    assert certidoes["D2"].value == "Já emitida no mês"
    assert certidoes["F2"].value == "03/02/2027"
    assert certidoes["G2"].value == "ABC123"
    assert certidoes["H2"].value == "certidao.pdf"


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

    certidoes = load_workbook(caminho)["Certidões"]
    nomes = [linha[0] for linha in certidoes.iter_rows(min_row=2,
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

    certidoes = load_workbook(caminho)["Certidões"]
    assert certidoes["H2"].value == "certidao.pdf"
    assert certidoes["H2"].hyperlink is None


def test_sem_orgao_traz_todos(conn, lote, tmp_path):
    for documento, orgao in (("11222333000181", "RFB_PJ"),
                             ("11444777000161", "SEFAZ_GO")):
        job = criar_job(conn, lote, documento=documento, orgao=orgao,
                        nome=f"EMPRESA {orgao}")
        conn.execute("UPDATE job SET status = ?, desfecho = ? WHERE id = ?",
                     (Status.DONE, Desfecho.NEGATIVA, job))

    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "tudo.xlsx")

    certidoes = load_workbook(caminho)["Certidões"]
    assert certidoes.max_row == 3, "cabeçalho + duas empresas"


def test_mes_anterior_fica_de_fora(conn, lote, tmp_path):
    """A emissão é mensal: a planilha de agosto não leva o que saiu em
    julho, senão o cliente recebe certidão vencida na conferência."""
    job = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PJ",
                    nome="EMPRESA ANTIGA")
    conn.execute(
        "UPDATE job SET status = ?, desfecho = ?, atualizado_em = ? WHERE id = ?",
        (Status.DONE, Desfecho.NEGATIVA, "2020-01-15T10:00:00.000Z", job))

    caminho = gerar(conn, Recorte(mes_corrente()), tmp_path / "mes.xlsx")

    certidoes = load_workbook(caminho)["Certidões"]
    assert certidoes.max_row == 1, "só o cabeçalho"
