"""Uma planilha por vez: a que chegou antes termina antes.

Cada automação puxava o item mais antigo de QUALQUER planilha ativa. Com
três envios na máquina, os três andavam ao mesmo tempo: a tela mostrava uma,
o robô emitia de outra, e nenhuma fechava (22/09/2026). Agora o robô só pega
itens da planilha da vez, e as outras esperam.
"""
from __future__ import annotations

import shutil
from dataclasses import replace as _replace

import pytest

from cnd.core import controle, fila, tempo
from cnd.core.modelos import Desfecho, ResultadoTentativa, Status
from cnd.infra.config import ConfigRede, carregar
from cnd.infra.db import conectar, criar_schema
from tests.conftest import RAIZ, criar_job


def _lote(conn, nome: str) -> int:
    return conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
        (nome, f"{nome}.xlsx")).lastrowid


def test_a_planilha_mais_antiga_vem_primeiro(conn):
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="GOIANIA")

    job = fila.reivindicar(conn, "GOIANIA")

    assert job is not None and job.lote_id == primeiro


def test_a_outra_planilha_espera_mesmo_em_outra_automacao(conn):
    """O que trava é a PLANILHA, não a automação: senão o MT de um envio
    rodaria junto com o Goiânia de outro, que é o que se quer evitar."""
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

    assert fila.reivindicar(conn, "SEFAZ_MT") is None
    assert fila.reivindicar(conn, "GOIANIA") is not None


def test_terminada_a_primeira_a_seguinte_assume(conn):
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="GOIANIA")

    job = fila.reivindicar(conn, "GOIANIA")
    fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.NEGATIVA))

    seguinte = fila.reivindicar(conn, "GOIANIA")
    assert seguinte is not None and seguinte.lote_id == segundo


def test_rodar_agora_fura_a_fila_das_planilhas(conn):
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "urgente")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="GOIANIA")

    controle.priorizar(conn, segundo, "GOIANIA")

    job = fila.reivindicar(conn, "GOIANIA")
    assert job is not None and job.lote_id == segundo


def test_planilha_toda_em_espera_cede_a_vez(conn):
    """Parar a máquina para esperar uma espera seria trocar mistura por
    ociosidade: a seguinte usa a vez nesse meio-tempo."""
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="GOIANIA")

    job = fila.reivindicar(conn, "GOIANIA")
    fila.reagendar(conn, job, Desfecho.ERRO_TECNICO, espera_s=3600)

    seguinte = fila.reivindicar(conn, "GOIANIA")
    assert seguinte is not None and seguinte.lote_id == segundo


def test_planilha_estacionada_nao_segura_a_vez(conn):
    """Estacionada sai da conta: senão ela travaria a máquina inteira."""
    primeiro = _lote(conn, "estacionada")
    segundo = _lote(conn, "ativa")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="GOIANIA")
    controle.definir(conn, primeiro, "GOIANIA", controle.ESTACIONADA)

    job = fila.reivindicar(conn, "GOIANIA")

    assert job is not None and job.lote_id == segundo


def test_item_em_execucao_segura_a_vez_da_planilha(conn):
    """O último item de uma planilha ainda sendo emitido não pode deixar a
    seguinte começar: é a mistura que se quer evitar."""
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

    fila.reivindicar(conn, "GOIANIA")      # fica RUNNING, sem concluir

    assert fila.reivindicar(conn, "SEFAZ_MT") is None
    assert fila.lote_da_vez(conn) == primeiro


def test_terminado_o_item_em_curso_a_seguinte_assume(conn):
    primeiro = _lote(conn, "antiga")
    segundo = _lote(conn, "nova")
    criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
    criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

    job = fila.reivindicar(conn, "GOIANIA")
    fila.concluir(conn, job, ResultadoTentativa(desfecho=Desfecho.NEGATIVA))

    seguinte = fila.reivindicar(conn, "SEFAZ_MT")
    assert seguinte is not None and seguinte.lote_id == segundo


