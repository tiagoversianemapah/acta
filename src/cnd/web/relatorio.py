"""Relatório final do lote em Excel — ver docs/05, seção 2.

Quatro abas, cada uma respondendo uma pergunta da operação: quanto saiu
(Resumo), o que eu entrego ao cliente (Certidões), quem precisa de
tratamento (Pendências) e o que não terminou (Erros).

Antes era uma aba por desfecho — nove no total, quatro delas com colunas
idênticas e só o desfecho mudando. Quem abria o arquivo tinha de somar
guias na mão para saber quantas certidões tinha em mãos, e ainda passava
pela Auditoria (uma linha por tentativa do robô), que é diagnóstico
técnico e não relatório. O desfecho não sumiu: virou coluna, que o Excel
filtra melhor do que uma guia separada.
"""
from __future__ import annotations

import csv
import sqlite3
import zipfile
from dataclasses import dataclass
from io import BytesIO, StringIO
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


@dataclass(frozen=True)
class Recorte:
    """O pedaço do trabalho que vai para a planilha.

    Mês e órgão, não lote — o mesmo corte da tela e do pacote de certidões.
    Exportar por lote entregava outra coisa que o operador via na frente
    dele: ele filtra "Receita Federal", pede a planilha, e recebia tudo.
    """

    mes: str
    orgao: str | None = None

    @property
    def onde(self) -> str:
        clausula = "strftime('%Y-%m', j.atualizado_em) = ?"
        return clausula + (" AND j.orgao = ?" if self.orgao else "")

    @property
    def valores(self) -> list:
        return [self.mes, self.orgao] if self.orgao else [self.mes]

    @property
    def descricao(self) -> str:
        return self.mes + (f" · {self.orgao}" if self.orgao else "")


# Rótulo de cada desfecho dentro da planilha. Com o desfecho virando
# coluna, ele passa a ser lido linha a linha — e "PENDENCIA_MANUAL" numa
# célula não diz nada a quem confere. Cobre também as falhas, que antes
# saíam como código cru na aba de erros e no resumo.
ROTULO = {
    Desfecho.NEGATIVA: "Negativa",
    Desfecho.CPEN: "Positiva c/ efeito de negativa",
    Desfecho.APROVEITADA: "Já emitida no mês",
    Desfecho.POSITIVA: "Positiva (com pendência)",
    Desfecho.PENDENCIA_MANUAL: "Informações insuficientes",
    Desfecho.INAPTA: "CNPJ inapto (omissão de declarações)",
    Desfecho.CAPTCHA: "Exigiu captcha",
    Desfecho.BLOQUEIO_TEMPORARIO: "Portal recusou",
    Desfecho.RESULTADO_PENDENTE: "Resultado pendente no portal",
    Desfecho.ERRO_TECNICO: "Erro técnico",
}

# A ordem em que as linhas aparecem dentro da aba. Não é a alfabética dos
# códigos: é a da conversa com o cliente — primeiro o que está resolvido,
# depois o que exige providência dele, por gravidade.
COM_CERTIDAO = (Desfecho.NEGATIVA, Desfecho.CPEN, Desfecho.APROVEITADA)
A_TRATAR = (Desfecho.POSITIVA, Desfecho.PENDENCIA_MANUAL, Desfecho.INAPTA)


def _por_desfecho(conn: sqlite3.Connection, recorte: Recorte,
                  desfechos: tuple[Desfecho, ...]) -> list[sqlite3.Row]:
    """Os jobs que terminaram em qualquer um desses desfechos.

    A ordem das linhas segue a ordem dos desfechos pedidos, e não a
    alfabética do código: dentro da aba, quem lê espera encontrar os
    grupos na sequência em que eles aparecem no cabeçalho.
    """
    marcadores = ", ".join("?" * len(desfechos))
    ordem = " ".join(f"WHEN ? THEN {i}" for i, _ in enumerate(desfechos))
    codigos = [str(d) for d in desfechos]
    return conn.execute(
        f"""
        SELECT e.nome, e.documento, j.orgao, j.desfecho, j.atualizado_em,
               c.emitida_em, c.valida_ate, c.codigo_controle, c.caminho_pdf,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS mensagem
          FROM job j
          JOIN empresa e ON e.id = j.empresa_id
          LEFT JOIN certidao c ON c.job_id = j.id
         WHERE {recorte.onde} AND j.desfecho IN ({marcadores})
         ORDER BY CASE j.desfecho {ordem} END, e.nome
        """,
        [*recorte.valores, *codigos, *codigos],
    ).fetchall()


