"""Consultas de leitura usadas pelo painel e pelo relatório.

Ficam juntas porque são a mesma pergunta feita por dois canais: a tela
mostra ao vivo, o Excel congela no fim do lote. Uma fonte só evita que os
dois divirjam.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import ceil

from cnd.core import breaker, tempo
from cnd.core.documentos import limpar
from cnd.core.modelos import Desfecho, Status

ROTULOS = {
    Desfecho.NEGATIVA: "Negativas",
    Desfecho.CPEN: "Positivas c/ efeito de negativa",
    Desfecho.POSITIVA: "Positivas (com pendência)",
    Desfecho.PENDENCIA_MANUAL: "Informações insuficientes",
    Desfecho.APROVEITADA: "Aproveitadas (já vigentes)",
    Desfecho.RESULTADO_PENDENTE: "Resultado pendente",
}

ERROS_DIAGNOSTICO = frozenset({
    Desfecho.CAPTCHA,
    Desfecho.BLOQUEIO_TEMPORARIO,
    Desfecho.ERRO_TECNICO,
})


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
    breaker_aberturas: int
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
        breaker_aberturas=estado_breaker.aberturas,
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


def _filtro_jobs(
    lote_id: int | None = None,
    orgao: str | None = None,
    status: str | None = None,
    desfecho: str | None = None,
    busca: str | None = None,
    mes: str | None = None,
    job_id: int | None = None,
) -> tuple[str, list]:
    condicoes, args = [], []
    if job_id:
        condicoes.append("j.id = ?")
        args.append(job_id)
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
        busca_limpa = limpar(busca)
        condicoes.append("(e.documento LIKE ? OR e.documento LIKE ? OR e.nome LIKE ?)")
        args.extend([f"%{busca}%", f"%{busca_limpa}%", f"%{busca}%"])

    onde = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""
    return onde, args


def contar_jobs(
    conn: sqlite3.Connection,
    lote_id: int | None = None,
    orgao: str | None = None,
    status: str | None = None,
    desfecho: str | None = None,
    busca: str | None = None,
    mes: str | None = None,
    job_id: int | None = None,
) -> int:
    onde, args = _filtro_jobs(lote_id, orgao, status, desfecho, busca, mes, job_id)
    return conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM job j
          JOIN empresa e ON e.id = j.empresa_id
          {onde}
        """,
        args,
    ).fetchone()["n"]


