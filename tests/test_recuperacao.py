"""Recuperação automática: o lote tem de fechar sozinho.

O que estes testes protegem é uma promessa de operação — "sempre tem que
concluir tudo" (17/08/2026). Um item só pode parar de ser tentado quando o
portal deu resposta definitiva sobre a empresa; falha de infraestrutura
nunca é motivo para encerrar trabalho.
"""
from __future__ import annotations

import pytest

from cnd.core import fila, recuperacao, tempo
from cnd.core.modelos import Desfecho, Status
from cnd.core.recuperacao import ParametrosRecuperacao
from tests.conftest import criar_job


@pytest.fixture
def p():
    return ParametrosRecuperacao(espera_inicial_s=1800.0, fator=2.0,
                                 espera_maxima_s=21600.0, avisar_apos=5)


class TestEsperaCrescente:
    def test_dobra_a_cada_rodada(self, p):
        assert recuperacao.espera_da_rodada(0, p) == 1800.0
        assert recuperacao.espera_da_rodada(1, p) == 3600.0
        assert recuperacao.espera_da_rodada(2, p) == 7200.0
        assert recuperacao.espera_da_rodada(3, p) == 14400.0

    def test_respeita_o_teto(self, p):
        """Sem teto, a rodada 10 esperaria 21 dias — o lote nunca fecharia."""
        assert recuperacao.espera_da_rodada(4, p) == 21600.0
        assert recuperacao.espera_da_rodada(50, p) == 21600.0

    def test_um_item_travado_custa_poucas_consultas_por_dia(self, p):
        """A razão de ser do teto de 6h: item quebrado não vira martelo."""
        por_dia = 24 * 3600 / recuperacao.espera_da_rodada(99, p)
        assert por_dia == 4


class TestContagem:
    def test_comeca_zerada(self, conn):
        assert recuperacao.estado(conn, "RFB_PJ").virgem

    def test_agendar_nao_consome_rodada(self, conn, p):
        """A 1ª recuperação não é imediata: dá tempo de o portal se curar."""
        estado = recuperacao.agendar(conn, "RFB_PJ", p)

        assert estado.rodadas == 0
        assert estado.proxima_em is not None
        assert not recuperacao.pode_recuperar(conn, "RFB_PJ")

    def test_agendar_duas_vezes_nao_adia_a_recuperacao(self, conn, p):
        """Sem isto, o vigia empurraria a hora a cada rodada de 60s e a
        recuperação nunca aconteceria."""
        primeiro = recuperacao.agendar(conn, "RFB_PJ", p)
        segundo = recuperacao.agendar(conn, "RFB_PJ", p)

        assert primeiro.proxima_em == segundo.proxima_em

    def test_registrar_rodada_conta_e_reagenda(self, conn, p):
        recuperacao.agendar(conn, "RFB_PJ", p)
        estado = recuperacao.registrar_rodada(conn, "RFB_PJ", p)

        assert estado.rodadas == 1
        assert estado.proxima_em > tempo.agora_iso()

    def test_zerar_devolve_ao_estado_virgem(self, conn, p):
        """O lote seguinte não pode herdar a espera de 6h do anterior."""
        recuperacao.agendar(conn, "RFB_PJ", p)
        recuperacao.registrar_rodada(conn, "RFB_PJ", p)
        recuperacao.registrar_rodada(conn, "RFB_PJ", p)

        recuperacao.zerar(conn, "RFB_PJ")

        assert recuperacao.estado(conn, "RFB_PJ").virgem

    def test_nunca_ha_rodada_final(self, conn, p):
        """Não existe 'desisti': depois do aviso ele continua agendando."""
        recuperacao.agendar(conn, "RFB_PJ", p)
        for _ in range(p.avisar_apos + 3):
            estado = recuperacao.registrar_rodada(conn, "RFB_PJ", p)

        assert estado.rodadas > p.avisar_apos
        assert estado.proxima_em is not None


