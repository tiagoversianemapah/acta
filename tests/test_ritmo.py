"""Testes do ritmo adaptativo (AIMD).

O comportamento que estes testes travam: acelerar devagar, punir forte,
e nunca sair dos limites configurados.
"""
from __future__ import annotations

from cnd.core import ritmo

P = ritmo.ParametrosRitmo(
    intervalo_inicial_s=10.0,
    intervalo_piso_s=1.0,
    intervalo_teto_s=100.0,
    acelera_apos=3,
    fator_aceleracao=0.5,
    fator_punicao=4.0,
)


def test_comeca_no_intervalo_inicial(conn):
    estado = ritmo.estado(conn, "FAKE", P)

    assert estado.intervalo_s == 10.0
    assert estado.consultas_limpas == 0


def test_so_acelera_depois_do_limiar(conn):
    ritmo.registrar_sucesso(conn, "FAKE", P)
    estado = ritmo.registrar_sucesso(conn, "FAKE", P)

    assert estado.intervalo_s == 10.0, "não pode acelerar antes de 3 sucessos"
    assert estado.consultas_limpas == 2


def test_acelera_e_zera_a_contagem(conn):
    for _ in range(3):
        estado = ritmo.registrar_sucesso(conn, "FAKE", P)

    assert estado.intervalo_s == 5.0     # 10 * 0.5
    assert estado.consultas_limpas == 0


def test_captcha_pune_multiplicativamente(conn):
    estado = ritmo.registrar_captcha(conn, "FAKE", P)

    assert estado.intervalo_s == 40.0    # 10 * 4


def test_captcha_zera_a_sequencia_limpa(conn):
    ritmo.registrar_sucesso(conn, "FAKE", P)
    ritmo.registrar_sucesso(conn, "FAKE", P)

    estado = ritmo.registrar_captcha(conn, "FAKE", P)

    assert estado.consultas_limpas == 0


def test_nunca_passa_do_piso(conn):
    for _ in range(60):
        estado = ritmo.registrar_sucesso(conn, "FAKE", P)

    assert estado.intervalo_s == P.intervalo_piso_s


def test_nunca_passa_do_teto(conn):
    for _ in range(10):
        estado = ritmo.registrar_captcha(conn, "FAKE", P)

    assert estado.intervalo_s == P.intervalo_teto_s


def test_convergencia(conn):
    """Cenário realista: acelera até apanhar, e se acomoda.

    É o comportamento que substitui o ajuste manual de configuração.
    """
    for _ in range(6):                       # 6 sucessos => acelerou 2x
        ritmo.registrar_sucesso(conn, "FAKE", P)
    assert ritmo.estado(conn, "FAKE", P).intervalo_s == 2.5

    ritmo.registrar_captcha(conn, "FAKE", P)  # bateu no limite
    assert ritmo.estado(conn, "FAKE", P).intervalo_s == 10.0

    for _ in range(3):                        # recomeça a subir devagar
        ritmo.registrar_sucesso(conn, "FAKE", P)
    assert ritmo.estado(conn, "FAKE", P).intervalo_s == 5.0


def test_estado_sobrevive_a_reinicio(conn):
    ritmo.registrar_captcha(conn, "FAKE", P)

    # Uma "nova execução" lê do banco, não da memória.
    estado = ritmo.estado(conn, "FAKE", P)

    assert estado.intervalo_s == 40.0


def test_jitter_varia_dentro_da_faixa():
    esperas = [ritmo.proxima_espera(10.0, jitter=0.3) for _ in range(200)]

    assert all(7.0 <= e <= 13.0 for e in esperas)
    assert len(set(esperas)) > 100, "as esperas precisam variar, não ser metronômicas"


def test_jitter_zero_devolve_o_intervalo():
    assert ritmo.proxima_espera(5.0, jitter=0.0) == 5.0
