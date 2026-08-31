"""Zerar a máquina apaga o trabalho — e só o trabalho.

O que sobrevive é o que custa caro para refazer: a calibragem (medida tela
a tela, na máquina) e o config.toml. Um reset que leva essas duas junto
transforma "recomeçar o lote" em "reinstalar a máquina".
"""
from __future__ import annotations

from cnd.core.modelos import Status
from cnd.infra import limpeza
from tests.conftest import criar_job


def _com_trabalho(conn, tmp_path):
    lote = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('l', 'l.xlsx')"
    ).lastrowid
    job = criar_job(conn, lote, "11222333000181")
    conn.execute(
        "INSERT INTO tentativa (job_id, numero, iniciada_em, desfecho) "
        "VALUES (?, 1, '2026-08-14T10:00:00.000Z', 'NEGATIVA')",
        (job,),
    )
    conn.execute(
        "INSERT INTO certidao (job_id, tipo, emitida_em, caminho_pdf, sha256) "
        "VALUES (?, 'NEGATIVA', '2026-08-14T10:00:00.000Z', 'x.pdf', 'abc')",
        (job,),
    )
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.DONE, job))

    certidoes = tmp_path / "certidoes"
    (certidoes / "lote-1" / "RFB_PJ").mkdir(parents=True)
    (certidoes / "lote-1" / "RFB_PJ" / "a.pdf").write_bytes(b"%PDF")
    (certidoes / "lote-1" / "RFB_PJ" / "b.pdf").write_bytes(b"%PDF")
    evidencias = tmp_path / "evidencias"
    evidencias.mkdir()
    (evidencias / "print.png").write_bytes(b"x")
    return certidoes, evidencias


def test_zerar_apaga_banco_e_arquivos(conn, tmp_path):
    certidoes, evidencias = _com_trabalho(conn, tmp_path)

    resultado = limpeza.zerar(conn, (certidoes, evidencias))

    assert resultado.arquivos == 3
    for tabela in ("lote", "job", "tentativa", "certidao", "empresa"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {tabela}").fetchone()["n"]
        assert n == 0, f"{tabela} ficou com {n} linha(s)"
    assert list(certidoes.iterdir()) == []
    assert list(evidencias.iterdir()) == []


def test_as_pastas_continuam_existindo(conn, tmp_path):
    """O programa espera encontrá-las: apagá-las faria a próxima emissão
    falhar na hora de salvar o PDF, com a certidão já emitida no portal."""
    certidoes, evidencias = _com_trabalho(conn, tmp_path)

    limpeza.zerar(conn, (certidoes, evidencias))

    assert certidoes.is_dir()
    assert evidencias.is_dir()


def test_a_calibragem_nao_e_tocada(conn, tmp_path):
    certidoes, evidencias = _com_trabalho(conn, tmp_path)
    calibragem = tmp_path / "calibragem"
    calibragem.mkdir()
    medida = calibragem / "rfb_cego.json"
    medida.write_text('{"janela": [0, 0, 2560, 1600]}', encoding="utf-8")

    limpeza.zerar(conn, (certidoes, evidencias))

    assert medida.exists()
    assert "2560" in medida.read_text(encoding="utf-8")


def test_pasta_inexistente_nao_quebra(conn, tmp_path):
    _com_trabalho(conn, tmp_path)

    resultado = limpeza.zerar(conn, (tmp_path / "nao-existe",))

    assert resultado.arquivos == 0


def test_zerar_funciona_com_automacao_estacionada(conn, tmp_path):
    """`fila_controle` referencia `lote`: fora da lista, ela derrubava a
    zeragem inteira com FOREIGN KEY constraint failed.

    E como a limpeza é uma transação só, não sobrava meio banco apagado:
    sobrava o banco intacto e um botão que simplesmente não funcionava,
    em toda máquina onde alguém já tivesse estacionado uma automação.
    """
    from cnd.core import controle

    certidoes, evidencias = _com_trabalho(conn, tmp_path)
    lote = conn.execute("SELECT id FROM lote").fetchone()["id"]
    controle.definir(conn, lote, "FAKE", controle.ESTACIONADA)

    limpeza.zerar(conn, (certidoes, evidencias))

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fila_controle").fetchone()["n"] == 0


def test_zerar_nao_deixa_contagem_de_recuperacao_para_tras(conn, tmp_path):
    """Ela conta rodadas do lote que acabou de ser apagado — mantida,
    mentiria para o robô do lote seguinte."""
    certidoes, evidencias = _com_trabalho(conn, tmp_path)
    conn.execute(
        "INSERT INTO recuperacao (orgao, rodadas, atualizado_em) "
        "VALUES ('RFB_PJ', 2, '2026-08-14T10:00:00.000Z')")

    limpeza.zerar(conn, (certidoes, evidencias))

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM recuperacao").fetchone()["n"] == 0


def test_nenhuma_tabela_do_schema_sobrevive(conn):
    """Não há mais lista escrita à mão — as tabelas vêm do próprio banco.

    A lista fixa envelheceu em silêncio duas vezes: `fila_controle` e
    `recuperacao` nasceram depois dela, e depois um banco de produção
    trouxe uma tabela de schema antigo que ninguém lembrava. Este teste
    garante o que importa no fim: depois de zerar, nada sobra.
    """
    from tests.conftest import criar_job

    cursor = conn.execute("INSERT INTO lote (descricao) VALUES ('x')")
    criar_job(conn, cursor.lastrowid)
    conn.commit()

    limpeza.zerar(conn)

    for tabela in limpeza._tabelas_do_banco(conn):
        assert conn.execute(
            f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0] == 0, tabela


def test_o_proximo_lote_volta_a_ser_o_numero_1(conn, tmp_path):
    """Ids reiniciam porque a máquina zerada não tem passado: "lote #4"
    numa tela vazia só faria alguém procurar os três anteriores."""
    certidoes, evidencias = _com_trabalho(conn, tmp_path)

    limpeza.zerar(conn, (certidoes, evidencias))
    novo = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('novo', 'n.xlsx')"
    ).lastrowid

    assert novo == 1


