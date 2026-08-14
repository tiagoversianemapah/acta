"""Circuit breaker por órgão — ver docs/04, seção 5.

Retry por item não basta: se a heurística antirrobô já bloqueou, insistir
com OUTROS itens do mesmo órgão só reforça o sinal de robô. Por isso o
disjuntor é por órgão — ele para a fila daquele portal e deixa os demais
trabalhando normalmente.

    FECHADO      tudo normal
    ABERTO       nada é despachado; conta o cooldown
    MEIO_ABERTO  despacha 1 job de sondagem; o resultado decide o estado
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from cnd.core import tempo
from cnd.core.modelos import BLOQUEIOS, Desfecho

FECHADO = "FECHADO"
ABERTO = "ABERTO"
MEIO_ABERTO = "MEIO_ABERTO"
COOLDOWN_MAXIMO_DO_SISTEMA_S = 9000


@dataclass(frozen=True)
class ParametrosBreaker:
    captchas_para_abrir: int = 3
    janela_jobs: int = 10
    erros_para_abrir: int = 5
    cooldown_inicial_s: int = 1800
    cooldown_maximo_s: int = COOLDOWN_MAXIMO_DO_SISTEMA_S
    cooldown_erro_inicial_s: int | None = None
    cooldown_erro_maximo_s: int | None = None

    @classmethod
    def de_config(cls, dados: dict) -> ParametrosBreaker:
        campos = {c: dados[c] for c in cls.__dataclass_fields__ if c in dados}
        return cls(**campos)


@dataclass(frozen=True)
class EstadoBreaker:
    estado: str
    aberto_ate: str | None
    aberturas: int
    motivo: str | None

    @property
    def bloqueado(self) -> bool:
        return self.estado == ABERTO


def _garantir(conn: sqlite3.Connection, orgao: str) -> sqlite3.Row:
    linha = conn.execute("SELECT * FROM breaker WHERE orgao = ?", (orgao,)).fetchone()
    if linha is None:
        conn.execute(
            "INSERT INTO breaker (orgao, estado, atualizado_em) VALUES (?, ?, ?)",
            (orgao, FECHADO, tempo.agora_iso()),
        )
        linha = conn.execute("SELECT * FROM breaker WHERE orgao = ?", (orgao,)).fetchone()
    return linha


def consultar(conn: sqlite3.Connection, orgao: str) -> EstadoBreaker:
    linha = _garantir(conn, orgao)
    return _estado_da_linha(linha)


def _estado_da_linha(linha: sqlite3.Row) -> EstadoBreaker:
    return EstadoBreaker(
        estado=linha["estado"],
        aberto_ate=_aberto_ate_efetivo(linha),
        aberturas=linha["aberturas"],
        motivo=linha["motivo"],
    )


def _aberto_ate_efetivo(linha: sqlite3.Row) -> str | None:
    aberto_ate = linha["aberto_ate"]
    if linha["estado"] != ABERTO or not aberto_ate or not linha["atualizado_em"]:
        return aberto_ate
    try:
        limite = tempo.para_iso(
            tempo.de_iso(linha["atualizado_em"])
            + timedelta(seconds=COOLDOWN_MAXIMO_DO_SISTEMA_S)
        )
    except Exception:
        return aberto_ate
    return min(aberto_ate, limite)


def pode_despachar(conn: sqlite3.Connection, orgao: str) -> bool:
    """Pode mandar um job agora?

    Se estava ABERTO e o cooldown venceu, promove para MEIO_ABERTO e
    libera exatamente uma sondagem.
    """
    linha = _garantir(conn, orgao)
    atual = _estado_da_linha(linha)

    if atual.estado == FECHADO:
        return True

    if atual.estado == MEIO_ABERTO:
        return True

    agora = tempo.agora_iso()
    if atual.aberto_ate and agora >= atual.aberto_ate:
        conn.execute(
            "UPDATE breaker SET estado = ?, atualizado_em = ? WHERE orgao = ?",
            (MEIO_ABERTO, tempo.agora_iso(), orgao),
        )
        return True

    return False


def _faixa_cooldown(p: ParametrosBreaker,
                    desfecho: Desfecho | str | None) -> tuple[int, int]:
    if (str(desfecho or "") == str(Desfecho.ERRO_TECNICO)
            and p.cooldown_erro_inicial_s is not None):
        teto = p.cooldown_erro_maximo_s
        return (
            p.cooldown_erro_inicial_s,
            min(teto if teto is not None else p.cooldown_maximo_s,
                COOLDOWN_MAXIMO_DO_SISTEMA_S),
        )
    return p.cooldown_inicial_s, min(
        p.cooldown_maximo_s, COOLDOWN_MAXIMO_DO_SISTEMA_S
    )


def cooldown_atual_s(
    estado: EstadoBreaker,
    p: ParametrosBreaker,
    desfecho: Desfecho | str | None = None,
) -> int | None:
    """Duracao planejada da pausa aberta agora.

    O horario restante muda a cada segundo; este valor e a faixa original:
    30 min, 1 h, 2 h, 2 h 30 min, conforme a quantidade de reaberturas.
    """
    if estado.estado != ABERTO or estado.aberturas <= 0:
        return None
    if desfecho is None and estado.motivo:
        texto = estado.motivo.lower()
        if "erro" in texto:
            desfecho = Desfecho.ERRO_TECNICO
    base, teto = _faixa_cooldown(p, desfecho)
    return min(teto, base * (2 ** (estado.aberturas - 1)))


def abrir(conn: sqlite3.Connection, orgao: str, motivo: str,
          p: ParametrosBreaker,
          desfecho: Desfecho | str | None = None) -> EstadoBreaker:
    """Interrompe o órgão. O cooldown dobra a cada reabertura, até o teto —
    se o portal continua bloqueando, esperar mais é a resposta certa."""
    atual = consultar(conn, orgao)
    aberturas = atual.aberturas + 1
    base, teto = _faixa_cooldown(p, desfecho)
    cooldown = min(teto, base * (2 ** (aberturas - 1)))

    conn.execute(
        """
        UPDATE breaker
           SET estado = ?, aberto_ate = ?, aberturas = ?, motivo = ?, atualizado_em = ?
         WHERE orgao = ?
        """,
        (ABERTO, tempo.daqui_a(cooldown), aberturas, motivo, tempo.agora_iso(), orgao),
    )
    return consultar(conn, orgao)


def fechar(conn: sqlite3.Connection, orgao: str) -> None:
    conn.execute(
        """
        UPDATE breaker
           SET estado = ?, aberto_ate = NULL, motivo = NULL, atualizado_em = ?
         WHERE orgao = ?
        """,
        (FECHADO, tempo.agora_iso(), orgao),
    )


def avaliar(conn: sqlite3.Connection, orgao: str, desfecho: Desfecho,
            p: ParametrosBreaker) -> EstadoBreaker:
    """Chamado depois de cada tentativa. Decide se abre, fecha ou mantém.

    Devolve o estado resultante — o orquestrador usa isso para saber se
    precisa disparar alerta por e-mail.
    """
    _garantir(conn, orgao)
    atual = consultar(conn, orgao)

    # Sondagem em MEIO_ABERTO: um resultado limpo religa o órgão.
    if atual.estado == MEIO_ABERTO:
        if desfecho in BLOQUEIOS or desfecho == Desfecho.ERRO_TECNICO:
            return abrir(conn, orgao, f"sondagem falhou ({desfecho})", p, desfecho)
        fechar(conn, orgao)
        return consultar(conn, orgao)

    recentes = [
        linha["desfecho"]
        for linha in conn.execute(
            """
            SELECT t.desfecho
              FROM tentativa t
              JOIN job j ON j.id = t.job_id
             WHERE j.orgao = ? AND t.desfecho IS NOT NULL
             ORDER BY t.id DESC
             LIMIT ?
            """,
            (orgao, p.janela_jobs),
        )
    ]

    bloqueios = sum(1 for d in recentes if d in BLOQUEIOS)
    if bloqueios >= p.captchas_para_abrir:
        return abrir(conn, orgao,
                     f"{bloqueios} bloqueios nas últimas {len(recentes)} tentativas",
                     p, desfecho)

    erros_seguidos = 0
    for d in recentes:
        if d == Desfecho.ERRO_TECNICO:
            erros_seguidos += 1
        else:
            break
    if erros_seguidos >= p.erros_para_abrir:
        return abrir(conn, orgao, f"{erros_seguidos} erros técnicos seguidos",
                     p, desfecho)

    return atual