def jobs(conn: sqlite3.Connection, lote_id: int | None = None, orgao: str | None = None,
         status: str | None = None, desfecho: str | None = None,
         busca: str | None = None, limite: int = 200,
         mes: str | None = None, job_id: int | None = None,
         offset: int = 0) -> list[sqlite3.Row]:
    onde, args = _filtro_jobs(lote_id, orgao, status, desfecho, busca, mes, job_id)
    args.append(limite)
    args.append(offset)

    return conn.execute(
        f"""
        SELECT j.id, j.lote_id, j.orgao, j.status, j.desfecho, j.tentativas,
               j.proxima_execucao_em, j.atualizado_em,
               e.documento, e.nome,
               c.tipo, c.emitida_em, c.valida_ate, c.codigo_controle, c.caminho_pdf,
               (SELECT t.mensagem_portal FROM tentativa t
                 WHERE t.job_id = j.id ORDER BY t.id DESC LIMIT 1) AS ultima_mensagem
          FROM job j
          JOIN empresa e ON e.id = j.empresa_id
          LEFT JOIN certidao c ON c.job_id = j.id
          {onde}
         ORDER BY j.atualizado_em DESC
         LIMIT ? OFFSET ?
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


def _hora_local(valor: str | None) -> str:
    if not valor:
        return ""
    try:
        return tempo.de_iso(valor).astimezone().strftime("%H:%M:%S")
    except Exception:
        texto = str(valor)
        return texto[11:19] if len(texto) >= 19 else texto


def rotulo_duracao(segundos: int | float | None) -> str:
    if segundos is None:
        return ""
    minutos = max(1, round(float(segundos) / 60))
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if resto == 0:
        return f"{horas} h"
    return f"{horas} h {resto} min"


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
    eventos = []
    for linha in linhas:
        quando = linha["finalizada_em"] or linha["iniciada_em"]
        eventos.append({
        "nome": linha["nome"],
        "documento": linha["documento"],
        "orgao": linha["orgao"],
        "desfecho": linha["desfecho"],
        "mensagem_portal": linha["mensagem_portal"],
        "quando": quando,
        "hora": _hora_local(quando),
        "em_curso": bool(linha["em_curso"]),
        "interrompida": linha["finalizada_em"] is None and not linha["em_curso"],
        })
    return eventos


def _captcha_por_hora_sql_antigo(
    conn: sqlite3.Connection, orgao: str, dias: int = 7
) -> list[dict]:
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


def captcha_por_hora(conn: sqlite3.Connection, orgao: str, dias: int = 7) -> list[dict]:
    """Alimenta a decisao sobre janela ativa e sobre a hipotese de IP.

    A decisao de horario nao depende de "informacoes insuficientes": isso e
    resposta de negocio da empresa. Para horario, o que importa e o portal
    barrando a automacao, entao a taxa junta CAPTCHA e BLOQUEIO_TEMPORARIO.
    """
    desde = tempo.daqui_a(-dias * 86400)
    linhas = conn.execute(
        """
        SELECT t.iniciada_em, t.desfecho
          FROM tentativa t JOIN job j ON j.id = t.job_id
         WHERE j.orgao = ? AND t.iniciada_em >= ?
         ORDER BY t.iniciada_em
        """,
        (orgao, desde),
    ).fetchall()

    por_hora = defaultdict(lambda: {
        "total": 0, "captchas": 0, "bloqueios": 0, "recusas": 0,
        "insuficientes": 0, "erros": 0,
    })
    for linha in linhas:
        try:
            hora = tempo.de_iso(linha["iniciada_em"]).astimezone().strftime("%H")
        except Exception:
            hora = str(linha["iniciada_em"] or "")[11:13] or "??"

        balde = por_hora[hora]
        balde["total"] += 1
        desfecho = linha["desfecho"]
        if desfecho == Desfecho.CAPTCHA:
            balde["captchas"] += 1
            balde["recusas"] += 1
        elif desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
            balde["bloqueios"] += 1
            balde["recusas"] += 1
        elif desfecho == Desfecho.PENDENCIA_MANUAL:
            balde["insuficientes"] += 1
        elif desfecho == Desfecho.ERRO_TECNICO:
            balde["erros"] += 1

    return [
        {
            "hora": hora,
            **valores,
            "taxa": (
                valores["recusas"] / valores["total"] if valores["total"] else 0.0
            ),
        }
        for hora, valores in sorted(por_hora.items())
    ]


def _rotulo_erro(desfecho: str | None, mensagem: str | None) -> str:
    texto = " ".join(str(mensagem or "").split())
    minusculo = texto.lower()
    if "não veio pdf" in minusculo or "nao veio pdf" in minusculo:
        return "Não veio PDF / faixa de aviso"
    if "faixa de aviso" in minusculo:
        return "Não veio PDF / faixa de aviso"
    if desfecho == Desfecho.CAPTCHA:
        return "Captcha não resolvido"
    if desfecho == Desfecho.BLOQUEIO_TEMPORARIO:
        return "Bloqueio temporário"
    if desfecho == Desfecho.ERRO_TECNICO:
        return texto[:80] if texto else "Erro técnico"
    return texto[:80] if texto else "Sem mensagem"


def principais_erros(
    conn: sqlite3.Connection, orgao: str, dias: int = 7, limite: int = 5
) -> list[dict]:
    """Erros operacionais mais frequentes, sem contar resultado de negócio."""
    desde = tempo.daqui_a(-dias * 86400)
    linhas = conn.execute(
        """
        SELECT t.desfecho, t.mensagem_portal
          FROM tentativa t JOIN job j ON j.id = t.job_id
         WHERE j.orgao = ? AND t.iniciada_em >= ?
           AND t.desfecho IN ('CAPTCHA', 'BLOQUEIO_TEMPORARIO', 'ERRO_TECNICO')
        """,
        (orgao, desde),
    ).fetchall()

    contagem = Counter(
        _rotulo_erro(linha["desfecho"], linha["mensagem_portal"])
        for linha in linhas
    )
    return [
        {"erro": erro, "quantidade": quantidade}
        for erro, quantidade in contagem.most_common(limite)
    ]


def _eixo_de_erros(maior_valor: int) -> tuple[int, list[int]]:
    """Topo e marcações do eixo Y para o gráfico de erros por horário."""
    passo = max(1, ceil(max(5, maior_valor) / 5))
    topo = passo * 5
    return topo, [topo - passo * indice for indice in range(6)]


def diagnostico_orgao(
    conn: sqlite3.Connection, orgao: str, dias: int = 7
) -> dict:
    """Pacote pronto para a aba Diagnóstico.

    "Informações insuficientes" fica separado de erro: é uma resposta
    conclusiva da empresa, não falha do robô nem bloqueio do portal.
    """
    horas = captcha_por_hora(conn, orgao, dias)
    por_hora = {linha["hora"]: dict(linha) for linha in horas}
    for linha in por_hora.values():
        linha["erros_operacionais"] = (
            int(linha.get("captchas") or 0)
            + int(linha.get("bloqueios") or 0)
            + int(linha.get("erros") or 0)
        )
        total_linha = int(linha.get("total") or 0)
        linha["taxa_erro"] = (
            linha["erros_operacionais"] / total_linha if total_linha else 0.0
        )

    total = sum(int(h.get("total") or 0) for h in por_hora.values())
    erros = sum(int(h.get("erros_operacionais") or 0) for h in por_hora.values())
    bloqueios = sum(int(h.get("bloqueios") or 0) for h in por_hora.values())
    captchas = sum(int(h.get("captchas") or 0) for h in por_hora.values())
    tecnicos = sum(int(h.get("erros") or 0) for h in por_hora.values())
    insuficientes = sum(int(h.get("insuficientes") or 0) for h in por_hora.values())
    taxa_erro = erros / total if total else 0.0

    maior_barra = max(
        [1, *(int(h.get("erros_operacionais") or 0) for h in por_hora.values())]
    )
    topo_eixo, eixo = _eixo_de_erros(maior_barra)
    serie = []
    for hora in range(24):
        chave = f"{hora:02d}"
        linha = por_hora.get(chave, {
            "hora": chave, "total": 0, "captchas": 0, "bloqueios": 0,
            "recusas": 0, "insuficientes": 0, "erros": 0,
            "erros_operacionais": 0, "taxa_erro": 0.0,
        })
        erros_hora = int(linha.get("erros_operacionais") or 0)
        serie.append({
            **linha,
            "altura": round(erros_hora / topo_eixo * 100, 1) if erros_hora else 0,
            "marcar": hora % 2 == 0,
        })

    resumo_horario = sorted(
        (h for h in por_hora.values() if int(h.get("total") or 0) > 0),
        key=lambda h: h["hora"],
    )
    pior_hora = max(
        resumo_horario,
        key=lambda h: (h.get("taxa_erro", 0.0), h.get("erros_operacionais", 0)),
        default=None,
    )
    melhores = [
        {
            "hora": h["hora"],
            "total": h["total"],
            "taxa": h.get("taxa_erro", 0.0),
        }
        for h in sorted(
            [h for h in resumo_horario if int(h.get("total") or 0) >= 3],
            key=lambda h: (h.get("taxa_erro", 1.0), -h.get("total", 0), h["hora"]),
        )[:3]
    ]

    return {
        "orgao": orgao,
        "consultas": total,
        "erros": erros,
        "taxa_erro": taxa_erro,
        "bloqueios": bloqueios,
        "captchas": captchas,
        "tecnicos": tecnicos,
        "insuficientes": insuficientes,
        "serie": serie,
        "eixo": eixo,
        "horas": resumo_horario,
        "pior_hora": pior_hora,
        "melhores": melhores,
        "principais_erros": principais_erros(conn, orgao, dias),
    }
