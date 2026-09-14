"""OCR do captcha da SEFAZ-MA — mecanismo, sem rede e sem captcha real.

Estes testes exercitam a máquina (segmentar, normalizar, casar, aprender,
gravar) com glifos SINTÉTICOS desenhados na hora pela Pillow. Não medem a
acurácia contra o portal — isso é papel da ferramenta de treino, que roda
contra o site real —, mas travam o que quebra em silêncio: segmentar o número
errado de caracteres, um banco de outro tamanho ser lido como se servisse, ou
o aprendizado aceitar rótulo de captcha que o portal não confirmou.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from cnd.adapters.estadual import captcha_ma
from cnd.adapters.estadual.captcha_ma import BancoCaptcha


def _compor(texto: str) -> Image.Image:
    """Escreve o texto em quatro células de 25px, com folga entre elas.

    A fonte embutida da Pillow é pequena, então cada caractere fica sozinho na
    sua célula e sobra coluna em branco entre eles — que é exatamente a
    condição que a segmentação por projeção vertical espera.
    """
    imagem = Image.new("L", (100, 25), 255)
    desenho = ImageDraw.Draw(imagem)
    for i, ch in enumerate(texto):
        desenho.text((i * 25 + 9, 9), ch, fill=0)
    return imagem


class TestSegmentacao:
    def test_quatro_caracteres_viram_quatro_recortes(self):
        recortes = captcha_ma.segmentar(_compor("ab12"))
        assert len(recortes) == 4

    def test_recorte_vem_no_bounding_box_da_tinta(self):
        # A célula tem 25px, mas o recorte é justo no glifo — bem menor.
        recortes = captcha_ma.segmentar(_compor("wwww"))
        assert all(r.size[0] < 25 for r in recortes)


class TestLeitura:
    def _banco_treinado(self, palavras: list[str]) -> BancoCaptcha:
        banco = BancoCaptcha()
        for p in palavras:
            banco.aprender(_compor(p), p)
        return banco

    def test_le_o_que_aprendeu(self):
        banco = self._banco_treinado(["abcd", "ef12", "gh34"])
        # Mesmos glifos, outra ordem: como o desenho é idêntico, a distância
        # é zero e a leitura tem de bater exatamente.
        assert banco.ler(_compor("dcba")) == "dcba"
        assert banco.ler(_compor("4h2f")) == "4h2f"

    def test_banco_vazio_nao_arrisca_palpite(self):
        assert BancoCaptcha().ler(_compor("abcd")) is None

    def test_numero_errado_de_glifos_vira_none(self):
        banco = self._banco_treinado(["abcd"])
        # Três caracteres: não é um captcha válido, então não há o que conferir.
        assert banco.ler(_compor("abc")) is None


class TestAprendizado:
    def test_so_aprende_captcha_do_tamanho_certo(self):
        banco = BancoCaptcha()
        assert banco.aprender(_compor("abc"), "abc") == 0
        assert banco.total == 0

    def test_nao_repete_bitmap_identico(self):
        banco = BancoCaptcha()
        banco.aprender(_compor("aaaa"), "aaaa")
        antes = banco.total
        banco.aprender(_compor("aaaa"), "aaaa")
        assert banco.total == antes, "o mesmo glifo não deve inflar o banco"

    def test_respeita_o_teto_por_classe(self, monkeypatch):
        monkeypatch.setattr(captcha_ma, "MAX_POR_CLASSE", 2)
        banco = BancoCaptcha()
        for i in range(5):
            # Bitmaps diferentes para não caírem no dedupe.
            banco.registrar("a", 1 << i)
        assert len(banco.amostras["a"]) == 2

    def test_ignora_classe_fora_do_alfabeto(self):
        banco = BancoCaptcha()
        banco.registrar("Ç", 123)
        banco.registrar("!", 456)
        assert banco.total == 0


class TestPersistencia:
    def test_ida_e_volta_preserva_o_banco(self, tmp_path):
        banco = BancoCaptcha()
        banco.aprender(_compor("ab12"), "ab12")
        alvo = tmp_path / "banco.json"
        banco.salvar(alvo)

        recarregado = BancoCaptcha.carregar(alvo)
        assert recarregado.amostras == banco.amostras

    def test_banco_ausente_vira_banco_vazio(self, tmp_path):
        banco = BancoCaptcha.carregar(tmp_path / "nao_existe.json")
        assert banco.vazio

    def test_banco_de_outra_caixa_e_recusado(self, tmp_path):
        alvo = tmp_path / "banco.json"
        alvo.write_text('{"caixa": 999, "amostras": {"a": ["1"]}}',
                        encoding="utf-8")
        # Ilegível para esta versão -> carga devolve vazio (não lê lixo),
        # e o de_json cru levanta, para a ferramenta de treino saber.
        assert BancoCaptcha.carregar(alvo).vazio
        import pytest
        with pytest.raises(ValueError):
            BancoCaptcha.de_json(alvo.read_text(encoding="utf-8"))

    def test_json_corrompido_vira_banco_vazio(self, tmp_path):
        alvo = tmp_path / "banco.json"
        alvo.write_text("{nao é json", encoding="utf-8")
        assert BancoCaptcha.carregar(alvo).vazio
