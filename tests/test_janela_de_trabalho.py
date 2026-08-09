"""O robô roda por temporada, não é serviço de pé o ano inteiro.

Ficar parado é o estado NORMAL em uns 28 dias de cada 30. Cobrar sinal de
vida fora da janela de trabalho encheria o canal de alarme falso — e canal
com alarme falso deixa de ser lido, levando junto o aviso que importava.
"""
from __future__ import annotations

import pytest
from tests.conftest import criar_job

from cnd.core import fila
from cnd.core.modelos import Desfecho, ResultadoTentativa, Status
from cnd.desktop.remoto import EstadoRemoto
from cnd.infra.config import Maquina
from cnd.web import consultas

fastapi_testclient = pytest.importorskip("fastapi.testclient")


class TestContagemDaFila:
    def test_conta_o_que_espera_consulta(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181")
        criar_job(conn, lote, documento="11444777000161")
        assert consultas.pendentes(conn) == 2

    def test_item_concluido_sai_da_conta(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181")
        job = fila.reivindicar(conn, "FAKE")
        fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.NEGATIVA))

        assert consultas.pendentes(conn) == 0

    def test_item_esperando_nova_tentativa_ainda_conta(self, conn, lote):
        """RETRY_WAIT é trabalho a fazer: o robô precisa estar de pé."""
        criar_job(conn, lote, documento="11222333000181")
        conn.execute("UPDATE job SET status = ?", (Status.RETRY_WAIT,))
        assert consultas.pendentes(conn) == 1

    def test_banco_vazio_nao_tem_fila(self, conn):
        assert consultas.pendentes(conn) == 0


@pytest.fixture
def painel(monkeypatch, tmp_path):
    """Painel apontado para um banco vazio, sem senha."""
    from dataclasses import replace

    from cnd.infra.config import ConfigRede, carregar
    from cnd.infra.db import conectar, criar_schema
    from cnd.web import app as modulo

    banco = tmp_path / "cnd.db"
    conexao = conectar(banco)
    criar_schema(conexao)

    monkeypatch.setattr(modulo, "cfg",
                        replace(carregar(), banco=banco, rede=ConfigRede()))
    with fastapi_testclient.TestClient(modulo.app) as cliente:
        yield cliente, conexao
    conexao.close()


def _encher_a_fila(conn) -> None:
    conn.execute("INSERT INTO lote (id, descricao) VALUES (1, 'teste')")
    criar_job(conn, 1, documento="11222333000181")


class TestPing:
    """Devolve 503 só quando há trabalho parado.

    Antes era 503 sempre que o robô estivesse parado — o verificador
    externo chamaria todo dia sem motivo, até alguém desligá-lo, e aí ele
    estaria mudo justamente no dia em que importasse.
    """

    def test_fila_vazia_e_robo_parado_sao_200(self, painel):
        cliente, _ = painel
        resposta = cliente.get("/ping")

        assert resposta.status_code == 200
        assert resposta.json()["robo"] == "ocioso"
        assert resposta.json()["ok"] is True

    def test_fila_com_trabalho_e_robo_mudo_sao_503(self, painel):
        cliente, conn = painel
        _encher_a_fila(conn)

        resposta = cliente.get("/ping")

        assert resposta.status_code == 503
        assert resposta.json()["robo"] == "parado"
        assert "1 itens na fila" in resposta.json()["motivo"]

    def test_fila_com_trabalho_e_robo_vivo_sao_200(self, painel):
        from cnd.infra import heartbeat

        cliente, conn = painel
        _encher_a_fila(conn)
        heartbeat.bater(conn, "orquestrador")

        resposta = cliente.get("/ping")

        assert resposta.status_code == 200
        assert resposta.json()["robo"] == "em execução"

    def test_sempre_informa_o_tamanho_da_fila(self, painel):
        cliente, conn = painel
        _encher_a_fila(conn)
        assert cliente.get("/ping").json()["itens_na_fila"] == 1


def _maquina(pendentes: int = 0, ativo: bool = False,
             disjuntor: str = "FECHADO", online: bool = True) -> EstadoRemoto:
    return EstadoRemoto(
        Maquina("PC-01", "http://x"), online=online,
        dados={"robo_ativo": ativo, "orgaos": [{
            "orgao": "RFB_PJ", "total": 10, "concluidos": 0, "falhados": 0,
            "pendentes": pendentes, "por_desfecho": {},
            "disjuntor": disjuntor}]})


class TestSituacaoNaTela:
    def test_parado_sem_fila_e_ocioso_e_verde(self):
        """Vermelho o mês inteiro ensina a ignorar o vermelho.

        Verde e não cinza: a fila limpa é o estado saudável do mês, não uma
        incógnita — a máquina fez o que tinha para fazer.
        """
        assert _maquina(pendentes=0).situacao == ("Ociosa", "verde")

    def test_parado_com_fila_e_vermelho(self):
        assert _maquina(pendentes=5).situacao == ("Parada com fila", "vermelho")

    def test_trabalhando_e_verde(self):
        assert _maquina(pendentes=5, ativo=True).situacao == ("Trabalhando",
                                                              "verde")

    def test_suspensa_vem_antes_de_qualquer_coisa(self):
        """O disjuntor aberto explica tudo o mais que se veja no cartão."""
        estado = _maquina(pendentes=5, ativo=True, disjuntor="ABERTO")
        assert estado.situacao == ("Suspensa", "ambar")

    def test_sem_resposta_e_cinza(self):
        assert _maquina(online=False).situacao == ("Sem resposta", "cinza")


class TestOrdenacaoPorGravidade:
    def test_a_quebrada_vem_primeiro(self):
        maquinas = [
            _maquina(pendentes=0),                       # ociosa
            _maquina(pendentes=5, ativo=True),           # trabalhando
            _maquina(online=False),                      # sem resposta
            _maquina(pendentes=5),                       # parada com fila
            _maquina(pendentes=5, ativo=True, disjuntor="ABERTO"),
        ]
        ordenadas = sorted(maquinas, key=lambda m: m.gravidade)

        assert [m.situacao[0] for m in ordenadas] == [
            "Sem resposta", "Parada com fila", "Suspensa", "Trabalhando",
            "Ociosa"]
