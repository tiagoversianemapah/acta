"""Saúde da máquina, papel dela e gravação no config.toml."""
from __future__ import annotations

from dataclasses import replace

import pytest

from cnd.infra import ajustes, maquina
from cnd.infra.config import ConfigAlertas, ConfigRede


class TestConfigAlertas:
    def test_smtp_sem_senha_nao_fica_habilitado(self):
        cfg = ConfigAlertas(
            metodo="smtp",
            smtp_host="smtp.example.com",
            smtp_usuario="robo@example.com",
            destinatarios=("ops@example.com",),
        )

        assert not cfg.habilitado
        assert any("smtp_senha" in campo for campo in cfg.o_que_falta())


class TestSaudeDaMaquina:
    def test_le_memoria_e_disco_desta_maquina(self, tmp_path):
        saude = maquina.ler(tmp_path)

        assert saude.nome
        assert saude.ram_total_gb > 0, "toda máquina tem memória"
        assert saude.disco_total_gb > 0
        assert 0 <= saude.ram_percentual <= 100

    def test_caminho_inexistente_nao_quebra(self, tmp_path):
        """A pasta de dados pode ainda não existir na primeira execução."""
        saude = maquina.ler(tmp_path / "ainda" / "nao" / "existe")
        assert saude.disco_total_gb > 0

    def test_vira_dicionario_com_numeros_redondos(self, tmp_path):
        dados = maquina.ler(tmp_path).como_dicionario()

        assert set(dados) == {"nome", "ram_total_gb", "ram_usada_gb",
                              "disco_total_gb", "disco_livre_gb",
                              "ligada_ha_h", "avisos"}
        assert isinstance(dados["avisos"], list)

    def test_disco_cheio_vira_aviso(self, monkeypatch, tmp_path):
        """Disco cheio faz o robô emitir a certidão e não conseguir salvá-la."""
        import shutil

        class UsoFalso:
            total, free, used = 500 * maquina.GIGA, maquina.GIGA, 0

        monkeypatch.setattr(shutil, "disk_usage", lambda _: UsoFalso)
        assert "disco quase cheio" in maquina.ler(tmp_path).avisos


class TestPapelDaMaquina:
    def test_sem_maquinas_cadastradas_ela_e_o_robo(self):
        assert ConfigRede().roda_robo

    def test_quem_lista_outras_maquinas_esta_acompanhando(self):
        from cnd.infra.config import Maquina

        rede = ConfigRede(maquinas=(Maquina("PC-01", "http://x"),))
        assert not rede.roda_robo

    def test_o_que_esta_escrito_manda(self):
        """Máquina de robô que também acompanha as outras é caso real."""
        from cnd.infra.config import Maquina

        rede = ConfigRede(papel="robo", maquinas=(Maquina("PC-01", "http://x"),))
        assert rede.roda_robo
        assert not replace(rede, papel="console").roda_robo


class TestGravarNoConfig:
    """O config é escrito à mão e cheio de comentário — é ele que documenta
    a instalação. Gravar não pode levar os comentários junto."""

    @pytest.fixture(autouse=True)
    def config_temporario(self, monkeypatch, tmp_path):
        arquivo = tmp_path / "config.toml"
        arquivo.write_text(
            "# comentário do topo\n"
            "[rede]\n"
            "# como esta máquina aparece\n"
            'nome  = "PC-CND-01"\n'
            'senha = ""\n'
            "\n"
            "[geral]\n"
            'banco = "data/cnd.db"\n',
            encoding="utf-8")
        monkeypatch.setattr(ajustes, "_caminho", lambda: arquivo)
        return arquivo

    def test_altera_chave_existente(self, config_temporario):
        ajustes.gravar_valor("rede", "nome", "PC-CND-09")

        assert ajustes.ler_valor("rede", "nome") == "PC-CND-09"
        assert ajustes.ler_valor("geral", "banco") == "data/cnd.db"

    def test_preserva_os_comentarios(self, config_temporario):
        ajustes.gravar_valor("rede", "nome", "OUTRO")

        texto = config_temporario.read_text(encoding="utf-8")
        assert "# comentário do topo" in texto
        assert "# como esta máquina aparece" in texto

    def test_cria_a_chave_que_nao_existe_na_secao_certa(self, config_temporario):
        ajustes.gravar_valor("rede", "anydesk", "123456789")

        assert ajustes.ler_valor("rede", "anydesk") == "123456789"
        # A chave nova não pode vazar para a seção seguinte.
        texto = config_temporario.read_text(encoding="utf-8")
        assert texto.index("anydesk") < texto.index("[geral]")

    def test_cria_a_secao_que_nao_existe(self, config_temporario):
        ajustes.gravar_valor("novidade", "chave", "valor")
        assert ajustes.ler_valor("novidade", "chave") == "valor"

    def test_chave_ausente_le_como_vazio(self):
        assert ajustes.ler_valor("rede", "inexistente") == ""

    def test_continua_valido_para_o_leitor_do_projeto(self, config_temporario):
        """De nada adianta gravar se o config parar de carregar depois."""
        import tomllib

        ajustes.gravar_valor("rede", "anydesk", "123 456 789")
        dados = tomllib.loads(config_temporario.read_text(encoding="utf-8"))

        assert dados["rede"]["anydesk"] == "123 456 789"
        assert dados["geral"]["banco"] == "data/cnd.db"
