"""A fila de trabalho e a máquina de estados do job.

A fila é a própria tabela `job`: não existe broker de mensagens.
Um worker "reivindica" o próximo job com uma transação exclusiva, o que
garante que dois workers nunca peguem o mesmo item — ver ADR-002.
"""
from __future__ import annotations

import sqlite3

from cnd.core import tempo
from cnd.core.modelos import (
    COM_PDF,
    CONCLUSIVOS,
    Desfecho,
    Documento,
    JobReivindicado,
    ResultadoTentativa,
    Status,
)

# --------------------------------------------------------------------------
# Tirar trabalho da fila
# --------------------------------------------------------------------------

def reivindicar(conn: sqlite3.Connection, orgao: str) -> JobReivindicado | None:
    """Pega o próximo job disponível do órgão e marca como RUNNING.

    Vale para item novo (`PENDING`) e retry cuja espera já venceu
    (`RETRY_WAIT`). Devolve None se não houver nada disponível agora.

    O BEGIN IMMEDIATE trava a escrita já na abertura da transação: é isso
    que impede dois workers de selecionarem a mesma linha antes de qualquer
    um marcar RUNNING.
    """
    agora = tempo.agora_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        linha = conn.execute(
            """
            SELECT j.id, j.lote_id, j.tentativas, j.empresa_id,
                   e.documento, e.tipo_documento, e.nome
              FROM job j
              JOIN empresa e ON e.id = j.empresa_id
             WHERE j.orgao = ?
               AND j.status IN (?, ?)
               AND j.proxima_execucao_em <= ?
             ORDER BY j.proxima_execucao_em, j.id
             LIMIT 1
            """,
            (orgao, Status.PENDING, Status.RETRY_WAIT, agora),
        ).fetchone()

        if linha is None:
            conn.execute("COMMIT")
            return None

        conn.execute(
            "UPDATE job SET status = ?, atualizado_em = ? WHERE id = ?",
            (Status.RUNNING, agora, linha["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return JobReivindicado(
        job_id=linha["id"],
        lote_id=linha["lote_id"],
        orgao=orgao,
        tentativas=linha["tentativas"],
        doc=Documento(
            empresa_id=linha["empresa_id"],
            documento=linha["documento"],
            tipo=linha["tipo_documento"],
            nome=linha["nome"],
            lote_id=linha["lote_id"],
        ),
    )


def ha_trabalho(conn: sqlite3.Connection, orgao: str) -> bool:
    """Existe algo pendente ou aguardando retry para este órgão?"""
    linha = conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE orgao = ? AND status IN (?, ?, ?)",
        (orgao, Status.PENDING, Status.RUNNING, Status.RETRY_WAIT),
    ).fetchone()
    return linha["n"] > 0


# --------------------------------------------------------------------------
# Registrar o que aconteceu
# --------------------------------------------------------------------------

def abrir_tentativa(conn: sqlite3.Connection, job: JobReivindicado, worker: int) -> int:
    """Cria a linha de tentativa e devolve o id dela."""
    cursor = conn.execute(
        "INSERT INTO tentativa (job_id, numero, iniciada_em, worker) VALUES (?, ?, ?, ?)",
        (job.job_id, job.tentativas + 1, tempo.agora_iso(), worker),
    )
    return int(cursor.lastrowid)


def fechar_tentativa(conn: sqlite3.Connection, tentativa_id: int,
                     resultado: ResultadoTentativa) -> None:
    conn.execute(
        """
        UPDATE tentativa
           SET finalizada_em = ?, desfecho = ?, mensagem_portal = ?, evidencia = ?
         WHERE id = ?
        """,
        (
            tempo.agora_iso(),
            str(resultado.desfecho),
            resultado.mensagem_portal,
            str(resultado.evidencia) if resultado.evidencia else None,
            tentativa_id,
        ),
    )


def concluir(conn: sqlite3.Connection, job: JobReivindicado,
             resultado: ResultadoTentativa) -> None:
    """Fecha o job com um desfecho definitivo e guarda a certidão, se houver."""
    if resultado.desfecho not in CONCLUSIVOS:
        raise ValueError(f"{resultado.desfecho} não é um desfecho conclusivo")

    agora = tempo.agora_iso()
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            UPDATE job
               SET status = ?, desfecho = ?, tentativas = tentativas + 1, atualizado_em = ?
             WHERE id = ?
            """,
            (Status.DONE, str(resultado.desfecho), agora, job.job_id),
        )

        if resultado.desfecho in COM_PDF and resultado.caminho_pdf is not None:
            from cnd.infra.arquivos import hash_arquivo

            conn.execute(
                """
                INSERT INTO certidao
                    (job_id, tipo, emitida_em, valida_ate, codigo_controle, caminho_pdf, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    str(resultado.desfecho),
                    agora,
                    resultado.validade.isoformat() if resultado.validade else None,
                    resultado.codigo_controle,
                    str(resultado.caminho_pdf),
                    hash_arquivo(resultado.caminho_pdf),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reagendar(conn: sqlite3.Connection, job: JobReivindicado,
              desfecho: Desfecho, espera_s: float) -> None:
    """Devolve o job para a fila, para nova tentativa daqui a `espera_s`."""
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = NULL, tentativas = tentativas + 1,
               proxima_execucao_em = ?, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.RETRY_WAIT, tempo.daqui_a(espera_s), tempo.agora_iso(), job.job_id),
    )


