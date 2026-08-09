"""Consultas de leitura usadas pelo painel e pelo relatório.

Ficam juntas porque são a mesma pergunta feita por dois canais: a tela
mostra ao vivo, o Excel congela no fim do lote. Uma fonte só evita que os
dois divirjam.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cnd.core import breaker, tempo
from cnd.core.modelos import Desfecho, Status

ROTULOS = {
    Desfecho.NEGATIVA: "Negativas",
    Desfecho.CPEN: "Positivas c/ efeito de negativa",
    Desfecho.POSITIVA: "Positivas (com pendência)",
    Desfecho.PENDENCIA_MANUAL: "Pendência manual",
    Desfecho.APROVEITADA: "Aproveitadas (já vigentes)",
}


@dataclass
class ResumoOrgao:
    orgao: str
    total: int
    concluidos: int
    pendentes: int
    em_execucao: int
    falhados: int
    por_desfecho: dict[str, int]
    breaker_estado: str
    breaker_motivo: str | None
    breaker_ate: str | None
    intervalo_s: float | None
    ritmo_por_hora: float | None
    taxa_captcha: float | None
    ultima_tentativa: str | None

    @property
    def percentual(self) -> float:
        return (self.concluidos / self.total * 100) if self.total else 0.0

    @property
    def restantes(self) -> int:
        return self.total - self.concluidos - self.falhados


def lotes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM job j WHERE j.lote_id = l.id) AS jobs
          FROM lote l
         ORDER BY l.id DESC
        """
    ).fetchall()


def orgaos_do_lote(conn: sqlite3.Connection, lote_id: int | None) -> list[str]:
    if lote_id is None:
        linhas = conn.execute("SELECT DISTINCT orgao FROM job ORDER BY orgao")
    else:
        linhas = conn.execute(
            "SELECT DISTINCT orgao FROM job WHERE lote_id = ? ORDER BY orgao", (lote_id,)
        )
    return [linha["orgao"] for linha in linhas]


def resumo(conn: sqlite3.Connection, orgao: str, lote_id: int | None = None) -> ResumoOrgao:
    filtro = "AND lote_id = ?" if lote_id else ""
    args: tuple = (orgao, lote_id) if lote_id else (orgao,)

    contagens = {
        linha["status"]: linha["n"]
        for linha in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM job WHERE orgao = ? {filtro} GROUP BY status",
            args,
        )
    }
    total = sum(contagens.values())

    por_desfecho = {
        linha["desfecho"]: linha["n"]
        for linha in conn.execute(
            f"""
            SELECT desfecho, COUNT(*) AS n FROM job
             WHERE orgao = ? {filtro} AND desfecho IS NOT NULL AND status = 'DONE'
             GROUP BY desfecho
            """,
            args,
        )
    }

    estado_breaker = breaker.consultar(conn, orgao)

    linha_ritmo = conn.execute(
        "SELECT intervalo_s FROM ritmo WHERE orgao = ?", (orgao,)
    ).fetchone()
    intervalo = linha_ritmo["intervalo_s"] if linha_ritmo else None

    # Ritmo real da última hora, e não o teórico.
    uma_hora_atras = tempo.daqui_a(-3600)
    linha = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN t.desfecho = 'CAPTCHA' THEN 1 ELSE 0 END) AS captchas,
               MAX(t.iniciada_em) AS ultima
          FROM tentativa t JOIN job j ON j.id = t.job_id
         WHERE j.orgao = ? AND t.iniciada_em >= ?
        """,
        (orgao, uma_hora_atras),
    ).fetchone()

    feitas = linha["n"] or 0
    captchas = linha["captchas"] or 0

    ultima = conn.execute(
        """
        SELECT MAX(t.iniciada_em) AS ultima FROM tentativa t
          JOIN job j ON j.id = t.job_id WHERE j.orgao = ?
        """,
        (orgao,),
    ).fetchone()["ultima"]

    return ResumoOrgao(
        orgao=orgao,
        total=total,
        concluidos=contagens.get(Status.DONE, 0),
        pendentes=contagens.get(Status.PENDING, 0) + contagens.get(Status.RETRY_WAIT, 0),
        em_execucao=contagens.get(Status.RUNNING, 0),
        falhados=contagens.get(Status.FAILED, 0),
        por_desfecho=por_desfecho,
        breaker_estado=estado_breaker.estado,
        breaker_motivo=estado_breaker.motivo,
        breaker_ate=estado_breaker.aberto_ate,
        intervalo_s=intervalo,
        ritmo_por_hora=float(feitas) if feitas else None,
        taxa_captcha=(captchas / feitas) if feitas else None,
        ultima_tentativa=ultima,
    )


def eta_horas(resumo_orgao: ResumoOrgao) -> float | None:
    """Previsão baseada no ritmo real da última hora."""
    if not resumo_orgao.ritmo_por_hora or resumo_orgao.restantes <= 0:
        return None
    return resumo_orgao.restantes / resumo_orgao.ritmo_por_hora


def meses_com_itens(conn: sqlite3.Connection) -> list[str]:
    """Os meses que têm item, do mais recente para trás.

    O trabalho é mensal: emite-se a carteira inteira uma vez por mês. Sem
    recorte de mês, a lista mistura agosto com julho e junho, e a pergunta
    real — "o que saiu neste mês?" — fica sem resposta.
    """
    return [linha["mes"] for linha in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', atualizado_em) AS mes FROM job "
        "WHERE atualizado_em IS NOT NULL ORDER BY mes DESC")]


def jobs(conn: sqlite3.Connection, lote_id: int | None = None, orgao: str | None = None,
         status: str | None = None, desfecho: str | None = None,
         busca: str | None = None, limite: int = 200,
         mes: str | None = None) -> list[sqlite3.Row]:
    condicoes, args = [], []
    if mes:
        condicoes.append("strftime('%Y-%m', j.atualizado_em) = ?")
        args.append(mes)
    # Os nomes de coluna são literais desta tupla — nunca vêm de fora. O
    # que vem do usuário é sempre o valor, e vai por parâmetro.
    for coluna, valor in (("j.lote_id", lote_id), ("j.orgao", orgao),
                          ("j.status", status), ("j.desfecho", desfecho)):
        if valor:
            condicoes.append(f"{coluna} = ?")
            args.append(valor)
    if busca:
        condicoes.append("(e.documento LIKE ? OR e.nome LIKE ?)")
        args.extend([f"%{busca}%", f"%{busca}%"])

    onde = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""
    args.append(limite)

    return conn.execute(
        f"""
        SELECT j.id, j.orgao, j.status, j.desfecho, j.tentativas,
               j.proxima_execucao_em, j.atualizado_em,
               e.documento, e.nome,
               c.caminho_pdf, c.valida_ate,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS ultima_mensagem
          FROM job j
          JOIN empresa e ON e.id = j.empresa_id
          LEFT JOIN certidao c ON c.job_id = j.id
          {onde}
         ORDER BY j.atualizado_em DESC
         LIMIT ?
        """,
        args,
    ).fetchall()


def tentativas_do_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tentativa WHERE job_id = ? ORDER BY numero", (job_id,)
    ).fetchall()


# Depois disso, uma tentativa sem fim gravado não está mais em curso: o
# processo morreu no meio. O robô cego leva ~25s por consulta, então cinco
# minutos é folga larga.
MINUTOS_EM_CURSO = 5


def pendentes(conn: sqlite3.Connection) -> int:
    """Itens esperando processamento, em qualquer órgão.

    É o que define a JANELA DE TRABALHO do sistema. O robô não é um serviço
    de pé o ano inteiro: é tarefa mensal, e ficar parado é o estado normal
    na maior parte do mês. Só faz sentido cobrar que ele esteja de pé quando
    existe algo para fazer — e é esta contagem que diz isso.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE status IN (?, ?)",
        (Status.PENDING, Status.RETRY_WAIT),
    ).fetchone()["n"]


