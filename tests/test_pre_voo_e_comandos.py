"""Duas travas que impedem estrago: espaço em disco e comando remoto."""
from __future__ import annotations

from dataclasses import replace

import pytest
from tests.conftest import criar_job

from cnd.infra.config import ConfigRede, carregar
from cnd.orquestrador.loop import _conferir_espaco
from cnd.web import comandos

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
