"""Publica a pasta construída na rede local, para as máquinas baixarem.

Fica no ar enquanto a janela estiver aberta e some quando ela fechar: é
uma entrega pontual, não um servidor. Só escuta a rede interna, e o pacote
não leva `data\\` nem `config.toml` — banco, calibragem e o endereço da
máquina são dela.

Nada aqui vai para a internet. O painel das máquinas e este publicador
vivem os dois dentro da rede da empresa, que é onde os dados dos clientes
têm de ficar.
"""
from __future__ import annotations

import http.server
import shutil
import socket
import socketserver
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CONSTRUIDO = RAIZ / "dist" / "ACTA"
PORTA = 8899
# O que é da máquina e não pode ser sobrescrito por uma atualização.
DELA = {"data", "config.toml"}


def montar_pacote(destino: Path) -> Path:
    """Copia o que é do programa, deixando de fora o que é da máquina."""
    area = destino / "acta"
    area.mkdir(parents=True)
    for item in CONSTRUIDO.iterdir():
        if item.name in DELA:
            continue
        alvo = area / item.name
        if item.is_dir():
            shutil.copytree(item, alvo)
        else:
            shutil.copy2(item, alvo)
    return Path(shutil.make_archive(str(destino / "acta"), "zip", area))


def endereco_na_rede() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))   # não envia nada; só resolve a rota
        return s.getsockname()[0]


def main() -> int:
    if not CONSTRUIDO.exists():
        print(f"Não achei {CONSTRUIDO}. Rode antes:  python empacotar/construir.py")
        return 1

    with tempfile.TemporaryDirectory() as temporario:
        area = Path(temporario)
        pacote = montar_pacote(area)
        shutil.copy2(RAIZ / "empacotar" / "atualizar-pela-rede.ps1", area)

        tamanho = pacote.stat().st_size / (1024 * 1024)
        ip = endereco_na_rede()
        print(f"Publicando {tamanho:.0f} MB em http://{ip}:{PORTA}")
        print("\nNa máquina a atualizar, num Prompt como ADMINISTRADOR:\n")
        print(f'  powershell -ExecutionPolicy Bypass -Command "iwr '
              f'http://{ip}:{PORTA}/atualizar-pela-rede.ps1 -OutFile '
              f'$env:TEMP\\a.ps1; & $env:TEMP\\a.ps1 -Origem '
              f'http://{ip}:{PORTA}"')
        print("\nDeixe esta janela aberta até terminar. Ctrl+C encerra.")

        class Servidor(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(area), **k)

            def log_message(self, formato, *args):
                print(f"  {self.client_address[0]}  {formato % args}")

        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("0.0.0.0", PORTA), Servidor) as servidor:
            try:
                servidor.serve_forever()
            except KeyboardInterrupt:
                print("\nEncerrado. A pasta não está mais publicada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
