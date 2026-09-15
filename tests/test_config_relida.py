"""O painel relê o config.toml quando ele muda.

Ler uma vez só na importação fazia o painel discordar do robô: em
15/09/2026, o SEFAZ-ES ficou com o ritmo preso em 300s porque o arquivo
já dizia 10s e o processo do painel ainda respondia pelos valores
velhos — inclusive na normalização do ritmo do início manual.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

BASE = """
[geral]
banco = "{banco}"

[rede]
nome = "teste"
senha = "segredo"

[orgaos.SEFAZ_ES]
ativo = true
adapter = "sefaz_es"

[orgaos.SEFAZ_ES.pacing]
intervalo_inicial_s = {ritmo}
intervalo_piso_s    = {ritmo}

[orgaos.SEFAZ_ES.breaker]
captchas_para_abrir = {captchas}
"""


BANCO = "data/cnd.db"


@pytest.fixture
def painel(monkeypatch, tmp_path):
    """O módulo do painel apontado para um config.toml descartável."""
    global BANCO
    BANCO = (tmp_path / "cnd.db").as_posix()
    arquivo = tmp_path / "config.toml"
    arquivo.write_text(BASE.format(banco=BANCO, ritmo=10.0, captchas=1),
                       encoding="utf-8")
    monkeypatch.setenv("CND_CONFIG", str(arquivo))

    from cnd.infra import config as modulo_config
    importlib.reload(modulo_config)
    from cnd.web import app as modulo
    importlib.reload(modulo)
    yield modulo, arquivo
    # Devolve os módulos ao estado do resto da suíte.
    monkeypatch.undo()
    importlib.reload(modulo_config)
    importlib.reload(modulo)


def _ritmo(modulo) -> float:
    return modulo.config_atual().orgaos["SEFAZ_ES"].pacing.intervalo_inicial_s


def test_editar_o_arquivo_muda_o_que_o_painel_responde(painel):
    modulo, arquivo = painel
    assert _ritmo(modulo) == 10.0

    arquivo.write_text(BASE.format(banco=BANCO, ritmo=7.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 7.0


def test_dois_salvamentos_no_mesmo_instante_nao_passam_batido(painel):
    """A data do arquivo não serve de marca: dois writes seguidos saem com
    `st_mtime_ns` idêntico no Windows, que é o caso de um editor que grava
    um temporário e renomeia por cima."""
    modulo, arquivo = painel
    arquivo.write_text(BASE.format(banco=BANCO, ritmo=7.0, captchas=1), encoding="utf-8")
    assert _ritmo(modulo) == 7.0

    # Sem pausa nenhuma entre um e outro.
    arquivo.write_text(BASE.format(banco=BANCO, ritmo=3.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 3.0


def test_config_quebrada_mantem_a_anterior_e_nao_derruba_o_painel(painel):
    modulo, arquivo = painel
    arquivo.write_text(BASE.format(banco=BANCO, ritmo=7.0, captchas=1), encoding="utf-8")
    assert _ritmo(modulo) == 7.0

    arquivo.write_text("isto [ nao e toml", encoding="utf-8")

    assert _ritmo(modulo) == 7.0


def test_config_consertada_volta_a_valer(painel):
    modulo, arquivo = painel
    arquivo.write_text("isto [ nao e toml", encoding="utf-8")
    assert _ritmo(modulo) == 10.0

    arquivo.write_text(BASE.format(banco=BANCO, ritmo=5.0, captchas=1), encoding="utf-8")

    assert _ritmo(modulo) == 5.0


def test_arquivo_sumido_mantem_a_ultima_config_boa(painel):
    modulo, arquivo = painel
    assert _ritmo(modulo) == 10.0

    Path(arquivo).unlink()

    assert _ritmo(modulo) == 10.0


def test_inicio_manual_enxerga_o_ritmo_novo_sem_reiniciar_o_painel(
    monkeypatch, painel, tmp_path
):
    """O caso que custou a tarde de 15/09/2026, ponta a ponta.

    Painel de pe com a config velha (300s), ritmo do SEFAZ-ES castigado em
    255s, alguem corrige o arquivo para 10s e reinicia SO o robo. Antes,
    `_normalizar_ritmo_para_inicio` comparava 255 contra o limite antigo de
    420s, achava que estava dentro e nao regravava nada - o botao Iniciar
    rodava e legitimamente nao fazia nada.

    O banco vai DENTRO do config, e nao por monkeypatch em `modulo.cfg`:
    reler a config troca esse global, que e justamente o ponto do teste.
    """
    import fastapi.testclient as fastapi_testclient

    from cnd.infra.db import conectar, criar_schema
    from cnd.web import comandos

    modulo, arquivo = painel
    criar_schema(conectar(Path(BANCO)))

    arquivo.write_text(BASE.format(banco=BANCO, ritmo=300.0, captchas=1),
                       encoding="utf-8")
    monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                        lambda: True)
    monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: False)
    monkeypatch.setattr(comandos, "_iniciar_robo_visual",
                        lambda _raiz: (True, "iniciado"))

    conn = conectar(Path(BANCO))
    try:
        conn.execute(
            "INSERT INTO ritmo (orgao, intervalo_s, consultas_limpas, "
            "atualizado_em) VALUES ('SEFAZ_ES', 255, 0, '2026-09-15T00:00:00Z')"
        )
    finally:
        conn.close()

    cliente = fastapi_testclient.TestClient(modulo.app)

    def ritmo_gravado() -> float:
        conn = conectar(Path(BANCO))
        try:
            return conn.execute(
                "SELECT intervalo_s FROM ritmo WHERE orgao = 'SEFAZ_ES'"
            ).fetchone()["intervalo_s"]
        finally:
            conn.close()

    # Com 300s no arquivo o limite e 420: 255 passa por baixo, nada muda.
    assert cliente.post("/api/robo/iniciar",
                        headers={"X-CND-Senha": "segredo"}).status_code == 200
    assert ritmo_gravado() == 255.0

    # Corrige o arquivo e NAO reinicia o painel.
    arquivo.write_text(BASE.format(banco=BANCO, ritmo=10.0, captchas=1),
                       encoding="utf-8")

    assert cliente.post("/api/robo/iniciar",
                        headers={"X-CND-Senha": "segredo"}).status_code == 200
    assert ritmo_gravado() == 10.0
