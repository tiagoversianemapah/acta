from cnd.core import tempo
from cnd.web import consultas, diagnostico


def _tentativa(conn, job_id: int, hora: str, desfecho: str, mensagem: str = ""):
    conn.execute(
        """
        INSERT INTO tentativa
            (job_id, numero, iniciada_em, finalizada_em, desfecho, mensagem_portal)
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (
            job_id,
            f"2026-08-11T{hora}:00:00.000Z",
            f"2026-08-11T{hora}:00:20.000Z",
            desfecho,
            mensagem,
        ),
    )


def _hora_local(hora: str) -> str:
    return tempo.de_iso(f"2026-08-11T{hora}:00:00.000Z").astimezone().strftime("%H")


def test_diagnostico_usa_tentativas_e_nao_confunde_insuficiente_com_erro(
    conn, lote
):
    from tests.conftest import criar_job

    job = criar_job(conn, lote, orgao="RFB_PJ")
    for _ in range(6):
        _tentativa(conn, job, "17", "CAPTCHA")
    for _ in range(2):
        _tentativa(conn, job, "18", "BLOQUEIO_TEMPORARIO")
    for _ in range(3):
        _tentativa(conn, job, "18", "ERRO_TECNICO", "não veio PDF")
    for _ in range(4):
        _tentativa(conn, job, "19", "PENDENCIA_MANUAL")
    _tentativa(conn, job, "19", "NEGATIVA")

    diag = consultas.diagnostico_orgao(conn, "RFB_PJ", dias=30)

    assert diag["consultas"] == 16
    assert diag["erros"] == 11
    assert diag["bloqueios"] == 2
    assert diag["insuficientes"] == 4
    assert round(diag["taxa_erro"], 4) == round(11 / 16, 4)
    assert diag["eixo"] == [10, 8, 6, 4, 2, 0]

    por_hora = {linha["hora"]: linha for linha in diag["horas"]}
    assert por_hora[_hora_local("17")]["erros_operacionais"] == 6
    assert por_hora[_hora_local("18")]["erros_operacionais"] == 5
    assert por_hora[_hora_local("19")]["erros_operacionais"] == 0

    assert diag["principais_erros"][0] == {
        "erro": "Captcha não resolvido",
        "quantidade": 6,
    }
    assert {"erro": "Não veio PDF / faixa de aviso", "quantidade": 3} in (
        diag["principais_erros"]
    )


def test_diagnostico_prepara_valores_para_a_tela():
    bruto = diagnostico.vazio()
    bruto.update({
        "consultas": 1234,
        "erros": 12,
        "taxa_erro": 12 / 1234,
        "bloqueios": 3,
        "insuficientes": 2,
        "pior_hora": {"hora": "17", "erros_operacionais": 4},
        "horas": [
            {"hora": "17", "total": 10, "erros_operacionais": 4, "taxa_erro": 0.4},
            {"hora": "18", "total": 10, "erros_operacionais": 0, "taxa_erro": 0.0},
        ],
    })

    preparado = diagnostico.preparar(bruto)

    assert preparado["consultas_fmt"] == "1.234"
    assert preparado["taxa_erro_fmt"] == "1,0"
    assert preparado["horas"][0]["erro_alto"]
    assert not preparado["horas"][1]["erro_alto"]


def test_insight_diferencia_sem_dados_sem_erro_e_pior_horario():
    assert "Ainda não há amostra" in diagnostico.insight({"consultas": 0})[0]

    sem_erro = {
        "consultas": 10,
        "pior_hora": {"hora": "10", "erros_operacionais": 0},
        "melhores": [{"hora": "10"}],
    }
    assert "Não houve concentração" in diagnostico.insight(sem_erro)[0]

    com_erro = {
        "consultas": 10,
        "pior_hora": {
            "hora": "17",
            "erros_operacionais": 2,
            "taxa_erro": 0.2,
        },
    }
    assert "17:00" in diagnostico.insight(com_erro)[0]


def test_contexto_de_diagnostico_fica_pronto_para_o_template():
    ctx = diagnostico.contexto(
        estados=[],
        selecionada=None,
        selecionada_idx=None,
        diagnosticos=[],
        erro=None,
        dias=diagnostico.normalizar_dias(200),
    )

    assert ctx["dias"] == diagnostico.DIAS_MAXIMOS
    assert ctx["exportar_url"] == "/diagnostico.json?maquina=&dias=30"
    assert ctx["diagnostico"]["eixo"] == [5, 4, 3, 2, 1, 0]