class TestOrgaoParadoNaoSeguraAVez:
    """Planilha cujo órgão não pode trabalhar agora passa a bola.

    Disjuntor aberto ou janela de horário fechada. Sem isto, a planilha de
    um órgão de castigo era a da vez do mesmo jeito e a fila não entregava
    item a NINGUÉM: a máquina ficava de pé sem emitir nada, e a janela do
    RFB fechada à noite parava também o estadual do envio seguinte
    (23/09/2026).
    """

    def test_planilha_de_orgao_parado_cede_a_vez(self, conn):
        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

        assert fila.lote_da_vez(conn, {"GOIANIA"}) == segundo
        job = fila.reivindicar(conn, "SEFAZ_MT", {"GOIANIA"})
        assert job is not None and job.lote_id == segundo

    def test_quem_pergunta_nao_impede_a_si_mesmo(self, conn):
        """Outro worker do mesmo órgão pode tê-lo marcado no ciclo anterior;
        quem chegou até aqui está podendo trabalhar."""
        primeiro = _lote(conn, "antiga")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")

        job = fila.reivindicar(conn, "GOIANIA", {"GOIANIA"})

        assert job is not None and job.lote_id == primeiro

    def test_item_em_curso_segura_a_vez_mesmo_com_o_orgao_parado(self, conn):
        """O disjuntor pode abrir com um item ainda na mão: ele vai
        terminar, e até lá a planilha dele continua sendo a da vez."""
        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

        fila.reivindicar(conn, "GOIANIA")       # fica RUNNING

        assert fila.lote_da_vez(conn, {"GOIANIA"}) == primeiro
        assert fila.reivindicar(conn, "SEFAZ_MT", {"GOIANIA"}) is None

    def test_a_vez_volta_quando_o_orgao_destrava(self, conn):
        """Disjuntor fecha, janela abre: a planilha antiga retoma a vez."""
        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

        assert fila.lote_da_vez(conn, {"GOIANIA"}) == segundo
        assert fila.lote_da_vez(conn) == primeiro


class TestLoteDaVez:
    def test_diz_qual_planilha_esta_valendo(self, conn):
        primeiro = _lote(conn, "antiga")
        _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")

        assert fila.lote_da_vez(conn) == primeiro

    def test_sem_trabalho_nao_ha_vez(self, conn):
        _lote(conn, "vazia")

        assert fila.lote_da_vez(conn) is None

    def test_concorda_com_quem_a_fila_entrega(self, conn):
        """A tela promete uma planilha; o robô precisa emitir a mesma."""
        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

        vez = fila.lote_da_vez(conn)
        job = fila.reivindicar(conn, "GOIANIA")

        assert job is not None and job.lote_id == vez

    def test_item_agendado_para_depois_nao_da_a_vez(self, conn):
        lote = _lote(conn, "so espera")
        criar_job(conn, lote, documento="11222333000181", orgao="GOIANIA")
        conn.execute(
            "UPDATE job SET status = ?, proxima_execucao_em = ?",
            (Status.RETRY_WAIT, tempo.daqui_a(3600)))

        assert fila.lote_da_vez(conn) is None


