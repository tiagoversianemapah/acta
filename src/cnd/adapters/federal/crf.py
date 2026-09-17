"""Caixa — Certificado de Regularidade do FGTS (CRF).

Mapeado navegando no portal em 18/08/2026. O fluxo real:

    1. https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf
    2. "Tipo de Inscrição" já vem CNPJ e a UF fica EM BRANCO — a instrução da
       própria página só pede UF para consulta por CNPJ básico (8 dígitos),
       que não é o nosso caso. Preenche `mainForm:txtInscricao1` com o
       documento SEM máscara e clica em `mainForm:btnConsultar`.
    3. Desfechos observados na tela seguinte:
         · "A empresa abaixo identificada esta REGULAR no FGTS."   -> NEGATIVA
           (aparece junto o link "Certificado de Regularidade do FGTS - CRF")
         · "Não foi possível verificar a regularidade junto à CAIXA.
            Solicitamos tentar mais tarde. Caso persista solicitamos
            comparecer a uma das Agências da CAIXA."               -> POSITIVA
         · "Inscrição: informar o CNPJ correto" (banner ACIMA do título,
            de volta no formulário)                                -> PENDENCIA_MANUAL
    4. Sendo regular: link do CRF -> página com Validade e Certificado Número
       -> botão "Visualizar" -> página de impressão.

Duas decisões que valem registro.

A PRIMEIRA é usar navegador de verdade por ELEMENTO, e não o robô cego de
`rfb_cego.py`. O `pyproject.toml` desliga o Playwright porque "o portal da
Receita o detecta" — e continua verdade PARA A RECEITA. A Caixa protege o
portal com ShieldSquare/Radware, que barra `urllib` (testado em 18/08: volta
página de captcha), mas não barrou o Playwright dirigindo o Edge JÁ
INSTALADO via `channel="msedge"`. Sem download de navegador, sem calibragem,
sem coordenada de tela: a decisão do Playwright passa a ser por órgão.

A SEGUNDA é gerar o PDF por `page.pdf()`. O caminho manual é Visualizar ->
Imprimir -> salvar, que passa pela janela de impressão do Windows — nativa,
fora do DOM, e que automação nenhuma controla bem. O `page.pdf()` imprime
pelo protocolo do navegador direto para arquivo e pula esse trecho inteiro.

"Não foi possível verificar a regularidade" NÃO é falha momentânea, apesar
do "tentar mais tarde": é como o portal diz que a empresa está irregular
(confirmado com quem opera, 18/08/2026). Por isso é POSITIVA e conclusivo —
tratá-lo como retentável gastaria as três tentativas de cada empresa com
débito e ainda terminaria sem resposta.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.crf")

URL_CONSULTA = ("https://consulta-crf.caixa.gov.br/consultacrf/pages/"
                "consultaEmpregador.jsf")

# Seletores por atributo, e não `#mainForm\:campo`: o id do JSF traz `:`, que
# em CSS precisa de escape e já custou um seletor quebrado em silêncio.
CAMPO_INSCRICAO = '[id="mainForm:txtInscricao1"]'
BOTAO_CONSULTAR = '[id="mainForm:btnConsultar"]'
LINK_CERTIFICADO = 'a:has-text("Certificado de Regularidade do FGTS")'
BOTAO_VISUALIZAR = 'input[value="Visualizar"], a:has-text("Visualizar")'

RE_VALIDADE = re.compile(
    r"Validade:\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})")
RE_NUMERO = re.compile(r"Certifica\w*\s+N[uú]mero:\s*([\d.]+)")


def _texto(pagina) -> str:
    """Corpo da página em uma linha, para casar frase sem depender de quebra."""
    try:
        return " ".join(pagina.inner_text("body").split())
    except Exception:
        return ""


def _esperar_texto(pagina, frases: tuple[str, ...], timeout_ms: float) -> bool:
    """Espera a tela mostrar uma das frases esperadas.

    `wait_for_load_state("networkidle")` NÃO serve aqui: o portal é JSF e
    responde por postback parcial, então a rede aquieta enquanto a tela
    ainda é a anterior. Foi assim que a primeira versão leu a página de
    situação achando que era a do certificado, e devolveu certidão sem
    validade e sem número — com um PDF da tela errada, que é pior do que
    falhar. Esperar pelo conteúdo é a única pergunta honesta.
    """
    alvo = [_sem_acento(f) for f in frases]
    try:
        pagina.wait_for_function(
            """alvos => {
                // `document.body &&` nao e paranoia: durante a navegacao do
                // JSF o body fica nulo por um instante, e sem isto o
                // wait_for_function estoura TypeError em vez de continuar
                // esperando -- virando ERRO_TECNICO intermitente.
                const t = ((document.body && document.body.innerText) || '')
                    .normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase();
                return alvos.some(a => t.includes(a));
            }""",
            arg=alvo, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _sem_acento(texto: str) -> str:
    import unicodedata
    sem = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem if not unicodedata.combining(c)).lower()


def classificar(texto: str) -> Desfecho:
    """Traduz a tela do portal para o vocabulário do sistema.

    Sem acento e em minúsculas dos dois lados: a tela já apareceu com
    "esta REGULAR" (sem crase) e com "está", e uma comparação literal
    passaria a depender de qual dia o portal foi escrito.
    """
    t = _sem_acento(texto)
    if "regular no fgts" in t:
        return Desfecho.NEGATIVA
    if "nao foi possivel verificar a regularidade" in t:
        return Desfecho.POSITIVA
    if "informar o cnpj correto" in t or "informar o cpf correto" in t:
        return Desfecho.PENDENCIA_MANUAL
    return Desfecho.ERRO_TECNICO


def _data(texto: str) -> date | None:
    with contextlib.suppress(ValueError):
        return datetime.strptime(texto, "%d/%m/%Y").date()
    return None


@dataclass
class AdapterCRF:
    """Um navegador vivo entre consultas; uma aba nova por documento."""

    # Não mexe no mouse, mas abre um Edge VISÍVEL — e os robôs cegos fecham
    # todos os Edge da máquina a cada item e procuram "a janela do Edge" pela
    # maior aberta. Ligado junto com eles, o CRF perdia o navegador no meio
    # da consulta e um cego podia digitar dentro da janela dele. Na vez da
    # tela, um espera o outro (orquestrador/vez_da_tela.py).
    usa_tela: ClassVar[bool] = True

    orgao: str
    cfg: Config
    timeout_ms: float = 45000.0
    _playwright: object | None = field(default=None, repr=False)
    _navegador: object | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    def preparar(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        # `channel="msedge"` usa o Edge que a máquina já tem. Sem isto o
        # Playwright baixaria o Chromium dele (~100 MB por máquina), que é
        # exatamente a objeção registrada no pyproject.
        self._navegador = self._playwright.chromium.launch(
            channel="msedge", headless=False)
        log.info("navegador_pronto", extra={"orgao": self.orgao})

    def reiniciar_sessao(self) -> None:
        self.encerrar()
        self.preparar()

    def encerrar(self) -> None:
        for alvo, fechar in ((self._navegador, "close"),
                             (self._playwright, "stop")):
            if alvo is not None:
                with contextlib.suppress(Exception):
                    getattr(alvo, fechar)()
        self._navegador = None
        self._playwright = None

    # ------------------------------------------------------------------
    def emitir(self, doc: Documento) -> ResultadoTentativa:
        if self._navegador is None:
            self.preparar()

        # Contexto novo por documento: o portal guarda a consulta anterior na
        # sessão, e uma aba herdada devolveria o resultado da empresa de trás.
        contexto = self._navegador.new_context()
        pagina = contexto.new_page()
        pagina.set_default_timeout(self.timeout_ms)
        try:
            return self._consultar(pagina, doc)
        except Exception as erro:
            log.warning("falha_na_consulta", extra={
                "documento": doc.documento, "erro": str(erro)[:200]})
            return ResultadoTentativa(
                desfecho=Desfecho.ERRO_TECNICO,
                mensagem_portal=f"{type(erro).__name__}: {erro}"[:500],
            )
        finally:
            with contextlib.suppress(Exception):
                contexto.close()

    # ------------------------------------------------------------------
    def _consultar(self, pagina, doc: Documento) -> ResultadoTentativa:
        pagina.goto(URL_CONSULTA)
        # Só dígitos: o campo recusa máscara, e a planilha já entrega assim.
        pagina.fill(CAMPO_INSCRICAO, doc.documento)
        pagina.click(BOTAO_CONSULTAR)
        # Qualquer um dos desfechos conhecidos serve de sinal de que a
        # resposta chegou; não chegando nenhum, `texto` cai em ERRO_TECNICO
        # com a tela inteira de evidência, que é o que se quer ver.
        _esperar_texto(pagina, ("regular no fgts",
                                "nao foi possivel verificar a regularidade",
                                "informar o cnpj correto",
                                "informar o cpf correto"), self.timeout_ms)
        texto = _texto(pagina)
        desfecho = classificar(texto)
        if desfecho is not Desfecho.NEGATIVA:
            return ResultadoTentativa(desfecho=desfecho,
                                      mensagem_portal=texto[:500])

        return self._obter_certificado(pagina, doc, texto)

    def _obter_certificado(self, pagina, doc: Documento,
                           texto_situacao: str) -> ResultadoTentativa:
        """Regular: buscar validade, número e o PDF."""
        link = pagina.locator(LINK_CERTIFICADO)
        if not link.count():
            # Diz REGULAR mas não oferece o certificado: não é resposta que o
            # escritório possa entregar, e chamar de negativa sem PDF criaria
            # uma certidão que não existe.
            return ResultadoTentativa(
                desfecho=Desfecho.ERRO_TECNICO,
                mensagem_portal=("regular, mas sem link do certificado; "
                                 + texto_situacao)[:500])

        link.first.click()
        if not _esperar_texto(pagina, ("validade:",), self.timeout_ms):
            return ResultadoTentativa(
                desfecho=Desfecho.ERRO_TECNICO,
                mensagem_portal=("clicou no certificado e a tela nao mudou; "
                                 + _texto(pagina))[:500])
        crf = _texto(pagina)

        validade = None
        if achado := RE_VALIDADE.search(crf):
            validade = _data(achado.group(2))    # fim da validade
        numero = None
        if achado := RE_NUMERO.search(crf):
            numero = achado.group(1)

        caminho = self._salvar_pdf(pagina, doc, validade, numero)
        return ResultadoTentativa(
            desfecho=Desfecho.NEGATIVA,
            caminho_pdf=caminho,
            validade=validade,
            codigo_controle=numero,
            mensagem_portal=("CRF emitido" if caminho else
                             "CRF na tela, PDF não salvo"),
        )

    def _salvar_pdf(self, pagina, doc: Documento, validade: date | None,
                    numero: str | None) -> Path | None:
        visualizar = pagina.locator(BOTAO_VISUALIZAR)
        if visualizar.count():
            visualizar.first.click()
            # Ancorado no NÚMERO do certificado, e não na palavra "Imprimir".
            # A primeira versão esperava por "imprimir" e imprimia a tela de
            # Situação: aquela página traz o link "Obtenha o Certificado de
            # Regularidade do FGTS - CRF", então até conferir o título dava
            # falso positivo. O número só existe na folha do certificado.
            if numero:
                _esperar_texto(pagina, (numero,), self.timeout_ms)

        destino = caminho_certidao(
            self.cfg.pasta_certidoes, doc.lote_id, self.orgao,
            doc.documento, validade, doc.nome)
        try:
            # `emulate_media("print")` antes: a página tem estilo de
            # impressão próprio, e sem isto o PDF sai com os botões
            # Voltar/Imprimir carimbados no meio da certidão.
            if numero and numero not in _texto(pagina):
                log.warning("folha_nao_e_do_certificado", extra={
                    "documento": doc.documento, "numero": numero})
                return None
            pagina.emulate_media(media="print")
            pagina.pdf(path=str(destino), format="A4", print_background=True)
        except Exception as erro:
            log.warning("pdf_nao_gerado", extra={
                "documento": doc.documento, "erro": str(erro)[:200]})
            return None
        return destino if destino.exists() else None


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterCRF:
    ajustes = orgao.extras.get("navegador", {})
    return AdapterCRF(
        orgao=orgao.codigo, cfg=cfg,
        timeout_ms=float(ajustes.get("timeout_ms", 45000.0)),
    )