def devolver(conn: sqlite3.Connection, job: JobReivindicado) -> None:
    """Devolve um job reivindicado sem contar tentativa.

    Usado quando o worker para antes de abrir a tentativa de verdade. Parada
    limpa não pode aparecer como erro técnico nem consumir uma das três chances
    do item.
    """
    agora = tempo.agora_iso()
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = NULL, proxima_execucao_em = ?, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.PENDING, agora, agora, job.job_id),
    )


def falhar(conn: sqlite3.Connection, job: JobReivindicado, desfecho: Desfecho) -> None:
    """Esgotou as tentativas. Terminal para o sistema, reenfileirável pelo painel."""
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = ?, tentativas = tentativas + 1, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.FAILED, str(desfecho), tempo.agora_iso(), job.job_id),
    )


# --------------------------------------------------------------------------
# Manutenção
# --------------------------------------------------------------------------

def recuperar_orfaos(conn: sqlite3.Connection) -> int:
    """Jobs que ficaram RUNNING por causa de um crash voltam para a fila.

    Chamado na subida do orquestrador (RNF-08). Como o orquestrador é o
    único que marca RUNNING, qualquer RUNNING encontrado na subida é órfão.
    """
    cursor = conn.execute(
        "UPDATE job SET status = ?, atualizado_em = ? WHERE status = ?",
        (Status.PENDING, tempo.agora_iso(), Status.RUNNING),
    )
    return cursor.rowcount


def reenfileirar_falhados(
    conn: sqlite3.Connection, orgao: str | None = None,
    lote_id: int | None = None,
) -> int:
    """Devolve jobs FAILED para a fila, zerando o contador de tentativas.
    Usado pelo painel depois que a causa da falha foi corrigida."""
    agora = tempo.agora_iso()
    condicoes = ["status = ?"]
    args: list = [Status.FAILED]
    if orgao:
        condicoes.append("orgao = ?")
        args.append(orgao)
    if lote_id:
        condicoes.append("lote_id = ?")
        args.append(lote_id)

    where = " AND ".join(condicoes)
    cursor = conn.execute(
        f"""
        UPDATE job SET status = ?, desfecho = NULL, tentativas = 0,
                       proxima_execucao_em = ?, atualizado_em = ?
         WHERE {where}
        """,
        (Status.PENDING, agora, agora, *args),
    )
    return cursor.rowcount


def certidao_do_mes(conn: sqlite3.Connection, empresa_id: int, orgao: str) -> sqlite3.Row | None:
    """Certidão desta empresa/órgão emitida no MÊS CORRENTE (RNF-04).

    O critério é a data de emissão, não a validade — e a diferença importa.
    A certidão da Receita vale 180 dias, mas quem recebe (bancos, licitações,
    tomadores) exige emissão do mês corrente. Reaproveitar uma certidão ainda
    válida porém do mês passado entregaria um documento que seria recusado,
    e o job apareceria como concluído: erro silencioso, o pior tipo.

    Vale, portanto, só dentro do mesmo mês: em 03/09 aproveita a de 01/09,
    mas não a de 31/08.
    """
    return conn.execute(
        """
        SELECT c.*
          FROM certidao c
          JOIN job j ON j.id = c.job_id
         WHERE j.empresa_id = ?
           AND j.orgao = ?
           AND strftime('%Y-%m', c.emitida_em) = strftime('%Y-%m', 'now')
         ORDER BY c.emitida_em DESC
         LIMIT 1
        """,
        (empresa_id, orgao),
    ).fetchone()
