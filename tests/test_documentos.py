import pytest

from cnd.core.documentos import (
    DocumentoInvalido,
    formatar,
    validar_cnpj,
    validar_cpf,
)


class TestCNPJ:
    def test_valido_com_mascara(self):
        assert validar_cnpj("11.222.333/0001-81") == "11222333000181"

    def test_valido_sem_mascara(self):
        assert validar_cnpj("11222333000181") == "11222333000181"

    def test_alfanumerico(self):
        # Exemplo oficial do novo formato da Receita Federal.
        assert validar_cnpj("12.ABC.345/01DE-35") == "12ABC34501DE35"

    def test_zeros_a_esquerda_comidos_pelo_excel(self):
        assert validar_cnpj(11222333000181) == "11222333000181"

    def test_digito_errado(self):
        with pytest.raises(DocumentoInvalido, match="dígito verificador"):
            validar_cnpj("11222333000182")

    def test_todos_iguais(self):
        with pytest.raises(DocumentoInvalido, match="iguais"):
            validar_cnpj("00000000000000")

    def test_curto_demais(self):
        with pytest.raises(DocumentoInvalido):
            validar_cnpj("123")

    def test_vazio(self):
        with pytest.raises(DocumentoInvalido):
            validar_cnpj(None)


class TestCPF:
    def test_valido_com_mascara(self):
        assert validar_cpf("529.982.247-25") == "52998224725"

    def test_zeros_a_esquerda(self):
        assert validar_cpf("1234567890") == "01234567890"

    def test_digito_errado(self):
        with pytest.raises(DocumentoInvalido, match="dígito verificador"):
            validar_cpf("52998224726")

    def test_todos_iguais(self):
        with pytest.raises(DocumentoInvalido, match="iguais"):
            validar_cpf("11111111111")


def test_formatar():
    assert formatar("11222333000181") == "11.222.333/0001-81"
    assert formatar("52998224725") == "529.982.247-25"
