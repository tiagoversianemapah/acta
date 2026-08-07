"""Testes da política de espera entre tentativas.

O que protegem: que cada tipo de falha tenha o tempo de recuo adequado.
Esperar 1 hora por um erro de rede desperdiça o dia; esperar 2 minutos
depois de um captcha reforça o sinal de robô.
"""
from __future__ import annotations

from cnd.core.modelos import BLOQUEIOS, CONCLUSIVOS, RETENTAVEIS, Desfecho
from cnd.infra.config import ParametrosRetry

P = ParametrosRetry(
    max_tentativas=4,
    backoff_erro_s=(120, 600, 2700),
    backoff_captcha_s=(3600, 14400, 43200),
    backoff_bloqueio_s=(300, 900, 1800),
)


class TestBackoff:
    def test_erro_tecnico_recua_pouco(self):
        assert P.espera(Desfecho.ERRO_TECNICO, 1) == 120
        assert P.espera(Desfecho.ERRO_TECNICO, 2) == 600

    def test_captcha_recua_muito(self):
        assert P.espera(Desfecho.CAPTCHA, 1) == 3600

    def test_bloqueio_recua_minutos(self):
        """O portal pede 'alguns minutos' — esperar horas seria exagero."""
        assert P.espera(Desfecho.BLOQUEIO_TEMPORARIO, 1) == 300
        assert P.espera(Desfecho.BLOQUEIO_TEMPORARIO, 2) == 900

    def test_bloqueio_e_captcha_tem_esperas_diferentes(self):
        assert (P.espera(Desfecho.BLOQUEIO_TEMPORARIO, 1)
                < P.espera(Desfecho.CAPTCHA, 1))

    def test_espera_cresce(self):
        for desfecho in (Desfecho.ERRO_TECNICO, Desfecho.CAPTCHA,
                         Desfecho.BLOQUEIO_TEMPORARIO):
            esperas = [P.espera(desfecho, n) for n in (1, 2, 3)]
            assert esperas == sorted(esperas)

    def test_ultima_faixa_se_repete(self):
        """Mais tentativas que faixas na tabela não pode estourar índice."""
        assert P.espera(Desfecho.ERRO_TECNICO, 99) == 2700

    def test_tentativa_zero_nao_estoura(self):
        assert P.espera(Desfecho.ERRO_TECNICO, 0) == 120


class TestVocabulario:
    def test_conclusivo_e_retentavel_nao_se_misturam(self):
        assert CONCLUSIVOS.isdisjoint(RETENTAVEIS)

    def test_todo_desfecho_esta_classificado(self):
        assert set(Desfecho) == CONCLUSIVOS | RETENTAVEIS

    def test_bloqueios_sao_retentaveis(self):
        assert BLOQUEIOS <= RETENTAVEIS

    def test_erro_tecnico_nao_e_bloqueio(self):
        """Falha nossa ou instabilidade é diferente do portal nos barrando:
        só o segundo caso deve desacelerar o ritmo."""
        assert Desfecho.ERRO_TECNICO not in BLOQUEIOS
