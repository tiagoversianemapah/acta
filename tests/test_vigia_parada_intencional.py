"""Robô parado de propósito não é incidente, e não se relança sozinho.

Enviar planilha passou a marcar a parada manual, para o robô não começar
sozinho. Só que o vigia do painel via "fila cheia + robô sem sinal" e
tentava religar de cinco em cinco minutos, apanhando da parada manual, e
ainda abria "Robô parado com trabalho na fila" a cada minuto — o alarme
falso que o vigia existe justamente para evitar.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from cnd.infra.db import caminho_parada_manual, garantir
from cnd.web import comandos


@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "cnd.db"
    garantir(caminho)
    return caminho


def test_sinalizador_reflete_o_arquivo(banco):
    assert comandos.parada_manual_ativa(banco) is False
    marcador = caminho_parada_manual(banco)
    marcador.parent.mkdir(parents=True, exist_ok=True)
    marcador.write_text("parado", encoding="utf-8")
    assert comandos.parada_manual_ativa(banco) is True


class _Parar(Exception):
    """Interrompe o laço depois de uma volta."""


def _uma_volta(modulo, monkeypatch):
    """Roda UMA iteração do vigia de verdade, e não uma cópia da lógica.

    O laço é `while True` com `sleep` no fim; trocar o sleep por algo que
    levanta faz a função executar exatamente uma vez. Testar uma cópia da
    decisão não protegeria contra alguém mudar o original.
    """
    import asyncio

    async def _explode(_):
        raise _Parar

    monkeypatch.setattr(modulo.asyncio, "sleep", _explode)
    with pytest.raises(_Parar):
        asyncio.run(modulo.vigiar_orquestrador())


def test_vigia_nao_relanca_nem_alerta_com_parada_intencional(banco, monkeypatch):
    """O caso real: itens na fila e o robô parado porque ninguém mandou
    começar. Nem retomada, nem incidente."""
    from cnd.infra.config import carregar
    from cnd.web import app as modulo

    marcador = caminho_parada_manual(banco)
    marcador.parent.mkdir(parents=True, exist_ok=True)
    marcador.write_text("parado", encoding="utf-8")
    monkeypatch.setattr(modulo, "cfg", replace(carregar(), banco=banco))

    monkeypatch.setattr(modulo.heartbeat, "segundos_desde", lambda *a: 9999.0)
    monkeypatch.setattr(modulo.consultas, "pendentes", lambda *a: 2041)

    tentou, abertos = [], []
    monkeypatch.setattr(modulo, "_tentar_retomada_automatica",
                        lambda *a: tentou.append(a) or False)
    monkeypatch.setattr(modulo.alertas, "abrir_incidente",
                        lambda *a, **k: abertos.append(a))
    monkeypatch.setattr(modulo.alertas, "fechar_incidente", lambda *a, **k: None)

    _uma_volta(modulo, monkeypatch)

    assert tentou == [], "não pode tentar religar o que foi parado de propósito"
    assert abertos == [], "não pode abrir incidente por parada intencional"


def test_vigia_continua_alertando_quando_o_robo_morreu_sozinho(banco, monkeypatch):
    """A correção não pode custar o alarme que importa: robô que caiu de
    verdade, com fila, sem ninguém ter mandado parar."""
    from cnd.infra.config import carregar
    from cnd.web import app as modulo

    monkeypatch.setattr(modulo, "cfg", replace(carregar(), banco=banco))
    monkeypatch.setattr(modulo.heartbeat, "segundos_desde", lambda *a: 9999.0)
    monkeypatch.setattr(modulo.consultas, "pendentes", lambda *a: 2041)

    abertos = []
    monkeypatch.setattr(modulo, "_tentar_retomada_automatica", lambda *a: False)
    monkeypatch.setattr(modulo.alertas, "abrir_incidente",
                        lambda *a, **k: abertos.append(a))
    monkeypatch.setattr(modulo.alertas, "fechar_incidente", lambda *a, **k: None)

    _uma_volta(modulo, monkeypatch)
    assert abertos, "sem parada manual, o incidente TEM de abrir"
