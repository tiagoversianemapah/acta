"""Acesso remoto às máquinas do robô, pelo AnyDesk.

O painel diz o que está acontecendo em cada máquina; quando algo precisa de
mão humana — o Edge fechou, o portal pediu captcha, a sessão do Windows
caiu — alguém tem que entrar nela. Sem isto, a pessoa sai do aplicativo,
abre o AnyDesk e procura o número numa lista; com isto, é um clique a
partir do cartão da máquina que já está mostrando o problema.

O número não é senha: para conectar, o AnyDesk ainda pede a senha de acesso
não vigiado ou a confirmação de quem está na outra ponta. Guardá-lo no
config não abre porta nenhuma.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Onde o instalador do AnyDesk costuma deixar o programa. A versão portátil
# não instala nada e não entra em lista nenhuma — por isso a última tentativa
# é o protocolo `anydesk:`, que funciona se o programa já rodou uma vez.
LUGARES = (
    r"%ProgramFiles(x86)%\AnyDesk\AnyDesk.exe",
    r"%ProgramFiles%\AnyDesk\AnyDesk.exe",
    r"%LOCALAPPDATA%\Programs\AnyDesk\AnyDesk.exe",
    r"%APPDATA%\AnyDesk\AnyDesk.exe",
    r"%PROGRAMDATA%\AnyDesk\AnyDesk.exe",
    r"%USERPROFILE%\Downloads\AnyDesk.exe",
    r"%USERPROFILE%\Desktop\AnyDesk.exe",
    r"%USERPROFILE%\OneDrive\Desktop\AnyDesk.exe",
)

RAIZES_DE_BUSCA = (
    "%ProgramFiles(x86)%",
    "%ProgramFiles%",
    "%LOCALAPPDATA%\\Programs",
    "%APPDATA%",
    "%PROGRAMDATA%",
    "%USERPROFILE%\\Downloads",
    "%USERPROFILE%\\Desktop",
    "%USERPROFILE%\\OneDrive\\Desktop",
)

CHAVES_DO_WINDOWS = (
    r"Software\Microsoft\Windows\CurrentVersion\App Paths\AnyDesk.exe",
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AnyDesk",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\AnyDesk.exe",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\AnyDesk",
)


def normalizar(numero: str) -> str:
    """Tira a formatação que o AnyDesk usa ao exibir o número.

    Ele mostra `123 456 789`, e é assim que a pessoa copia. A linha de
    comando quer `123456789`. Endereços com apelido (`mapah-cnd@ad`) passam
    inteiros, porque ali os caracteres fazem parte do nome.
    """
    numero = numero.strip()
    if "@" in numero:
        return numero
    return "".join(c for c in numero if c.isdigit())


def _candidatos_do_registro() -> list[Path]:
    try:
        import winreg
    except ImportError:
        return []

    candidatos = []
    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for chave in CHAVES_DO_WINDOWS:
            try:
                with winreg.OpenKey(raiz, chave) as aberta:
                    for nome in ("", "Path", "InstallLocation", "DisplayIcon"):
                        try:
                            valor, _ = winreg.QueryValueEx(aberta, nome)
                        except OSError:
                            continue
                        texto = str(valor).strip().strip('"')
                        if ".exe" in texto.lower():
                            fim = texto.lower().find(".exe") + len(".exe")
                            texto = texto[:fim]
                        if texto:
                            candidatos.append(Path(texto))
            except OSError:
                continue
    return candidatos


def _protocolo_anydesk_registrado() -> bool:
    try:
        import winreg
    except ImportError:
        return False

    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE,
                 winreg.HKEY_CLASSES_ROOT):
        for chave in (r"Software\Classes\anydesk", "anydesk"):
            try:
                with winreg.OpenKey(raiz, chave):
                    return True
            except OSError:
                continue
    return False


def _expandir_candidato(caminho: Path) -> list[Path]:
    caminho = Path(os.path.expandvars(str(caminho)))
    if caminho.name.lower() == "anydesk.exe":
        return [caminho]
    return [caminho / "AnyDesk.exe"]


def encontrar_anydesk() -> Path | None:
    if caminho := shutil.which("AnyDesk.exe"):
        return Path(caminho)

    candidatos: list[Path] = []
    for lugar in LUGARES:
        candidatos.append(Path(lugar))
    candidatos.extend(_candidatos_do_registro())

    for candidato in candidatos:
        for caminho in _expandir_candidato(candidato):
            if caminho.exists():
                return caminho

    for raiz in RAIZES_DE_BUSCA:
        base = Path(os.path.expandvars(raiz))
        if not base.exists():
            continue
        for padrao in ("AnyDesk*.exe", "*AnyDesk*/AnyDesk.exe"):
            for caminho in base.glob(padrao):
                if caminho.exists():
                    return caminho
    return None


def abrir(numero: str) -> None:
    """Abre a conexão com a máquina. Levanta RuntimeError com o motivo.

    A mensagem de erro é escrita para quem opera, não para quem programa:
    quem vê é a pessoa do escritório que só queria acessar o computador da
    Receita.
    """
    endereco = normalizar(numero)
    if not endereco:
        raise RuntimeError(
            "Esta máquina não tem o número do AnyDesk cadastrado.\n\n"
            "Abra o AnyDesk nela, copie o número que aparece em "
            '"Este computador" e anote no config.toml, no campo anydesk.'
        )

    programa = encontrar_anydesk()
    if programa is not None:
        subprocess.Popen([str(programa), endereco])
        return

    # Sem instalação encontrada: o protocolo é a última chance. Existe se o
    # AnyDesk já rodou nesta máquina, mesmo em versão portátil. Conferimos
    # antes para evitar a janela do Windows pedindo app da Microsoft Store.
    if not _protocolo_anydesk_registrado():
        raise RuntimeError(
            "O AnyDesk não foi encontrado neste computador.\n\n"
            "Instale o AnyDesk na máquina que está rodando o ACTA e tente "
            f"de novo, ou conecte manualmente no número {endereco}."
        )

    try:
        os.startfile(f"anydesk:{endereco}")
    except OSError as erro:
        raise RuntimeError(
            "O AnyDesk não foi encontrado neste computador.\n\n"
            "Instale-o e tente de novo, ou conecte manualmente no número "
            f"{endereco}."
        ) from erro
