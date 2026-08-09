"""Relatório final do lote em Excel — ver docs/05, seção 2.

Espelha o formato que a equipe já usa: uma aba por tipo de desfecho, mais
resumo e erros. É o produto que sai do sistema para a operação.
"""
from __future__ import annotations

import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from cnd.core import tempo
from cnd.core.documentos import formatar
from cnd.core.modelos import Desfecho, Status
from cnd.infra import config
from cnd.web import consultas

CABECALHO = Font(bold=True, color="FFFFFF")
FUNDO = PatternFill("solid", fgColor="1F4E79")


def _escrever(planilha, titulos: list[str], linhas: list[list]) -> None:
    planilha.append(titulos)
    for celula in planilha[1]:
        celula.font = CABECALHO
        celula.fill = FUNDO
        celula.alignment = Alignment(horizontal="center")
    for linha in linhas:
        planilha.append(linha)

    for coluna in range(1, len(titulos) + 1):
        largura = max(
            [len(str(titulos[coluna - 1]))]
            + [len(str(linha[coluna - 1])) for linha in linhas[:200]
               if coluna - 1 < len(linha) and linha[coluna - 1] is not None]
            + [10]
        )
        planilha.column_dimensions[get_column_letter(coluna)].width = min(largura + 2, 55)
    planilha.freeze_panes = "A2"


def _data_curta(iso: str | None) -> str:
    """'2026-08-07T13:28:14.000Z' vira '07/08/2026'.

    Quem abre a planilha quer conferir data de emissão e validade de
    relance; carimbo de tempo em UTC com milissegundos atrapalha.
    """
    if not iso:
        return ""
    texto = str(iso)
    if len(texto) >= 10 and texto[4] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[:4]}"
    return texto


def _linkar_pdfs(planilha, registros: list[sqlite3.Row], coluna: int) -> None:
    """Transforma o nome do arquivo em link clicável para o PDF.

    Sem isso, quem recebe a planilha lê o nome do arquivo e vai procurar a
    pasta na mão. Com o link, clica na célula e o PDF abre.
    """
    for indice, registro in enumerate(registros, start=2):     # linha 1 = cabeçalho
        caminho = registro["caminho_pdf"]
        if not caminho:
            continue
        celula = planilha.cell(row=indice, column=coluna)
        celula.hyperlink = Path(caminho).resolve().as_uri()
        celula.font = Font(color="0563C1", underline="single")