class TestBancoComSchemaAntigo:
    """`criar_schema` usa CREATE TABLE IF NOT EXISTS e NUNCA remove tabela.

    Um banco de meses atrás carrega tabelas de schemas passados, que a
    versão de hoje não conhece e que ainda apontam para `lote` ou `job`.
    Lista fixa de tabelas não tem como saber disso — e o sintoma é dos
    piores: a transação volta atrás inteira e o botão de zerar simplesmente
    não funciona. Aconteceu em produção em 31/08/2026.
    """

    def _com_tabela_legada(self, conn):
        conn.execute("CREATE TABLE legado_antigo (id INTEGER PRIMARY KEY, "
                     "lote_id INTEGER NOT NULL REFERENCES lote(id))")
        conn.execute("INSERT INTO lote (descricao) VALUES ('julho')")
        conn.execute("INSERT INTO legado_antigo (lote_id) VALUES (1)")
        conn.commit()

    def test_zera_mesmo_com_tabela_que_o_codigo_nao_conhece(self, conn):
        self._com_tabela_legada(conn)

        limpeza.zerar(conn)

        for tabela in ("lote", "legado_antigo"):
            assert conn.execute(
                f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0] == 0, tabela

    def test_a_tabela_legada_aparece_no_relato(self, conn):
        self._com_tabela_legada(conn)

        resultado = limpeza.zerar(conn)

        assert "legado_antigo" in resultado.como_texto()

    def test_a_chave_estrangeira_volta_ligada_depois(self, conn):
        """Desligar a FK é só durante a limpeza. Deixá-la desligada
        transformaria o banco num lugar onde filho sem pai passa."""
        self._com_tabela_legada(conn)

        limpeza.zerar(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_a_lista_vem_do_banco_e_nao_do_codigo(self, conn):
        vistas = limpeza._tabelas_do_banco(conn)

        assert "job" in vistas and "fila_controle" in vistas
        assert not any(t.startswith("sqlite_") for t in vistas)
