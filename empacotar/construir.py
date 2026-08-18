"""Gera o ACTA.exe e os atalhos do Windows.

    python empacotar/construir.py                 # constrói e cria atalhos
    python empacotar/construir.py --sem-atalhos   # só constrói

O resultado sai em dist/ACTA/. Essa pasta é o programa inteiro: copiar ela
para outra máquina é a instalação, sem Python, sem pip, sem nada. É de
propósito — as máquinas do robô são computadores de escritório, e pedir
instalação de ambiente em cada uma seria um convite a versões diferentes
rodando em lugares diferentes.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PASTA = Path(__file__).resolve().parent

sys.path.insert(0, str(RAIZ / "src"))
from cnd.desktop import marca  # noqa: E402 — depende do sys.path acima

NOME = marca.NOME_PRODUTO
DESTINO = RAIZ / "dist" / NOME
VERSAO = (1, 1, 1, 0)
EMPRESA = "Mapah Auditoria e Contabilidade"

# Nomes que o programa já usou. Os atalhos antigos apontam para um
# executável que não existe mais, e atalho quebrado na Área de Trabalho é
# a primeira coisa que alguém clica.
NOMES_ANTIGOS = ("EMISSOR CND",)


def gerar_icone() -> Path:
    """Desenha o ícone a partir da marca, em vez de guardar um .ico no repo.

    Assim a identidade visual tem uma fonte só: mexeu em marca.py, o ícone
    do atalho acompanha na próxima build.
    """
    caminho = PASTA / "acta.ico"
    marca.salvar_icone_janela(caminho)
    print(f"  ícone   {caminho.name}")
    return caminho


def gerar_versao() -> Path:
    """Preenche as propriedades que o Windows mostra em Propriedades > Detalhes.

    Executável sem isso aparece como "programa desconhecido" no aviso do
    SmartScreen e nas políticas de aplicativo — e num ambiente corporativo
    é a diferença entre parecer software da empresa ou parecer arquivo
    baixado da internet.
    """
    v = ", ".join(map(str, VERSAO))
    texto = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', '{EMPRESA}'),
        StringStruct('FileDescription', '{NOME} — {marca.DESCRICAO_PRODUTO}'),
        StringStruct('FileVersion', '{".".join(map(str, VERSAO))}'),
        StringStruct('InternalName', '{NOME}'),
        StringStruct('OriginalFilename', '{NOME}.exe'),
        StringStruct('ProductName', '{NOME}'),
        StringStruct('ProductVersion', '{".".join(map(str, VERSAO))}'),
        StringStruct('LegalCopyright', '{EMPRESA}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    caminho = PASTA / "versao.txt"
    caminho.write_text(texto, encoding="utf-8")
    return caminho


def construir() -> None:
    """Reconstrói o executável preservando o que é do operador.

    O PyInstaller apaga a pasta de saída inteira antes de gerar a nova — e
    ali dentro moram o config ajustado, o banco e as certidões já baixadas.
    Guardamos essas coisas fora do caminho e devolvemos depois.
    """
    guardado = RAIZ / "build" / "preservado"
    if guardado.exists():
        shutil.rmtree(guardado)

    salvos = []
    for nome in ("config.toml", "data"):
        origem = DESTINO / nome
        if origem.exists():
            guardado.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if origem.is_dir() else shutil.copy2)(
                origem, guardado / nome)
            salvos.append(nome)
    if salvos:
        print(f"  preservando  {', '.join(salvos)}")

    print("Construindo o executável (leva alguns minutos)...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(PASTA / "acta.spec")],
        cwd=str(RAIZ), check=True,
    )

    for nome in salvos:
        origem = guardado / nome
        (shutil.copytree if origem.is_dir() else shutil.copy2)(
            origem, DESTINO / nome, **({"dirs_exist_ok": True}
                                       if origem.is_dir() else {}))
    if salvos:
        shutil.rmtree(guardado, ignore_errors=True)
        print(f"  devolvidos   {', '.join(salvos)}")


def levar_arquivos_do_operador() -> None:
    """Põe config.toml e as pastas de trabalho ao lado do executável.

    O programa empacotado procura essas coisas na pasta onde ele está — é
    o que faz `RAIZ_PROJETO` no modo congelado. Sem o config ali, o
    programa abriria e não saberia nem qual órgão emitir.

    O config existente nunca é sobrescrito: numa reconstrução, quem já
    ajustou a máquina não perde o ajuste.
    """
    destino = DESTINO / "config.toml"
    if destino.exists():
        print("  config  já existe, mantido")
    else:
        shutil.copy2(RAIZ / "config.toml", destino)
        print("  config  copiado")

    for pasta in ("data/certidoes", "data/evidencias", "data/logs",
                  "data/calibragem"):
        (DESTINO / pasta).mkdir(parents=True, exist_ok=True)

    calibragem = RAIZ / "data" / "calibragem"
    if calibragem.is_dir():
        for arquivo in calibragem.glob("*.json"):
            alvo = DESTINO / "data" / "calibragem" / arquivo.name
            if not alvo.exists():
                shutil.copy2(arquivo, alvo)
                print(f"  calibragem {arquivo.name} copiada")


def assinar(exes: list[Path]) -> None:
    """Assina os executáveis, se houver um certificado configurado.

    Sem assinatura, o Controle Inteligente de Aplicativos do Windows 11
    recusa o programa e não oferece "executar assim mesmo" — ele não tem
    lista de exceção. Com um certificado de verdade (comprado de uma
    autoridade certificadora reconhecida), o bloqueio desaparece e o
    SmartScreen para de avisar.

    Certificado autoassinado NÃO resolve este caso: o Controle Inteligente
    avalia contra o serviço de reputação da Microsoft, não contra o
    armazenamento de confiança da máquina. Ele ajuda em política de
    aplicativo dentro de um domínio, e só.

    Configure com:
        setx CND_CERT_PFX    "C:\\caminho\\certificado.pfx"
        setx CND_CERT_SENHA  "..."
    """
    pfx = os.environ.get("CND_CERT_PFX", "")
    if not pfx:
        print("  assinatura  nenhuma (defina CND_CERT_PFX para assinar)")
        return
    if not Path(pfx).exists():
        print(f"  assinatura  IGNORADA — {pfx} não existe")
        return

    senha = os.environ.get("CND_CERT_SENHA", "")
    alvos = ", ".join(f"'{caminho}'" for caminho in exes)
    script = f"""