def gerar(conn: sqlite3.Connection, recorte: Recorte,
          destino: Path | None = None) -> Path:

    livro = Workbook()
    livro.remove(livro.active)

    # --- o que se entrega ao cliente ---
    # As três valem como certidão em mãos: negativa, positiva com efeito
    # de negativa (débito parcelado ou suspenso) e a que já havia sido
    # emitida no mês. Juntas numa aba só porque o destino delas é o mesmo
    # — o pacote de PDFs — e porque "quantas certidões eu tenho?" deixa de
    # exigir soma de guias.
    _escrever(
        livro.create_sheet("Certidões"),
        ["Empresa", "Documento", "Órgão", "Tipo", "Emitida em", "Válida até",
         "Código de controle", "Arquivo PDF"],
        [[linha["nome"], formatar(linha["documento"]), linha["orgao"],
          ROTULO.get(linha["desfecho"], linha["desfecho"]),
          _data_curta(linha["emitida_em"]), _data_curta(linha["valida_ate"]),
          linha["codigo_controle"],
          Path(linha["caminho_pdf"]).name if linha["caminho_pdf"] else ""]
         for linha in _por_desfecho(conn, recorte, COM_CERTIDAO)],
    )

    # --- o que precisa de providência ---
    # Nenhuma gerou PDF, e em todas a bola está com o cliente: pagar o
    # débito, procurar o e-CAC ou entregar as declarações atrasadas. O que
    # o escritório faz em cada caso muda, e é isso que a coluna Situação
    # diz — sem ela seriam três abas com o mesmo cabeçalho.
    _escrever(
        livro.create_sheet("Pendências"),
        ["Empresa", "Documento", "Órgão", "Situação", "Consultado em",
         "Mensagem do portal"],
        [[linha["nome"], formatar(linha["documento"]), linha["orgao"],
          ROTULO.get(linha["desfecho"], linha["desfecho"]),
          _data_curta(linha["atualizado_em"]), (linha["mensagem"] or "")[:300]]
         for linha in _por_desfecho(conn, recorte, A_TRATAR)],
    )

    # --- erros ---
    erros = conn.execute(
        f"""
        SELECT e.nome, e.documento, j.orgao, j.desfecho, j.tentativas, j.atualizado_em,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS mensagem
          FROM job j JOIN empresa e ON e.id = j.empresa_id
         WHERE {recorte.onde} AND j.status = ?
         ORDER BY e.nome
        """,
        [*recorte.valores, Status.FAILED],
    ).fetchall()
    _escrever(
        livro.create_sheet("Erros"),
        ["Empresa", "Documento", "Órgão", "Última falha", "Tentativas",
         "Quando", "Mensagem"],
        [[linha["nome"], formatar(linha["documento"]), linha["orgao"],
          ROTULO.get(linha["desfecho"], linha["desfecho"]),
          linha["tentativas"], _data_curta(linha["atualizado_em"]),
          (linha["mensagem"] or "")[:300]] for linha in erros],
    )

    # --- resumo ---
    resumo_linhas: list[list] = [
        ["Recorte", recorte.descricao],
        ["Mês de referência", recorte.mes],
        ["Órgão", recorte.orgao or "todos"],
        ["Relatório gerado em", tempo.agora_iso()],
        [],
        ["Órgão", "Total", "Concluídos", "Falhados", "Pendentes", "% concluído"],
    ]
    linha_dos_orgaos = len(resumo_linhas)      # 1-indexado, como no Excel
    # O MESMO recorte das abas de cima. Sem o mês aqui, a planilha de agosto
    # trazia as abas com agosto e a linha de total com o ano inteiro — e
    # listava órgão que não trabalhou no mês, com números de outro.
    for orgao in ([recorte.orgao] if recorte.orgao
                  else consultas.orgaos_do_lote(conn, None, recorte.mes)):
        r = consultas.resumo(conn, orgao, None, recorte.mes)
        resumo_linhas.append([orgao, r.total, r.concluidos, r.falhados,
                              r.pendentes, f"{r.percentual:.1f}%"])

    resumo_linhas.append([])
    resumo_linhas.append(["Desfecho", "Quantidade"])
    linha_dos_desfechos = len(resumo_linhas)   # 1-indexado, como no Excel
    for linha in conn.execute(
        f"SELECT j.desfecho AS desfecho, COUNT(*) AS n FROM job j "
        f"WHERE {recorte.onde} AND j.desfecho IS NOT NULL "
        f"GROUP BY j.desfecho ORDER BY n DESC", recorte.valores
    ):
        resumo_linhas.append([
            ROTULO.get(linha["desfecho"], linha["desfecho"]), linha["n"]
        ])

    planilha = livro.create_sheet("Resumo", 0)
    for linha in resumo_linhas:
        planilha.append(linha)

    # As outras abas passam por _escrever, que formata. O Resumo era
    # montado a mão e saía torto: cabeçalhos sem destaque, colunas
    # estreitas cortando "% concluído" e números encostados no texto.
    planilha["A1"].font = Font(bold=True, size=14)
    for celula in planilha["A"]:
        if celula.value and not isinstance(celula.value, (int, float)):
            celula.font = Font(bold=True)
    # As duas linhas de cabeçalho de tabela ganham fundo e texto claro. As
    # DUAS são contadas na hora em que são escritas, e nenhuma é chutada:
    # o número fixo 7 apontava para a primeira linha de ÓRGÃO — a tabela
    # começa na 6 —, então o cabeçalho ficava sem destaque e a Receita
    # Federal aparecia pintada de azul escuro como se fosse título.
    for numero_da_linha in (linha_dos_orgaos, linha_dos_desfechos):
        for celula in planilha[numero_da_linha]:
            if celula.value:
                celula.font = CABECALHO
                celula.fill = FUNDO
                celula.alignment = Alignment(horizontal="center")

    for coluna, largura in zip("ABCDEF", (34, 30, 14, 14, 14, 14),
                               strict=False):
        planilha.column_dimensions[coluna].width = largura
    for linha_de_dados in planilha.iter_rows(min_row=linha_dos_orgaos + 1,
                                             min_col=3):
        for celula in linha_de_dados:
            celula.alignment = Alignment(horizontal="center")
    planilha.freeze_panes = "A2"

    destino = destino or (Path("data") / f"relatorio_{recorte.mes}.xlsx")
    destino.parent.mkdir(parents=True, exist_ok=True)
    livro.save(destino)
    return destino


