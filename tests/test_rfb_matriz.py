from cnd.adapters import rfb_matriz
from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa


def _doc(documento: str, tipo: str = "CNPJ") -> Documento:
    return Documento(
        empresa_id=1,
        documento=documento,
        tipo=tipo,
        nome="EMPRESA TESTE",
        lote_id=7,
    )


def test_documento_para_consulta_mantem_matriz():
    original = _doc("00082253000151")

    consulta, mensagem = rfb_matriz.documento_para_consulta(original)

    assert consulta is original
    assert mensagem is None


def test_documento_para_consulta_troca_filial_por_matriz():
    original = _doc("00082253000232")

    consulta, mensagem = rfb_matriz.documento_para_consulta(original)

    assert original.documento == "00082253000232"
    assert consulta.documento == "00082253000151"
    assert consulta.nome == original.nome
    assert "00.082.253/0002-32" in mensagem
    assert "00.082.253/0001-51" in mensagem


def test_documento_para_consulta_ignora_cpf():
    original = _doc("52998224725", tipo="CPF")

    consulta, mensagem = rfb_matriz.documento_para_consulta(original)

    assert consulta is original
    assert mensagem is None


def test_anotar_matriz_preserva_mensagem_do_portal():
    resultado = ResultadoTentativa(
        Desfecho.PENDENCIA_MANUAL,
        mensagem_portal="informacoes insuficientes",
    )

    anotado = rfb_matriz.anotar_matriz(resultado, "filial X consultada pela matriz Y")

    assert anotado.mensagem_portal == (
        "filial X consultada pela matriz Y; informacoes insuficientes"
    )
    assert resultado.mensagem_portal == "informacoes insuficientes"
