"""Testes de classificação do adapter da Receita Federal.

Os textos abaixo foram colhidos do portal e de um PDF reais, no mapeamento
de 07/08/2026. Nenhum teste toca na internet: eles protegem exatamente a
parte que quebra em silêncio quando a Receita muda uma palavra — a leitura
do resultado.
"""
from __future__ import annotations

from datetime import date

from cnd.adapters.rfb_pj import (
    FRASE_BLOQUEIO,
    FRASE_INSUFICIENTE,
    FRASE_PROCESSANDO,
    FRASE_SUCESSO,
    TITULO_CPEN,
    TITULO_NEGATIVA,
    _extrair_codigo,
    _extrair_validade,
    _normalizar,
)

# --- textos reais do portal -------------------------------------------------

TELA_PROCESSANDO = "Estamos analisando seu pedido de emissão de certidão. Aguarde."

TELA_SUCESSO = """A certidão foi emitida com sucesso para o CNPJ 12.188.874/0001-01.

Por favor, verifique se o arquivo PDF da certidão foi apresentado ou se houve
download do arquivo no navegador.

Caso não ocorra o download automático da certidão, faça o download do documento
PDF da certidão."""

TELA_INSUFICIENTE = """As informações disponíveis na Receita Federal sobre o
contribuinte 32.874.104/0001-11 são insuficientes para emitir a certidão pela
Internet."""

# Faixa amarela vista no piloto de 07/08/2026.
ALERTA_BLOQUEIO = """Não foi possível concluir a ação para o contribuinte
informado. Por favor, tente novamente dentro de alguns minutos. 023 -
07/08/2026 10:49:50"""

PDF_NEGATIVA = """MINISTÉRIO DA FAZENDA
Secretaria da Receita Federal do Brasil
Procuradoria-Geral da Fazenda Nacional

CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA
ATIVA DA UNIÃO

Nome: BRAVA ENGENHARIA LTDA
CNPJ: 12.188.874/0001-01

Ressalvado o direito de a Fazenda Nacional cobrar e inscrever quaisquer dívidas
de responsabilidade do sujeito passivo acima identificado que vierem a ser
apuradas, é certificado que não constam pendências em seu nome.

Certidão emitida gratuitamente com base na Portaria Conjunta RFB/PGFN nº 1.751,
de 2/10/2014.
Emitida às 10:28:14 do dia 07/08/2026 <hora e data de Brasília>.
Válida até 03/02/2027.
Código de controle da certidão: EE24.3D3A.8D62.B7B1
Qualquer rasura ou emenda invalidará este documento."""

PDF_CPEN = PDF_NEGATIVA.replace(
    "CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA\nATIVA DA UNIÃO",
    "CERTIDÃO POSITIVA COM EFEITOS DE NEGATIVA DE DÉBITOS RELATIVOS AOS\nTRIBUTOS FEDERAIS E À DÍVIDA ATIVA DA UNIÃO",
)


class TestNormalizar:
    def test_junta_quebras_de_linha(self):
        """O portal quebra linha no meio das frases: sem colapsar espaços,
        a comparação daria falso negativo."""
        assert FRASE_INSUFICIENTE in _normalizar(TELA_INSUFICIENTE)

    def test_ignora_maiusculas(self):
        assert TITULO_NEGATIVA in _normalizar(PDF_NEGATIVA)

    def test_texto_vazio(self):
        assert _normalizar(None) == ""
        assert _normalizar("   \n  ") == ""


class TestReconhecimentoDeTela:
    def test_sucesso(self):
        normalizado = _normalizar(TELA_SUCESSO)
        assert FRASE_SUCESSO in normalizado
        assert FRASE_INSUFICIENTE not in normalizado

    def test_insuficiente(self):
        normalizado = _normalizar(TELA_INSUFICIENTE)
        assert FRASE_INSUFICIENTE in normalizado
        assert FRASE_SUCESSO not in normalizado

    def test_tela_de_espera_e_reconhecida(self):
        """Se o robô ler esta tela como resultado, classifica errado sem
        quebrar nada — o pior tipo de bug."""
        assert FRASE_PROCESSANDO in _normalizar(TELA_PROCESSANDO)

    def test_tela_de_espera_nao_parece_resultado(self):
        normalizado = _normalizar(TELA_PROCESSANDO)
        assert FRASE_SUCESSO not in normalizado
        assert FRASE_INSUFICIENTE not in normalizado

    def test_bloqueio_temporario(self):
        """O portal pedindo para voltar depois não é erro nosso nem resposta
        sobre a empresa: é ele nos barrando. Confundir com erro técnico faria
        o robô insistir no ritmo errado."""
        normalizado = _normalizar(ALERTA_BLOQUEIO)
        assert FRASE_BLOQUEIO in normalizado
        assert FRASE_SUCESSO not in normalizado
        assert FRASE_INSUFICIENTE not in normalizado


class TestClassificacaoDoPDF:
    def test_negativa(self):
        normalizado = _normalizar(PDF_NEGATIVA)
        assert TITULO_NEGATIVA in normalizado
        assert TITULO_CPEN not in normalizado

    def test_cpen(self):
        normalizado = _normalizar(PDF_CPEN)
        assert TITULO_CPEN in normalizado

    def test_cpen_tem_prioridade_sobre_negativa(self):
        """'positiva com efeitos de negativa' contém a palavra 'negativa'.
        A ordem dos testes no adapter precisa checar CPEN primeiro."""
        normalizado = _normalizar(PDF_CPEN)
        assert TITULO_CPEN in normalizado
        assert TITULO_NEGATIVA not in normalizado


class TestExtracao:
    def test_validade(self):
        assert _extrair_validade(PDF_NEGATIVA) == date(2027, 2, 3)

    def test_validade_ausente(self):
        assert _extrair_validade("documento sem data") is None

    def test_validade_ignora_a_data_de_emissao(self):
        """O PDF tem duas datas; a validade é a que vem depois de 'Válida até'."""
        assert _extrair_validade(PDF_NEGATIVA) != date(2026, 8, 7)

    def test_codigo_de_controle(self):
        assert _extrair_codigo(PDF_NEGATIVA) == "EE24.3D3A.8D62.B7B1"

    def test_codigo_ausente(self):
        assert _extrair_codigo("documento sem código") is None

    def test_codigo_nao_leva_o_ponto_final(self):
        texto = "Código de controle da certidão: AB12.CD34.EF56.7890."
        assert _extrair_codigo(texto) == "AB12.CD34.EF56.7890"
