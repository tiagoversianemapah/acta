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
from cnd.core.modelos import CONCLUSIVOS, Desfecho, ResultadoTentativa, Status
from cnd.core.ritmo import ParametrosRitmo
from cnd.infra.config import Config, ConfigAlertas, ConfigOrgao, ParametrosRetry
from cnd.infra.db import caminho_pedido_parada
from cnd.orquestrador.loop import Contexto, _consumir_pedido_de_parada, executar
from tests.conftest import criar_job


class AdapterSequencial:
    def __init__(self, resultados: list[ResultadoTentativa]) -> None:
        self.resultados = list(resultados)
        self.chamadas: list[str] = []
        self.reinicios = 0

    def preparar(self) -> None:
        pass

    def emitir(self, doc) -> ResultadoTentativa:
        self.chamadas.append(doc.documento)
        if not self.resultados:
            raise AssertionError("adapter chamado mais vezes que o esperado")
        return self.resultados.pop(0)

    def reiniciar_sessao(self) -> None:
        self.reinicios += 1

    def encerrar(self) -> None:
        pass


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
        retry=ParametrosRetry(
            max_tentativas=3,
            backoff_erro_s=(0,),
            backoff_captcha_s=(0,),
            backoff_bloqueio_s=(0,),
            retentativa_bloqueio_s=(0,),
        ),
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


def test_bloqueio_106_faz_retentativa_rapida_e_recupera(
    conn, lote, tmp_path, monkeypatch
):
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    adapter = AdapterSequencial([
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal="Nao foi possivel concluir a acao. 106 - 14/08/2026",
        ),
        ResultadoTentativa(
            Desfecho.NEGATIVA,
            mensagem_portal="PDF baixado",
        ),
    ])
    monkeypatch.setattr("cnd.orquestrador.loop.carregar_adapter", lambda *_: adapter)
    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2], {})

    executar(cfg, ate_esvaziar=True)

    assert adapter.chamadas == ["00000000000001", "00000000000001"]
    assert adapter.reinicios == 1
    job = conn.execute("SELECT status, desfecho, tentativas FROM job").fetchone()
    assert job["status"] == Status.DONE
    assert job["desfecho"] == Desfecho.NEGATIVA
    assert job["tentativas"] == 1
    tentativas = conn.execute("SELECT desfecho, mensagem_portal FROM tentativa").fetchall()
    assert len(tentativas) == 1
    assert tentativas[0]["desfecho"] == Desfecho.NEGATIVA
    assert "micro-retentativa" in tentativas[0]["mensagem_portal"]
    assert breaker.consultar(conn, "FAKE").estado == breaker.FECHADO


def test_bloqueio_106_persistente_reagenda_sem_consumir_duas_tentativas(
    conn, lote, tmp_path, monkeypatch
):
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    adapter = AdapterSequencial([
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal="Nao foi possivel concluir a acao. 106 - 14/08/2026",
        ),
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal="Nao foi possivel concluir a acao. 106 - 14/08/2026",
        ),
    ])
    monkeypatch.setattr("cnd.orquestrador.loop.carregar_adapter", lambda *_: adapter)
    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2], {})

    executar(cfg, limite=1)

    assert adapter.chamadas == ["00000000000001", "00000000000001"]
    job = conn.execute("SELECT status, tentativas FROM job").fetchone()
    assert job["status"] == Status.RETRY_WAIT
    assert job["tentativas"] == 1
    tentativas = conn.execute("SELECT desfecho FROM tentativa").fetchall()
    assert len(tentativas) == 1
    assert tentativas[0]["desfecho"] == Desfecho.BLOQUEIO_TEMPORARIO
    assert breaker.consultar(conn, "FAKE").estado == breaker.FECHADO


def test_bloqueio_005_faz_retentativa_rapida_e_recupera(
    conn, lote, tmp_path, monkeypatch
):
    """005 é o código que mais apareceu em produção (17/08/2026) e o único
    que morria sem nunca tentar com sessão nova."""
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    adapter = AdapterSequencial([
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal=("Nao foi possivel emitir a certidao. Tente "
                             "novamente em alguns minutos. 005 - 17/08/2026"),
        ),
        ResultadoTentativa(
            Desfecho.NEGATIVA,
            mensagem_portal="PDF baixado",
        ),
    ])
    monkeypatch.setattr("cnd.orquestrador.loop.carregar_adapter", lambda *_: adapter)
    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2], {})

    executar(cfg, ate_esvaziar=True)

    assert adapter.chamadas == ["00000000000001", "00000000000001"]
    assert adapter.reinicios == 1
    job = conn.execute("SELECT status, desfecho, tentativas FROM job").fetchone()
    assert job["status"] == Status.DONE
    assert job["desfecho"] == Desfecho.NEGATIVA
    assert job["tentativas"] == 1
    tentativas = conn.execute("SELECT mensagem_portal FROM tentativa").fetchall()
    assert len(tentativas) == 1
    assert "micro-retentativa apos bloqueio temporario 005" in (
        tentativas[0]["mensagem_portal"]
    )


def test_bloqueio_033_nao_usa_retentativa_rapida(conn, lote, tmp_path, monkeypatch):
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    adapter = AdapterSequencial([
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal="Nao foi possivel emitir a certidao. 033 - 14/08/2026",
        ),
    ])
    monkeypatch.setattr("cnd.orquestrador.loop.carregar_adapter", lambda *_: adapter)
    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2], {})

    executar(cfg, limite=1)

    assert adapter.chamadas == ["00000000000001"]
    job = conn.execute("SELECT status, tentativas FROM job").fetchone()
    assert job["status"] == Status.RETRY_WAIT
    assert job["tentativas"] == 1


def test_cnpj_com_005_nao_dispara_micro_retentativa(conn, lote, tmp_path, monkeypatch):
    """Os dígitos do CNPJ não são código do portal.

    Sem a data no regex, uma empresa com 005 no número ganhava a segunda
    chance por acaso — e outra, sem ele, não ganhava mesmo tendo levado
    bloqueio de verdade."""
    criar_job(conn, lote, documento="00000000000001", orgao="FAKE")
    adapter = AdapterSequencial([
        ResultadoTentativa(
            Desfecho.BLOQUEIO_TEMPORARIO,
            mensagem_portal="cnpj 12.005.678/0001-99 nao foi possivel emitir",
        ),
    ])
    monkeypatch.setattr("cnd.orquestrador.loop.carregar_adapter", lambda *_: adapter)
    cfg = montar_config(tmp_path, conn.execute("PRAGMA database_list").fetchone()[2], {})

    executar(cfg, limite=1)

    assert adapter.chamadas == ["00000000000001"]
