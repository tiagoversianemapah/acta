from cnd.web.app import _totais


def test_totais_expoem_resultado_geral():
    totais = _totais([
        {
            "total": 10,
            "concluidos": 6,
            "pendentes": 3,
            "em_execucao": 1,
            "falhados": 0,
            "por_desfecho": {"NEGATIVA": 4, "POSITIVA": 1},
        },
        {
            "total": 5,
            "concluidos": 2,
            "pendentes": 2,
            "em_execucao": 0,
            "falhados": 1,
            "por_desfecho": {"CPEN": 1, "PENDENCIA_MANUAL": 1},
        },
    ])

    assert totais["negativas"] == 4
    assert totais["positivas"] == 1
    assert totais["cpen"] == 1
    assert totais["insuficientes"] == 1
    assert totais["percentual"] == 8 / 15 * 100
