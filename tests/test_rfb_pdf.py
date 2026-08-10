from __future__ import annotations

from cnd.adapters import rfb_pdf
from cnd.core.modelos import Desfecho


class _Pagina:
    def __init__(self, texto: str) -> None:
        self._texto = texto

    def extract_text(self) -> str:
        return self._texto


class _Leitor:
    def __init__(self, texto: str) -> None:
        self.pages = [_Pagina(texto)]


def _pdf(monkeypatch, texto: str):
    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", lambda _caminho: _Leitor(texto))


def test_classifica_negativa(monkeypatch, tmp_path):
    _pdf(monkeypatch, """
    CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS
    Valida ate 03/02/2027
    Codigo de controle da certidao: EE24.3D3A.8D62.B7B1.
    """)

    resultado = rfb_pdf.ler_pdf(tmp_path / "cert.pdf", "ok")

    assert resultado.desfecho == Desfecho.NEGATIVA
    assert resultado.validade.isoformat() == "2027-02-03"
    assert resultado.codigo_controle == "EE24.3D3A.8D62.B7B1"


def test_cpen_nao_cai_como_positiva(monkeypatch, tmp_path):
    _pdf(monkeypatch, """
    CERTIDÃO POSITIVA COM EFEITOS DE NEGATIVA DE DÉBITOS RELATIVOS AOS
    TRIBUTOS FEDERAIS
    """)

    resultado = rfb_pdf.ler_pdf(tmp_path / "cert.pdf", "ok")

    assert resultado.desfecho == Desfecho.CPEN


def test_positiva_fica_como_evidencia(monkeypatch, tmp_path):
    caminho = tmp_path / "cert.pdf"
    _pdf(monkeypatch, "CERTIDÃO POSITIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS")

    resultado = rfb_pdf.ler_pdf(caminho, "ok")

    assert resultado.desfecho == Desfecho.POSITIVA
    assert resultado.evidencia == caminho
    assert resultado.caminho_pdf is None
