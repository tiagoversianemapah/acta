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


def test_faixa_do_portal_e_reconhecida_como_pedido_de_matriz():
    texto = (
        "A certidão deve ser emitida para o CNPJ da matriz – 04.401.250/0001-94"
    )

    assert rfb_matriz.exige_matriz(texto) is True
    assert rfb_matriz.matriz_exigida_no_texto(texto) == "04401250000194"


def test_frase_quebrada_em_varias_linhas_continua_sendo_reconhecida():
    """O portal quebra linha no meio da frase; comparar o texto cru daria
    falso negativo e o robô trataria a faixa como bloqueio."""
    texto = (
        "A certidão deve ser emitida\npara o CNPJ da   matriz\n"
        "– 04.401.250/0001-94"
    )

    assert rfb_matriz.matriz_exigida_no_texto(texto) == "04401250000194"


def test_cnpj_digitado_antes_da_frase_nao_e_confundido_com_a_matriz():
    """A tela mostra os dois números. Pegar o primeiro da página traria de
    volta justamente o CNPJ que o portal acabou de recusar."""
    texto = (
        "CNPJ 04.401.250/0008-60 "
        "A certidão deve ser emitida para o CNPJ da matriz – 04.401.250/0001-94"
    )

    assert rfb_matriz.matriz_exigida_no_texto(texto) == "04401250000194"


def test_texto_sem_a_frase_nao_devolve_matriz():
    texto = "Não foi possível concluir a ação para o contribuinte informado."

    assert rfb_matriz.exige_matriz(texto) is False
    assert rfb_matriz.matriz_exigida_no_texto(texto) is None


def test_frase_sem_cnpj_legivel_devolve_none():
    """Melhor não achar do que achar errado: com um número ilegível, quem
    chama decide (e manda para conferência) em vez de digitar lixo."""
    texto = "A certidão deve ser emitida para o CNPJ da matriz – 04.401.250/0001"

    assert rfb_matriz.exige_matriz(texto) is True
    assert rfb_matriz.matriz_exigida_no_texto(texto) is None


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
