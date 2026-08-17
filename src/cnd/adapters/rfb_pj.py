"""Receita Federal — Certidão de Débitos de pessoa jurídica.

Mapeado manualmente em 07/08/2026 navegando no portal. O fluxo real:

    1. https://.../servico/certidoes/#/home/cnpj   (endereço direto do formulário)
    2. Preenche o campo `niContribuinte` e clica em "Emitir Certidão"
    3. Se a empresa já tem certidão vigente, aparece a janela "Certidão Válida
       Encontrada" com duas opções. SEMPRE clicamos em "Emitir Nova Certidão":
       a certidão vale 180 dias, mas quem recebe exige emissão do mês corrente.
    4. Vai para #/home/cnpj/resultado, que passa por uma etapa assíncrona
       ("Estamos analisando seu pedido... Aguarde") antes do resultado real
    5. Desfechos observados na tela:
         · "A certidão foi emitida com sucesso para o CNPJ ..."  -> baixa o PDF
         · "As informações disponíveis ... são insuficientes para emitir a
            certidão pela Internet."                             -> POSITIVA
           (é assim que o portal recusa quem tem débito: não há certidão a
            baixar, e a empresa não está regular)

A tela NÃO diz se a certidão é negativa ou positiva com efeitos de negativa —
isso só existe no título do PDF. Por isso a classificação final lê o PDF.

O captcha é hCaptcha, dentro do componente <app-hcaptcha>, que fica na página
desde o início mas só se materializa quando o sistema decide desafiar. Foi por
isso que três consultas manuais seguidas passaram sem nenhum desafio.
"""
from __future__ import annotations

import contextlib
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from cnd.adapters import rfb_matriz
from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra import entrada_real
from cnd.infra.arquivos import caminho_certidao
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.rfb_pj")

URL_HOME = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home"
URL_FORMULARIO = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj"

SELETORES = {
    # name="niContribuinte" é escolha dos programadores da Receita e tem
    # significado. O id do mesmo campo (id3f749047cf9730) é gerado a cada
    # renderização — usar aquele quebraria no primeiro deploy deles.
    "campo_cnpj": "input[name='niContribuinte']",
    "botao_emitir": "button:has-text('Emitir Certidão')",
    "modal_titulo": "#titulo-modal",
    "botao_emitir_nova": "button:has-text('Emitir Nova Certidão')",
    "resultado": "app-resultado-certidao",
    "botao_nova_consulta": "button:has-text('Nova Consulta')",
    "link_pdf": "a:has-text('download do documento PDF da certidão')",
    "aceitar_cookies": "button:has-text('Aceitar')",
    "erro_do_campo": ".feedback[role='alert']",
    "alerta_do_portal": "br-alert-messages",
    "opcao_pessoa_juridica": "text=Pessoa Jurídica",
}

ASSINATURAS_CAPTCHA = (
    "app-hcaptcha iframe",
    "iframe[src*='hcaptcha']",
    "iframe[src*='recaptcha']",
)

# Frases colhidas do portal. Comparadas sobre texto normalizado
# (minúsculas e espaços colapsados), porque o site quebra linha no meio delas.
FRASE_SUCESSO = "certidão foi emitida com sucesso"
FRASE_INSUFICIENTE = "são insuficientes para emitir a certidão pela internet"
FRASE_PROCESSANDO = "estamos analisando seu pedido"
# "Inscrição no CNPJ ... Inapta - Omissão de declarações, emissão de
# certidão não permitida." Ver o mesmo par de frases em rfb_cego.
FRASE_INAPTA = "inapta"
FRASE_INAPTA_MOTIVO = "emissão de certidão não permitida"
FRASE_RETORNE_RESULTADO = "retorne em alguns minutos para o resultado"
FRASE_SERVICO_INDISPONIVEL = (
    "servico de emissao de certidao esta temporariamente indisponivel"
)

# Faixa amarela no topo do formulário, com código de erro do portal:
# "Não foi possível concluir a ação para o contribuinte informado.
#  Por favor, tente novamente dentro de alguns minutos. 023 - 07/08/2026 ..."
# Não é captcha, mas é o portal nos barrando — a resposta certa é a mesma:
# desacelerar e voltar depois.
FRASE_BLOQUEIO = "tente novamente dentro de alguns minutos"
FRASES_BLOQUEIO = (
    FRASE_BLOQUEIO,
    "não foi possível emitir a certidão",
    "não foi possível concluir a ação para o contribuinte informado",
)
RE_CODIGO_033 = re.compile(r"\b033\b")

