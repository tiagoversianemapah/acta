"""Vigilância do lote: o que precisa virar e-mail.

O robô roda numa máquina que ninguém olha. O painel serve para quem quer
acompanhar; o e-mail é para quem NÃO está olhando e precisa ser puxado.

Três perguntas são feitas periodicamente:

  1. O lote acabou?            -> avisa uma vez, com o resumo
  2. Parou de andar?           -> há trabalho na fila mas nada conclui
  3. Itens desistiram de vez?  -> esgotaram as tentativas

A terceira é resumida, não item a item: 200 e-mails de "falhou" não são
aviso, são ruído — e ruído demais faz a caixa de entrada ser ignorada
justamente no dia em que o aviso importava.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cnd.core import tempo
from cnd.core.modelos import Status

# Sem nada concluído por este tempo, com fila cheia, é sinal de travamento.
MINUTOS_SEM_PROGRESSO = 30

# Nome interno -> como se diz para uma pessoa.
ROTULOS = {
    "NEGATIVA": "Certidões negativas",
    "CPEN": "Com efeito de negativa",
    "POSITIVA": "Positivas (com pendência)",
    "PENDENCIA_MANUAL": "Exigem atendimento",
    "INAPTA": "CNPJ inapto (omissão de declarações)",
    "APROVEITADA": "Já emitidas no mês",
    "BLOQUEIO_TEMPORARIO": "Recusadas pelo portal",
    "RESULTADO_PENDENTE": "Resultado pendente no portal",
    "CAPTCHA": "Bloqueadas por captcha",
    "ERRO_TECNICO": "Erros técnicos",
}


@dataclass(frozen=True)
class ResumoLote:
    lote_id: int
    descricao: str
    total: int
    por_desfecho: dict[str, int]
    falhados: int
    criado_em: str = ""
    encerrado_em: str = ""

    @property
    def com_certidao(self) -> int:
        """Quantas geraram PDF — negativa ou com efeito de negativa."""
        return (self.por_desfecho.get("NEGATIVA", 0)
                + self.por_desfecho.get("CPEN", 0)
                + self.por_desfecho.get("APROVEITADA", 0))

    @property
    def sem_certidao(self) -> int:
        """Empresas que precisam de tratamento: pendência ou atendimento."""
        return (self.por_desfecho.get("POSITIVA", 0)
                + self.por_desfecho.get("PENDENCIA_MANUAL", 0)
                + self.por_desfecho.get("INAPTA", 0))

    @property
    def duracao(self) -> str:
        if not (self.criado_em and self.encerrado_em):
            return "-"
        segundos = (tempo.de_iso(self.encerrado_em)
                    - tempo.de_iso(self.criado_em)).total_seconds()
        horas, resto = divmod(int(segundos), 3600)
        return f"{horas}h{resto // 60:02d}min" if horas else f"{resto // 60}min"

    def como_campos(self) -> dict[str, str]:
        """Resumo em campos, para virar tabela no aviso.

        Ordem pensada para quem lê: primeiro o que interessa (quantas
        certidões saíram), depois o que exige trabalho, e só então o
        detalhamento. Os nomes internos (CPEN, PENDENCIA_MANUAL) viram
        português — ninguém precisa decorar código de sistema.
        """
        def n(valor: int) -> str:
            return f"{valor:,}".replace(",", ".")

        pct = (self.com_certidao / self.total * 100) if self.total else 0
        campos: dict[str, str] = {
            "Certidões obtidas": f"{n(self.com_certidao)} de {n(self.total)}  ({pct:.0f}%)",
        }

        if self.sem_certidao:
            campos["Sem certidão"] = f"{n(self.sem_certidao)} (positivas ou exigem atendimento)"
        if self.falhados:
            campos["Não concluíram"] = f"{n(self.falhados)} (esgotaram 3 tentativas)"

        campos["Detalhamento"] = " · ".join(
            f"{ROTULOS.get(d, d)}: {n(q)}"
            for d, q in sorted(self.por_desfecho.items(), key=lambda x: -x[1])
        ) or "-"
        campos["Duração"] = self.duracao
        campos["Origem"] = self.descricao
        return campos

    def como_texto(self) -> str:
        linhas = [f"Lote #{self.lote_id} — {self.descricao}",
                  f"Total de itens: {self.total}", ""]
        for rotulo, valor in self.como_campos().items():
            linhas.append(f"  {rotulo:32s} {valor}")
        if self.falhados:
            linhas.append("")
            linhas.append(f"  {self.falhados} item(ns) esgotaram as tentativas. "
                          f"O robô continua tentando sozinho, com intervalo "
                          f"crescente, até o portal responder.")
        return "\n".join(linhas)


def lotes_recem_concluidos(conn: sqlite3.Connection) -> list[ResumoLote]:
    """Lotes cujo último item acabou de sair da fila.

    `lote.encerrado_em` funciona como marca de "já avisei": preencher a
    coluna é o que impede o mesmo lote de gerar e-mail a cada ciclo.
    """
    abertos = conn.execute(
        """
        SELECT l.id, l.descricao, l.criado_em
          FROM lote l
         WHERE l.encerrado_em IS NULL
           AND EXISTS (SELECT 1 FROM job j WHERE j.lote_id = l.id)
           AND NOT EXISTS (
                 SELECT 1 FROM job j
                  WHERE j.lote_id = l.id AND j.status IN (?, ?, ?)
               )
        """,
        (Status.PENDING, Status.RUNNING, Status.RETRY_WAIT),
    ).fetchall()

    concluidos: list[ResumoLote] = []
    for lote in abertos:
        por_desfecho = {
            linha["desfecho"]: linha["n"]
            for linha in conn.execute(
                "SELECT desfecho, COUNT(*) AS n FROM job "
                "WHERE lote_id = ? AND desfecho IS NOT NULL GROUP BY desfecho",
                (lote["id"],),
            )
        }
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE lote_id = ?", (lote["id"],)
        ).fetchone()["n"]
        falhados = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE lote_id = ? AND status = ?",
            (lote["id"], Status.FAILED),
        ).fetchone()["n"]

        agora = tempo.agora_iso()
        conn.execute("UPDATE lote SET encerrado_em = ? WHERE id = ?",
                     (agora, lote["id"]))
        concluidos.append(ResumoLote(
            lote_id=lote["id"], descricao=lote["descricao"], total=total,
            por_desfecho=por_desfecho, falhados=falhados,
            criado_em=lote["criado_em"], encerrado_em=agora,
        ))
    return concluidos


def minutos_sem_progresso(conn: sqlite3.Connection, orgao: str) -> float | None:
    """Há quanto tempo nada conclui, tendo trabalho na fila.

    Devolve None quando não há mais o que fazer — fila vazia não é
    travamento, é serviço terminado.
    """
    pendentes = conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE orgao = ? AND status IN (?, ?, ?)",
        (orgao, Status.PENDING, Status.RUNNING, Status.RETRY_WAIT),
    ).fetchone()["n"]
    if not pendentes:
        return None

    ultima = conn.execute(
        """
        SELECT MAX(t.finalizada_em) AS quando
          FROM tentativa t JOIN job j ON j.id = t.job_id
         WHERE j.orgao = ? AND t.finalizada_em IS NOT NULL
        """,
        (orgao,),
    ).fetchone()["quando"]

    if ultima is None:
        return None      # nunca rodou; não há do que reclamar ainda
    return (tempo.agora() - tempo.de_iso(ultima)).total_seconds() / 60


def falhas_definitivas(conn: sqlite3.Connection, orgao: str,
                       desde_iso: str) -> list[sqlite3.Row]:
    """Itens que esgotaram as tentativas depois do momento informado."""
    return conn.execute(
        """
        SELECT e.nome, e.documento, j.desfecho, j.atualizado_em
          FROM job j JOIN empresa e ON e.id = j.empresa_id
         WHERE j.orgao = ? AND j.status = ? AND j.atualizado_em > ?
         ORDER BY j.atualizado_em
        """,
        (orgao, Status.FAILED, desde_iso),
    ).fetchall()


def campos_das_falhas(falhas: list[sqlite3.Row], limite: int = 12) -> dict[str, str]:
    """As falhas como campos, para virar tabela no aviso.

    Limite baixo de propósito: cartão gigante não é lido. Quem quiser a
    lista inteira clica no botão que leva ao painel.
    """
    from cnd.core.documentos import formatar

    campos: dict[str, str] = {}
    for falha in falhas[:limite]:
        rotulo = ROTULOS.get(falha["desfecho"], falha["desfecho"] or "-")
        campos[f"{falha['nome'][:38]}"] = f"{formatar(falha['documento'])} · {rotulo}"
    if len(falhas) > limite:
        campos["..."] = f"e mais {len(falhas) - limite} item(ns)"
    return campos


def texto_das_falhas(falhas: list[sqlite3.Row], limite: int = 25) -> str:
    from cnd.core.documentos import formatar

    linhas = [f"{len(falhas)} item(ns) esgotaram as tentativas:", ""]
    for falha in falhas[:limite]:
        linhas.append(f"  {formatar(falha['documento'])}  {falha['nome'][:45]}"
                      f"   ({falha['desfecho']})")
    if len(falhas) > limite:
        linhas.append(f"  ... e mais {len(falhas) - limite}.")
    linhas += ["", "Eles continuam no sistema e podem ser reenviados para a "
                   "fila pelo painel, em Itens > Reenviar itens com falha."]
    return "\n".join(linhas)
