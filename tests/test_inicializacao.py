"""O painel tem de subir sozinho quando a máquina liga.

Dependendo de uma janela de terminal aberta, ele morre no primeiro
reinício — e do outro lado o ACTA só consegue dizer "sem resposta", sem
explicar que ninguém subiu o programa.
"""
from __future__ import annotations

import sys

import pytest

from cnd.infra import inicializacao

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="atalho .lnk é coisa do Windows")


@pytest.fixture(autouse=True)
def pasta_falsa(tmp_path, monkeypatch):
    """Nunca na pasta de verdade: o teste não pode mexer no logon de quem
    o roda."""
    monkeypatch.setattr(inicializacao, "pasta_de_inicializacao",
                        lambda: tmp_path / "Startup")


def test_instalar_cria_o_atalho():
    destino = inicializacao.instalar()

    assert destino.exists()
    assert inicializacao.instalado()


def test_instalar_duas_vezes_nao_duplica():
    """Reinstalar é o caminho normal depois de atualizar a pasta."""
    inicializacao.instalar()
    inicializacao.instalar()

    assert len(list(inicializacao.pasta_de_inicializacao().iterdir())) == 1


def test_o_atalho_abre_o_painel_pela_rede():
    """Em 127.0.0.1 ele sobe e nenhuma outra máquina o alcança — o ACTA
    continuaria dizendo "sem resposta" com o painel de pé."""
    import subprocess

    destino = inicializacao.instalar()
    lido = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$w=New-Object -ComObject WScript.Shell;"
         f"$a=$w.CreateShortcut('{destino}');"
         f"$a.Arguments; $a.WindowStyle"],
        capture_output=True, text=True, check=True).stdout

    assert "painel" in lido
    assert "--host 0.0.0.0" in lido
    assert "7" in lido, "janela minimizada: janela preta aberta convida a fechar"


def test_tarefa_de_boot_sobe_como_sistema_e_pela_rede(monkeypatch):
    """Sem ONSTART/SYSTEM ela volta a depender de logon; sem 0.0.0.0 o
    painel sobe e nenhuma outra máquina o alcança."""
    vistos = {}

    def fingir(argv, **_k):
        vistos["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(inicializacao.subprocess, "run", fingir)
    inicializacao.instalar_no_boot()

    argv = vistos["argv"]
    assert argv[argv.index("/SC") + 1] == "ONSTART"
    assert argv[argv.index("/RU") + 1] == "SYSTEM"
    assert "--host 0.0.0.0" in argv[argv.index("/TR") + 1]


def test_acesso_negado_explica_o_que_fazer(monkeypatch):
    """O texto cru do schtasks não diz a quem o lê o que ele deve fazer."""
    monkeypatch.setattr(
        inicializacao.subprocess, "run",
        lambda *a, **k: type("R", (), {
            "returncode": 1, "stdout": "", "stderr": "ERRO: Acesso negado."})())

    with pytest.raises(PermissionError, match="administrador"):
        inicializacao.instalar_no_boot()


def test_remover_desfaz():
    inicializacao.instalar()

    assert inicializacao.remover() is True
    assert not inicializacao.instalado()


def test_remover_o_que_nao_existe_nao_quebra():
    assert inicializacao.remover() is False
