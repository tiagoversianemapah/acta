"""Faz o painel subir sozinho quando alguém entra no Windows.

Sem isto, o painel depende de uma janela de terminal aberta: morre no
primeiro reinício, ou quando alguém fecha a janela sem querer. Numa
máquina que fica sozinha num canto emitindo certidão o mês inteiro, isso
acontece o tempo todo — e do outro lado o ACTA só consegue dizer "sem
resposta", sem explicar que ninguém subiu o programa.

Pasta de Inicialização, e não tarefa agendada, de propósito: o robô cego
digita na tela de verdade, então precisa de uma sessão com alguém logado.
Uma tarefa "rodar mesmo sem logon" subiria o painel numa sessão sem área
de trabalho — ele responderia, diria que está tudo bem, e o robô não
conseguiria clicar em nada.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NOME_DO_ATALHO = "ACTA Painel.lnk"


def pasta_de_inicializacao() -> Path:
    """A pasta que o Windows abre em `shell:startup`."""
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def _executavel_do_painel() -> tuple[str, str]:
    """O que chamar e com quais argumentos, empacotado ou pela fonte."""
    if getattr(sys, "frozen", False):
        # cnd.exe, ao lado do ACTA.exe na pasta instalada.
        return str(Path(sys.executable).parent / "cnd.exe"), "painel --host 0.0.0.0"
    # Pela fonte, pythonw em vez de python: o "w" é o que evita a janela
    # preta piscando na cara de quem acabou de ligar a máquina.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    alvo = pythonw if pythonw.exists() else Path(sys.executable)
    # cnd.cli, não cnd: o pacote não tem __main__ e `-m cnd` morre com
    # "cannot be directly executed" — num atalho de logon, calado.
    return str(alvo), "-m cnd.cli painel --host 0.0.0.0"


def instalar() -> Path:
    """Cria o atalho. Devolve onde ficou. Idempotente: refaz por cima."""
    alvo, argumentos = _executavel_do_painel()
    destino = pasta_de_inicializacao() / NOME_DO_ATALHO
    destino.parent.mkdir(parents=True, exist_ok=True)

    # WindowStyle 7 = minimizado. O painel é console: sem isto, a máquina
    # liga com uma janela preta aberta no meio da tela, convidando a ser
    # fechada — que é justamente o que se quer evitar.
    script = f"""
$w = New-Object -ComObject WScript.Shell
$a = $w.CreateShortcut('{destino}')
$a.TargetPath = '{alvo}'
$a.Arguments = '{argumentos}'
$a.WorkingDirectory = '{Path(alvo).parent}'
$a.WindowStyle = 7
$a.Description = 'Painel do ACTA — responde ao aplicativo pela rede'
$a.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", script], check=True,
                   capture_output=True, text=True)
    return destino


def remover() -> bool:
    """Tira o atalho. Devolve se havia algo para tirar."""
    atalho = pasta_de_inicializacao() / NOME_DO_ATALHO
    if not atalho.exists():
        return False
    atalho.unlink()
    return True


def instalado() -> bool:
    return (pasta_de_inicializacao() / NOME_DO_ATALHO).exists()
