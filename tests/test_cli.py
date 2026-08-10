from __future__ import annotations

from types import SimpleNamespace

from cnd import cli


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

    assert cli._painel(SimpleNamespace(host="127.0.0.1", porta=8000)) == 0

    assert chamadas
    assert banco.exists()
