"""Duas travas que impedem estrago: espaço em disco e comando remoto."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from cnd.core.modelos import Status
from cnd.infra import heartbeat
from cnd.infra.config import ConfigRede, carregar
from cnd.orquestrador.loop import _conferir_espaco
from cnd.web import comandos
from tests.conftest import criar_job

fastapi_testclient = pytest.importorskip("fastapi.testclient")


class TestPreVooDeDisco:
    """Recusa começar quando o lote não cabe.

    Disco cheio no meio do lote faz o robô emitir a certidão no portal e
    não conseguir salvar o PDF: a consulta foi gasta, o portal contou a
    emissão, e o arquivo não existe. Avisar depois é relatar prejuízo.
    """

    @pytest.fixture
    def cfg(self, tmp_path):
        return replace(carregar(), banco=tmp_path / "cnd.db",
                       pasta_certidoes=tmp_path / "certidoes")

    def _pdfs(self, cfg, quantidade: int, kb: int) -> None:
        cfg.pasta_certidoes.mkdir(parents=True, exist_ok=True)
        for indice in range(quantidade):
            (cfg.pasta_certidoes / f"{indice}.pdf").write_bytes(b"x" * kb * 1024)

    def test_fila_vazia_nao_impede(self, conn, cfg):
        assert _conferir_espaco(conn, cfg) == ""

    def test_sem_pdf_anterior_deixa_comecar(self, conn, lote, cfg):
        """Sem amostra não há como estimar; travar por uma conta que não dá
        para fazer seria pior que deixar trabalhar."""
        criar_job(conn, lote, documento="11222333000181")
        assert _conferir_espaco(conn, cfg) == ""

    def test_espaco_de_sobra_deixa_comecar(self, conn, lote, cfg):
        criar_job(conn, lote, documento="11222333000181")
        self._pdfs(cfg, 2, kb=1)
        assert _conferir_espaco(conn, cfg) == ""

    def test_lote_que_nao_cabe_e_recusado(self, conn, lote, cfg, monkeypatch):
        from cnd.infra import maquina

        criar_job(conn, lote, documento="11222333000181")
        self._pdfs(cfg, 1, kb=100)
        # Disco com 1 MB livre contra um item de ~100 KB e folga de 2x.
        monkeypatch.setattr(
            maquina, "ler",
            lambda *_: maquina.Saude(disco_livre_gb=0.0001))

        problema = _conferir_espaco(conn, cfg)
        assert "disco" in problema.lower()
        assert "1 itens" in problema


class TestComandosPelaRede:
    """As rotas que mexem na máquina exigem senha configurada.

    A capacidade perigosa nasce desligada: sem [rede] senha preenchida elas
    recusam tudo, em vez de ficarem abertas a quem alcançar a porta.
    """

    def _cliente(self, monkeypatch, tmp_path, senha: str):
        from cnd.infra.db import conectar, criar_schema
        from cnd.web import app as modulo

        banco = tmp_path / "cnd.db"
        criar_schema(conectar(banco))
        monkeypatch.setattr(
            modulo, "cfg",
            replace(carregar(), banco=banco,
                    rede=ConfigRede(nome="teste", senha=senha)))
        return fastapi_testclient.TestClient(modulo.app)

    def test_sem_senha_configurada_recusa_iniciar(self, monkeypatch, tmp_path):
        with self._cliente(monkeypatch, tmp_path, senha="") as cliente:
            resposta = cliente.post("/api/robo/iniciar")

        assert resposta.status_code == 403
        assert "senha" in resposta.json()["detail"]

    def test_sem_senha_configurada_recusa_planilha(self, monkeypatch, tmp_path):
        with self._cliente(monkeypatch, tmp_path, senha="") as cliente:
            resposta = cliente.post("/api/planilha",
                                    files={"arquivo": ("a.xlsx", b"x")})
        assert resposta.status_code == 403

    def test_com_senha_o_arquivo_errado_e_recusado(self, monkeypatch, tmp_path):
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post(
                "/api/planilha", headers={"X-CND-Senha": "segredo"},
                files={"arquivo": ("lista.txt", b"nao sou planilha")})

        assert resposta.status_code == 400
        assert ".xlsx" in resposta.json()["detail"]

    def test_area_bloqueada_recusa_com_motivo(self, monkeypatch, tmp_path):
        """O robô move o mouse de verdade; com a estação bloqueada ele
        gastaria consultas gravando erro atrás de erro."""
        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: False)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post("/api/robo/iniciar",
                                    headers={"X-CND-Senha": "segredo"})

        assert resposta.status_code == 409
        assert "bloqueada" in resposta.json()["detail"]

    def test_iniciar_pelo_painel_web_dispara_robo_visual(
        self, monkeypatch, tmp_path
    ):
        visto = {}

        def iniciar(raiz):
            visto["raiz"] = raiz
            return True, "iniciado"

        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: False)
        monkeypatch.setattr(comandos, "_iniciar_robo_visual", iniciar)
        (tmp_path / "parar.txt").write_text("parar", encoding="utf-8")

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post("/api/robo/iniciar",
                                    headers={"X-CND-Senha": "segredo"})

        assert resposta.status_code == 200
        assert resposta.json()["situacao"] == "iniciado"
        assert resposta.json()["mensagem"] == "Robô iniciado."
        assert visto["raiz"]
        assert not (tmp_path / "parar.txt").exists()

    def test_iniciar_pelo_painel_normaliza_ritmo_excessivo(
        self, monkeypatch, tmp_path
    ):
        from cnd.infra.db import conectar

        banco = tmp_path / "cnd.db"
        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: False)
        monkeypatch.setattr(
            comandos, "_iniciar_robo_visual", lambda _raiz: (True, "iniciado")
        )

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            conn = conectar(banco)
            try:
                conn.execute(
                    "INSERT INTO ritmo "
                    "(orgao, intervalo_s, consultas_limpas, atualizado_em) "
                    "VALUES ('RFB_PJ', 120, 0, '2026-01-01T00:00:00Z')"
                )
            finally:
                conn.close()

            resposta = cliente.post("/api/robo/iniciar",
                                    headers={"X-CND-Senha": "segredo"})

            conn = conectar(banco)
            try:
                intervalo = conn.execute(
                    "SELECT intervalo_s FROM ritmo WHERE orgao = 'RFB_PJ'"
                ).fetchone()["intervalo_s"]
            finally:
                conn.close()

        esperado = carregar().orgaos["RFB_PJ"].pacing.intervalo_inicial_s
        assert resposta.status_code == 200
        assert intervalo == esperado

    def test_iniciar_se_ja_tem_robo_vivo_nao_duplica(
        self, monkeypatch, tmp_path
    ):
        def nao_deveria_iniciar(*_args, **_kwargs):
            raise AssertionError("nao deveria iniciar outro robo")

        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: True)
        monkeypatch.setattr(comandos, "_iniciar_robo_visual", nao_deveria_iniciar)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post("/api/robo/iniciar",
                                    headers={"X-CND-Senha": "segredo"})

        assert resposta.status_code == 200
        assert resposta.json()["situacao"] == "Já está em execução."
        assert resposta.json()["mensagem"] == "Robô já está em execução."

    def test_iniciar_recupera_robo_ocioso_sem_sinal(
        self, monkeypatch, tmp_path
    ):
        visto = {"encerrou": False, "iniciou": False}

        def encerrar(_banco):
            visto["encerrou"] = True
            return True

        def iniciar(_raiz):
            visto["iniciou"] = True
            return True, "iniciado"

        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: True)
        monkeypatch.setattr(comandos, "_robo_ocioso_sem_sinal",
                            lambda _banco: True)
        monkeypatch.setattr(comandos, "_encerrar_robo_ocioso", encerrar)
        monkeypatch.setattr(comandos, "_iniciar_robo_visual", iniciar)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post("/api/robo/iniciar",
                                    headers={"X-CND-Senha": "segredo"})

        assert resposta.status_code == 200
        assert resposta.json()["situacao"] == "iniciado"
        assert visto == {"encerrou": True, "iniciou": True}

    def test_retomada_automatica_respeita_parada_manual(self, monkeypatch, tmp_path):
        cfg = replace(carregar(), banco=tmp_path / "cnd.db")
        comandos._marcar_parada_manual(cfg.banco)
        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: True)
        monkeypatch.setattr(
            comandos, "_iniciar_robo_visual",
            lambda _raiz: (_ for _ in ()).throw(
                AssertionError("nao deveria iniciar")
            ),
        )

        ok, situacao = comandos.iniciar_robo_da_maquina(
            cfg, tmp_path, automatico=True
        )

        assert ok is False
        assert "Parada manual" in situacao

    def test_api_reseta_pausa_do_orgao(self, monkeypatch, tmp_path):
        from cnd.core import breaker
        from cnd.infra.db import conectar

        banco = tmp_path / "cnd.db"
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            conn = conectar(banco)
            try:
                breaker.abrir(conn, "RFB_PJ", "teste", breaker.ParametrosBreaker())
                conn.execute("UPDATE breaker SET aberturas = 4 WHERE orgao = 'RFB_PJ'")
            finally:
                conn.close()

            resposta = cliente.post(
                "/api/breaker/RFB_PJ/retomar",
                headers={"X-CND-Senha": "segredo"},
            )

            conn = conectar(banco)
            try:
                estado = breaker.consultar(conn, "RFB_PJ")
            finally:
                conn.close()

        assert resposta.status_code == 200
        assert estado.estado == breaker.FECHADO
        assert estado.aberturas == 0

    def test_iniciar_visual_sem_desktop_do_processo_usa_tarefa(
        self, monkeypatch, tmp_path
    ):
        chamado = {}

        def por_tarefa(raiz):
            chamado["raiz"] = raiz
            return True, "Iniciado na sessão visual."

        def direto(_raiz):
            raise AssertionError("nao deveria iniciar direto")

        monkeypatch.setattr(comandos.sys, "platform", "win32")
        monkeypatch.setattr(comandos.maquina, "processo_robo_rodando",
                            lambda: False)
        monkeypatch.setattr(comandos.maquina, "processo_tem_area_de_trabalho",
                            lambda: False)
        monkeypatch.setattr(comandos, "_processo_e_system", lambda: False)
        monkeypatch.setattr(comandos, "_iniciar_robo_por_tarefa", por_tarefa)
        monkeypatch.setattr(comandos, "_iniciar_robo_direto", direto)

        ok, situacao = comandos._iniciar_robo_visual(tmp_path)

        assert ok is True
        assert situacao == "Iniciado na sessão visual."
        assert chamado["raiz"] == tmp_path

    def test_parar_nao_exige_area_de_trabalho(self, monkeypatch, tmp_path):
        """Parar é seguro com a tela bloqueada — e é justamente aí que se
        quer parar."""
        monkeypatch.setattr(comandos.maquina, "area_de_trabalho_disponivel",
                            lambda: False)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post("/api/robo/parar",
                                    headers={"X-CND-Senha": "segredo"})

        assert resposta.status_code == 200
        assert resposta.json()["ok"] is True
        assert (tmp_path / "parar.txt").exists()

    def test_parar_ocioso_encerra_processo_e_limpa_heartbeat(self, conn, monkeypatch):
        banco = conn.execute("PRAGMA database_list").fetchone()[2]
        heartbeat.bater(conn, "orquestrador")
        visto = {}

        def fingir_run(comando, **kwargs):
            visto["comando"] = comando
            visto["kwargs"] = kwargs
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(comandos.sys, "platform", "win32")
        monkeypatch.setattr(comandos.subprocess, "run", fingir_run)

        assert comandos._encerrar_robo_ocioso(banco)
        assert "powershell" in visto["comando"][0].lower()
        assert heartbeat.ultimo(conn, "orquestrador") is None

    def test_parar_nao_mata_processo_com_item_em_execucao(self, conn, lote, monkeypatch):
        job = criar_job(conn, lote, documento="11222333000181", orgao="RFB_PJ")
        conn.execute("UPDATE job SET status = ? WHERE id = ?", (Status.RUNNING, job))

        def nao_deveria_rodar(*_args, **_kwargs):
            raise AssertionError("nao deveria chamar o PowerShell")

        monkeypatch.setattr(comandos.sys, "platform", "win32")
        monkeypatch.setattr(comandos.subprocess, "run", nao_deveria_rodar)

        banco = conn.execute("PRAGMA database_list").fetchone()[2]
        assert not comandos._encerrar_robo_ocioso(banco)

    def test_parar_robo_inativo_devolve_job_orfao(self, monkeypatch, tmp_path):
        from cnd.infra.db import conectar

        banco = tmp_path / "cnd.db"
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            conn = conectar(banco)
            try:
                lote_id = conn.execute(
                    "INSERT INTO lote (descricao, arquivo_origem) VALUES ('t', 't.xlsx')"
                ).lastrowid
                job = criar_job(conn, lote_id, orgao="RFB_PJ")
                conn.execute("UPDATE job SET status = ? WHERE id = ?",
                             (Status.RUNNING, job))
            finally:
                conn.close()

            monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: False)
            resposta = cliente.post("/api/robo/parar",
                                    headers={"X-CND-Senha": "segredo"})

            conn = conectar(banco)
            try:
                status = conn.execute(
                    "SELECT status FROM job WHERE id = ?", (job,)
                ).fetchone()["status"]
            finally:
                conn.close()

        assert resposta.status_code == 200
        assert "devolvido" in resposta.json()["situacao"]
        assert status == Status.PENDING

    def test_atualizar_exige_origem_http(self, monkeypatch, tmp_path):
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post(
                "/api/atualizar", headers={"X-CND-Senha": "segredo"},
                data={"origem": "file:///C:/ACTA"},
            )

        assert resposta.status_code == 400
        assert "HTTP" in resposta.json()["detail"]

    def test_atualizar_recusa_sem_pacote_publicado(self, monkeypatch, tmp_path):
        monkeypatch.setattr(comandos, "_pacote_disponivel", lambda origem: False)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post(
                "/api/atualizar", headers={"X-CND-Senha": "segredo"},
                data={"origem": "http://10.1.11.86:8899"},
            )

        assert resposta.status_code == 400
        assert "acta.zip" in resposta.json()["detail"]

    def test_atualizar_recusa_item_em_execucao(self, monkeypatch, tmp_path):
        from cnd.infra.db import conectar

        banco = tmp_path / "cnd.db"
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: True)
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            conn = conectar(banco)
            try:
                lote_id = conn.execute(
                    "INSERT INTO lote (descricao, arquivo_origem) VALUES ('t', 't.xlsx')"
                ).lastrowid
                job = criar_job(conn, lote_id, orgao="RFB_PJ")
                conn.execute("UPDATE job SET status = ? WHERE id = ?",
                             (Status.RUNNING, job))
            finally:
                conn.close()

            resposta = cliente.post(
                "/api/atualizar", headers={"X-CND-Senha": "segredo"},
                data={"origem": "http://10.1.11.86:8899"},
            )

        assert resposta.status_code == 409
        assert "execução" in resposta.json()["detail"]

    def test_atualizar_devolve_orfao_antes_de_validar_execucao(
        self, monkeypatch, tmp_path
    ):
        from cnd.infra.db import conectar

        visto = {}
        banco = tmp_path / "cnd.db"

        def fingir_popen(comando, **kwargs):
            visto["comando"] = comando
            visto["kwargs"] = kwargs
            return SimpleNamespace(pid=123)

        monkeypatch.setattr(comandos, "_pacote_disponivel", lambda origem: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda _banco: False)
        monkeypatch.setattr(comandos, "_escrever_atualizador",
                            lambda: tmp_path / "a.ps1")
        monkeypatch.setattr(comandos.subprocess, "Popen", fingir_popen)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            conn = conectar(banco)
            try:
                lote_id = conn.execute(
                    "INSERT INTO lote (descricao, arquivo_origem) VALUES ('t', 't.xlsx')"
                ).lastrowid
                job = criar_job(conn, lote_id, orgao="RFB_PJ")
                conn.execute("UPDATE job SET status = ? WHERE id = ?",
                             (Status.RUNNING, job))
            finally:
                conn.close()

            resposta = cliente.post(
                "/api/atualizar", headers={"X-CND-Senha": "segredo"},
                data={"origem": "http://10.1.11.86:8899"},
            )

            conn = conectar(banco)
            try:
                status = conn.execute(
                    "SELECT status FROM job WHERE id = ?", (job,)
                ).fetchone()["status"]
            finally:
                conn.close()

        assert resposta.status_code == 200
        assert status == Status.PENDING
        assert "powershell" in visto["comando"][0].lower()

    def test_atualizar_dispara_script_em_segundo_plano(self, monkeypatch, tmp_path):
        visto = {}

        def fingir_popen(comando, **kwargs):
            visto["comando"] = comando
            visto["kwargs"] = kwargs
            return SimpleNamespace(pid=123)

        monkeypatch.setattr(comandos, "_pacote_disponivel", lambda origem: True)
        monkeypatch.setattr(comandos, "_robo_rodando", lambda banco: False)
        monkeypatch.setattr(comandos, "_escrever_atualizador",
                            lambda: tmp_path / "a.ps1")
        monkeypatch.setattr(comandos.subprocess, "Popen", fingir_popen)

        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.post(
                "/api/atualizar", headers={"X-CND-Senha": "segredo"},
                data={"origem": "http://10.1.11.86:8899"},
            )

        assert resposta.status_code == 200
        assert resposta.json()["situacao"] == "Atualização iniciada."
        assert resposta.json()["mensagem"] == "Atualização iniciada."
        assert "powershell" in visto["comando"][0].lower()
        assert "-Origem" in visto["comando"]
        assert "http://10.1.11.86:8899" in visto["comando"]

    def test_flash_polida_na_tela(self, monkeypatch, tmp_path):
        with self._cliente(monkeypatch, tmp_path, senha="segredo") as cliente:
            resposta = cliente.get(
                "/?erro=nao+consegui+baixar+o+pacote+em+"
                "http%3A%2F%2F100.125.207.8%3A8899%2Facta.zip",
                headers={"X-CND-Senha": "segredo"},
            )

        assert resposta.status_code == 200
        assert (
            "Não consegui baixar o pacote em "
            "http://100.125.207.8:8899/acta.zip."
        ) in resposta.text
        assert "history.replaceState" in resposta.text
        assert "searchParams.delete('erro')" in resposta.text
        assert "searchParams.delete('mensagem')" in resposta.text

    def test_detecta_robo_rodando_no_banco_configurado(self, conn, monkeypatch):
        monkeypatch.setattr(comandos.maquina, "processo_robo_rodando",
                            lambda: None)
        heartbeat.bater(conn, "orquestrador")
        banco = conn.execute("PRAGMA database_list").fetchone()[2]

        assert comandos._robo_rodando(banco)
