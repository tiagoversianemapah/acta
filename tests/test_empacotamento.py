"""A construção documentada tem de funcionar num checkout limpo.

O README manda `pip install -e ".[dev]"` e depois
`python empacotar/construir.py`. Duas coisas quebravam isso, e as duas
eram invisíveis para quem já tinha a máquina montada: o script copiava um
`config.toml` que não existe no repositório (ele é da INSTALAÇÃO), e o
.spec exigia o Playwright, que mora no extra `navegador` e não vem com
`[dev]`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture
def construir(monkeypatch):
    monkeypatch.syspath_prepend(str(RAIZ / "empacotar"))
    import construir as modulo
    return modulo


class TestArquivosDoOperador:
    def test_sem_config_na_raiz_cai_no_exemplo(self, construir, monkeypatch,
                                               tmp_path, capsys):
        """Estado de um checkout limpo. Quebrar aqui obrigava a inventar um
        config só para conseguir empacotar."""
        destino = tmp_path / "ACTA"
        destino.mkdir()
        monkeypatch.setattr(construir, "DESTINO", destino)
        monkeypatch.setattr(construir, "RAIZ", tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        (tmp_path / "repo" / "config.exemplo.toml").write_text(
            '[rede]\nnome = ""\n', encoding="utf-8")

        construir.levar_arquivos_do_operador()

        assert (destino / "config.toml").exists()
        assert "exemplo" in capsys.readouterr().out

    def test_config_existente_nunca_e_sobrescrito(self, construir, monkeypatch,
                                                  tmp_path):
        """Numa reconstrução, quem já ajustou a máquina não perde o ajuste."""
        destino = tmp_path / "ACTA"
        destino.mkdir()
        (destino / "config.toml").write_text("ajustado", encoding="utf-8")
        monkeypatch.setattr(construir, "DESTINO", destino)

        construir.levar_arquivos_do_operador()

        assert (destino / "config.toml").read_text(encoding="utf-8") == "ajustado"

    def test_cria_as_pastas_que_o_programa_espera(self, construir, monkeypatch,
                                                  tmp_path):
        destino = tmp_path / "ACTA"
        destino.mkdir()
        (destino / "config.toml").write_text("x", encoding="utf-8")
        monkeypatch.setattr(construir, "DESTINO", destino)

        construir.levar_arquivos_do_operador()

        assert {p.name for p in (destino / "data").iterdir()} >= {
            "certidoes", "evidencias", "logs", "calibragem"}


class TestSpec:
    def test_o_playwright_e_opcional(self):
        """Ele mora no extra `navegador`; `[dev]` não o traz. Exigir aqui
        fazia a construção documentada morrer com um ImportError do
        PyInstaller que não diz o que fazer.

        A conferência é na ÁRVORE do arquivo, e não por texto: o que
        importa é a chamada estar DENTRO de um `try`, e um `try` em outro
        ponto qualquer satisfaria uma busca por substring.
        """
        import ast

        fonte = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        protegidas = {
            no.func.id
            for tentativa in ast.walk(ast.parse(fonte))
            if isinstance(tentativa, ast.Try)
            for no in ast.walk(tentativa)
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
        }
        assert "collect_all" in protegidas, (
            "collect_all('playwright') fora de try: a construção documentada "
            "morre num checkout sem o extra `navegador`")
        assert "navegador" in fonte, "o aviso diz como incluir o CRF"

    def test_o_metadado_do_cnd_viaja_no_pacote(self):
        """Sem ele `importlib.metadata.version` volta vazia numa instalacao
        por copia de pasta — que e como o ACTA se instala. A tela fica sem
        versao, e "esta maquina pegou a atualizacao?" fica sem resposta."""
        fonte = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        assert 'copy_metadata("cnd")' in fonte
        assert "*cnd_metadados," in fonte, "coletado mas nao incluido em `dados`"

    def test_o_spec_e_python_valido(self):
        """Erro de sintaxe aqui só aparece na hora de empacotar, que é o
        pior momento para descobrir."""
        fonte = (RAIZ / "empacotar" / "acta.spec").read_text(encoding="utf-8")
        compile(fonte, "acta.spec", "exec")


def test_o_ruff_documentado_cobre_todo_o_codigo_do_repositorio():
    """Pasta fora do comando do README é pasta que ninguém confere: o
    apontamento só aparece para quem rodar o comando completo por acaso."""
    raiz = RAIZ
    texto = (raiz / "README.md").read_text(encoding="utf-8")
    linha = next(li for li in texto.splitlines() if li.startswith("ruff check"))
    for pasta in ("src", "tests", "empacotar", "ferramentas"):
        assert pasta in linha, f"{pasta} fora do ruff documentado"


def test_o_readme_manda_instalar_o_extra_para_o_pacote_completo():
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    assert '".[dev,navegador]"' in texto


@pytest.mark.skipif(sys.platform != "win32", reason="empacotamento é Windows")
def test_construir_importa_sem_efeito_colateral(construir):
    """Importar o módulo não pode construir nada nem apagar pasta."""
    assert hasattr(construir, "levar_arquivos_do_operador")
    assert construir.DESTINO.name == "ACTA"
