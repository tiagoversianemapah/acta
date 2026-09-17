"""A data de nascimento, da planilha até o robô da Receita PF.

O formulário de CPF da Receita não emite sem ela (visto em 17/09/2026), e o
portal não tem de onde tirá-la: só a planilha sabe. Estes testes cobrem o
caminho inteiro — ler a coluna, recusar a linha sem data, guardar no banco e
entregá-la ao adapter pela fila.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from openpyxl import Workbook

from cnd.core import fila
from cnd.infra.db import conectar, criar_schema
from cnd.ingestao.planilha import importar, ler

CPF_WALDO = "03010236115"
CPF_WALKIRIA = "80539157104"


def _planilha(tmp_path, cabecalho, *linhas, aba="PF"):
    livro = Workbook()
    folha = livro.active
    folha.title = aba
    folha.append(cabecalho)
    for linha in linhas:
        folha.append(list(linha))
    caminho = tmp_path / "pf.xlsx"
    livro.save(caminho)
    return caminho


def test_coluna_com_titulo_de_nascimento_e_lida(tmp_path):
    caminho = _planilha(
        tmp_path, ["Nome", "CPF", "Competência", "Data de Nascimento"],
        ["WALDO", "030.102.361-15", datetime(2026, 9, 1), "22/02/1948"],
        ["WALKIRIA", "805.391.571-04", datetime(2026, 9, 1),
         datetime(1953, 8, 2)],
    )

    leitura = ler(caminho, abas=["PF"], orgao="RFB_PF")

    assert [(i.documento, i.data_nascimento) for i in leitura.itens] == [
        (CPF_WALDO, date(1948, 2, 22)),
        (CPF_WALKIRIA, date(1953, 8, 2)),
    ]
    assert not leitura.rejeitados


def test_sem_coluna_de_nascimento_nenhuma_data_e_adivinhada(tmp_path):
    """As abas da carteira trazem a competência na coluna C. Numa planilha
    reimportada meses depois ela já parece nascimento — e o robô a digitaria."""
    caminho = _planilha(
        tmp_path, ["Empresa", "CNPJ", "Competência"],
        ["WALDO", "030.102.361-15", datetime(2025, 1, 1)],
    )

    leitura = ler(caminho, abas=["PF"], orgao="RFB_PF")

    assert leitura.itens == []
    assert "não tem a coluna 'Data de Nascimento'" in leitura.rejeitados[0].motivo


def test_celula_vazia_tem_motivo_proprio(tmp_path):
    caminho = _planilha(
        tmp_path, ["Nome", "CPF", "Data de Nascimento"],
        ["WALDO", "030.102.361-15", None],
        ["WALKIRIA", "805.391.571-04", "ontem"],
    )

    leitura = ler(caminho, abas=["PF"], orgao="RFB_PF")

    assert leitura.itens == []
    assert [r.motivo for r in leitura.rejeitados] == [
        "data de nascimento vazia ou ilegível (use dd/mm/aaaa)"] * 2


def test_data_no_futuro_e_recusada(tmp_path):
    caminho = _planilha(
        tmp_path, ["Nome", "CPF", "Nascimento"],
        ["WALDO", "030.102.361-15", "22/02/2948"],
    )

    leitura = ler(caminho, abas=["PF"], orgao="RFB_PF")

    assert leitura.itens == []
    assert "no futuro" in leitura.rejeitados[0].motivo


def test_outras_automacoes_nao_exigem_nascimento(tmp_path):
    """O FGTS e as SEFAZ não pedem a data: recusar ali seria inventar regra."""
    caminho = _planilha(
        tmp_path, ["Empresa", "CNPJ"],
        ["EMPRESA A", "11.222.333/0001-81"],
    )

    leitura = ler(caminho, abas=["PF"], orgao="CRF")

    assert [i.documento for i in leitura.itens] == ["11222333000181"]


def test_banco_guarda_a_data_e_a_fila_entrega_ao_adapter(tmp_path, conn):
    caminho = _planilha(
        tmp_path, ["Nome", "CPF", "Data de Nascimento"],
        ["WALDO", "030.102.361-15", "22/02/1948"],
    )
    importar(conn, caminho, "PF", abas=["PF"], orgao="RFB_PF")

    job = fila.reivindicar(conn, "RFB_PF")

    assert job.doc.documento == CPF_WALDO
    assert job.doc.tipo == "CPF"
    assert job.doc.data_nascimento == date(1948, 2, 22)


def test_banco_antigo_ganha_a_coluna_ao_abrir(tmp_path):
    """Máquina atualizada abre o banco de antes da coluna existir."""
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.execute(
        "CREATE TABLE empresa (id INTEGER PRIMARY KEY, documento TEXT NOT NULL "
        "UNIQUE, tipo_documento TEXT NOT NULL, nome TEXT NOT NULL)"
    )
    antigo.execute("INSERT INTO empresa (documento, tipo_documento, nome) "
                   "VALUES ('11222333000181', 'CNPJ', 'EMPRESA A')")
    antigo.commit()
    antigo.close()

    conexao = conectar(caminho)
    criar_schema(conexao)
    criar_schema(conexao)       # abrir de novo não tenta criar outra vez

    colunas = {linha[1] for linha in conexao.execute("PRAGMA table_info(empresa)")}
    assert "data_nascimento" in colunas
    assert conexao.execute("SELECT nome FROM empresa").fetchone()[0] == "EMPRESA A"
    conexao.close()


def test_titulo_vindo_de_sistema_e_reconhecido(tmp_path):
    for titulo in ("DT_NASCIMENTO", "DataNascimento", "Nasc."):
        caminho = _planilha(
            tmp_path, ["Nome", "CPF", titulo],
            ["WALDO", "030.102.361-15", "22/02/1948"],
        )

        leitura = ler(caminho, abas=["PF"], orgao="RFB_PF")

        assert leitura.itens[0].data_nascimento == date(1948, 2, 22), titulo


def test_migracao_simultanea_nao_derruba_quem_chega_segundo(tmp_path):
    """Aplicativo e robô sobem juntos; os dois veem a coluna faltando e o
    segundo ALTER encontra a coluna que o primeiro acabou de criar."""
    from cnd.infra import db

    primeiro = conectar(tmp_path / "junto.db")
    criar_schema(primeiro)

    class VistaAntiga:
        """Conexão que leu o PRAGMA antes de o outro processo migrar."""

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info"):
                return iter([(0, "id"), (1, "documento")])
            return primeiro.execute(sql, *args)

    db._acrescentar_colunas(VistaAntiga())       # não levanta

    primeiro.close()
