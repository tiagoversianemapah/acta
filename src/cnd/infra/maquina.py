"""Saúde do computador: memória, disco e há quanto tempo está ligado.

Serve para uma pergunta prática: a máquina do robô aguenta o lote? Ela roda
um navegador o dia inteiro, guarda milhares de PDFs e fica ligada por
semanas. Memória no limite explica lentidão, e disco cheio faz o robô
emitir a certidão e não conseguir salvá-la — que é o pior jeito de
descobrir que faltava espaço.

Tudo por ctypes e biblioteca padrão, sem dependência nova: são três
chamadas do Windows, e não valeria puxar um pacote inteiro por elas.
"""
from __future__ import annotations

import contextlib
import ctypes
import platform
import shutil
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

GIGA = 1024 ** 3


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class Saude:
    """O estado do computador, em números redondos."""

    nome: str = ""
    ram_total_gb: float = 0.0
    ram_usada_gb: float = 0.0
    disco_total_gb: float = 0.0
    disco_livre_gb: float = 0.0
    ligada_ha_h: float = 0.0
    avisos: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ram_percentual(self) -> float:
        return (self.ram_usada_gb / self.ram_total_gb * 100
                if self.ram_total_gb else 0.0)

    @property
    def disco_percentual(self) -> float:
        usado = self.disco_total_gb - self.disco_livre_gb
        return usado / self.disco_total_gb * 100 if self.disco_total_gb else 0.0

    def como_dicionario(self) -> dict:
        return {
            "nome": self.nome,
            "ram_total_gb": round(self.ram_total_gb, 1),
            "ram_usada_gb": round(self.ram_usada_gb, 1),
            "disco_total_gb": round(self.disco_total_gb, 1),
            "disco_livre_gb": round(self.disco_livre_gb, 1),
            "ligada_ha_h": round(self.ligada_ha_h, 1),
            "avisos": list(self.avisos),
        }


# Limites a partir dos quais vale interromper quem está olhando a tela.
# O robô cego mantém um navegador aberto o tempo todo; abaixo de 1 GB livre
# o Windows começa a paginar e o clique passa a chegar atrasado.
RAM_LIVRE_MINIMA_GB = 1.0
DISCO_LIVRE_MINIMO_GB = 5.0


def ler(caminho_dos_dados: Path | None = None) -> Saude:
    """Lê a saúde da máquina. Nunca levanta exceção.

    O disco medido é o da pasta de dados, e não o C: — é lá que os PDFs
    caem, e em algumas máquinas isso fica noutra unidade.
    """
    nome = platform.node()
    ram_total = ram_usada = 0.0
    disco_total = disco_livre = 0.0
    ligada = 0.0
    avisos: list[str] = []

    try:
        estado = _MEMORYSTATUSEX()
        estado.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado))
        ram_total = estado.ullTotalPhys / GIGA
        ram_usada = (estado.ullTotalPhys - estado.ullAvailPhys) / GIGA
        if (ram_total - ram_usada) < RAM_LIVRE_MINIMA_GB:
            avisos.append("memória quase no limite")
    except Exception:
        pass

    try:
        alvo = Path(caminho_dos_dados or Path.home())
        while not alvo.exists() and alvo != alvo.parent:
            alvo = alvo.parent
        uso = shutil.disk_usage(alvo)
        disco_total = uso.total / GIGA
        disco_livre = uso.free / GIGA
        if disco_livre < DISCO_LIVRE_MINIMO_GB:
            avisos.append("disco quase cheio")
    except Exception:
        pass

    with contextlib.suppress(Exception):
        ligada = ctypes.windll.kernel32.GetTickCount64() / 3_600_000

    return Saude(nome=nome, ram_total_gb=ram_total, ram_usada_gb=ram_usada,
                 disco_total_gb=disco_total, disco_livre_gb=disco_livre,
                 ligada_ha_h=ligada, avisos=tuple(avisos))
