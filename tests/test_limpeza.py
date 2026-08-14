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


def test_o_proximo_lote_volta_a_ser_o_numero_1(conn, tmp_path):
    """Ids reiniciam porque a máquina zerada não tem passado: "lote #4"
    numa tela vazia só faria alguém procurar os três anteriores."""
    certidoes, evidencias = _com_trabalho(conn, tmp_path)

    limpeza.zerar(conn, (certidoes, evidencias))
    novo = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('novo', 'n.xlsx')"
    ).lastrowid

    assert novo == 1
