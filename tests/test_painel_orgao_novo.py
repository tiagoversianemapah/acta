"""Ligar uma automação nova não pode derrubar a tela.

Em 18/08/2026 o CRF foi ligado na PC 01, recebeu 2.041 itens, e o
/api/estado passou a devolver 500: "attempt to write a readonly database".
A causa era `breaker.consultar` chamando `_garantir`, que INSERE a linha do
órgão quando ela não existe — numa conexão que o painel abre só para ler.

A Receita continuava funcionando porque a linha dela existia desde agosto,
então o defeito só aparecia com órgão novo. E a tela não dizia "erro": ela
dizia "Sem fila carregada", que é o oposto da verdade.
"""
from __future__ import annotations

import pytest

from cnd.core import breaker
from cnd.infra.db import conectar_leitura, garantir
from cnd.web import consultas


@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "cnd.db"
    garantir(caminho)
    return caminho


def test_resumo_de_orgao_novo_nao_escreve(banco):
    """O caso exato do 500, pelo mesmo caminho: resumo -> breaker."""
    with conectar_leitura(banco) as conn:
        r = consultas.resumo(conn, "CRF", None)   # não pode levantar
    assert r.total == 0


def test_consultar_leitura_nao_cria_linha(banco):
    with conectar_leitura(banco) as conn:
        estado = breaker.consultar_leitura(conn, "CRF")
    assert estado.bloqueado is False, "não ter rodado não é estar bloqueado"

    with conectar_leitura(banco) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM breaker").fetchone()["n"]
    assert n == 0, "ler não pode criar linha"


def test_consultar_normal_continua_criando(banco):
    """Quem escreve depende disso; a correção não pode custar esse caminho."""
    from cnd.infra.db import conectar
    conn = conectar(banco)
    try:
        breaker.consultar(conn, "CRF")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS n FROM breaker "
                         "WHERE orgao = 'CRF'").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1
