"""Como a janela chama o robô."""
from __future__ import annotations

import sys
from pathlib import Path

from cnd.desktop import estado


def test_no_codigo_fonte_chama_o_python_do_ambiente():
    assert estado._comando_base() == [sys.executable, "-m", "cnd.cli"]


def test_empacotado_chama_o_cnd_exe_ao_lado(monkeypatch, tmp_path):
    """O EMISSOR CND.exe não tem console; o robô precisa de um, porque a
    janela lê a saída dele para mostrar no Registro."""
    console = tmp_path / "cnd.exe"
    console.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "EMISSOR CND.exe"))

    assert estado._comando_base() == [str(console)]


def test_empacotado_sem_o_cnd_exe_cai_no_proprio_executavel(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "EMISSOR CND.exe"))

    assert estado._comando_base() == [str(tmp_path / "EMISSOR CND.exe")]


def test_o_robo_recebe_a_codificacao_explicita(monkeypatch):
    """Sem isto, o acento chega trocado no Registro."""
    capturado = {}

    class ProcessoFalso:
        stdout = None

        def poll(self):
            return None

    def popen_falso(comando, **kwargs):
        capturado.update(kwargs)
        return ProcessoFalso()

    monkeypatch.setattr(estado.subprocess, "Popen", popen_falso)
    estado.Robo(Path(".")).iniciar()

    assert capturado["env"]["PYTHONIOENCODING"] == "utf-8"
    assert capturado["encoding"] == "utf-8"