def gerar_bytes(conn: sqlite3.Connection, recorte: Recorte) -> bytes:
    """Mesma planilha, em memória — usado no download do painel."""
    import tempfile

    with tempfile.TemporaryDirectory() as pasta:
        caminho = gerar(conn, recorte, Path(pasta) / "relatorio.xlsx")
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
    indice_csv = StringIO()
    indice = csv.writer(indice_csv, delimiter=";", lineterminator="\n")
    indice.writerow(["orgao", "tipo", "empresa", "documento", "emitida_em",
                     "valida_ate", "arquivo"])

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as pacote:
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
            rotulo_orgao = nomes.get(linha["orgao"], config.nome_do_orgao(linha["orgao"]))
            pasta = PASTAS_DO_ZIP.get(linha["tipo"], linha["tipo"])
            pacote.write(origem, arcname=f"{rotulo_orgao}/{pasta}/{origem.name}")
            indice.writerow([
                rotulo_orgao, linha["tipo"], linha["nome"], formatar(linha["documento"]),
                _data_curta(linha["emitida_em"]), _data_curta(linha["valida_ate"]),
                origem.name,
            ])

        # Índice dentro do próprio pacote: quem recebe só o zip consegue
        # conferir o conteúdo sem abrir arquivo por arquivo.
        pacote.writestr("indice.csv", indice_csv.getvalue())

    return buffer.getvalue()


def meses_com_certidao(conn: sqlite3.Connection) -> list[str]:
    """Os meses que têm certidão guardada, do mais recente para trás."""
    return [linha["mes"] for linha in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', emitida_em) AS mes FROM certidao "
        "WHERE emitida_em IS NOT NULL ORDER BY mes DESC"
    )]
