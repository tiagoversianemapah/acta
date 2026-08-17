"""Testes da vigilância — o que vira e-mail.

O que protegem: que o aviso de lote concluído saia uma vez só, que fila
vazia não seja confundida com travamento, e que o resumo de falhas não
avise duas vezes o mesmo item.
"""
from __future__ import annotations

from cnd.core import fila, tempo
from cnd.core.modelos import Desfecho, ResultadoTentativa
from cnd.orquestrador import vigilancia
from tests.conftest import criar_job


def _concluir(conn, orgao="FAKE", desfecho=Desfecho.NEGATIVA):
    job = fila.reivindicar(conn, orgao)
    tentativa = fila.abrir_tentativa(conn, job, worker=0)
    resultado = ResultadoTentativa(desfecho=desfecho)
    fila.fechar_tentativa(conn, tentativa, resultado)
    fila.concluir(conn, job, resultado)
    return job


class TestLoteConcluido:
    def test_lote_em_andamento_nao_avisa(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        criar_job(conn, lote, "22222222222222")
        _concluir(conn)

        assert vigilancia.lotes_recem_concluidos(conn) == []

    def test_lote_terminado_gera_resumo(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        criar_job(conn, lote, "22222222222222")
        _concluir(conn)
        _concluir(conn, desfecho=Desfecho.CPEN)

        concluidos = vigilancia.lotes_recem_concluidos(conn)

        assert len(concluidos) == 1
        resumo = concluidos[0]
        assert resumo.total == 2
        assert resumo.por_desfecho == {"NEGATIVA": 1, "CPEN": 1}

    def test_avisa_uma_vez_so(self, conn, lote):
        """Sem isso, o lote geraria e-mail a cada minuto, para sempre."""
        criar_job(conn, lote, "11111111111111")
        _concluir(conn)

        assert len(vigilancia.lotes_recem_concluidos(conn)) == 1
        assert vigilancia.lotes_recem_concluidos(conn) == []

    def test_lote_vazio_nao_conta_como_concluido(self, conn, lote):
        assert vigilancia.lotes_recem_concluidos(conn) == []

    def test_resumo_menciona_os_falhados(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        job = fila.reivindicar(conn, "FAKE")
        fila.falhar(conn, job, Desfecho.ERRO_TECNICO)

        resumo = vigilancia.lotes_recem_concluidos(conn)[0]

        assert resumo.falhados == 1
        # Não pede conferência manual: desde 17/08/2026 o robô devolve esses
        # itens à fila sozinho, então mandar alguém olhar seria trabalho
        # inventado. O resumo diz que ele continua tentando.
        assert "continua tentando sozinho" in resumo.como_texto()


class TestTravamento:
    def test_fila_vazia_nao_e_travamento(self, conn, lote):
        """Serviço terminado não é problema — não pode virar alerta."""
        assert vigilancia.minutos_sem_progresso(conn, "FAKE") is None

    def test_sem_nunca_ter_rodado_nao_acusa(self, conn, lote):
        criar_job(conn, lote)

        assert vigilancia.minutos_sem_progresso(conn, "FAKE") is None

    def test_progresso_recente_da_zero(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        criar_job(conn, lote, "22222222222222")
        _concluir(conn)

        parado_ha = vigilancia.minutos_sem_progresso(conn, "FAKE")

        assert parado_ha is not None and parado_ha < 1

    def test_fila_cheia_e_parado_ha_muito_tempo(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        criar_job(conn, lote, "22222222222222")
        _concluir(conn)
        conn.execute("UPDATE tentativa SET finalizada_em = ?",
                     (tempo.daqui_a(-3600),))

        assert vigilancia.minutos_sem_progresso(conn, "FAKE") > 55


class TestFalhasDefinitivas:
    def _falhar(self, conn, documento):
        job = fila.reivindicar(conn, "FAKE")
        fila.falhar(conn, job, Desfecho.BLOQUEIO_TEMPORARIO)
        return job

    def test_lista_apenas_o_que_e_novo(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        criar_job(conn, lote, "22222222222222")

        self._falhar(conn, "11111111111111")
        marca = tempo.agora_iso()
        self._falhar(conn, "22222222222222")

        novas = vigilancia.falhas_definitivas(conn, "FAKE", marca)

        assert len(novas) == 1, "o item já avisado não pode aparecer de novo"

    def test_concluidos_nao_entram(self, conn, lote):
        criar_job(conn, lote, "11111111111111")
        _concluir(conn)

        assert vigilancia.falhas_definitivas(conn, "FAKE", "2000-01-01") == []

    def test_texto_resume_em_vez_de_listar_tudo(self, conn, lote):
        for i in range(40):
            criar_job(conn, lote, f"{i:014d}")
            self._falhar(conn, f"{i:014d}")

        texto = vigilancia.texto_das_falhas(
            vigilancia.falhas_definitivas(conn, "FAKE", "2000-01-01"))

        assert "40 item(ns)" in texto
        assert "e mais 15" in texto, "e-mail com 40 linhas vira ruído"
        assert "Reenviar itens com falha" in texto


class TestLimiteDeTentativas:
    def test_padrao_e_tres(self):
        from cnd.infra.config import ParametrosRetry

        assert ParametrosRetry().max_tentativas == 3

    def test_config_do_projeto_usa_tres(self):
        from cnd.infra.config import carregar

        for orgao in carregar().orgaos.values():
            assert orgao.retry.max_tentativas <= 3, \
                f"{orgao.codigo} deveria desistir em no máximo 3 tentativas"
