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

from cnd.core import perfis, tempo
from cnd.core.modelos import BLOQUEIOS, Desfecho

FECHADO = "FECHADO"
ABERTO = "ABERTO"
MEIO_ABERTO = "MEIO_ABERTO"
COOLDOWN_MAXIMO_DO_SISTEMA_S = 9000

# Desfechos que fazem o ÓRGÃO parar, e não o item apanhar. Todos têm em
# comum não serem resposta sobre a empresa: são o portal indisponível,
# desconfiado ou engasgado. Insistir com outros itens do mesmo órgão nesse
# estado não adianta e ainda reforça o sinal de robô.
ALIMENTAM_O_DISJUNTOR = BLOQUEIOS | {
    Desfecho.ERRO_TECNICO,
    Desfecho.RESULTADO_PENDENTE,
}


@dataclass(frozen=True)
class ParametrosBreaker:
    ativo: bool = True
    captchas_para_abrir: int = 3
    janela_jobs: int = 10
    erros_para_abrir: int = 5
    cooldown_inicial_s: int = 1800
    cooldown_maximo_s: int = COOLDOWN_MAXIMO_DO_SISTEMA_S
    cooldown_erro_inicial_s: int | None = None
    cooldown_erro_maximo_s: int | None = None
    # "Retorne em alguns minutos" não é recado sobre aquela empresa: é o
    # portal engasgado para todo mundo. Punir o CNPJ (esperar 1h para tentar
    # ELE de novo) fazia o item morrer sem culpa nenhuma, enquanto o robô
    # seguia batendo no portal doente com os outros. O certo é o inverso —
    # o item volta para o fim da fila na hora, e quem descansa é o órgão.
    # Decisão de operação, 17/08/2026.
    pendentes_para_pausar: int = 3
    cooldown_pendente_s: int = 1800      # 30 min, e NÃO dobra

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
    """Para quem pode escrever. Cria a linha do órgão se ela não existir."""
    linha = _garantir(conn, orgao)
    return _estado_da_linha(linha)


def ativo_para(orgao: str, p: ParametrosBreaker | None = None) -> bool:
    # Órgão cujo captcha é nosso não tem o que o disjuntor proteja: ele
    # existe para recuar diante de portal que barra, e esse não barra.
    if perfis.captcha_lido_por_nos(orgao):
        return False
    return True if p is None else p.ativo


def desativar(conn: sqlite3.Connection, orgao: str) -> EstadoBreaker:
    """Apaga qualquer pausa deste órgão e devolve o estado limpo.

    Escreve só quando há o que apagar: `pode_despachar` chama isto a cada
    job de um órgão isento, e um UPDATE por consulta é escrita no banco em
    troca de nada.
    """
    atual = consultar(conn, orgao)
    if (atual.estado == FECHADO and atual.aberturas == 0
            and not atual.aberto_ate and not atual.motivo):
        return atual

    conn.execute(
        """
        UPDATE breaker
           SET estado = ?, aberto_ate = NULL, aberturas = 0,
               motivo = NULL, atualizado_em = ?
         WHERE orgao = ?
        """,
        (FECHADO, tempo.agora_iso(), orgao),
    )
    return consultar(conn, orgao)


def consultar_leitura(conn: sqlite3.Connection, orgao: str) -> EstadoBreaker:
    """O mesmo estado, mas SEM nunca escrever.

    O painel abre o banco em modo leitura, e `consultar` chama `_garantir`,
    que INSERE a linha quando ela não existe. Bastou o CRF ser ligado e
    ganhar fila em 18/08/2026 para o /api/estado devolver 500 —
    "attempt to write a readonly database" — e a tela de Operação aparecer
    vazia, enquanto a Receita seguia funcionando porque a linha dela já
    existia desde agosto.

    Órgão sem linha é órgão que ainda não rodou, e não rodar não é estar
    bloqueado: devolve fechado, que é a verdade.
    """
    if not ativo_para(orgao):
        return EstadoBreaker(estado=FECHADO, aberto_ate=None,
                             aberturas=0, motivo=None)

    linha = conn.execute(
        "SELECT * FROM breaker WHERE orgao = ?", (orgao,)).fetchone()
    if linha is None:
        return EstadoBreaker(estado=FECHADO, aberto_ate=None,
                             aberturas=0, motivo=None)
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


def pode_despachar(
    conn: sqlite3.Connection,
    orgao: str,
    p: ParametrosBreaker | None = None,
) -> bool:
    """Pode mandar um job agora?

    Se estava ABERTO e o cooldown venceu, promove para MEIO_ABERTO e
    libera exatamente uma sondagem.
    """
    if not ativo_para(orgao, p):
        desativar(conn, orgao)
        return True

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
    # Piso e teto iguais => pausa FIXA. O portal disse "alguns minutos";
    # dobrar até 2h30 puniria o robô por um problema que não é dele e que
    # costuma passar sozinho.
    if str(desfecho or "") == str(Desfecho.RESULTADO_PENDENTE):
        fixo = min(p.cooldown_pendente_s, COOLDOWN_MAXIMO_DO_SISTEMA_S)
        return fixo, fixo
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
    if not ativo_para(orgao, p):
        return desativar(conn, orgao)

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
    if not ativo_para(orgao, p):
        return desativar(conn, orgao)

    _garantir(conn, orgao)
    atual = consultar(conn, orgao)

    # Sondagem em MEIO_ABERTO: um resultado limpo religa o órgão. Resultado
    # pendente não é limpo — o portal continua sem entregar certidão.
    if atual.estado == MEIO_ABERTO:
        if desfecho in ALIMENTAM_O_DISJUNTOR:
            return abrir(conn, orgao, f"sondagem falhou ({desfecho})", p, desfecho)
        fechar(conn, orgao)
        return consultar(conn, orgao)

    if desfecho not in ALIMENTAM_O_DISJUNTOR:
        return atual

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

    if desfecho in BLOQUEIOS:
        bloqueios = sum(1 for d in recentes if d in BLOQUEIOS)
        if bloqueios >= p.captchas_para_abrir:
            return abrir(
                conn, orgao,
                f"{bloqueios} bloqueios nas últimas {len(recentes)} tentativas",
                p, desfecho,
            )

    if desfecho == Desfecho.RESULTADO_PENDENTE:
        pendentes = sum(1 for d in recentes if d == Desfecho.RESULTADO_PENDENTE)
        if pendentes >= p.pendentes_para_pausar:
            return abrir(
                conn, orgao,
                f"{pendentes} resultados pendentes nas últimas "
                f"{len(recentes)} tentativas",
                p, desfecho,
            )

    if desfecho == Desfecho.ERRO_TECNICO:
        erros_seguidos = 0
        for d in recentes:
            if d == Desfecho.ERRO_TECNICO:
                erros_seguidos += 1
            else:
                break
        if erros_seguidos >= p.erros_para_abrir:
            return abrir(
                conn, orgao,
                f"{erros_seguidos} erros técnicos seguidos",
                p, desfecho,
            )

    return atual