# Títulos do PDF, na ORDEM em que devem ser testados. CPEN vem primeiro
# porque o título dela contém a palavra "positiva" — testar positiva
# antes classificaria toda CPEN como positiva.
TITULO_CPEN = "certidão positiva com efeitos de negativa"
TITULO_NEGATIVA = "certidão negativa de débitos"
TITULO_POSITIVA = "certidão positiva de débitos"

RE_VALIDADE = re.compile(r"v[áa]lida at[ée]\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_CODIGO = re.compile(
    r"c[óo]digo de controle da certid[ãa]o:?\s*([A-Za-z0-9.]+)", re.IGNORECASE
)

TEMPO_RESULTADO_MS = 120_000     # a emissão é assíncrona e pode demorar
TEMPO_DOWNLOAD_S = 25.0


def _normalizar(texto: str) -> str:
    """Minúsculas com espaços colapsados — o portal quebra linha no meio das
    frases, então comparar o texto cru daria falso negativo."""
    return re.sub(r"\s+", " ", (texto or "")).strip().lower()


def _normalizar_sem_acento(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    ascii_puro = sem_acento.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_puro).strip().lower()


def _tem_bloqueio(texto_normalizado: str) -> bool:
    if RE_CODIGO_033.search(texto_normalizado):
        return False
    return any(frase in texto_normalizado for frase in FRASES_BLOQUEIO)


