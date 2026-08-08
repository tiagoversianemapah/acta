"""O nome da máquina é um link que abre o AnyDesk.

Este é o único teste que abre janela de verdade. Vale a pena: a ligação
entre o rótulo e a ação mora num `bind` do Tk, e nenhuma checagem de código
prova que clicar ali chama alguém. Aqui o clique é gerado de fato.
"""
from __future__ import annotations

import pytest

ctk = pytest.importorskip("customtkinter")

from cnd.desktop.remoto import EstadoRemoto  # noqa: E402
from cnd.infra.config import Maquina  # noqa: E402

FEDERAL = Maquina(nome="PC-CND-01", url="http://192.168.0.21:8000",
                  orgao="ACTA CND FEDERAL", anydesk="123 456 789")
SEM_ANYDESK = Maquina(nome="PC-CND-09", url="http://192.168.0.29:8000",
                      orgao="ACTA CND FGTS")
LOCAL = Maquina(nome="MEU-PC", url="")


@pytest.fixture(scope="module")
def janela():
    """Uma janela só para o arquivo inteiro.

    Abrir e fechar várias raízes do Tk no mesmo processo falha na segunda —
    o interpretador Tcl não volta ao estado inicial. Como cada teste começa
    redesenhando os cartões do zero, reaproveitar não mistura nada.
    """
    from cnd.desktop.app import Aplicativo

    app = Aplicativo()
    app.withdraw()          # existe, responde, mas não aparece na tela
    yield app
    app.destroy()


def _rotulos(janela) -> list:
    """Os rótulos clicáveis dos cartões desenhados.

    A mãozinha é o que marca um link nesta tela — é o mesmo sinal que a
    pessoa recebe ao passar o mouse.
    """
    achados = []

    def varrer(widget):
        for filho in widget.winfo_children():
            if isinstance(filho, ctk.CTkLabel) and filho.cget("cursor") == "hand2":
                achados.append(filho)
            varrer(filho)

    varrer(janela.painel_maquinas)
    return achados


def _clicar(rotulo) -> None:
    """Clica onde a ligação realmente está.

    `CTkLabel.bind` repassa aos widgets internos (o canvas e o Label do Tk);
    o invólucro em si não recebe evento nenhum. Disparar no invólucro
    passaria no teste sem provar coisa alguma.
    """
    for interno in rotulo.winfo_children():
        if "<Button-1>" in interno.bind():
            interno.event_generate("<Button-1>")
            return
    raise AssertionError("nenhum widget interno responde ao clique")


def test_clicar_no_nome_abre_o_anydesk_daquela_maquina(janela, monkeypatch):
    chamadas = []
    monkeypatch.setattr("cnd.desktop.acesso.abrir", chamadas.append)

    janela._desenhar_maquinas([EstadoRemoto(FEDERAL, online=True)])
    janela.update_idletasks()

    links = _rotulos(janela)
    assert len(links) == 1, "o nome da máquina deveria estar clicável"

    _clicar(links[0])
    janela.update()
    assert chamadas == ["123 456 789"]


def test_maquina_fora_do_ar_continua_clicavel(janela, monkeypatch):
    """É quando o painel diz 'sem resposta' que alguém precisa entrar."""
    chamadas = []
    monkeypatch.setattr("cnd.desktop.acesso.abrir", chamadas.append)

    janela._desenhar_maquinas([EstadoRemoto(FEDERAL, erro="não respondeu")])
    janela.update_idletasks()

    _clicar(_rotulos(janela)[0])
    janela.update()
    assert chamadas == ["123 456 789"]


def test_sem_anydesk_o_nome_nao_e_clicavel(janela):
    janela._desenhar_maquinas([EstadoRemoto(SEM_ANYDESK, online=True)])
    janela.update_idletasks()
    assert _rotulos(janela) == []


def test_o_proprio_computador_nao_oferece_acesso_remoto(janela):
    """Acessar a si mesmo por AnyDesk não faz sentido; a legenda explica."""
    estado = EstadoRemoto(LOCAL, online=True)
    assert estado.local
    assert estado.subtitulo == "este computador"

    janela._desenhar_maquinas([estado])
    janela.update_idletasks()
    assert _rotulos(janela) == []


def test_a_legenda_avisa_quando_falta_cadastrar_o_numero(janela):
    assert "sem AnyDesk cadastrado" in EstadoRemoto(SEM_ANYDESK).subtitulo
