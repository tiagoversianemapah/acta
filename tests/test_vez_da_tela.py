"""As automações cegas usam a tela uma de cada vez, na ordem da fila.

Pedido da operação em 17/09/2026, depois de ver que RFB PF e SEFAZ-ES
ligadas juntas fechavam o Edge uma da outra: a primeira da fila termina a
dela; se pausar, a seguinte usa a tela no meio-tempo; quando a primeira
volta a ter item, a tela volta para ela no fim do item em andamento.
"""
from __future__ import annotations

import threading
import time
from typing import ClassVar

import pytest

from cnd.core import controle, fila, tempo
from cnd.core.breaker import ParametrosBreaker
from cnd.core.modelos import Desfecho, ResultadoTentativa, Status
from cnd.core.ritmo import ParametrosRitmo
from cnd.infra.config import Config, ConfigAlertas, ConfigOrgao, ParametrosRetry
from cnd.orquestrador import worker as modulo_worker
from cnd.orquestrador.loop import _nada_a_fazer, executar
from cnd.orquestrador.vez_da_tela import VezDaTela
from tests.conftest import criar_job


# ---------------------------------------------------------------------------
# A regra, sem thread nenhuma
# ---------------------------------------------------------------------------
def test_automacoes_que_dividem_a_tela():
    """Cegas e o CRF, que abre Edge visível. GO e MA são HTTP e ficam soltas."""
    from cnd.adapters.estadual import sefaz_go, sefaz_ma
    from cnd.adapters.estadual.sefaz_es import AdapterSEFAZES
    from cnd.adapters.federal.crf import AdapterCRF
    from cnd.adapters.federal.rfb.cego import AdapterRFBCego
    from cnd.adapters.federal.rfb.cego_pf import AdapterRFBPFCego

    for classe in (AdapterRFBCego, AdapterRFBPFCego, AdapterSEFAZES, AdapterCRF):
        assert classe.usa_tela is True, classe.__name__
    for modulo in (sefaz_go, sefaz_ma):
        adapters = [v for v in vars(modulo).values()
                    if isinstance(v, type) and v.__name__.startswith("Adapter")]
        assert adapters, modulo.__name__
        assert not any(getattr(a, "usa_tela", False) for a in adapters)


class TestAVez:
    def test_primeira_da_fila_tem_a_vez(self):
        vez = VezDaTela()
        vez.anunciar("RFB_PF", (0, 1, 10))
        vez.anunciar("SEFAZ_ES", (0, 2, 20))

        assert vez.e_a_vez("RFB_PF")
        assert not vez.e_a_vez("SEFAZ_ES")

    def test_pausada_sai_da_disputa_e_a_seguinte_trabalha(self):
        vez = VezDaTela()
        vez.anunciar("RFB_PF", (0, 1, 10))
        vez.anunciar("SEFAZ_ES", (0, 2, 20))

        vez.anunciar("RFB_PF", None)          # disjuntor abriu

        assert vez.e_a_vez("SEFAZ_ES")

    def test_quando_a_primeira_volta_a_vez_volta_para_ela(self):
        vez = VezDaTela()
        vez.anunciar("SEFAZ_ES", (0, 2, 20))
        assert vez.e_a_vez("SEFAZ_ES")

        vez.anunciar("RFB_PF", (0, 1, 10))    # pausa acabou

        assert vez.e_a_vez("RFB_PF")
        assert not vez.e_a_vez("SEFAZ_ES")

    def test_quem_nao_anunciou_nao_tem_a_vez(self):
        assert not VezDaTela().e_a_vez("RFB_PF")


class TestUsoDaTela:
    def test_reabre_o_navegador_so_quando_a_tela_vem_de_outra(self):
        vez = VezDaTela()
        reaberturas = []

        vez.preparar("RFB_PF", lambda: None)
        with vez.usar("RFB_PF", lambda: reaberturas.append("RFB_PF")):
            pass
        with vez.usar("SEFAZ_ES", lambda: reaberturas.append("SEFAZ_ES")):
            pass
        with vez.usar("SEFAZ_ES", lambda: reaberturas.append("SEFAZ_ES")):
            pass
        with vez.usar("RFB_PF", lambda: reaberturas.append("RFB_PF")):
            pass

        assert reaberturas == ["SEFAZ_ES", "RFB_PF"]

    def test_preparar_que_falha_ainda_obriga_a_outra_a_reabrir(self):
        """O preparar fecha todos os Edge antes de falhar na calibragem."""
        vez = VezDaTela()
        reaberturas = []
        vez.preparar("SEFAZ_ES", lambda: None)

        def preparar_quebrado():
            raise RuntimeError("proporção da janela mudou")

        with pytest.raises(RuntimeError):
            vez.preparar("RFB_PF", preparar_quebrado)
        with vez.usar("SEFAZ_ES", lambda: reaberturas.append("SEFAZ_ES")):
            pass

        assert reaberturas == ["SEFAZ_ES"]

    def test_preparar_de_uma_nao_atropela_o_da_outra(self):
        """Na partida todas abrem o Edge juntas, e cada uma fecha todos."""
        vez = VezDaTela()
        dentro = []
        maximo = []

        def preparar_devagar():
            dentro.append(1)
            maximo.append(len(dentro))
            time.sleep(0.05)
            dentro.pop()

        threads = [threading.Thread(target=vez.preparar,
                                    args=(orgao, preparar_devagar))
                   for orgao in ("RFB_PF", "SEFAZ_ES", "RFB_PJ")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max(maximo) == 1


# ---------------------------------------------------------------------------
# A ordem vem da fila
# ---------------------------------------------------------------------------
def _novo_lote(conn) -> int:
    return conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES ('t', 't.xlsx')"
    ).lastrowid


