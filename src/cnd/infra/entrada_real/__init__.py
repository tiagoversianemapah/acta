"""Mouse e teclado de verdade, no nível do Windows.

Por que isto existe: o Playwright move o mouse *injetando eventos dentro do
navegador* pela porta de depuração — o cursor físico da tela nunca sai do
lugar. Aqui usamos `SendInput`, a mesma API que o driver do seu mouse usa,
então o cursor anda de verdade e o navegador recebe entrada indistinguível
de uma pessoa, porque é o mesmo caminho no sistema operacional.

Sem dependência externa: só ctypes, que já vem no Python.

Limitação importante: a janela precisa estar visível e em primeiro plano —
o mouse é um só, então enquanto o robô trabalha ninguém mexe na máquina.

Este arquivo é a FACHADA. Quem chama continua escrevendo
`entrada_real.clicar(...)`, que é como se lê melhor no adapter; por dentro,
cada assunto mora no seu lugar:

    win32.py               DLLs, constantes e estruturas — sem decisão
    ponteiro.py            onde o mouse está e como ele anda
    teclado.py             teclas, texto e atalhos
    area_transferencia.py  copiar, colar e ler o que o portal devolveu
    janelas.py             achar, focar, posicionar e fechar janela
"""
from __future__ import annotations

from cnd.infra.entrada_real.area_transferencia import (
    colar,
    definir_area_transferencia,
    limpar_area_transferencia,
    texto_area_transferencia,
)
from cnd.infra.entrada_real.janelas import (
    achar_janela,
    em_primeiro_plano,
    fechar_janelas,
    garantir_em_primeiro_plano,
    listar_janelas,
    maximizar,
    posicionar_janela,
    retangulo_janela,
    trazer_para_frente,
)
from cnd.infra.entrada_real.ponteiro import (
    area_util,
    clicar,
    mover,
    posicao,
    tela_virtual,
)
from cnd.infra.entrada_real.teclado import (
    atalho,
    digitar,
    ir_para_url,
    limpar_campo,
    tecla,
)
from cnd.infra.entrada_real.win32 import (
    VK_A,
    VK_C,
    VK_CONTROL,
    VK_DELETE,
    VK_END,
    VK_ESCAPE,
    VK_F5,
    VK_HOME,
    VK_L,
    VK_RETURN,
    VK_RIGHT,
    VK_SHIFT,
    VK_TAB,
    VK_V,
)

__all__ = [
    "VK_A", "VK_C", "VK_CONTROL", "VK_DELETE", "VK_END", "VK_ESCAPE",
    "VK_F5", "VK_HOME", "VK_L", "VK_RETURN", "VK_RIGHT", "VK_SHIFT",
    "VK_TAB", "VK_V",
    "achar_janela", "area_util", "atalho", "clicar", "colar",
    "definir_area_transferencia", "digitar", "em_primeiro_plano",
    "fechar_janelas", "garantir_em_primeiro_plano", "ir_para_url",
    "limpar_area_transferencia", "limpar_campo", "listar_janelas",
    "maximizar", "mover", "posicao", "posicionar_janela",
    "retangulo_janela", "tecla", "tela_virtual",
    "texto_area_transferencia", "trazer_para_frente",
]
