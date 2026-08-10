"""Faz o painel subir sozinho quando alguém entra no Windows.

Sem isto, o painel depende de uma janela de terminal aberta: morre no
primeiro reinício, ou quando alguém fecha a janela sem querer. Numa
máquina que fica sozinha num canto emitindo certidão o mês inteiro, isso
acontece o tempo todo — e do outro lado o ACTA só consegue dizer "sem
resposta", sem explicar que ninguém subiu o programa.

São duas coisas diferentes, e a diferença importa:

- O ROBÔ precisa de sessão logada de verdade: ele digita na tela, sem
  área de trabalho não clica em nada. Pasta de Inicialização serve.
- O PAINEL só lê o banco e responde. Não precisa de tela. Pode subir no
  boot, como tarefa do sistema, antes de qualquer logon — e é o que
  mantém a máquina visível no ACTA 24 horas, mesmo com ninguém dentro.

O painel dizer "estou de pé" não significa que o robô consegue trabalhar;
por isso as rotas de escrita conferem `area_de_trabalho_disponivel()`
antes de aceitar um "iniciar robô". Sem essa checagem, o console mandaria
o robô rodar numa máquina trancada e nada aconteceria, em silêncio.
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


NOME_DA_TAREFA = "ACTA Painel"


def instalar_no_boot() -> str:
    """O painel como tarefa do sistema: sobe no boot, sem depender de logon.

    Roda como SYSTEM para não guardar senha de ninguém em lugar nenhum —
    uma tarefa "rodar mesmo sem logon" com conta de usuário exige gravar a
    credencial, e senha de operador guardada em máquina que fica sozinha
    num canto é dívida que não vale a pena.

    Exige prompt de administrador; sem ele o Windows recusa e dizemos por
    quê, em vez de falhar com o texto cru do schtasks.
    """
    alvo, argumentos = _executavel_do_painel()
    comando = f'"{alvo}" {argumentos}'
    fim = subprocess.run(
        ["schtasks", "/Create", "/TN", NOME_DA_TAREFA, "/TR", comando,
         "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"],
        capture_output=True, text=True)
    if fim.returncode != 0:
        saida = (fim.stderr or fim.stdout).strip()
        if "negado" in saida.lower() or "denied" in saida.lower():
            raise PermissionError(
                "O Windows recusou: isto precisa de um Prompt de Comando "
                "aberto como administrador.")
        raise RuntimeError(saida or "o schtasks falhou sem dizer por quê")
    return NOME_DA_TAREFA


def remover_do_boot() -> bool:
    """Tira a tarefa. Devolve se havia algo para tirar."""
    fim = subprocess.run(
        ["schtasks", "/Delete", "/TN", NOME_DA_TAREFA, "/F"],
        capture_output=True, text=True)
    return fim.returncode == 0


def remover() -> bool:
    """Tira o atalho. Devolve se havia algo para tirar."""
    atalho = pasta_de_inicializacao() / NOME_DO_ATALHO
    if not atalho.exists():
        return False
    atalho.unlink()
    return True


def instalado() -> bool:
    return (pasta_de_inicializacao() / NOME_DO_ATALHO).exists()
