"""As planilhas guardadas na máquina, para enfileirar sem reenviar.

O arquivo enviado passou a ficar guardado — e o nome dele vem de fora, do
formulário de upload. Estes testes cobrem o que isso abre: nome que tenta
sair da pasta, nome que o Windows não aceita, e reenvio no mesmo segundo.
"""
from __future__ import annotations

import pytest

from cnd.infra import carteiras


@pytest.fixture
def banco(tmp_path):
    return tmp_path / "cnd.db"


@pytest.fixture
def planilha(tmp_path):
    caminho = tmp_path / "origem.xlsx"
    caminho.write_bytes(b"conteudo da planilha")
    return caminho


class TestQuantasFicam:
    def test_guarda_as_tres_mais_recentes(self, banco, planilha):
        for i in range(5):
            carteiras.guardar(banco, planilha, f"CARTEIRA_{i}.xlsx")

        guardadas = carteiras.listar(banco)

        assert len(guardadas) == carteiras.QUANTAS_GUARDAR == 3
        assert [g.nome for g in guardadas] == [
            "CARTEIRA_4.xlsx", "CARTEIRA_3.xlsx", "CARTEIRA_2.xlsx"]

    def test_a_mais_antiga_some_do_disco(self, banco, planilha):
        for i in range(4):
            carteiras.guardar(banco, planilha, f"C_{i}.xlsx")

        no_disco = [p.name for p in carteiras.pasta(banco).iterdir()]

        assert len(no_disco) == 3
        assert not any("C_0" in n for n in no_disco), "a mais antiga ficou"

    def test_pasta_vazia_nao_quebra(self, banco):
        assert carteiras.listar(banco) == []
        assert carteiras.aposentar(banco) == 0


class TestNomeVindoDeFora:
    """O nome vem do formulário de upload — é entrada de fora."""

    def test_nome_que_tenta_subir_de_pasta_fica_dentro(self, banco, planilha):
        guardada = carteiras.guardar(
            banco, planilha, "../../../ACTA/config.toml")

        assert carteiras.pasta(banco) in guardada.caminho.parents
        assert guardada.nome == "config.toml"

    def test_caractere_que_o_windows_recusa_vira_underline(self, banco, planilha):
        """`CND:MIA.xlsx` criava um alternate data stream: o arquivo
        aparecia como `CND`, sem extensão, e a planilha ficava inacessível
        pelo nome que a tela mostrava. E a cópia não dava erro nenhum."""
        guardada = carteiras.guardar(banco, planilha, "CND:MIA?0826.xlsx")

        assert guardada.caminho.suffix == ".xlsx", "perdeu a extensão"
        assert ":" not in guardada.caminho.name
        assert guardada.caminho.exists()

    def test_token_de_fora_nao_acha_arquivo(self, banco, planilha):
        carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")

        assert carteiras.buscar(banco, "../../config.toml") is None
        assert carteiras.buscar(banco, "nao-existe.xlsx") is None

    def test_token_valido_acha(self, banco, planilha):
        guardada = carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")

        achada = carteiras.buscar(banco, guardada.token)

        assert achada is not None
        assert achada.caminho == guardada.caminho


class TestReenvioNoMesmoSegundo:
    def test_mesma_planilha_duas_vezes_nao_sobrescreve(self, banco, planilha):
        """É exatamente o que acontece quando alguém manda a versão
        corrigida logo em seguida — e perder a anterior sem aviso seria o
        pior jeito de descobrir que o carimbo tem só segundos."""
        primeira = carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")
        segunda = carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")

        assert primeira.token != segunda.token
        assert len(carteiras.listar(banco)) == 2
        assert primeira.caminho.exists() and segunda.caminho.exists()

    def test_as_duas_mostram_o_mesmo_nome_para_quem_le(self, banco, planilha):
        """O sufixo de desempate é do disco, não da tela."""
        carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")
        carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")

        assert [g.nome for g in carteiras.listar(banco)] == [
            "CARTEIRA.xlsx", "CARTEIRA.xlsx"]


class TestConteudo:
    def test_o_arquivo_guardado_e_o_que_foi_enviado(self, banco, planilha):
        guardada = carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")

        assert guardada.caminho.read_bytes() == planilha.read_bytes()

    def test_arquivo_estranho_na_pasta_e_ignorado(self, banco, planilha):
        """Alguém que copie um arquivo ali por AnyDesk não quebra a tela."""
        carteiras.guardar(banco, planilha, "CARTEIRA.xlsx")
        (carteiras.pasta(banco) / "anotacao.txt").write_text("oi")

        assert [g.nome for g in carteiras.listar(banco)] == ["CARTEIRA.xlsx"]
