"""Testes do adapter cego da Receita PF.

O robô é o mesmo da PJ (`test_cego.py` cobre a leitura de cor, texto e
sessão). Aqui fica só o que o formulário de CPF muda: a data de nascimento,
os pontos calibrados e o PDF.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from cnd.adapters import calibragem
from cnd.adapters.federal.rfb import cego as rfb_cego
from cnd.adapters.federal.rfb import cego_pf
from cnd.adapters.federal.rfb.cego import AdapterRFBCego, Calibragem, CalibragemAusente
from cnd.adapters.federal.rfb.cego_pf import AdapterRFBPFCego
from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa

BRANCO = (255, 255, 255)
JANELA = (0, 0, 2560, 1600)
ABSOLUTOS_PF = {
    "campo_cpf": (640, 800),
    "campo_nascimento": (1600, 800),
    "botao_emitir": (2048, 1440),
    "botao_emitir_nova": (1700, 950),
    "fundo_pagina": (256, 800),
    "faixa_alerta": (1280, 320),
}
WALDO = Documento(empresa_id=1, documento="03010236115", tipo="CPF",
                  nome="WALDO PALMERSTON XAVIER",
                  data_nascimento=date(1948, 2, 22))


def _adapter(tmp_path, absolutos=None) -> AdapterRFBPFCego:
    cfg = SimpleNamespace(pasta_evidencias=tmp_path)
    adapter = AdapterRFBPFCego("RFB_PF", cfg, tmp_path, tmp_path / "cal.json")
    adapter._calibragem = Calibragem.de_absolutos(
        JANELA, absolutos or ABSOLUTOS_PF, BRANCO)
    return adapter


def _gravar_entrada(monkeypatch, adapter):
    """Troca mouse e teclado por uma lista do que teria sido feito."""
    feito = []
    monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
    monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
    monkeypatch.setattr(rfb_cego.entrada_real, "clicar",
                        lambda x, y: feito.append(("clicar", (x, y))))
    monkeypatch.setattr(rfb_cego.entrada_real, "limpar_campo",
                        lambda: feito.append(("limpar",)))
    monkeypatch.setattr(rfb_cego.entrada_real, "digitar",
                        lambda texto: feito.append(("digitar", texto)))
    monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)
    return feito


class TestFormulario:
    def test_digita_o_cpf_e_depois_a_data_so_com_digitos(self, monkeypatch,
                                                         tmp_path):
        """O campo da data tem máscara: digitar "22021948" vira 22/02/1948."""
        adapter = _adapter(tmp_path)
        adapter._nascimento = date(1948, 2, 22)
        feito = _gravar_entrada(monkeypatch, adapter)

        adapter._preencher_formulario("03010236115")

        assert feito == [
            ("clicar", ABSOLUTOS_PF["campo_cpf"]),
            ("limpar",),
            ("digitar", "03010236115"),
            ("clicar", ABSOLUTOS_PF["campo_nascimento"]),
            ("limpar",),
            ("digitar", "22021948"),
        ]

    def test_a_pj_continua_so_com_o_cnpj(self, monkeypatch, tmp_path):
        cfg = SimpleNamespace(pasta_evidencias=tmp_path)
        adapter = AdapterRFBCego("RFB_PJ", cfg, tmp_path, tmp_path / "cal.json")
        adapter._calibragem = Calibragem.de_absolutos(
            JANELA, {"campo_cnpj": (640, 800)}, BRANCO)
        feito = _gravar_entrada(monkeypatch, adapter)

        adapter._preencher_formulario("32874104000111")

        assert feito == [("clicar", (640, 800)), ("limpar",),
                         ("digitar", "32874104000111")]

    def test_espera_o_formulario_pelo_campo_de_cpf(self, monkeypatch, tmp_path):
        adapter = _adapter(tmp_path)
        medidos = []

        def cor_media(_imagem, x, y, raio=0):
            medidos.append((x, y))
            return BRANCO if (x, y) == ABSOLUTOS_PF["campo_cpf"] else (19, 81, 180)

        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", cor_media)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)

        assert adapter._esperar_formulario(segundos=1) is True
        assert medidos[0] == ABSOLUTOS_PF["campo_cpf"]


class TestDataDeNascimento:
    def test_sem_data_nao_abre_o_portal(self, monkeypatch, tmp_path):
        """Sem a data o formulário não envia, e retentar não a traz."""
        adapter = _adapter(tmp_path)
        monkeypatch.setattr(adapter, "_submeter",
                            lambda _doc: pytest.fail("não devia submeter"))
        sem_data = Documento(empresa_id=1, documento="03010236115",
                             tipo="CPF", nome="WALDO")

        resultado = adapter.emitir(sem_data)

        assert resultado.desfecho == Desfecho.PENDENCIA_MANUAL
        assert "data de nascimento" in resultado.mensagem_portal

    def test_a_data_acompanha_a_tentativa_e_nao_sobra_para_a_proxima(
        self, monkeypatch, tmp_path
    ):
        adapter = _adapter(tmp_path)
        vistas = []
        pdf = tmp_path / "Certidao-03010236115.pdf"

        def submeter(documento):
            vistas.append((documento, adapter._nascimento))
            return "pdf", pdf

        monkeypatch.setattr(adapter, "_submeter", submeter)
        monkeypatch.setattr(
            adapter, "_ler_pdf",
            lambda _c, _d: ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=pdf))

        resultado = adapter.emitir(WALDO)

        assert resultado.desfecho == Desfecho.NEGATIVA
        assert vistas == [("03010236115", date(1948, 2, 22))]
        assert adapter._nascimento is None


class TestCalibragemDaPF:
    def test_preparar_recusa_calibragem_sem_o_campo_da_data(self, monkeypatch,
                                                            tmp_path):
        """A calibragem da PJ no arquivo da PF poria a data em lugar nenhum."""
        sem_data = {k: v for k, v in ABSOLUTOS_PF.items()
                    if k != "campo_nascimento"}
        Calibragem.de_absolutos(JANELA, sem_data, BRANCO).salvar(
            tmp_path / "cal.json")
        adapter = _adapter(tmp_path)
        monkeypatch.setattr(adapter, "_abrir_navegador",
                            lambda: pytest.fail("não devia abrir o Edge"))

        with pytest.raises(CalibragemAusente, match="campo_nascimento"):
            adapter.preparar()

    def test_janela_de_certidao_vigente_sem_botao_calibrado_nao_clica(
        self, monkeypatch, tmp_path
    ):
        """Calibragem feita antes de o botão ser obrigatório pode não tê-lo.
        Clicar num ponto que não existe seria clicar às cegas."""
        sem_botao = {k: v for k, v in ABSOLUTOS_PF.items()
                     if k != "botao_emitir_nova"}
        adapter = _adapter(tmp_path, sem_botao)
        adapter._nascimento = date(1948, 2, 22)
        _gravar_entrada(monkeypatch, adapter)
        monkeypatch.setattr(adapter, "_renovar_sessao_se_gasta", lambda: None)
        monkeypatch.setattr(adapter, "_focar", lambda: None)
        monkeypatch.setattr(adapter, "_esperar_formulario", lambda: True)
        monkeypatch.setattr(rfb_cego.entrada_real, "ir_para_url", lambda _u: None)
        monkeypatch.setattr(adapter, "_aguardar_reacao",
                            lambda *_a, **_kw: ("modal", None))

        with pytest.raises(CalibragemAusente, match="Emitir Nova Certidão"):
            adapter._submeter("03010236115")

    def test_abre_o_formulario_de_cpf(self, monkeypatch, tmp_path):
        adapter = _adapter(tmp_path)
        abertos = []
        monkeypatch.setattr(adapter, "encerrar", lambda: None)
        monkeypatch.setattr(adapter, "_esperar_janela", lambda: True)
        monkeypatch.setattr(adapter, "_posicionar_janela_calibrada", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(rfb_cego, "_achar_edge", lambda: "msedge.exe")
        monkeypatch.setattr(rfb_cego.subprocess, "Popen", abertos.append)
        monkeypatch.setattr(rfb_cego.entrada_real, "maximizar",
                            lambda _t, _e: True)

        adapter._abrir_navegador()

        assert abertos[0][-1].endswith("#/home/cpf")

    def test_botao_emitir_nova_e_obrigatorio_como_na_pj(self):
        """A janela de certidão vigente apareceu nos dois CPFs do piloto."""
        assert "botao_emitir_nova" in cego_pf.PONTOS_NECESSARIOS

    def test_perfil_de_calibragem_pede_os_dois_campos(self):
        perfil = calibragem._perfil("RFB_PF")

        assert perfil.codigo == "rfb_pf"
        assert perfil.url == cego_pf.URL_FORMULARIO
        assert [p for p, _ in perfil.passos][:2] == ["campo_cpf",
                                                    "campo_nascimento"]
        assert "botao_emitir_nova" in perfil.pontos_condicionais

    def test_criar_le_a_calibragem_do_proprio_adapter(self, tmp_path):
        """PJ e PF são layouts diferentes: um arquivo só misturaria os pontos."""
        orgao = SimpleNamespace(codigo="RFB_PF", adapter="rfb_pf", extras={})

        adapter = cego_pf.criar(orgao, SimpleNamespace())

        assert isinstance(adapter, AdapterRFBPFCego)
        assert adapter.caminho_calibragem.name == "rfb_pf.json"


class TestPDF:
    def test_acha_o_pdf_com_o_cpf_no_nome(self, monkeypatch, tmp_path):
        adapter = _adapter(tmp_path)
        (tmp_path / "Certidao-03010236115.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "Certidao-80539157104.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)

        achado = adapter._pdf_pronto("03010236115")

        assert achado.name == "Certidao-03010236115.pdf"

    def test_limpa_so_o_pdf_antigo_deste_cpf(self, tmp_path):
        adapter = _adapter(tmp_path)
        (tmp_path / "Certidao-03010236115.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "Certidao-80539157104.pdf").write_bytes(b"%PDF-1.4")

        adapter._limpar_downloads_antigos("03010236115")

        assert [a.name for a in tmp_path.glob("*.pdf")] == [
            "Certidao-80539157104.pdf"]

    def test_pdf_de_cnpj_que_contem_o_cpf_nao_e_confundido(self, monkeypatch,
                                                          tmp_path):
        """Os 11 dígitos do CPF cabem nos 14 de um CNPJ. A certidão de uma
        empresa esquecida na pasta não pode sair como a de uma pessoa."""
        adapter = _adapter(tmp_path)
        (tmp_path / "Certidao-03010236115000.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)

        assert adapter._pdf_pronto("03010236115") is None
        adapter._limpar_downloads_antigos("03010236115")
        assert (tmp_path / "Certidao-03010236115000.pdf").exists()

    def test_copia_baixada_de_novo_continua_valendo(self, monkeypatch, tmp_path):
        """O Edge nomeia a segunda cópia "Certidao-X (1).pdf"."""
        adapter = _adapter(tmp_path)
        (tmp_path / "Certidao-03010236115 (1).pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)

        assert adapter._pdf_pronto("03010236115").name == (
            "Certidao-03010236115 (1).pdf")


class TestMesmasRespostasDaPJ:
    """O portal é o mesmo, e as respostas também. A PF não tem leitura de
    texto própria: herda a da PJ. Estes testes existem para que uma mudança
    no `cego.py` que quebre a PF apareça aqui, e não num lote de CPFs."""

    CPF_MASCARADO = "030.102.361-15"

    def _diagnosticar(self, monkeypatch, tmp_path, texto, cor=BRANCO):
        adapter = _adapter(tmp_path)
        adapter._ultimo_texto_portal = texto
        monkeypatch.setattr(adapter, "_exigir_foco", lambda: None)
        monkeypatch.setattr(adapter, "_janela", lambda: JANELA)
        monkeypatch.setattr(adapter, "_print",
                            lambda *_a: tmp_path / "print.png")
        monkeypatch.setattr(adapter, "_texto_da_pagina", lambda: "")
        monkeypatch.setattr(rfb_cego.tela, "capturar", lambda: object())
        monkeypatch.setattr(rfb_cego.tela, "cor_media", lambda *_a, **_kw: cor)
        monkeypatch.setattr(rfb_cego.entrada_real, "atalho", lambda *_a: None)
        monkeypatch.setattr(rfb_cego.entrada_real, "tecla", lambda *_a: None)
        monkeypatch.setattr(rfb_cego.time, "sleep", lambda _s: None)
        return adapter._diagnosticar_falha(WALDO)

    @pytest.mark.parametrize("texto,desfecho,trecho", [
        # Débito: o portal recusa emitir. É POSITIVA, e não pendência.
        ("As informações disponíveis na Receita Federal sobre o contribuinte "
         f"{CPF_MASCARADO} são insuficientes para emitir a certidão pela "
         "internet.", Desfecho.POSITIVA, "insuficientes"),
        # 033 junto da data: resultado pendente, volta para a fila.
        ("Não foi possível emitir a certidão. Tente novamente em alguns "
         "minutos. 033 - 17/09/2026 15:20:10",
         Desfecho.RESULTADO_PENDENTE, "033"),
        # Tela vista em 17/09/2026 no formulário de CPF, em #/home/cpf/resultado,
        # numa caixa branca comum: só o texto a denuncia.
        ("Resultado da Emissão de Certidão CPF 348.933.181-87 Estamos "
         "analisando seu pedido de emissão de certidão. Retorne em alguns "
         "minutos para o resultado. Avaliar Serviço Nova Consulta",
         Desfecho.RESULTADO_PENDENTE, "Retorne em alguns minutos"),
        ("O servico de emissao de certidao esta temporariamente indisponivel. "
         "Tente novamente em alguns minutos. 001 - 17/09/2026 22:23:04",
         Desfecho.RESULTADO_PENDENTE, "001"),
        # 023 e 106: o portal nos barrou. Desacelera e alimenta o disjuntor.
        ("Não foi possível concluir a ação para o contribuinte informado. Por "
         "favor, tente novamente dentro de alguns minutos. 023 - 17/09/2026 "
         "10:49:50", Desfecho.BLOQUEIO_TEMPORARIO, "023"),
        ("Não foi possível concluir a ação para o contribuinte informado. Por "
         "favor, tente novamente dentro de alguns minutos. 106 - 17/09/2026 "
         "12:27:54", Desfecho.BLOQUEIO_TEMPORARIO, "106"),
        # Cookies estourados: falha nossa de sessão, retentável.
        ("400 Bad Request\nRequest Header Or Cookie Too Large\nnginx/1.28.3",
         Desfecho.ERRO_TECNICO, "cookies"),
    ])
    def test_texto_do_portal_tem_o_mesmo_desfecho_da_pj(
        self, monkeypatch, tmp_path, texto, desfecho, trecho
    ):
        resultado = self._diagnosticar(monkeypatch, tmp_path, texto)

        assert resultado.desfecho == desfecho
        assert trecho in resultado.mensagem_portal

    def test_033_dentro_do_cpf_nao_vira_resultado_pendente(
        self, monkeypatch, tmp_path
    ):
        """O caso da ANGONESE, em CPF: o código só conta com a data junto."""
        texto = ("As informações disponíveis na Receita Federal sobre o "
                 "contribuinte 123.033.456-78 são insuficientes para emitir a "
                 "certidão pela internet.")

        resultado = self._diagnosticar(monkeypatch, tmp_path, texto)

        assert resultado.desfecho == Desfecho.POSITIVA

    def test_faixa_vermelha_sem_texto_legivel_e_bloqueio(
        self, monkeypatch, tmp_path
    ):
        """O 106 visto em 17/09/2026 veio numa faixa rosa no topo: a cor já
        basta para recuar, mesmo sem conseguir ler a frase."""
        resultado = self._diagnosticar(monkeypatch, tmp_path, None,
                                       cor=(248, 215, 218))

        assert resultado.desfecho == Desfecho.BLOQUEIO_TEMPORARIO

    def test_tela_ilegivel_volta_para_a_fila_sem_chutar_positiva(
        self, monkeypatch, tmp_path
    ):
        resultado = self._diagnosticar(monkeypatch, tmp_path, None)

        assert resultado.desfecho == Desfecho.ERRO_TECNICO
        assert "não foi possível ler a tela" in resultado.mensagem_portal


class TestJanelaQueMudaNaCalibragem:
    """17/09/2026: pontos medidos com o Edge no monitor da esquerda, e no fim
    a janela encontrada estava no principal. Saíam seis "caiu FORA da
    janela" sem dizer que quem tinha mudado era a janela."""

    MEDIDA = (-1366, 0, 1366, 1600)
    NO_FIM = (528, 181, 1299, 1050)

    def test_janela_que_mudou_de_monitor_e_apontada(self):
        recado = calibragem._janela_mudou(self.MEDIDA, self.NO_FIM)

        assert "mudou durante a calibragem" in recado
        assert "sem arrastar" in recado

    def test_mesma_janela_nao_acusa_nada(self):
        assert calibragem._janela_mudou(self.MEDIDA, self.MEDIDA) is None

    def test_sem_janela_medida_deixa_a_validacao_normal_decidir(self):
        assert calibragem._janela_mudou(None, self.NO_FIM) is None
