"""Teste de ponta a ponta, com o adapter de simulação.

Exercita fila + ritmo + breaker + retry + conclusão juntos, sem tocar em
nenhum site. É este teste que dá confiança para, depois, trocar só o
adapter pelo da Receita.
"""
from __future__ import annotations

import threading

from cnd.adapters import fake
from cnd.core import breaker, ritmo
from cnd.core.breaker import ParametrosBreaker
from cnd.core.modelos import CONCLUSIVOS, Status
from cnd.core.ritmo import ParametrosRitmo
from cnd.infra.config import Config, ConfigAlertas, ConfigOrgao, ParametrosRetry
from cnd.infra.db import caminho_pedido_parada
from cnd.orquestrador.loop import Contexto, _consumir_pedido_de_parada, executar
from tests.conftest import criar_job


def montar_config(tmp_path, banco, simulacao: dict, workers: int = 1) -> Config:
    orgao = ConfigOrgao(
        codigo="FAKE",
        ativo=True,
        adapter="fake",
        workers=workers,
        pacing=ParametrosRitmo(
            intervalo_inicial_s=0.01, intervalo_piso_s=0.01, intervalo_teto_s=0.05,
            acelera_apos=3, fator_aceleracao=0.5, fator_punicao=2.0, jitter=0.0,
        ),
        breaker=ParametrosBreaker(
            captchas_para_abrir=3, janela_jobs=10, erros_para_abrir=5,
            cooldown_inicial_s=0, cooldown_maximo_s=0,
        ),
        retry=ParametrosRetry(max_tentativas=3, backoff_erro_s=(0,), backoff_captcha_s=(0,)),
        extras={"simulacao": simulacao},
    )
    return Config(
        banco=banco,
        pasta_certidoes=tmp_path / "certidoes",
        pasta_evidencias=tmp_path / "evidencias",
        pasta_logs=tmp_path / "logs",
        alertas=ConfigAlertas(),          # SMTP vazio => alertas desligados
        orgaos={"FAKE": orgao},
    )


def test_reserva_do_limite_pode_ser_cancelada(tmp_path):
    ctx = Contexto(cfg=montar_config(tmp_path, tmp_path / "cnd.db", {}),
                   parar=threading.Event(), limite=1)

    assert ctx.reservar_vaga() is True
    ctx.cancelar_reserva()
    assert ctx.reservar_vaga() is True


def test_consumir_pedido_de_parada_remove_o_sinal(tmp_path):
    cfg = montar_config(tmp_path, tmp_path / "cnd.db", {})
    pedido = caminho_pedido_parada(cfg.banco)
    pedido.parent.mkdir(parents=True, exist_ok=True)
    pedido.write_text("parar", encoding="utf-8")

    assert _consumir_pedido_de_parada(cfg) is True
    assert not pedido.exists()
    assert _consumir_pedido_de_parada(cfg) is False


def test_lote_inteiro_e_processado(conn, lote, tmp_path):
    """Sem captcha, todo job precisa terminar em DONE."""
    for i in range(12):
        criar_job(conn, lote, documento=f"{i:014d}", orgao="FAKE")

    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2],
                        simulacao={"limiar_heuristica_s": 0.0, "chance_erro_tecnico": 0.0,
                                   "duracao_min_s": 0.0, "duracao_max_s": 0.0})
    executar(cfg, ate_esvaziar=True)

    situacoes = {linha["status"] for linha in conn.execute("SELECT status FROM job")}
    assert situacoes == {Status.DONE}

    desfechos = {linha["desfecho"] for linha in conn.execute("SELECT desfecho FROM job")}
    assert desfechos <= {str(d) for d in CONCLUSIVOS}


def test_cada_job_gera_tentativa(conn, lote, tmp_path):
    for i in range(5):
        criar_job(conn, lote, documento=f"{i:014d}", orgao="FAKE")

    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2],
                        simulacao={"limiar_heuristica_s": 0.0, "chance_erro_tecnico": 0.0,
                                   "duracao_min_s": 0.0, "duracao_max_s": 0.0})
    executar(cfg, ate_esvaziar=True)

    tentativas = conn.execute("SELECT COUNT(*) AS n FROM tentativa").fetchone()["n"]
    assert tentativas >= 5


def test_captcha_pune_o_ritmo_e_abre_o_disjuntor(conn, lote, tmp_path):
    """Com captcha garantido, o robô precisa frear e se pausar sozinho."""
    for i in range(8):
        criar_job(conn, lote, documento=f"{i:014d}", orgao="FAKE")

    cfg = montar_config(
        tmp_path, conn.execute("PRAGMA database_list").fetchone()[2],
        simulacao={"captcha_base": 1.0, "duracao_min_s": 0.0, "duracao_max_s": 0.0},
    )
    executar(cfg, ate_esvaziar=True)

    # Freou: o intervalo saiu do inicial em direção ao teto.
    estado = ritmo.estado(conn, "FAKE", cfg.orgaos["FAKE"].pacing)
    assert estado.intervalo_s > 0.01

    # Abriu o disjuntor pelo menos uma vez.
    assert breaker.consultar(conn, "FAKE").aberturas >= 1

    # Nada ficou preso em RUNNING.
    presos = conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE status = ?", (Status.RUNNING,)
    ).fetchone()["n"]
    assert presos == 0


def test_esgotar_tentativas_leva_a_failed(conn, lote, tmp_path):
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")

    cfg = montar_config(
        tmp_path, conn.execute("PRAGMA database_list").fetchone()[2],
        simulacao={"captcha_base": 1.0, "duracao_min_s": 0.0, "duracao_max_s": 0.0},
    )
    executar(cfg, ate_esvaziar=True)

    linha = conn.execute("SELECT status, tentativas FROM job").fetchone()
    assert linha["status"] == Status.FAILED
    assert linha["tentativas"] == 3, "precisa parar no máximo configurado"


def test_erro_tecnico_reinicia_sessao_do_adapter(conn, lote, tmp_path, monkeypatch):
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    reinicios = []
    reiniciar_original = fake.AdapterFake.reiniciar_sessao

    def reiniciar_contando(self):
        reinicios.append(self.orgao)
        reiniciar_original(self)

    monkeypatch.setattr(fake.AdapterFake, "reiniciar_sessao", reiniciar_contando)
    cfg = montar_config(
        tmp_path, conn.execute("PRAGMA database_list").fetchone()[2],
        simulacao={
            "chance_erro_tecnico": 1.0,
            "duracao_min_s": 0.0,
            "duracao_max_s": 0.0,
        },
    )

    executar(cfg, ate_esvaziar=True)

    assert reinicios == ["FAKE", "FAKE", "FAKE"]
