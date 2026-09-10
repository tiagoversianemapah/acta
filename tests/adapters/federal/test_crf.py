"""Classificação das telas do portal do CRF.

Os textos abaixo são os que o portal devolveu de verdade em 18/08/2026, não
paráfrases: é o que torna este teste capaz de perceber quando a Caixa mudar
a redação.
"""
from __future__ import annotations

import pytest

from cnd.adapters.federal.crf import classificar
from cnd.core.modelos import CONCLUSIVOS, Desfecho

REGULAR = (
    "Dúvidas mais Frequentes | Início | V - 2.3 Situação de Regularidade do "
    "Empregador A empresa abaixo identificada esta REGULAR no FGTS. "
    "Inscrição: 33.949.051/0001-13 Razão social: ABL GRAN LIFE "
    "EMPREENDIMENTOS IMOBILIARIOS LTDA Resultado da consulta em 18/08/2026"
)
IRREGULAR = (
    "Situação de Regularidade do Empregador Inscrição: 08.068.098/0002-01 "
    "Não foi possível verificar a regularidade junto à CAIXA. Solicitamos "
    "tentar mais tarde. Caso persista solicitamos comparecer a uma das "
    "Agências da CAIXA para obter esclarecimentos adicionais."
)
INVALIDO = (
    "Dúvidas mais Frequentes | Início | V - 2.3 Inscrição: informar o CNPJ "
    "correto Consulta Regularidade do Empregador Estar regular perante o FGTS"
)


def test_regular_vira_negativa():
    assert classificar(REGULAR) is Desfecho.NEGATIVA


def test_irregular_vira_positiva_e_nao_retenta():
    """O "tentar mais tarde" da tela engana: é assim que o portal diz que a
    empresa está irregular (confirmado com quem opera, 18/08/2026).

    Se virasse retentável, toda empresa com débito de FGTS queimaria as três
    tentativas e ainda terminaria sem resposta no relatório.
    """
    assert classificar(IRREGULAR) is Desfecho.POSITIVA
    assert Desfecho.POSITIVA in CONCLUSIVOS


def test_cnpj_recusado_vira_pendencia_manual():
    """O banner fica ACIMA do título — procurar a partir dele não acha."""
    assert classificar(INVALIDO) is Desfecho.PENDENCIA_MANUAL


def test_tela_desconhecida_vira_erro_tecnico():
    """Melhor guardar evidência do que chutar um desfecho."""
    assert classificar("qualquer outra coisa") is Desfecho.ERRO_TECNICO


@pytest.mark.parametrize("variante", [
    "A empresa abaixo identificada esta REGULAR no FGTS.",
    "A empresa abaixo identificada está regular no FGTS.",
    "A EMPRESA ABAIXO IDENTIFICADA ESTA REGULAR NO FGTS.",
])
def test_acento_e_caixa_nao_mudam_a_leitura(variante):
    """A tela veio com "esta" sem crase; a Caixa pode corrigir amanhã."""
    assert classificar(variante) is Desfecho.NEGATIVA


def test_nao_confunde_irregular_com_o_texto_da_pagina_inicial():
    """A home diz "Estar regular perante o FGTS é condição obrigatória".

    Casar frouxo por "regular" classificaria o formulário vazio como
    certidão negativa — o pior erro possível aqui.
    """
    inicial = ("Consulta Regularidade do Empregador Estar regular perante o "
               "FGTS é condição obrigatória para que o empregador possa "
               "relacionar-se com os órgãos da Administração Pública")
    assert classificar(inicial) is Desfecho.ERRO_TECNICO
