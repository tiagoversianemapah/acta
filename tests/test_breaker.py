"""Testes do circuit breaker.

O que protegem: que o robô pare sozinho quando o portal começa a
bloquear, que cada órgão seja independente, e que ele volte por conta
própria depois do cooldown.
"""
from __future__ import annotations

import itertools

from tests.conftest import criar_job

from cnd.core import breaker, tempo
from cnd.core.modelos import Desfecho

P = breaker.ParametrosBreaker(
    captchas_para_abrir=3,
    janela_jobs=10,
    erros_para_abrir=5,
    cooldown_inicial_s=60,
    cooldown_maximo_s=240,
)


_sequencia = itertools.count(1)


def _registrar(conn, lote, orgao: str, desfecho: Desfecho, quantidade: int = 1):
    """Grava tentativas com esse desfecho e avalia o breaker a cada uma.

    Cada chamada usa documentos novos, para poder empilhar tipos diferentes
    de desfecho no mesmo teste.
    """
    estado = None
    for _ in range(quantidade):
        job_id = criar_job(conn, lote, documento=f"{next(_sequencia):014d}",
                           orgao=orgao)
        conn.execute(
            "INSERT INTO tentativa (job_id, numero, iniciada_em, desfecho) VALUES (?, 1, ?, ?)",
            (job_id, tempo.agora_iso(), str(desfecho)),
        )
        estado = breaker.avaliar(conn, orgao, desfecho, P)
    return estado


def test_comeca_fechado(conn):
    assert breaker.consultar(conn, "FAKE").estado == breaker.FECHADO
    assert breaker.pode_despachar(conn, "FAKE") is True


def test_captchas_isolados_nao_abrem(conn, lote):
    estado = _registrar(conn, lote, "FAKE", Desfecho.CAPTCHA, 2)

    assert estado.estado == breaker.FECHADO


def test_tres_captchas_na_janela_abrem(conn, lote):
    estado = _registrar(conn, lote, "FAKE", Desfecho.CAPTCHA, 3)

    assert estado.estado == breaker.ABERTO
    assert "bloqueios" in estado.motivo
    assert breaker.pode_despachar(conn, "FAKE") is False


def test_bloqueio_temporario_tambem_abre(conn, lote):
    """O portal pedindo para voltar depois conta igual a captcha: nos dois
    casos ele está nos barrando, e insistir só piora."""
    estado = _registrar(conn, lote, "FAKE", Desfecho.BLOQUEIO_TEMPORARIO, 3)

    assert estado.estado == breaker.ABERTO


def test_bloqueios_de_tipos_diferentes_somam(conn, lote):
    _registrar(conn, lote, "FAKE", Desfecho.CAPTCHA, 2)
    estado = _registrar(conn, lote, "FAKE", Desfecho.BLOQUEIO_TEMPORARIO, 1)

    assert estado.estado == breaker.ABERTO


def test_erros_seguidos_abrem(conn, lote):
    estado = _registrar(conn, lote, "FAKE", Desfecho.ERRO_TECNICO, 5)

    assert estado.estado == breaker.ABERTO
    assert "erros técnicos" in estado.motivo


def test_um_orgao_aberto_nao_afeta_o_outro(conn, lote):
    _registrar(conn, lote, "FAKE", Desfecho.CAPTCHA, 3)

    assert breaker.pode_despachar(conn, "FAKE") is False
    assert breaker.pode_despachar(conn, "RFB_PJ") is True, \
        "cada portal tem o próprio disjuntor"


def test_cooldown_dobra_a_cada_reabertura(conn):
    primeiro = breaker.abrir(conn, "FAKE", "teste", P)
    breaker.fechar(conn, "FAKE")
    segundo = breaker.abrir(conn, "FAKE", "teste", P)

    assert primeiro.aberturas == 1
    assert segundo.aberturas == 2
    assert segundo.aberto_ate > primeiro.aberto_ate


def test_cooldown_respeita_o_teto(conn):
    for _ in range(8):
        estado = breaker.abrir(conn, "FAKE", "teste", P)
        breaker.fechar(conn, "FAKE")

    espera_s = (tempo.de_iso(estado.aberto_ate) - tempo.agora()).total_seconds()
    assert espera_s <= P.cooldown_maximo_s + 1


def test_erro_tecnico_pode_ter_cooldown_curto(conn):
    parametros = breaker.ParametrosBreaker(
        cooldown_inicial_s=60,
        cooldown_maximo_s=240,
        cooldown_erro_inicial_s=5,
        cooldown_erro_maximo_s=10,
    )

    estado = breaker.abrir(
        conn, "FAKE", "erro operacional", parametros, Desfecho.ERRO_TECNICO
    )

    espera_s = (tempo.de_iso(estado.aberto_ate) - tempo.agora()).total_seconds()
    assert espera_s <= 6


def test_vencido_o_cooldown_vira_meio_aberto(conn):
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute("UPDATE breaker SET aberto_ate = ? WHERE orgao = 'FAKE'",
                 (tempo.daqui_a(-1),))

    assert breaker.pode_despachar(conn, "FAKE") is True
    assert breaker.consultar(conn, "FAKE").estado == breaker.MEIO_ABERTO


def test_sondagem_boa_fecha_o_disjuntor(conn):
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute("UPDATE breaker SET estado = ? WHERE orgao = 'FAKE'", (breaker.MEIO_ABERTO,))

    estado = breaker.avaliar(conn, "FAKE", Desfecho.NEGATIVA, P)

    assert estado.estado == breaker.FECHADO


def test_sondagem_ruim_reabre(conn):
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute("UPDATE breaker SET estado = ? WHERE orgao = 'FAKE'", (breaker.MEIO_ABERTO,))

    estado = breaker.avaliar(conn, "FAKE", Desfecho.CAPTCHA, P)

    assert estado.estado == breaker.ABERTO
    assert estado.aberturas == 2