def ultimas_tentativas(conn: sqlite3.Connection, limite: int = 8) -> list[dict]:
    """O que o robô fez por último, da mais recente para a mais antiga.

    É o "o que está acontecendo agora" da tela de máquinas. Ordena por id e
    não por data: `finalizada_em` é nulo enquanto a tentativa está em curso,
    e é justamente a que está em curso que interessa aparecer no topo.

    Nulo, porém, não basta para dizer "está consultando": quando o robô é
    encerrado no meio, a linha fica sem fim para sempre. `recuperar_orfaos`
    devolve o job à fila, mas não reescreve a tentativa. Daí a janela de
    tempo — sem ela, a tela mostraria uma consulta de uma hora atrás como se
    estivesse acontecendo agora, que é o pior tipo de informação: a que
    parece certa.
    """
    # O limiar vem do Python e não do strftime do SQLite: o carimbo gravado
    # tem milissegundos ('...:22.481Z') e o do strftime não ('...:22Z'), e a
    # comparação é de texto — o ponto vem antes do Z na tabela ASCII, então
    # tempos do mesmo segundo sairiam invertidos.
    limiar = tempo.daqui_a(-60 * MINUTOS_EM_CURSO)
    linhas = conn.execute(
        """
        SELECT t.id, t.desfecho, t.iniciada_em, t.finalizada_em,
               t.mensagem_portal, e.nome, e.documento, j.orgao,
               (t.finalizada_em IS NULL AND t.iniciada_em > ?) AS em_curso
          FROM tentativa t
          JOIN job j     ON j.id = t.job_id
          JOIN empresa e ON e.id = j.empresa_id
         ORDER BY t.id DESC
         LIMIT ?
        """,
        (limiar, limite),
    ).fetchall()
    return [{
        "nome": linha["nome"],
        "documento": linha["documento"],
        "orgao": linha["orgao"],
        "desfecho": linha["desfecho"],
        "quando": linha["finalizada_em"] or linha["iniciada_em"],
        "em_curso": bool(linha["em_curso"]),
        "interrompida": linha["finalizada_em"] is None and not linha["em_curso"],
    } for linha in linhas]


def captcha_por_hora(conn: sqlite3.Connection, orgao: str, dias: int = 7) -> list[dict]:
    """Alimenta a decisão sobre janela ativa e sobre a hipótese de IP (risco R4)."""
    desde = tempo.daqui_a(-dias * 86400)
    linhas = conn.execute(
        """
        SELECT substr(t.iniciada_em, 12, 2) AS hora,
               COUNT(*) AS total,
               SUM(CASE WHEN t.desfecho = 'CAPTCHA' THEN 1 ELSE 0 END) AS captchas
          FROM tentativa t JOIN job j ON j.id = t.job_id
         WHERE j.orgao = ? AND t.iniciada_em >= ?
         GROUP BY hora ORDER BY hora
        """,
        (orgao, desde),
    ).fetchall()
    return [
        {"hora": linha["hora"], "total": linha["total"], "captchas": linha["captchas"],
         "taxa": (linha["captchas"] / linha["total"]) if linha["total"] else 0.0}
        for linha in linhas
    ]