class TestSobrevivenciaAReinicio:
    def test_a_contagem_fica_no_banco(self, conn, p):
        """O robô reinicia a cada atualização. Se a contagem morasse em
        memória, toda publicação zeraria a espera e o robô voltaria a
        martelar o portal de 30 em 30 minutos."""
        recuperacao.agendar(conn, "RFB_PJ", p)
        recuperacao.registrar_rodada(conn, "RFB_PJ", p)

        linha = conn.execute(
            "SELECT rodadas FROM recuperacao WHERE orgao = 'RFB_PJ'"
        ).fetchone()

        assert linha["rodadas"] == 1


class TestVigia:
    """O vigia é quem executa a rodada. Estes testes cobrem o caminho que
    roda de verdade no servidor, de 60 em 60 segundos."""

    def _montar(self, tmp_path, banco, p):
        from cnd.core.breaker import ParametrosBreaker
        from cnd.core.ritmo import ParametrosRitmo
        from cnd.infra.config import (Config, ConfigAlertas, ConfigOrgao,
                                      ParametrosRetry)

        orgao = ConfigOrgao(
            codigo="RFB_PJ", ativo=True, adapter="fake", workers=1,
            pacing=ParametrosRitmo(), breaker=ParametrosBreaker(),
            retry=ParametrosRetry(), recuperacao=p,
        )
        cfg = Config(
            banco=banco,
            pasta_certidoes=tmp_path / "certidoes",
            pasta_evidencias=tmp_path / "evidencias",
            pasta_logs=tmp_path / "logs",
            alertas=ConfigAlertas(),      # sem webhook => alertas desligados
            orgaos={"RFB_PJ": orgao},
        )
        return cfg, orgao

    def _falhar(self, conn, lote, documento="00000000000001"):
        job_id = criar_job(conn, lote, documento=documento, orgao="RFB_PJ")
        conn.execute(
            "UPDATE job SET status = ?, desfecho = ?, tentativas = 3 WHERE id = ?",
            (Status.FAILED, str(Desfecho.BLOQUEIO_TEMPORARIO), job_id),
        )
        return job_id

    def test_primeira_passagem_apenas_agenda(self, conn, lote, tmp_path, p):
        """Não recupera na hora: o problema do portal precisa de tempo."""
        from cnd.orquestrador.loop import Vigia

        job_id = self._falhar(conn, lote)
        cfg, orgao = self._montar(tmp_path, tmp_path / "t.db", p)

        Vigia(cfg, [orgao])._recuperar_falhas(conn, orgao)

        assert conn.execute(
            "SELECT status FROM job WHERE id = ?", (job_id,)
        ).fetchone()["status"] == Status.FAILED
        assert recuperacao.estado(conn, "RFB_PJ").proxima_em is not None

    def test_recupera_quando_a_hora_chega(self, conn, lote, tmp_path, p):
        from cnd.orquestrador.loop import Vigia

        job_id = self._falhar(conn, lote)
        cfg, orgao = self._montar(tmp_path, tmp_path / "t.db", p)
        vigia = Vigia(cfg, [orgao])

        vigia._recuperar_falhas(conn, orgao)          # agenda
        conn.execute("UPDATE recuperacao SET proxima_em = ? WHERE orgao = ?",
                     (tempo.daqui_a(-1), "RFB_PJ"))
        vigia._recuperar_falhas(conn, orgao)          # executa

        linha = conn.execute(
            "SELECT status, tentativas FROM job WHERE id = ?", (job_id,)
        ).fetchone()
        assert linha["status"] == Status.PENDING
        assert linha["tentativas"] == 0
        assert recuperacao.estado(conn, "RFB_PJ").rodadas == 1

    def test_nao_recupera_com_a_fila_andando(self, conn, lote, tmp_path, p):
        """Reenfileirar no meio do lote bagunçaria a ordem sem necessidade."""
        from cnd.orquestrador.loop import Vigia

        job_id = self._falhar(conn, lote)
        criar_job(conn, lote, documento="00000000000002", orgao="RFB_PJ")
        cfg, orgao = self._montar(tmp_path, tmp_path / "t.db", p)

        Vigia(cfg, [orgao])._recuperar_falhas(conn, orgao)

        assert conn.execute(
            "SELECT status FROM job WHERE id = ?", (job_id,)
        ).fetchone()["status"] == Status.FAILED

    def test_lote_fechado_zera_a_contagem(self, conn, lote, tmp_path, p):
        """Sem isto o próximo lote herdaria a espera de 6h deste."""
        from cnd.orquestrador.loop import Vigia

        cfg, orgao = self._montar(tmp_path, tmp_path / "t.db", p)
        recuperacao.agendar(conn, "RFB_PJ", p)
        recuperacao.registrar_rodada(conn, "RFB_PJ", p)

        Vigia(cfg, [orgao])._recuperar_falhas(conn, orgao)   # sem falhados

        assert recuperacao.estado(conn, "RFB_PJ").virgem

    def test_desligavel_pelo_config(self, conn, lote, tmp_path):
        from cnd.orquestrador.loop import Vigia

        job_id = self._falhar(conn, lote)
        cfg, orgao = self._montar(tmp_path, tmp_path / "t.db",
                                  ParametrosRecuperacao(ativa=False))

        Vigia(cfg, [orgao])._recuperar_falhas(conn, orgao)

        assert conn.execute(
            "SELECT status FROM job WHERE id = ?", (job_id,)
        ).fetchone()["status"] == Status.FAILED
        assert recuperacao.estado(conn, "RFB_PJ").virgem