@dataclass
class AdapterRFBPJ:
    orgao: str
    cfg: Config
    perfil: Path
    headless: bool = False
    canal: str = "msedge"
    # Mouse e teclado do Windows de verdade, em vez de eventos injetados no
    # navegador. Ocupa a máquina enquanto roda: o cursor é um só.
    entrada_real: bool = False
    _playwright: object | None = field(default=None, repr=False)
    _contexto: object | None = field(default=None, repr=False)
    _pagina: object | None = field(default=None, repr=False)
    _downloads: list = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def preparar(self) -> None:
        self._abrir_navegador()

    def _abrir_navegador(self) -> None:
        """Navegador com janela e perfil persistente.

        Três escolhas deliberadas, todas para não parecer robô:

        - **Edge instalado** (`channel`) em vez do Chromium empacotado pelo
          Playwright. É um navegador real, assinado, com a mesma versão que
          milhões de máquinas Windows — impressão digital muito mais comum.
        - **Com janela** (não headless): headless puro tem marcas detectáveis.
        - **Perfil persistente**: cookies e histórico sobrevivem entre
          execuções, então o robô parece um usuário recorrente e não um
          visitante novo a cada consulta. Também guarda o aceite de cookies.

        Em servidor Linux, rodar sob xvfb (e trocar o canal, se não houver Edge).
        """
        from playwright.sync_api import sync_playwright

        self.perfil.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        opcoes = dict(
            user_data_dir=str(self.perfil),
            headless=self.headless,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            # Sem viewport forçado: a página ocupa exatamente a janela real.
            # É o que permite converter posição de elemento em posição de tela
            # para o mouse do Windows (ver infra/entrada_real.py).
            no_viewport=True,
            accept_downloads=True,
            # Por padrão o Playwright abre o navegador com bandeiras de
            # automação: --no-sandbox (que faz o Edge exibir a faixa amarela
            # "sinalizador sem suporte") e --enable-automation (que liga o
            # navigator.webdriver). As duas são denúncia de robô — um Edge
            # de usuário comum não tem nenhuma delas.
            chromium_sandbox=True,
            ignore_default_args=["--enable-automation"],
        )
        try:
            self._contexto = self._playwright.chromium.launch_persistent_context(
                channel=self.canal, **opcoes
            )
            usado = self.canal
        except Exception as erro:
            # Sem Edge instalado (servidor Linux, por exemplo): cai para o
            # Chromium do Playwright em vez de deixar o órgão fora do ar.
            log.warning("canal_indisponivel_usando_chromium",
                        extra={"canal": self.canal, "erro": str(erro)})
            self._contexto = self._playwright.chromium.launch_persistent_context(**opcoes)
            usado = "chromium"

        # O Playwright deixa navigator.webdriver = true, que é a bandeira de
        # automação mais óbvia que existe: uma linha de JavaScript lê, e todo
        # verificador antirrobô checa primeiro. O resto da impressão digital
        # já é a de um Edge comum (mesmo user-agent, plugins, idiomas) — essa
        # propriedade era o único destoante.
        self._contexto.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        self._pagina = (self._contexto.pages[0] if self._contexto.pages
                        else self._contexto.new_page())
        # Precisa ser uma função nossa: o Playwright pendura um atributo no
        # handler, e métodos embutidos (list.append) não aceitam atributo.
        self._pagina.on("download", self._registrar_download)

        # Abre pela home, como uma pessoa faria: a janela não fica em branco,
        # os cookies são aceitos e a sessão já chega com histórico de
        # navegação em vez de aparecer direto no formulário.
        try:
            self._pagina.goto(URL_HOME, wait_until="domcontentloaded",
                              timeout=60_000)
            self._aceitar_cookies(self._pagina)
        except Exception:
            log.warning("nao_abriu_o_portal_na_partida")

        log.info("navegador_aberto", extra={"orgao": self.orgao, "canal": usado,
                                            "perfil": str(self.perfil)})

    def _registrar_download(self, download) -> None:
        self._downloads.append(download)

    def _navegador_vivo(self) -> bool:
        try:
            return (self._contexto is not None
                    and self._pagina is not None
                    and not self._pagina.is_closed())
        except Exception:
            return False

    def _garantir_navegador(self) -> None:
        """Reabre o navegador se ele tiver morrido.

        Sem isto, uma janela fechada (operador sem querer, crash do Edge,
        falta de memória) fazia TODOS os jobs seguintes falharem na hora com
        'Target page, context or browser has been closed' — cinco em sequência
        e o disjuntor fechava o órgão por um problema que se resolve
        reabrindo uma janela.
        """
        if self._navegador_vivo():
            return
        log.warning("navegador_caiu_reabrindo", extra={"orgao": self.orgao})
        self.encerrar()
        self._abrir_navegador()

    def reiniciar_sessao(self) -> None:
        """Descarta a sessão marcada e abre outra — chamado após captcha."""
        log.info("reiniciando_sessao", extra={"orgao": self.orgao})
        self.encerrar()
        self._abrir_navegador()

    def encerrar(self) -> None:
        try:
            if self._contexto is not None:
                self._contexto.close()
        except Exception:
            log.exception("falha_ao_fechar_contexto")
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                log.exception("falha_ao_parar_playwright")
            self._contexto = self._playwright = self._pagina = None
            self._downloads.clear()

    # ------------------------------------------------------------------
    # Fluxo principal
    # ------------------------------------------------------------------
    def emitir(self, doc: Documento) -> ResultadoTentativa:
        """Executa o fluxo completo para um CNPJ.

        Nunca lança exceção de negócio: todo desfecho volta como
        ResultadoTentativa (contrato do ADR-003).
        """
        self._garantir_navegador()
        pagina = self._pagina
        self._downloads.clear()
        aviso_matriz: str | None = None

        def com_aviso_matriz(resultado: ResultadoTentativa) -> ResultadoTentativa:
            return rfb_matriz.anotar_matriz(resultado, aviso_matriz)

        try:
            doc_consulta, aviso_matriz = rfb_matriz.documento_para_consulta(doc)
            self._ir_para_formulario(pagina)

            if self._tem_captcha(pagina):
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.CAPTCHA,
                        mensagem_portal="captcha exibido antes da consulta",
                        evidencia=self._evidencia(pagina, doc, "captcha-entrada"),
                    )
                )

            self._digitar_documento(pagina, doc_consulta.documento)

            erro_campo = self._erro_do_campo(pagina)
            if erro_campo:
                # Melhor parar aqui do que clicar em emitir com o formulário
                # inválido e receber uma tela que não sabemos classificar.
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.ERRO_TECNICO,
                        mensagem_portal=f"campo recusado pelo portal: {erro_campo}",
                        evidencia=self._evidencia(pagina, doc, "campo-invalido"),
                    )
                )

            # Uma pessoa confere o que digitou antes de enviar.
            self._pausa(pagina, 700, 1_900)
            self._clicar(pagina, SELETORES["botao_emitir"])

            reacao = self._aguardar_reacao(pagina)

            if reacao == "captcha":
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.CAPTCHA,
                        mensagem_portal="captcha exibido após o envio",
                        evidencia=self._evidencia(pagina, doc, "captcha-envio"),
                    )
                )

            if reacao == "bloqueio":
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.BLOQUEIO_TEMPORARIO,
                        mensagem_portal=self._texto_do_alerta(pagina),
                        evidencia=self._evidencia(pagina, doc, "bloqueio-temporario"),
                    )
                )

            if reacao == "modal":
                # Sempre nova: certidão do mês passado, ainda que válida,
                # é recusada por quem recebe.
                log.info("modal_certidao_vigente", extra={"orgao": self.orgao})
                self._pausa(pagina, 600, 1_500)      # tempo de ler a janela
                self._clicar(pagina, SELETORES["botao_emitir_nova"])
                reacao = self._aguardar_reacao(pagina, aceitar_modal=False)

                if reacao == "bloqueio":
                    return com_aviso_matriz(
                        ResultadoTentativa(
                            Desfecho.BLOQUEIO_TEMPORARIO,
                            mensagem_portal=self._texto_do_alerta(pagina),
                            evidencia=self._evidencia(pagina, doc, "bloqueio-temporario"),
                        )
                    )

            if reacao != "resultado":
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.ERRO_TECNICO,
                        mensagem_portal="o portal não respondeu ao envio a tempo",
                        evidencia=self._evidencia(pagina, doc, "sem-reacao"),
                    )
                )

            texto = self._aguardar_resultado(pagina)

            if self._tem_captcha(pagina):
                return com_aviso_matriz(
                    ResultadoTentativa(
                        Desfecho.CAPTCHA,
                        mensagem_portal="captcha exibido após o envio",
                        evidencia=self._evidencia(pagina, doc, "captcha-resultado"),
                    )
                )

            return com_aviso_matriz(self._classificar(pagina, doc, texto))

        except Exception as erro:
            # Página que não bate com o fluxo esperado nem com captcha é forte
            # indício de mudança de layout (risco R1): guarda tudo para análise.
            log.exception("falha_no_fluxo", extra={"documento": doc.documento})
            return com_aviso_matriz(
                ResultadoTentativa(
                    Desfecho.ERRO_TECNICO,
                    mensagem_portal=f"{type(erro).__name__}: {erro}"[:500],
                    evidencia=self._evidencia(pagina, doc, "erro"),
                )
            )

    # ------------------------------------------------------------------
    def _ir_para_formulario(self, pagina) -> None:
        """Volta ao formulário pelo caminho mais leve possível.

        Se já estamos na tela de resultado, "Nova Consulta" reaproveita a
        aplicação já carregada — menos requisições que recarregar a página, e
        portanto menos motivo para a heurística desconfiar.
        """
        botao_nova = pagina.locator(SELETORES["botao_nova_consulta"])
        alerta_pendurado = bool(self._texto_do_alerta(pagina))

        if "/resultado" in pagina.url and botao_nova.count() > 0 and not alerta_pendurado:
            # Caminho leve, e também o que uma pessoa faria: o botão está ali.
            self._clicar(pagina, SELETORES["botao_nova_consulta"])
            pagina.wait_for_selector(SELETORES["campo_cnpj"], timeout=45_000)
            self._pausa(pagina, 400, 1_200)
            return

        # Faixa de aviso na tela sobrevive à troca de rota; entrar pela home
        # limpa o estado e ainda refaz o caminho humano.
        self._entrar_pela_home(pagina)

    def _aceitar_cookies(self, pagina) -> None:
        """O banner de cookies cobre o rodapé, onde ficam os botões de ação.

        Com perfil persistente isso acontece só na primeira execução — mas
        precisa esperar o banner renderizar, senão o clique sai antes dele
        existir e o banner fica lá atrapalhando o resto da sessão.
        """
        try:
            botao = pagina.locator(SELETORES["aceitar_cookies"])
            botao.first.wait_for(state="visible", timeout=4_000)
            botao.first.click()
            log.info("cookies_aceitos", extra={"orgao": self.orgao})
        except Exception:
            pass  # já aceito em execução anterior, ou banner não apareceu

    # ------------------------------------------------------------------
    # Comportamento humano
    # ------------------------------------------------------------------
    def _pausa(self, pagina, minimo: int, maximo: int) -> None:
        pagina.wait_for_timeout(random.randint(minimo, maximo))

    def _coordenadas_na_tela(self, pagina, alvo) -> tuple[float, float] | None:
        """Converte a posição do elemento na página para posição na tela.

        Precisa somar onde a janela está (`screenX/Y`), a altura da barra de
        endereços e abas (`outerHeight - innerHeight`), e multiplicar pela
        escala do Windows (`devicePixelRatio`) — sem isso, com tela em 125%
        ou 150%, o cursor cai longe do alvo.
        """
        try:
            alvo.scroll_into_view_if_needed(timeout=8_000)
            caixa = alvo.bounding_box()
            if not caixa:
                return None
            m = pagina.evaluate("""() => ({
                screenX: window.screenX, screenY: window.screenY,
                outerWidth: window.outerWidth, outerHeight: window.outerHeight,
                innerWidth: window.innerWidth, innerHeight: window.innerHeight,
                dpr: window.devicePixelRatio || 1,
            })""")
        except Exception:
            return None

        borda = max((m["outerWidth"] - m["innerWidth"]) / 2, 0)
        topo = m["outerHeight"] - m["innerHeight"] - borda
        x = m["screenX"] + borda + caixa["x"] + caixa["width"] * random.uniform(0.30, 0.70)
        y = m["screenY"] + topo + caixa["y"] + caixa["height"] * random.uniform(0.35, 0.65)
        dpr = m["dpr"] or 1
        return x * dpr, y * dpr

    def _mover_para(self, pagina, alvo) -> None:
        """Leva o cursor até o elemento em vários passos.

        O clique do Playwright já dispara evento confiável, mas o cursor
        aparece do nada em cima do botão: nenhuma pessoa clica sem antes
        atravessar a tela com o mouse. É esse rastro que estamos criando.
        """
        try:
            caixa = alvo.bounding_box()
            if not caixa:
                return
            destino_x = caixa["x"] + caixa["width"] * random.uniform(0.25, 0.75)
            destino_y = caixa["y"] + caixa["height"] * random.uniform(0.3, 0.7)
            pagina.mouse.move(destino_x, destino_y, steps=random.randint(14, 28))
            self._pausa(pagina, 90, 280)
        except Exception:
            pass

    def _clicar(self, pagina, seletor: str) -> None:
        """Clique no elemento.

        Com `entrada_real`, usa o mouse do Windows: o cursor anda de verdade
        pela tela e o clique entra pelo mesmo caminho de uma pessoa. Sem ele,
        cai no clique do Playwright, que injeta o evento dentro do navegador
        sem mover o cursor físico.
        """
        alvo = pagina.locator(seletor).first

        if self.entrada_real:
            coords = self._coordenadas_na_tela(pagina, alvo)
            if coords:
                self._focar_janela()
                entrada_real.clicar(*coords)
                return
            log.warning("sem_coordenadas_usando_clique_do_navegador",
                        extra={"seletor": seletor})

        with contextlib.suppress(Exception):
            alvo.scroll_into_view_if_needed(timeout=8_000)
        self._mover_para(pagina, alvo)
        alvo.click()

    def _focar_janela(self) -> None:
        """Sem a janela em primeiro plano, o clique real vai para outro lugar."""
        with contextlib.suppress(Exception):
            self._pagina.bring_to_front()
        entrada_real.trazer_para_frente("Receita Federal")

    def _entrar_pela_home(self, pagina) -> None:
        """Chega ao formulário pelo caminho que uma pessoa faria.

        Antes o robô caía direto no link profundo #/home/cnpj e enviava em
        segundos — padrão de navegação que nenhum humano tem. Agora ele abre
        a home, lê a tela, escolhe "Pessoa Jurídica" e só então digita.
        """
        pagina.goto(URL_HOME, wait_until="domcontentloaded", timeout=60_000)
        self._aceitar_cookies(pagina)
        self._pausa(pagina, 900, 2_400)          # tempo de olhar as opções

        try:
            self._clicar(pagina, SELETORES["opcao_pessoa_juridica"])
        except Exception:
            log.warning("nao_achou_pessoa_juridica_indo_direto")
            pagina.goto(URL_FORMULARIO, wait_until="domcontentloaded", timeout=60_000)

        pagina.wait_for_selector(SELETORES["campo_cnpj"], timeout=45_000)
        self._pausa(pagina, 500, 1_400)

    def _digitar_documento(self, pagina, documento: str) -> None:
        """Digita o CNPJ tecla a tecla, com intervalos irregulares.

        Não é firula: o campo tem máscara (AA.AAA.AAA/AAAA-99) aplicada em
        resposta a eventos de teclado. Preencher o valor de uma vez (`fill`)
        mostra o texto na tela mas deixa o formulário do Angular inválido —
        foi exatamente o "CNPJ inválido. Devem ser digitados 14 caracteres"
        do primeiro piloto, com 14 caracteres no campo.

        De quebra, digitação irregular é o comportamento humano que a
        estratégia anti-captcha pede de qualquer forma.
        """
        campo = pagina.locator(SELETORES["campo_cnpj"])

        if self.entrada_real:
            coords = self._coordenadas_na_tela(pagina, campo.first)
            if coords:
                self._focar_janela()
                entrada_real.clicar(*coords)
                entrada_real.limpar_campo()
                entrada_real.digitar(documento)
                pagina.wait_for_timeout(random.randint(200, 500))
                self._conferir_campo(campo, documento)
                return
            log.warning("sem_coordenadas_digitando_pelo_navegador")

        campo.click()
        campo.press("Control+a")
        campo.press("Delete")

        for caractere in documento:
            campo.press(caractere)
            pagina.wait_for_timeout(random.randint(45, 165))

        self._conferir_campo(campo, documento)

    @staticmethod
    def _conferir_campo(campo, documento: str) -> None:
        digitado = re.sub(r"[^0-9A-Za-z]", "", campo.first.input_value() or "")
        if digitado.upper() != documento.upper():
            log.warning("campo_nao_recebeu_o_documento",
                        extra={"esperado": documento, "no_campo": digitado})

    def _erro_do_campo(self, pagina) -> str | None:
        """Mensagem de validação que o próprio portal exibe abaixo do campo."""
        try:
            alerta = pagina.locator(SELETORES["erro_do_campo"])
            if alerta.count() > 0 and alerta.first.is_visible():
                texto = (alerta.first.inner_text() or "").strip()
                return texto[:200] or None
        except Exception:
            pass
        return None

    def _aguardar_reacao(self, pagina, aceitar_modal: bool = True,
                         segundos: float = 90.0) -> str:
        """Espera o que vier primeiro depois do envio.

        Quatro coisas podem acontecer, e não dá para esperá-las em fila:
        no piloto de 07/08/2026 a faixa de bloqueio levou 12 segundos para
        aparecer, e o adapter — que esperava só 4s por ela antes de partir
        para a tela de resultado — ficou preso aguardando um resultado que
        nunca viria.

        Devolve: 'resultado' | 'modal' | 'bloqueio' | 'captcha' | 'nada'.
        """
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            if self._tem_captcha(pagina):
                return "captcha"

            if _tem_bloqueio(_normalizar(self._texto_do_alerta(pagina))):
                return "bloqueio"

            if aceitar_modal and self._visivel(pagina, SELETORES["modal_titulo"]):
                return "modal"

            if self._visivel(pagina, SELETORES["resultado"]):
                return "resultado"

            pagina.wait_for_timeout(400)

        return "nada"

    @staticmethod
    def _visivel(pagina, seletor: str) -> bool:
        try:
            alvo = pagina.locator(seletor)
            return alvo.count() > 0 and alvo.first.is_visible()
        except Exception:
            return False

    def _texto_do_alerta(self, pagina) -> str:
        """Faixa de aviso no topo da página (componente br-alert-messages)."""
        try:
            alerta = pagina.locator(SELETORES["alerta_do_portal"])
            if alerta.count() > 0 and alerta.first.is_visible():
                return (alerta.first.inner_text() or "").strip()[:400]
        except Exception:
            pass
        return ""

    def _aguardar_resultado(self, pagina) -> str:
        """Espera a etapa assíncrona ("Aguarde") terminar e devolve o texto.

        Sem isso o robô leria a tela de carregamento e classificaria errado —
        um erro que passaria despercebido, porque não quebra nada.
        """
        pagina.wait_for_selector(SELETORES["resultado"], timeout=45_000)

        limite = time.monotonic() + TEMPO_RESULTADO_MS / 1000
        texto = ""
        while time.monotonic() < limite:
            texto = pagina.inner_text(SELETORES["resultado"])
            normalizado = _normalizar(texto)
            if FRASE_RETORNE_RESULTADO in normalizado:
                return texto
            if FRASE_PROCESSANDO not in normalizado:
                return texto
            if self._tem_captcha(pagina):
                return texto
            pagina.wait_for_timeout(1_000)

        raise TimeoutError("resultado não saiu do estado 'analisando' a tempo")

    # ------------------------------------------------------------------
    def _classificar(self, pagina, doc: Documento, texto: str) -> ResultadoTentativa:
        normalizado = _normalizar(texto)

        if (FRASE_RETORNE_RESULTADO in normalizado
                or FRASE_SERVICO_INDISPONIVEL in _normalizar_sem_acento(texto)
                or RE_CODIGO_033.search(normalizado)):
            return ResultadoTentativa(
                Desfecho.RESULTADO_PENDENTE,
                mensagem_portal=texto.strip()[:500],
                evidencia=self._evidencia(pagina, doc, "resultado-pendente"),
            )

        if _tem_bloqueio(normalizado):
            return ResultadoTentativa(
                Desfecho.BLOQUEIO_TEMPORARIO,
                mensagem_portal=texto.strip()[:500],
                evidencia=self._evidencia(pagina, doc, "bloqueio-temporario"),
            )

        if rfb_matriz.exige_matriz(texto):
            # Faixa amarela pedindo o CNPJ da matriz. Não é bloqueio (o
            # portal não nos barrou) nem erro do robô — é o número errado.
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=texto.strip()[:500],
                evidencia=self._evidencia(pagina, doc, "exige-matriz"),
            )

        if FRASE_INAPTA in normalizado and FRASE_INAPTA_MOTIVO in normalizado:
            # Cadastro irregular, não débito: a empresa precisa entregar as
            # declarações atrasadas. Resposta definitiva — retentar não muda.
            return ResultadoTentativa(
                Desfecho.INAPTA,
                mensagem_portal=texto.strip()[:500],
                evidencia=self._evidencia(pagina, doc, "inapta"),
            )

        if FRASE_INSUFICIENTE in normalizado:
            # Recusa por débito: não existe certidão a baixar. Ver o
            # cabeçalho deste módulo.
            return ResultadoTentativa(
                Desfecho.POSITIVA,
                mensagem_portal=texto.strip()[:500],
            )

        if FRASE_SUCESSO in normalizado:
            caminho = self._obter_pdf(pagina, doc)
            return self._ler_pdf(caminho, texto)

        # Desfecho desconhecido: não inventamos classificação. Vira erro
        # técnico com evidência, para alguém olhar e ensinar o robô.
        log.warning("resultado_desconhecido",
                    extra={"documento": doc.documento, "texto": normalizado[:300]})
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal=f"resultado não reconhecido: {texto.strip()[:400]}",
            evidencia=self._evidencia(pagina, doc, "resultado-desconhecido"),
        )

    def _obter_pdf(self, pagina, doc: Documento) -> Path:
        """O portal baixa o PDF sozinho; se não baixar, existe um link de
        reserva na própria tela. Usar os dois torna falha de download rara."""
        download = self._esperar_download(TEMPO_DOWNLOAD_S)

        if download is None:
            log.info("download_automatico_nao_veio_usando_link",
                     extra={"documento": doc.documento})
            with pagina.expect_download(timeout=60_000) as espera:
                pagina.click(SELETORES["link_pdf"])
            download = espera.value

        destino = caminho_certidao(self.cfg.pasta_certidoes, doc.lote_id,
                                   self.orgao, doc.documento, nome=doc.nome)
        download.save_as(str(destino))
        return destino

    def _esperar_download(self, segundos: float):
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            if self._downloads:
                return self._downloads.pop(0)
            self._pagina.wait_for_timeout(500)
        return None

    @staticmethod
    def _ler_pdf(caminho: Path, texto_tela: str) -> ResultadoTentativa:
        """Classifica pelo título do PDF.

        A tela só diz "emitida com sucesso" — que tipo de certidão saiu está
        no título do documento, junto com validade e código de controle.

        Regra de entrega, definida pela operação:

          NEGATIVA  e  CPEN  ->  o PDF é o produto; vai para o acervo e para
                                 o pacote entregue ao cliente. A CPEN vale
                                 como negativa (débito parcelado ou suspenso).
          POSITIVA            ->  a empresa tem pendência real. O documento
                                 não é entregue: entra só no relatório, para
                                 alguém tratar.

        Título desconhecido NÃO é chutado. A versão anterior deste código
        assumia negativa quando não reconhecia o título — o que poderia
        entregar uma certidão positiva a um cliente como se estivesse limpa.
        Agora vira erro técnico com o arquivo guardado como evidência.
        """
        from pypdf import PdfReader

        try:
            conteudo = "\n".join(
                (pagina.extract_text() or "")
                for pagina in PdfReader(str(caminho)).pages
            )
        except Exception as erro:
            log.warning("pdf_ilegivel", extra={"arquivo": str(caminho),
                                               "erro": str(erro)})
            return ResultadoTentativa(
                Desfecho.ERRO_TECNICO,
                mensagem_portal=f"PDF baixado mas ilegível: {erro}"[:300],
                evidencia=caminho,
            )

        normalizado = _normalizar(conteudo)
        comuns = dict(
            validade=_extrair_validade(conteudo),
            codigo_controle=_extrair_codigo(conteudo),
            mensagem_portal=texto_tela.strip()[:500],
        )

        if TITULO_CPEN in normalizado:
            return ResultadoTentativa(Desfecho.CPEN, caminho_pdf=caminho, **comuns)

        if TITULO_NEGATIVA in normalizado:
            return ResultadoTentativa(Desfecho.NEGATIVA, caminho_pdf=caminho, **comuns)

        if TITULO_POSITIVA in normalizado:
            # Sem caminho_pdf de propósito: não vai para o acervo nem para o
            # pacote do cliente. O arquivo fica como evidência, para quem for
            # tratar a pendência poder conferir.
            log.info("certidao_positiva", extra={"arquivo": str(caminho)})
            return ResultadoTentativa(Desfecho.POSITIVA, evidencia=caminho, **comuns)

        log.warning("titulo_do_pdf_nao_reconhecido",
                    extra={"arquivo": str(caminho), "trecho": normalizado[:250]})
        return ResultadoTentativa(
            Desfecho.ERRO_TECNICO,
            mensagem_portal="título do PDF não reconhecido — conferir manualmente",
            evidencia=caminho,
        )

    # ------------------------------------------------------------------
    def _tem_captcha(self, pagina) -> bool:
        """O <app-hcaptcha> existe na página desde o início, vazio. Só conta
        como desafio quando um iframe visível aparece dentro dele."""
        for assinatura in ASSINATURAS_CAPTCHA:
            try:
                alvo = pagina.locator(assinatura)
                if alvo.count() > 0 and alvo.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _evidencia(self, pagina, doc: Documento, motivo: str) -> Path | None:
        try:
            pasta = self.cfg.pasta_evidencias / self.orgao / doc.documento
            pasta.mkdir(parents=True, exist_ok=True)
            marca = datetime.now().strftime("%Y%m%d-%H%M%S")
            imagem = pasta / f"{marca}-{motivo}.png"
            try:
                pagina.screenshot(path=str(imagem), full_page=True)
            except Exception:
                # full_page falha em páginas com layout fixo; a foto da tela
                # visível já serve, e é melhor que evidência nenhuma.
                pagina.screenshot(path=str(imagem))
            (pasta / f"{marca}-{motivo}.html").write_text(pagina.content(),
                                                          encoding="utf-8")
            return imagem
        except Exception:
            log.exception("falha_ao_salvar_evidencia")
            return None


def _extrair_validade(conteudo: str) -> date | None:
    achado = RE_VALIDADE.search(re.sub(r"\s+", " ", conteudo))
    if not achado:
        return None
    try:
        return datetime.strptime(achado.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def _extrair_codigo(conteudo: str) -> str | None:
    achado = RE_CODIGO.search(re.sub(r"\s+", " ", conteudo))
    return achado.group(1).rstrip(".") if achado else None


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterRFBPJ:
    from cnd.infra.db import RAIZ_PROJETO

    return AdapterRFBPJ(
        orgao=orgao.codigo,
        cfg=cfg,
        perfil=RAIZ_PROJETO / "data" / "perfis" / orgao.codigo.lower(),
        headless=bool(orgao.extras.get("headless", False)),
        canal=str(orgao.extras.get("canal", "msedge")),
        entrada_real=bool(orgao.extras.get("entrada_real", False)),
    )
