from cnd.core import tempo
from cnd.core.modelos import Desfecho, Status
from cnd.web import carteira, consultas


def _marcar(conn, job_id: int, status: Status, desfecho: Desfecho | None = None,
            tentativas: int = 1) -> None:
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = ?, tentativas = ?,
               atualizado_em = '2026-08-11T21:23:00.000Z'
         WHERE id = ?
        """,
        (status, desfecho, tentativas, job_id),
    )


def test_carteira_exibe_documento_com_mascara():
    item = carteira.preparar_item(
        {
            "id": 10,
            "documento": "11222333000181",
            "nome": "EMPRESA TESTE",
            "atualizado_em": "2026-08-11T21:23:00.000Z",
        },
        "0",
        "PC Receita Federal 01",
    )

    assert item["documento_fmt"] == "11.222.333/0001-81"
    assert item["detalhe_url"] == "/job/10?maquina=0"
    esperado = tempo.de_iso("2026-08-11T21:23:00.000Z").astimezone()
    assert item["atualizado_fmt"] == f"{esperado:%d/%m/%Y %H:%M}"


def test_item_da_carteira_preserva_arquivo_na_url():
    item = carteira.preparar_item(
        {
            "id": 10,
            "lote_id": 7,
            "documento": "11222333000181",
            "nome": "EMPRESA TESTE",
            "atualizado_em": "2026-08-11T21:23:00.000Z",
        },
        "0",
        "PC Receita Federal 01",
    )

    assert item["detalhe_url"] == "/job/10?maquina=0&arquivo=7"


def test_seleciona_arquivo_solicitado_ou_mais_recente():
    arquivos = [
        {"id": 3, "nome": "novo.xlsx"},
        {"id": 2, "nome": "antigo.xlsx"},
    ]

    assert carteira.escolher_arquivo(arquivos, 2)["nome"] == "antigo.xlsx"
    assert carteira.escolher_arquivo(arquivos, 99)["nome"] == "novo.xlsx"


def test_busca_da_carteira_aceita_cnpj_com_mascara(conn, lote):
    from tests.conftest import criar_job

    esperado = criar_job(conn, lote, documento="11222333000181",
                         orgao="RFB_PJ", nome="EMPRESA TESTE")

    itens = consultas.jobs(conn, busca="11.222.333/0001-81")

    assert [item["id"] for item in itens] == [esperado]


def test_contagem_e_paginacao_da_carteira(conn, lote):
    from tests.conftest import criar_job

    ids = [
        criar_job(conn, lote, documento=f"1122233300018{i}", nome=f"EMPRESA {i}")
        for i in range(1, 4)
    ]
    _marcar(conn, ids[0], Status.PENDING)
    _marcar(conn, ids[1], Status.RETRY_WAIT, Desfecho.ERRO_TECNICO)
    _marcar(conn, ids[2], Status.DONE, Desfecho.NEGATIVA)

    contagens = {
        str(status): consultas.contar_jobs(conn, status=str(status))
        for status in carteira.STATUS_CARTEIRA
    }
    itens = consultas.jobs(conn, limite=1, offset=1)
    paginacao = carteira.pagina_contexto(
        pagina=2, limite=1, total=consultas.contar_jobs(conn), filtros={}
    )

    assert carteira.metricas(contagens, 3) == {
        "total_fila": 2,
        "total_filtrado": 3,
        "concluidos": 1,
        "retry": 1,
        "erros": 0,
        "em_execucao": 0,
        "total_fila_fmt": "2",
        "total_filtrado_fmt": "3",
        "concluidos_fmt": "1",
        "retry_fmt": "1",
        "erros_fmt": "0",
        "em_execucao_fmt": "0",
    }
    assert len(itens) == 1
    assert paginacao["inicio"] == 2
    assert paginacao["fim"] == 2
    assert paginacao["paginas"] == 3


def test_desfecho_insuficiente_aparece_como_resultado_de_negocio():
    assert carteira.rotulo_desfecho(Desfecho.PENDENCIA_MANUAL) == (
        "Informações insuficientes"
    )
    assert carteira.classe_desfecho(Desfecho.PENDENCIA_MANUAL) == "alerta"