class TestTelaDeOperacao:
    """Uma planilha na tela, e as outras como recado.

    O seletor de planilhas que ficava no rodapé do "Trabalho atual" deixava
    escolher outra e dava a impressão de que dá para tocar duas ao mesmo
    tempo — e o robô emite uma de cada vez.
    """

    @pytest.fixture
    def painel(self, tmp_path, monkeypatch):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        raiz_config = RAIZ / "config.toml"
        criado = not raiz_config.exists()
        if criado:
            shutil.copy(RAIZ / "config.exemplo.toml", raiz_config)
        try:
            from cnd.web import app as modulo

            banco = tmp_path / "cnd.db"
            conexao = conectar(banco)
            criar_schema(conexao)
            monkeypatch.setattr(modulo, "cfg", _replace(
                carregar(RAIZ / "config.exemplo.toml"), banco=banco,
                rede=ConfigRede(nome="PC 01", senha="")))
            yield conexao, fastapi_testclient.TestClient(modulo.app)
            conexao.close()
        finally:
            if criado:
                raiz_config.unlink(missing_ok=True)

    def test_mostra_a_da_vez_e_conta_as_que_esperam(self, painel):
        conn, cliente = painel
        antiga = _lote(conn, "antiga")
        nova = _lote(conn, "nova")
        criar_job(conn, antiga, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, nova, documento="11444777000161", orgao="GOIANIA")
        conn.commit()

        with cliente as c:
            pagina = c.get("/").text

        assert "antiga.xlsx" in pagina
        assert "1 planilha(s) esperando a vez" in pagina
        assert "seletor-planilha" not in pagina, "o seletor saiu da tela"

    def test_sem_outra_planilha_nao_ha_recado(self, painel):
        conn, cliente = painel
        unica = _lote(conn, "unica")
        criar_job(conn, unica, documento="11222333000181", orgao="GOIANIA")
        conn.commit()

        with cliente as c:
            pagina = c.get("/").text

        assert "esperando a vez" not in pagina

    def test_abrir_uma_antiga_nao_poe_a_da_vez_na_espera(self, painel):
        """A da vez está valendo, não esperando.

        Quem abre uma planilha antiga pelo link continua vendo o recado das
        que esperam — e a tela contava a da vez entre elas, que é dizer que
        a planilha em curso aguarda a si mesma (23/09/2026).
        """
        conn, cliente = painel
        antiga = _lote(conn, "antiga")
        meio = _lote(conn, "meio")
        _lote(conn, "consultada")
        criar_job(conn, antiga, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, meio, documento="11444777000161", orgao="GOIANIA")
        conn.commit()

        with cliente as c:
            pagina = c.get("/?arquivo=3").text

        assert "consultada.xlsx" in pagina
        assert "1 planilha(s) esperando a vez" in pagina
        # Só a do meio espera: a "antiga" é a da vez, e o nome dela no
        # recado seria a tela dizendo que a planilha em curso aguarda.
        assert 'title="meio.xlsx"' in pagina

    def test_planilha_terminada_nao_espera_nada(self, painel):
        """Terminada não está na fila: contá-la faria a tela prometer
        trabalho que não existe."""
        conn, cliente = painel
        antiga = _lote(conn, "antiga")
        criar_job(conn, antiga, documento="11222333000181", orgao="GOIANIA")
        terminada = _lote(conn, "terminada")
        pronto = criar_job(conn, terminada, documento="11444777000161",
                           orgao="GOIANIA")
        conn.execute("UPDATE job SET status = ? WHERE id = ?",
                     (Status.DONE, pronto))
        conn.commit()

        with cliente as c:
            pagina = c.get("/").text

        assert "esperando a vez" not in pagina


class TestVigiaNaoAcusaQuemEspera:
    """Esperar a vez da planilha não é travamento.

    Com um envio grande segurando a vez, as outras automações ficam sem
    concluir nada por muito tempo — e o aviso de "travado" sairia para todas
    elas, alarme falso pelo mesmo motivo da vez da tela.
    """

    def test_quem_so_tem_trabalho_em_outra_planilha_esta_esperando(self, conn):
        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        criar_job(conn, segundo, documento="11444777000161", orgao="SEFAZ_MT")

        assert fila.esperando_a_vez(conn, "SEFAZ_MT") is True
        assert fila.esperando_a_vez(conn, "GOIANIA") is False

    def test_sem_trabalho_nenhum_nao_esta_esperando(self, conn):
        """Fila vazia é serviço terminado, e isso o vigia já trata."""
        primeiro = _lote(conn, "antiga")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")

        assert fila.esperando_a_vez(conn, "SEFAZ_MT") is False

    def test_vigia_nao_marca_progresso_parado_para_quem_espera(self, conn):
        from cnd.orquestrador import vigilancia

        primeiro = _lote(conn, "antiga")
        segundo = _lote(conn, "nova")
        criar_job(conn, primeiro, documento="11222333000181", orgao="GOIANIA")
        job = criar_job(conn, segundo, documento="11444777000161",
                        orgao="SEFAZ_MT")
        # Uma tentativa antiga, como a de quem já rodou e parou faz tempo.
        conn.execute(
            "INSERT INTO tentativa (job_id, numero, iniciada_em, finalizada_em)"
            " VALUES (?, 1, '2026-09-01T10:00:00.000Z', "
            "'2026-09-01T10:00:05.000Z')", (job,))

        assert vigilancia.minutos_sem_progresso(conn, "SEFAZ_MT") is None
