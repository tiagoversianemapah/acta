"""Testes do circuit breaker.

O que protegem: que o robô pare sozinho quando o portal começa a
bloquear, que cada órgão seja independente, e que ele volte por conta
própria depois do cooldown.
"""
from __future__ import annotations

import itertools
from datetime import timedelta

from cnd.core import breaker, tempo
from cnd.core.modelos import Desfecho
from cnd.web import consultas
from tests.conftest import criar_job

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


def test_resultado_limpo_nao_reabre_pausa_por_bloqueios_antigos(conn, lote):
    """Depois de resetar, um resultado limpo nao deve reacender pausa antiga."""
    _registrar(conn, lote, "FAKE", Desfecho.BLOQUEIO_TEMPORARIO, 3)
    breaker.fechar(conn, "FAKE")

    estado = _registrar(conn, lote, "FAKE", Desfecho.CPEN)

    assert estado.estado == breaker.FECHADO


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


def test_cooldown_atual_expoe_a_faixa_da_pausa(conn):
    primeiro = breaker.abrir(conn, "FAKE", "teste", P)
    breaker.fechar(conn, "FAKE")
    segundo = breaker.abrir(conn, "FAKE", "teste", P)

    assert breaker.cooldown_atual_s(primeiro, P) == 60
    assert breaker.cooldown_atual_s(segundo, P) == 120
    assert consultas.rotulo_duracao(1800) == "30 min"
    assert consultas.rotulo_duracao(3600) == "1 h"


def test_cooldown_respeita_o_teto(conn):
    for _ in range(8):
        estado = breaker.abrir(conn, "FAKE", "teste", P)
        breaker.fechar(conn, "FAKE")

    espera_s = (tempo.de_iso(estado.aberto_ate) - tempo.agora()).total_seconds()
    assert espera_s <= P.cooldown_maximo_s + 1


def test_cooldown_do_sistema_corta_config_antigo_de_quatro_horas(conn):
    parametros = breaker.ParametrosBreaker(
        cooldown_inicial_s=1800,
        cooldown_maximo_s=14400,
    )
    for _ in range(8):
        estado = breaker.abrir(conn, "FAKE", "teste", parametros)
        breaker.fechar(conn, "FAKE")

    espera_s = (tempo.de_iso(estado.aberto_ate) - tempo.agora()).total_seconds()
    assert espera_s <= 9001
    assert breaker.cooldown_atual_s(estado, parametros) == 9000


def test_cooldown_do_sistema_libera_pausa_antiga_ja_gravada(conn):
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute(
        """
        UPDATE breaker
           SET aberto_ate = ?, atualizado_em = ?
         WHERE orgao = 'FAKE'
        """,
        (
            tempo.daqui_a(3600),
            tempo.para_iso(
                tempo.agora()
                - timedelta(seconds=breaker.COOLDOWN_MAXIMO_DO_SISTEMA_S + 1)
            ),
        ),
    )

    assert breaker.pode_despachar(conn, "FAKE") is True
    assert breaker.consultar(conn, "FAKE").estado == breaker.MEIO_ABERTO


def test_consultar_mostra_pausa_antiga_no_teto_do_sistema(conn):
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute(
        """
        UPDATE breaker
           SET aberto_ate = ?, atualizado_em = ?
         WHERE orgao = 'FAKE'
        """,
        (tempo.daqui_a(14400), tempo.agora_iso()),
    )

    estado = breaker.consultar(conn, "FAKE")
    espera_s = (tempo.de_iso(estado.aberto_ate) - tempo.agora()).total_seconds()

    assert espera_s <= breaker.COOLDOWN_MAXIMO_DO_SISTEMA_S + 1


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


# --------------------------------------------------------------------------
# Resultado pendente: quem descansa é o órgão, não o CNPJ (17/08/2026)
# --------------------------------------------------------------------------

def test_pendentes_isolados_nao_pausam(conn, lote):
    """Um engasgo solto do portal não pode parar a fila inteira."""
    estado = _registrar(conn, lote, "FAKE", Desfecho.RESULTADO_PENDENTE, 2)

    assert estado.estado == breaker.FECHADO


def test_tres_pendentes_pausam_o_orgao(conn, lote):
    """O portal está engasgado para todo mundo: seguir batendo com os
    próximos CNPJs só gasta consulta contra uma tela que não vai responder."""
    estado = _registrar(conn, lote, "FAKE", Desfecho.RESULTADO_PENDENTE, 3)

    assert estado.estado == breaker.ABERTO
    assert "pendentes" in estado.motivo


def test_pausa_por_pendente_e_fixa_e_nao_dobra(conn):
    """Diferente do bloqueio: o portal disse "alguns minutos", e dobrar até
    2h30 puniria o robô por um problema que costuma passar sozinho."""
    parametros = breaker.ParametrosBreaker(cooldown_pendente_s=1800)

    primeira = breaker.abrir(conn, "FAKE", "pendentes", parametros,
                             Desfecho.RESULTADO_PENDENTE)
    segunda = breaker.abrir(conn, "FAKE", "pendentes", parametros,
                            Desfecho.RESULTADO_PENDENTE)

    assert breaker.cooldown_atual_s(primeira, parametros,
                                    Desfecho.RESULTADO_PENDENTE) == 1800
    assert breaker.cooldown_atual_s(segunda, parametros,
                                    Desfecho.RESULTADO_PENDENTE) == 1800
    assert segunda.aberturas == 2


def test_sondagem_com_pendente_reabre(conn):
    """Resultado pendente não é resultado limpo: o portal continua sem
    entregar certidão, então religar o órgão agora seria cedo demais."""
    breaker.abrir(conn, "FAKE", "teste", P)
    conn.execute("UPDATE breaker SET estado = ? WHERE orgao = 'FAKE'",
                 (breaker.MEIO_ABERTO,))

    estado = breaker.avaliar(conn, "FAKE", Desfecho.RESULTADO_PENDENTE, P)

    assert estado.estado == breaker.ABERTO