def _por_desfecho(conn: sqlite3.Connection, lote_id: int, desfecho: Desfecho) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.nome, e.documento, j.orgao, j.atualizado_em,
               c.emitida_em, c.valida_ate, c.codigo_controle, c.caminho_pdf,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS mensagem
          FROM job j
          JOIN empresa e ON e.id = j.empresa_id
          LEFT JOIN certidao c ON c.job_id = j.id
         WHERE j.lote_id = ? AND j.desfecho = ?
         ORDER BY e.nome
        """,
        (lote_id, str(desfecho)),
    ).fetchall()


def gerar(conn: sqlite3.Connection, lote_id: int, destino: Path | None = None) -> Path:
    lote = conn.execute("SELECT * FROM lote WHERE id = ?", (lote_id,)).fetchone()
    if lote is None:
        raise ValueError(f"Lote {lote_id} não existe")

    livro = Workbook()
    livro.remove(livro.active)

    # --- abas com PDF ---
    for desfecho, titulo in ((Desfecho.NEGATIVA, "Negativas"), (Desfecho.CPEN, "CPEN")):
        registros = _por_desfecho(conn, lote_id, desfecho)
        linhas = [
            [linha["nome"], formatar(linha["documento"]), linha["orgao"],
             _data_curta(linha["emitida_em"]), _data_curta(linha["valida_ate"]),
             linha["codigo_controle"],
             Path(linha["caminho_pdf"]).name if linha["caminho_pdf"] else ""]
            for linha in registros
        ]
        planilha = livro.create_sheet(titulo)
        _escrever(planilha,
                  ["Empresa", "Documento", "Órgão", "Emitida em", "Válida até",
                   "Código de controle", "Arquivo PDF"], linhas)
        _linkar_pdfs(planilha, registros, coluna=7)

    # --- abas sem PDF ---
    for desfecho, titulo in ((Desfecho.POSITIVA, "Positivas"),
                             (Desfecho.PENDENCIA_MANUAL, "Pendencia manual"),
                             (Desfecho.APROVEITADA, "Aproveitadas")):
        linhas = [
            [linha["nome"], formatar(linha["documento"]), linha["orgao"],
             linha["atualizado_em"], (linha["mensagem"] or "")[:300]]
            for linha in _por_desfecho(conn, lote_id, desfecho)
        ]
        _escrever(livro.create_sheet(titulo),
                  ["Empresa", "Documento", "Órgão", "Consultado em", "Mensagem do portal"],
                  linhas)

    # --- erros ---
    erros = conn.execute(
        """
        SELECT e.nome, e.documento, j.orgao, j.desfecho, j.tentativas, j.atualizado_em,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS mensagem
          FROM job j JOIN empresa e ON e.id = j.empresa_id
         WHERE j.lote_id = ? AND j.status = ?
         ORDER BY e.nome
        """,
        (lote_id, Status.FAILED),
    ).fetchall()
    _escrever(
        livro.create_sheet("Erros"),
        ["Empresa", "Documento", "Órgão", "Última falha", "Tentativas",
         "Quando", "Mensagem"],
        [[linha["nome"], formatar(linha["documento"]), linha["orgao"],
          linha["desfecho"], linha["tentativas"], linha["atualizado_em"],
          (linha["mensagem"] or "")[:300]] for linha in erros],
    )

    # --- auditoria ---
    auditoria = conn.execute(
        """
        SELECT t.id, t.job_id, t.numero, t.iniciada_em, t.finalizada_em,
               t.desfecho AS desfecho_tentativa, t.mensagem_portal,
               t.evidencia, t.worker,
               e.nome, e.documento, j.orgao, j.status, j.desfecho AS desfecho_final
          FROM tentativa t
          JOIN job j ON j.id = t.job_id
          JOIN empresa e ON e.id = j.empresa_id
         WHERE j.lote_id = ?
         ORDER BY t.id
        """,
        (lote_id,),
    ).fetchall()
    _escrever(
        livro.create_sheet("Auditoria"),
        ["Tentativa", "Job", "Empresa", "Documento", "Órgão", "Status final",
         "Desfecho final", "Nº tentativa", "Iniciada em", "Finalizada em",
         "Desfecho tentativa", "Worker", "Mensagem", "Evidência"],
        [[t["id"], t["job_id"], t["nome"], formatar(t["documento"]), t["orgao"],
          t["status"], t["desfecho_final"], t["numero"], t["iniciada_em"],
          t["finalizada_em"], t["desfecho_tentativa"], t["worker"],
          (t["mensagem_portal"] or "")[:500], t["evidencia"] or ""]
         for t in auditoria],
    )

    # --- resumo ---
    resumo_linhas: list[list] = [
        ["Lote", lote_id],
        ["Descrição", lote["descricao"]],
        ["Arquivo de origem", lote["arquivo_origem"]],
        ["Criado em", lote["criado_em"]],
        ["Relatório gerado em", tempo.agora_iso()],
        [],
        ["Órgão", "Total", "Concluídos", "Falhados", "Pendentes", "% concluído"],
    ]
    for orgao in consultas.orgaos_do_lote(conn, lote_id):
        r = consultas.resumo(conn, orgao, lote_id)
        resumo_linhas.append([orgao, r.total, r.concluidos, r.falhados,
                              r.pendentes, f"{r.percentual:.1f}%"])

    resumo_linhas.append([])
    resumo_linhas.append(["Desfecho", "Quantidade"])
    for linha in conn.execute(
        "SELECT desfecho, COUNT(*) AS n FROM job WHERE lote_id = ? AND desfecho IS NOT NULL "
        "GROUP BY desfecho ORDER BY n DESC", (lote_id,)
    ):
        resumo_linhas.append([
            consultas.ROTULOS.get(linha["desfecho"], linha["desfecho"]), linha["n"]
        ])

    planilha = livro.create_sheet("Resumo", 0)
    for linha in resumo_linhas:
        planilha.append(linha)
    planilha["A1"].font = Font(bold=True, size=13)
    planilha.column_dimensions["A"].width = 40
    planilha.column_dimensions["B"].width = 30

    destino = destino or (Path("data") / f"relatorio_lote_{lote_id}.xlsx")
    destino.parent.mkdir(parents=True, exist_ok=True)
    livro.save(destino)
    return destino


def gerar_bytes(conn: sqlite3.Connection, lote_id: int) -> bytes:
    """Mesma planilha, em memória — usado no download do painel."""
    import tempfile

    with tempfile.TemporaryDirectory() as pasta:
        caminho = gerar(conn, lote_id, Path(pasta) / "relatorio.xlsx")
        return caminho.read_bytes()


PASTAS_DO_ZIP = {
    "NEGATIVA": "CERTIDOES NEGATIVAS",
    "CPEN": "POSITIVAS COM EFEITO DE NEGATIVA",
}


def mes_corrente() -> str:
    """O mês de referência, no formato que o banco guarda ('2026-08')."""
    return tempo.agora_iso()[:7]


def zipar_pdfs(conn: sqlite3.Connection, mes: str | None = None,
               somente_negativas: bool = False,
               nomes: dict[str, str] | None = None,
               orgao: str | None = None) -> bytes:
    """Pacote com as certidões emitidas no mês, separadas por órgão.

    O corte é o MÊS, e não o lote, por dois motivos. O primeiro é a regra do
    negócio: a certidão vale 180 dias, mas quem a recebe exige emissão do
    mês corrente — é o mesmo critério que o robô usa para decidir se
    reemite. O segundo é prático: cada máquina numera os seus lotes por
    conta, então "lote 7" não quer dizer nada fora dela, e importar a
    planilha duas vezes no mesmo mês partiria a entrega em dois pacotes.

    Entram apenas NEGATIVA e CPEN — são os dois documentos que servem para
    entregar ao cliente. A CPEN (débito parcelado ou suspenso) vale como
    negativa na prática.

    Certidões POSITIVAS não entram: a empresa tem pendência real e o
    documento não é entregue — ela aparece no relatório para alguém tratar.
    Na verdade nem chegam aqui, porque o adapter não as guarda como
    certidão.

    Dentro do pacote, um nível por órgão e, dentro dele, um por tipo:

        RECEITA FEDERAL/CERTIDOES NEGATIVAS/...
        RECEITA FEDERAL/POSITIVAS COM EFEITO DE NEGATIVA/...
        SEFAZ GOIAS/CERTIDOES NEGATIVAS/...
        indice.csv
    """
    mes = mes or mes_corrente()
    nomes = nomes or {}
    filtro = "AND c.tipo = 'NEGATIVA'" if somente_negativas else ""
    # Um órgão de cada vez quando só ele fechou: no fim do mês entrega-se
    # tudo, mas a federal costuma terminar antes das estaduais, e não faz
    # sentido segurar a entrega dela esperando as outras.
    parametros: list = [mes]
    if orgao:
        filtro += " AND j.orgao = ?"
        parametros.append(orgao)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as pacote:
        indice = ["orgao;tipo;empresa;documento;emitida_em;valida_ate;arquivo"]

        for linha in conn.execute(
            f"""
            SELECT c.caminho_pdf, c.tipo, c.emitida_em, c.valida_ate,
                   j.orgao, e.documento, e.nome
              FROM certidao c
              JOIN job j ON j.id = c.job_id
              JOIN empresa e ON e.id = j.empresa_id
             WHERE strftime('%Y-%m', c.emitida_em) = ? {filtro}
             ORDER BY j.orgao, c.tipo, e.nome
            """,
            parametros,
        ):
            origem = Path(linha["caminho_pdf"])
            if not origem.exists():
                continue
            orgao = nomes.get(linha["orgao"], config.nome_do_orgao(linha["orgao"]))
            pasta = PASTAS_DO_ZIP.get(linha["tipo"], linha["tipo"])
            pacote.write(origem, arcname=f"{orgao}/{pasta}/{origem.name}")
            indice.append(";".join([
                orgao, linha["tipo"], linha["nome"], formatar(linha["documento"]),
                _data_curta(linha["emitida_em"]), _data_curta(linha["valida_ate"]),
                origem.name,
            ]))

        # Índice dentro do próprio pacote: quem recebe só o zip consegue
        # conferir o conteúdo sem abrir arquivo por arquivo.
        pacote.writestr("indice.csv", "\n".join(indice))

    return buffer.getvalue()


def meses_com_certidao(conn: sqlite3.Connection) -> list[str]:
    """Os meses que têm certidão guardada, do mais recente para trás."""
    return [linha["mes"] for linha in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', emitida_em) AS mes FROM certidao "
        "WHERE emitida_em IS NOT NULL ORDER BY mes DESC"
    )]