$senha = ConvertTo-SecureString '{senha}' -AsPlainText -Force
$cert  = Get-PfxCertificate -FilePath '{pfx}' -Password $senha
foreach ($alvo in @({alvos})) {{
  $r = Set-AuthenticodeSignature -FilePath $alvo -Certificate $cert `
       -TimestampServer 'http://timestamp.digicert.com' -HashAlgorithm SHA256
  Write-Output ("  assinatura  " + (Split-Path $alvo -Leaf) + ": " + $r.Status)
}}
"""
    resultado = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    print(resultado.stdout.strip() or resultado.stderr.strip())


def criar_atalhos(pela_fonte: bool = False) -> None:
    """Área de Trabalho e Menu Iniciar, como qualquer programa instalado.

    Feito por COM do Windows (WScript.Shell) porque é o único jeito de
    escrever um .lnk de verdade — o que aceita ícone próprio, pasta de
    trabalho e fixação na barra de tarefas. Um .bat ou um atalho de
    internet não permitem nada disso.

    `pela_fonte` aponta o atalho para o `pythonw.exe` do ambiente em vez do
    executável empacotado. Serve para máquina com o Controle Inteligente de
    Aplicativos ligado: o `pythonw.exe` é assinado pela Python Software
    Foundation e passa, enquanto o nosso executável, sem assinatura, é
    barrado. O ícone, o nome e a janela são os mesmos — a diferença não
    aparece para quem usa.
    """
    icone = PASTA / "acta.ico"
    if not icone.exists():
        gerar_icone()

    if pela_fonte:
        alvo = Path(sys.executable).with_name("pythonw.exe")
        argumentos = "-m cnd.lancador"
        pasta_de_trabalho = RAIZ
        origem_do_icone = icone
        if not alvo.exists():
            print(f"  atalhos IGNORADOS — {alvo} não existe")
            return
    else:
        alvo = DESTINO / f"{NOME}.exe"
        argumentos = ""
        pasta_de_trabalho = DESTINO
        origem_do_icone = alvo
        if not alvo.exists():
            print(f"  atalhos IGNORADOS — {alvo} não existe")
            return

    antigos = ", ".join(f"'{nome}.lnk'" for nome in NOMES_ANTIGOS)
    script = f"""
$w = New-Object -ComObject WScript.Shell
$lugares = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path ([Environment]::GetFolderPath('ApplicationData')) `
             'Microsoft\\Windows\\Start Menu\\Programs')
)
foreach ($lugar in $lugares) {{
  if (-not (Test-Path $lugar)) {{ continue }}
  foreach ($velho in @({antigos})) {{
    $caminho = Join-Path $lugar $velho
    if (Test-Path $caminho) {{
      Remove-Item $caminho -Force
      Write-Output ("  removido  " + $caminho)
    }}
  }}
  $atalho = $w.CreateShortcut((Join-Path $lugar '{NOME}.lnk'))
  $atalho.TargetPath       = '{alvo}'
  $atalho.Arguments        = '{argumentos}'
  $atalho.WorkingDirectory = '{pasta_de_trabalho}'
  $atalho.IconLocation     = '{origem_do_icone},0'
  $atalho.Description      = '{NOME} — {marca.DESCRICAO_PRODUTO} — Mapah'
  $atalho.Save()
  Write-Output ("  atalho    " + (Join-Path $lugar '{NOME}.lnk'))
}}
"""
    resultado = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    print(resultado.stdout.strip() or resultado.stderr.strip())
    if pela_fonte:
        print("  o atalho roda pelo Python do ambiente — passa pelo "
              "Controle Inteligente de Aplicativos")


def controle_inteligente_ligado() -> bool:
    """Se o Windows vai recusar um executável sem assinatura.

    Só existe no Windows 11 e só liga sozinho em instalação limpa; a maior
    parte das máquinas de escritório, que vieram de atualização, está com
    ele desligado.
    """
    try:
        import winreg

        chave = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SYSTEM\CurrentControlSet\Control\CI\Policy")
        with chave:
            valor, _ = winreg.QueryValueEx(chave,
                                           "VerifiedAndReputablePolicyState")
        return valor == 1
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=f"empacota o {NOME}")
    parser.add_argument("--sem-atalhos", action="store_true")
    parser.add_argument("--so-atalhos", action="store_true",
                        help="não reconstrói, só refaz os atalhos")
    parser.add_argument("--atalho-fonte", action="store_true",
                        help="atalho apontando para o Python do ambiente, "
                             "para máquina com o Controle Inteligente de "
                             "Aplicativos ligado")
    args = parser.parse_args()

    if not args.so_atalhos:
        gerar_icone()
        gerar_versao()
        construir()
        levar_arquivos_do_operador()
        assinar([DESTINO / f"{NOME}.exe", DESTINO / "cnd.exe"])

    if not args.sem_atalhos:
        criar_atalhos(pela_fonte=args.atalho_fonte)

    print()
    print(f"Pronto. O programa está em:  {DESTINO}")
    print("Para instalar em outra máquina, copie essa pasta inteira e rode")
    print("o construir.py --so-atalhos lá, ou crie o atalho na mão.")

    if controle_inteligente_ligado() and not args.atalho_fonte:
        print()
        print("ATENÇÃO: o Controle Inteligente de Aplicativos está LIGADO")
        print("nesta máquina e vai recusar o ACTA.exe, que não é assinado.")
        print("Ele não tem lista de exceção. Saídas, em docs/07:")
        print("  - use  --atalho-fonte  (roda pelo Python, que é assinado)")
        print("  - ou desligue o Controle Inteligente (decisão sem volta)")
        print("  - ou assine com certificado, via CND_CERT_PFX")
    return 0


if __name__ == "__main__":
    sys.exit(main())
