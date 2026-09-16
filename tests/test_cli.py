from __future__ import annotations

from types import SimpleNamespace

from cnd.cli import lotes, telas


def test_importar_respeita_config_do_banco(monkeypatch, tmp_path):
    from openpyxl import Workbook

    banco = tmp_path / "isolado" / "cnd.db"
    config_temporario = tmp_path / "config.toml"
    config_temporario.write_text(
        "[geral]\n"
        f'banco = "{banco.as_posix()}"\n'
        "[orgaos.SEFAZ_ES]\n"
        "ativo = true\n"
        'adapter = "sefaz_es"\n',
        encoding="utf-8",
    )
    planilha = tmp_path / "teste.xlsx"
    livro = Workbook()
    aba = livro.active
    aba.title = "ES"
    aba.append(["Empresa", "CNPJ"])
    aba.append(["EMPRESA TESTE", "32.465.841/0001-60"])
    livro.save(planilha)

    resultado = lotes.importar_planilha(SimpleNamespace(
        planilha=planilha,
        descricao="teste",
        abas=["ES"],
        config=config_temporario,
    ))

    assert resultado == 0
    assert banco.exists()

    from cnd.infra.db import conectar

    conn = conectar(banco)
    try:
        assert conn.execute("select count(*) from job").fetchone()[0] == 1
    finally:
        conn.close()


def test_painel_cria_banco_vazio_antes_de_subir(monkeypatch, tmp_path):
    from cnd.infra import config

    banco = tmp_path / "dados" / "cnd.db"
    config_temporario = tmp_path / "config.toml"
    config_temporario.write_text(
        "[geral]\n"
        f'banco = "{banco.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CAMINHO_PADRAO", config_temporario)

    import uvicorn

    chamadas = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: chamadas.append((args, kwargs)))

    assert telas.subir_painel(SimpleNamespace(host="127.0.0.1", porta=8000)) == 0

    assert chamadas
    assert banco.exists()