class TestOrdemNaFila:
    def test_sem_item_nao_disputa(self, conn):
        assert fila.ordem_na_fila(conn, "RFB_PF") is None

    def test_planilha_que_chegou_antes_vem_primeiro(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181", orgao="SEFAZ_ES")
        depois = _novo_lote(conn)
        criar_job(conn, depois, documento="11444777000161", orgao="RFB_PF")

        assert fila.ordem_na_fila(conn, "SEFAZ_ES") < fila.ordem_na_fila(conn, "RFB_PF")

    def test_rodar_agora_passa_na_frente(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181", orgao="SEFAZ_ES")
        depois = _novo_lote(conn)
        criar_job(conn, depois, documento="11444777000161", orgao="RFB_PF")

        controle.priorizar(conn, depois, "RFB_PF")

        assert fila.ordem_na_fila(conn, "RFB_PF") < fila.ordem_na_fila(conn, "SEFAZ_ES")

    def test_retentativa_agendada_para_depois_nao_segura_a_tela(self, conn, lote):
        job = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        conn.execute("UPDATE job SET status = ?, proxima_execucao_em = ? WHERE id = ?",
                     (Status.RETRY_WAIT, tempo.daqui_a(3600), job))

        assert fila.ordem_na_fila(conn, "RFB_PF") is None
        assert fila.ha_trabalho(conn, "RFB_PF"), "mas continua sendo trabalho"

    def test_planilha_estacionada_nao_disputa(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        controle.definir(conn, lote, "RFB_PF", controle.ESTACIONADA)

        assert fila.ordem_na_fila(conn, "RFB_PF") is None


class TestRoboParaSozinho:
    """Item que o robô nunca pega não pode segurá-lo de pé (17/09/2026)."""

    def _orgao(self, codigo="RFB_PF"):
        return ConfigOrgao(codigo=codigo, ativo=True, adapter="fake", workers=1,
                           pacing=ParametrosRitmo(), breaker=ParametrosBreaker(),
                           retry=ParametrosRetry())

    def test_planilha_estacionada_ou_cancelada_nao_e_trabalho(self, conn, lote):
        criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        assert fila.ha_trabalho(conn, "RFB_PF")

        controle.definir(conn, lote, "RFB_PF", controle.ESTACIONADA)
        assert not fila.ha_trabalho(conn, "RFB_PF")

        controle.definir(conn, lote, "RFB_PF", controle.CANCELADA)
        assert not fila.ha_trabalho(conn, "RFB_PF")
        assert _nada_a_fazer(conn, [self._orgao()])

    def test_item_em_andamento_conta_mesmo_estacionado(self, conn, lote):
        """Estacionar não interrompe o item que já está no portal."""
        criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        fila.reivindicar(conn, "RFB_PF")
        controle.definir(conn, lote, "RFB_PF", controle.ESTACIONADA)

        assert fila.ha_trabalho(conn, "RFB_PF")

    def test_falha_de_planilha_cancelada_nao_segura_o_robo(self, conn, lote):
        job = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.FAILED, job))
        assert not _nada_a_fazer(conn, [self._orgao()]), "ativa: vai ser recuperada"

        controle.definir(conn, lote, "RFB_PF", controle.CANCELADA)

        assert _nada_a_fazer(conn, [self._orgao()])

    def test_recuperacao_automatica_nao_mexe_em_planilha_parada(self, conn, lote):
        ativa = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PF")
        parado = _novo_lote(conn)
        cancelada = criar_job(conn, parado, documento="11444777000161", orgao="RFB_PF")
        conn.execute("UPDATE job SET status = ?", (Status.FAILED,))
        controle.definir(conn, parado, "RFB_PF", controle.CANCELADA)

        devolvidos = fila.reenfileirar_falhados(conn, "RFB_PF", somente_ativas=True)

        status = dict(conn.execute("SELECT id, status FROM job").fetchall())
        assert devolvidos == 1
        assert status[ativa] == Status.PENDING
        assert status[cancelada] == Status.FAILED


# ---------------------------------------------------------------------------
# Pelo orquestrador de verdade, com dois robôs cegos ligados
# ---------------------------------------------------------------------------
class AdapterDeTela:
    usa_tela = True
    PASSOS: ClassVar[list[tuple[str, str, str]]] = []
    DENTRO: ClassVar[list[str]] = []
    MAXIMO: ClassVar[list[int]] = [0]
    TRAVA = threading.Lock()

    def __init__(self, orgao: str, duracao_s: float = 0.03) -> None:
        self.orgao = orgao
        self.duracao_s = duracao_s
        self.reaberturas = 0

    def preparar(self) -> None:
        pass

    def emitir(self, doc) -> ResultadoTentativa:
        with self.TRAVA:
            self.DENTRO.append(self.orgao)
            self.MAXIMO[0] = max(self.MAXIMO[0], len(self.DENTRO))
            self.PASSOS.append(("inicio", self.orgao, doc.documento))
        time.sleep(self.duracao_s)
        with self.TRAVA:
            self.DENTRO.remove(self.orgao)
        return ResultadoTentativa(Desfecho.NEGATIVA)

    def reiniciar_sessao(self) -> None:
        self.reaberturas += 1

    def encerrar(self) -> None:
        pass


def _config(tmp_path, banco, codigos) -> Config:
    orgaos = {
        codigo: ConfigOrgao(
            codigo=codigo, ativo=True, adapter="fake", workers=1,
            pacing=ParametrosRitmo(intervalo_inicial_s=0.01, intervalo_piso_s=0.01,
                                   intervalo_teto_s=0.05, jitter=0.0),
            breaker=ParametrosBreaker(cooldown_inicial_s=0, cooldown_maximo_s=0),
            retry=ParametrosRetry(max_tentativas=3, backoff_erro_s=(0,)),
        )
        for codigo in codigos
    }
    return Config(banco=banco, pasta_certidoes=tmp_path / "certidoes",
                  pasta_evidencias=tmp_path / "evidencias",
                  pasta_logs=tmp_path / "logs", alertas=ConfigAlertas(),
                  orgaos=orgaos)


def _preparar_orquestrador(monkeypatch, adapters):
    AdapterDeTela.PASSOS.clear()
    AdapterDeTela.DENTRO.clear()
    AdapterDeTela.MAXIMO[0] = 0
    monkeypatch.setattr(modulo_worker, "carregar_adapter",
                        lambda orgao, _cfg: adapters[orgao.codigo])
    monkeypatch.setattr(modulo_worker, "PAUSA_SEM_TRABALHO_S", 0.05)
    monkeypatch.setattr(modulo_worker, "PAUSA_AGUARDANDO_A_TELA_S", 0.02)


def _banco(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_dois_robos_cegos_nunca_usam_a_tela_ao_mesmo_tempo(conn, lote, tmp_path,
                                                           monkeypatch):
    for i in range(4):
        criar_job(conn, lote, documento=f"1{i:013d}", orgao="RFB_PF")
    depois = _novo_lote(conn)
    for i in range(4):
        criar_job(conn, depois, documento=f"2{i:013d}", orgao="SEFAZ_ES")
    adapters = {"RFB_PF": AdapterDeTela("RFB_PF"), "SEFAZ_ES": AdapterDeTela("SEFAZ_ES")}
    _preparar_orquestrador(monkeypatch, adapters)

    executar(_config(tmp_path, _banco(conn), ["RFB_PF", "SEFAZ_ES"]), ate_esvaziar=True)

    ordem = [orgao for _, orgao, _ in AdapterDeTela.PASSOS]
    assert AdapterDeTela.MAXIMO[0] == 1, "duas automações na tela ao mesmo tempo"
    # A planilha da PF chegou primeiro: ela termina a fila dela, e só então a ES.
    assert ordem == ["RFB_PF"] * 4 + ["SEFAZ_ES"] * 4
    assert adapters["SEFAZ_ES"].reaberturas == 1, "reabre o Edge ao pegar a tela"
    assert {s for (s,) in conn.execute("SELECT status FROM job")} == {Status.DONE}


def test_primeira_da_fila_retoma_a_tela_quando_volta(conn, lote, tmp_path,
                                                     monkeypatch):
    """A PF chegou primeiro, mas está com os itens agendados para daqui a
    pouco. A ES trabalha no meio-tempo e devolve a tela quando a PF volta."""
    for i in range(2):
        job = criar_job(conn, lote, documento=f"1{i:013d}", orgao="RFB_PF")
        conn.execute("UPDATE job SET status = ?, proxima_execucao_em = ? WHERE id = ?",
                     (Status.RETRY_WAIT, tempo.daqui_a(0.6), job))
    depois = _novo_lote(conn)
    for i in range(12):
        criar_job(conn, depois, documento=f"2{i:013d}", orgao="SEFAZ_ES")
    adapters = {"RFB_PF": AdapterDeTela("RFB_PF", 0.02),
                "SEFAZ_ES": AdapterDeTela("SEFAZ_ES", 0.15)}
    _preparar_orquestrador(monkeypatch, adapters)

    executar(_config(tmp_path, _banco(conn), ["RFB_PF", "SEFAZ_ES"]), ate_esvaziar=True)

    ordem = [orgao for _, orgao, _ in AdapterDeTela.PASSOS]
    assert AdapterDeTela.MAXIMO[0] == 1
    assert ordem[0] == "SEFAZ_ES", "a PF não podia trabalhar: a ES usa a tela"
    primeira_pf = ordem.index("RFB_PF")
    assert ordem[primeira_pf:primeira_pf + 2] == ["RFB_PF", "RFB_PF"], \
        "quando volta, a PF termina a fila dela de uma vez"
    assert "SEFAZ_ES" in ordem[primeira_pf + 2:], "e a ES continua depois"


# ---------------------------------------------------------------------------
# O vigia não acusa de travada quem só espera a vez
# ---------------------------------------------------------------------------
class TestVigiaNaoAcusaQuemEspera:
    def test_esperando_a_vez_dispensa_cobranca(self):
        vez = VezDaTela()
        vez.anunciar("RFB_PF", (0, 1, 10))
        vez.anunciar("SEFAZ_ES", (0, 2, 20))

        assert vez.dispensa_cobranca("SEFAZ_ES", 1800)
        assert not vez.dispensa_cobranca("RFB_PF", 1800), "a da vez é cobrada"

    def test_quem_acabou_de_receber_a_tela_tem_carencia(self):
        vez = VezDaTela()
        vez.preparar("RFB_PF", lambda: None)
        vez.anunciar("SEFAZ_ES", (0, 2, 20))
        with vez.usar("SEFAZ_ES", lambda: None):
            pass

        assert vez.dispensa_cobranca("SEFAZ_ES", 1800)
        assert not vez.dispensa_cobranca("SEFAZ_ES", 0.0), "passada a carência, cobra"

    def test_vigia_nao_abre_incidente_de_travada_para_quem_espera(
        self, conn, lote, tmp_path, monkeypatch
    ):
        from cnd.orquestrador import vigia as modulo_vigia
        from cnd.orquestrador import vigilancia

        criar_job(conn, lote, documento="11222333000181", orgao="SEFAZ_ES")
        monkeypatch.setattr(vigilancia, "minutos_sem_progresso",
                            lambda *_: 240.0)          # 4h sem concluir
        abertos = []
        monkeypatch.setattr(modulo_vigia.alertas, "abrir_incidente",
                            lambda _cfg, chave, *_a, **_k: abertos.append(chave))
        monkeypatch.setattr(modulo_vigia.alertas, "fechar_incidente",
                            lambda *_a, **_k: None)
        cfg = _config(tmp_path, _banco(conn), ["RFB_PF", "SEFAZ_ES"])
        vez = VezDaTela()
        vez.anunciar("RFB_PF", (0, 1, 10))
        vez.anunciar("SEFAZ_ES", (0, 2, 20))

        modulo_vigia.Vigia(cfg, list(cfg.orgaos.values()), vez_da_tela=vez) \
            ._avisar_travamento(conn, cfg.orgaos["SEFAZ_ES"])
        assert abertos == []

        # Sem a vez da tela (automação de HTTP), 4h sem concluir é travamento.
        modulo_vigia.Vigia(cfg, list(cfg.orgaos.values())) \
            ._avisar_travamento(conn, cfg.orgaos["SEFAZ_ES"])
        assert abertos == ["travado:SEFAZ_ES"]

    def test_planilha_estacionada_nao_conta_como_parada(self, conn, lote):
        from cnd.orquestrador import vigilancia

        criar_job(conn, lote, documento="11222333000181", orgao="SEFAZ_ES")
        controle.definir(conn, lote, "SEFAZ_ES", controle.ESTACIONADA)

        assert vigilancia.minutos_sem_progresso(conn, "SEFAZ_ES") is None
