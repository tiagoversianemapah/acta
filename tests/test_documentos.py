import pytest

from cnd.core.documentos import (
    DocumentoInvalido,
    cnpj_da_matriz,
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

    def test_cnpj_da_matriz_mantem_matriz(self):
        assert cnpj_da_matriz("00.082.253/0001-51") == "00082253000151"

    def test_cnpj_da_matriz_converte_filial(self):
        assert cnpj_da_matriz("00.082.253/0002-32") == "00082253000151"


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
    assert formatar("12ABC34501DE35") == "12.ABC.345/01DE-35"
    assert formatar("52998224725") == "529.982.247-25"


def test_cpf_na_aba_de_cnpj_diz_que_e_cpf():
    """A queixa não pode ser sobre um número que ninguém digitou.

    O importador preenche com zeros à esquerda porque o Excel os come de
    CNPJ — e isso transformava um CPF de 11 dígitos em '000...' de 14, com
    o erro reclamando desse número inventado. Quem conferia achava que o
    cadastro estava errado, quando a linha só estava na aba errada.
    """
    with pytest.raises(DocumentoInvalido) as erro:
        validar_cnpj("111.444.777-35")
    assert "é um CPF" in str(erro.value)
    assert "aba CPF" in str(erro.value)
    assert "000" not in str(erro.value), "não pode citar o número preenchido"


def test_cnpj_com_zero_comido_pelo_excel_continua_passando():
    """A melhoria acima não pode custar o caso que o zfill resolve."""
    assert validar_cnpj("6031097000186") == "06031097000186"


def test_onze_digitos_que_nao_sao_cpf_mantem_a_queixa_de_cnpj():
    """Pode ser CNPJ com zeros comidos, então não se afirma que é CPF."""
    with pytest.raises(DocumentoInvalido) as erro:
        validar_cnpj("12345678901")
    assert "CPF" not in str(erro.value)
