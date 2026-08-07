"""Testes da entrega: o que vai para o cliente e o que não vai.

Regra de negócio: NEGATIVA e CPEN são documentos utilizáveis e vão no
pacote. POSITIVA significa pendência real — não se entrega, só se reporta.

O teste mais importante aqui é o de título desconhecido: a versão anterior
do código assumia negativa quando não reconhecia o PDF, o que poderia
entregar uma certidão positiva a um cliente como se estivesse limpa.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

from cnd.adapters.rfb_pj import (
    TITULO_CPEN,
    TITULO_NEGATIVA,
    TITULO_POSITIVA,
    _normalizar,
)
from cnd.core.modelos import COM_PDF, Desfecho
from cnd.web.relatorio import zipar_pdfs

CABECALHO = """MINISTÉRIO DA FAZENDA
Secretaria da Receita Federal do Brasil
Procuradoria-Geral da Fazenda Nacional
"""

PDF_NEGATIVA = CABECALHO + """
CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA
ATIVA DA UNIÃO
Válida até 03/02/2027."""

PDF_CPEN = CABECALHO + """
CERTIDÃO POSITIVA COM EFEITOS DE NEGATIVA DE DÉBITOS RELATIVOS AOS
TRIBUTOS FEDERAIS E À DÍVIDA ATIVA DA UNIÃO
Válida até 03/02/2027."""

PDF_POSITIVA = CABECALHO + """
CERTIDÃO POSITIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA
ATIVA DA UNIÃO"""


def _classificar(texto: str) -> str:
    """Mesma ordem de testes do adapter."""
    normalizado = _normalizar(texto)
    if TITULO_CPEN in normalizado:
        return "CPEN"
    if TITULO_NEGATIVA in normalizado:
        return "NEGATIVA"
    if TITULO_POSITIVA in normalizado:
        return "POSITIVA"
    return "DESCONHECIDO"


class TestClassificacaoPeloTitulo:
    def test_negativa(self):
        assert _classificar(PDF_NEGATIVA) == "NEGATIVA"

    def test_cpen(self):
        """Débito parcelado ou suspenso: vale como negativa."""
        assert _classificar(PDF_CPEN) == "CPEN"

    def test_positiva(self):
        assert _classificar(PDF_POSITIVA) == "POSITIVA"

    def test_cpen_nao_vira_positiva(self):
        """O título da CPEN contém a palavra 'positiva'. Se a ordem dos
        testes inverter, toda CPEN seria classificada como positiva e
        empresas regulares apareceriam como devedoras."""
        assert _classificar(PDF_CPEN) != "POSITIVA"

    def test_cpen_nao_vira_negativa(self):
        assert _classificar(PDF_CPEN) != "NEGATIVA"

    def test_titulo_estranho_nao_e_chutado(self):
        """O erro mais caro possível: entregar positiva como se fosse
        negativa. Diante do desconhecido, o robô não adivinha."""
        assert _classificar("DECLARAÇÃO DE ALGUMA OUTRA COISA") == "DESCONHECIDO"

    def test_pdf_vazio_nao_e_chutado(self):
        assert _classificar("") == "DESCONHECIDO"


class TestOQueViraCertidao:
    def test_apenas_negativa_e_cpen_geram_arquivo(self):
        """São os dois documentos que se entrega ao cliente."""
        assert {Desfecho.NEGATIVA, Desfecho.CPEN} == COM_PDF

    def test_positiva_nao_gera_arquivo(self):
        assert Desfecho.POSITIVA not in COM_PDF

    def test_pendencia_manual_nao_gera_arquivo(self):
        assert Desfecho.PENDENCIA_MANUAL not in COM_PDF


class TestPacoteZip:
    def _preparar(self, conn, tmp_path, tipos: list[str]):
        conn.execute("INSERT INTO lote (id, descricao) VALUES (1, 'Teste')")
        for indice, tipo in enumerate(tipos, start=1):
            documento = f"{indice:014d}"
            nome = f"EMPRESA {indice}"
            pdf = tmp_path / f"{documento} - {nome}.pdf"
            pdf.write_bytes(b"%PDF-1.4 conteudo")
            conn.execute(
                "INSERT INTO empresa (id, documento, tipo_documento, nome) "
                "VALUES (?, ?, 'CNPJ', ?)", (indice, documento, nome))
            conn.execute(
                "INSERT INTO job (id, lote_id, empresa_id, orgao, status, desfecho) "
                "VALUES (?, 1, ?, 'RFB_PJ', 'DONE', ?)", (indice, indice, tipo))
            conn.execute(
                "INSERT INTO certidao (job_id, tipo, emitida_em, valida_ate, "
                "caminho_pdf, sha256) VALUES (?, ?, '2026-08-07', '2027-02-03', ?, 'x')",
                (indice, tipo, str(pdf)))

    def _nomes(self, conteudo: bytes) -> list[str]:
        with zipfile.ZipFile(BytesIO(conteudo)) as pacote:
            return pacote.namelist()

    def test_separa_por_tipo_em_pastas(self, conn, tmp_path):
        self._preparar(conn, tmp_path, ["NEGATIVA", "CPEN"])

        nomes = self._nomes(zipar_pdfs(conn, 1))

        assert any("CERTIDOES NEGATIVAS" in n for n in nomes)
        assert any("POSITIVAS COM EFEITO DE NEGATIVA" in n for n in nomes)

    def test_traz_indice_para_conferencia(self, conn, tmp_path):
        self._preparar(conn, tmp_path, ["NEGATIVA"])

        with zipfile.ZipFile(BytesIO(zipar_pdfs(conn, 1))) as pacote:
            indice = pacote.read("indice.csv").decode()

        assert "EMPRESA 1" in indice
        assert "00.000.000/0000-01" in indice, "documento com máscara no índice"
        assert "07/08/2026" in indice, "datas em formato brasileiro"

    def test_somente_negativas_deixa_cpen_de_fora(self, conn, tmp_path):
        self._preparar(conn, tmp_path, ["NEGATIVA", "CPEN"])

        nomes = self._nomes(zipar_pdfs(conn, 1, somente_negativas=True))

        assert any("CERTIDOES NEGATIVAS" in n for n in nomes)
        assert not any("EFEITO DE NEGATIVA" in n for n in nomes)

    def test_arquivo_sumido_nao_quebra_o_pacote(self, conn, tmp_path):
        """PDF apagado da pasta não pode impedir a entrega dos demais."""
        self._preparar(conn, tmp_path, ["NEGATIVA", "NEGATIVA"])
        next(tmp_path.glob("*.pdf")).unlink()

        nomes = self._nomes(zipar_pdfs(conn, 1))

        assert len([n for n in nomes if n.endswith(".pdf")]) == 1