class TestIntegracaoComAFila:
    def test_reenfileirar_devolve_o_que_falhou(self, conn, lote):
        job_id = criar_job(conn, lote, documento="00000000000001", orgao="RFB_PJ")
        conn.execute(
            "UPDATE job SET status = ?, desfecho = ?, tentativas = 3 WHERE id = ?",
            (Status.FAILED, str(Desfecho.BLOQUEIO_TEMPORARIO), job_id),
        )

        devolvidos = fila.reenfileirar_falhados(conn, "RFB_PJ")

        assert devolvidos == 1
        linha = conn.execute(
            "SELECT status, tentativas, desfecho FROM job WHERE id = ?", (job_id,)
        ).fetchone()
        assert linha["status"] == Status.PENDING
        assert linha["tentativas"] == 0
        assert linha["desfecho"] is None


class TestAnteciparEsperas:
    """O robô parado esperando relógio, sem ninguém poder intervir, foi o
    que travou a operação em 17/08/2026."""

    def _esperando(self, conn, lote, documento, daqui_a_s):
        job_id = criar_job(conn, lote, documento=documento, orgao="RFB_PJ")
        conn.execute(
            "UPDATE job SET status = ?, proxima_execucao_em = ?, tentativas = 1 "
            "WHERE id = ?",
            (Status.RETRY_WAIT, tempo.daqui_a(daqui_a_s), job_id),
        )
        return job_id

    def test_traz_para_agora_o_que_esperava(self, conn, lote):
        job_id = self._esperando(conn, lote, "00000000000001", 3600)

        assert fila.antecipar_esperas(conn, "RFB_PJ") == 1
        assert conn.execute(
            "SELECT proxima_execucao_em FROM job WHERE id = ?", (job_id,)
        ).fetchone()["proxima_execucao_em"] <= tempo.agora_iso()

    def test_nao_devolve_chances_extras(self, conn, lote):
        """Quem aperta quer adiantar a fila, não dar mais tentativas."""
        job_id = self._esperando(conn, lote, "00000000000001", 3600)

        fila.antecipar_esperas(conn, "RFB_PJ")

        assert conn.execute(
            "SELECT tentativas FROM job WHERE id = ?", (job_id,)
        ).fetchone()["tentativas"] == 1

    def test_nao_mexe_em_quem_ja_podia_rodar(self, conn, lote):
        self._esperando(conn, lote, "00000000000001", -60)

        assert fila.antecipar_esperas(conn, "RFB_PJ") == 0

    def test_nao_mexe_em_outro_orgao(self, conn, lote):
        self._esperando(conn, lote, "00000000000001", 3600)

        assert fila.antecipar_esperas(conn, "CRF") == 0
