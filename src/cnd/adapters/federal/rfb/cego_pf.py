"""Receita Federal PF — o robô cego da PJ, no formulário de CPF.

**Por que cego.** Em 17/09/2026 o formulário de CPF foi dirigido por
Playwright com a mesma configuração do `pj.py` — Edge instalado, perfil
persistente, sem `--enable-automation`, `navigator.webdriver` escondido — e
os dois CPFs tentados tomaram o código 106 ("não foi possível concluir a
ação para o contribuinte informado") logo no clique em emitir, sem captcha
nenhum. É o mesmo 106 que o robô levou no teste A/B da PJ em 07/08/2026: o
portal é o mesmo, e a detecção também.

**O que muda da PJ**, e é só isto:

  - o endereço é `#/home/cpf`;
  - o formulário pede a **data de nascimento**, sem a qual não emite. Ela
    vem da planilha (ver `ingestao/planilha.py`);
  - não existe matriz: `rfb_matriz` já devolve o CPF intocado.

No piloto de 17/09/2026, o mesmo CPF que levou 106 pelo Playwright às 12:27
saiu pelo robô cego às 13:25, no mesmo IP: o bloqueio era a automação.

Todo o resto — sessão nova por emissão, faixa de alerta por cor, texto da
página por Ctrl+A, cookies estourados, PDF lido da pasta de downloads — é
herdado de `cego.py`, porque é o mesmo portal. Ver docs/fluxos/rfb-pf.md.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

from cnd.adapters.federal.rfb import cego
from cnd.adapters.federal.rfb.cego import AdapterRFBCego
from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra import entrada_real
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.rfb_pf")

URL_FORMULARIO = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cpf"

USA_TELA = True          # mesmo Edge da PJ — ver cego.USA_TELA

# `botao_emitir_nova` é obrigatório, como na PJ: no piloto de 17/09/2026 a
# janela de certidão vigente apareceu nos DOIS CPFs.
PONTOS_NECESSARIOS = ("campo_cpf", "campo_nascimento", "botao_emitir",
                      "botao_emitir_nova", "fundo_pagina", "faixa_alerta")


@dataclass
class AdapterRFBPFCego(AdapterRFBCego):
    url_formulario: ClassVar[str] = URL_FORMULARIO
    pontos_necessarios: ClassVar[tuple[str, ...]] = PONTOS_NECESSARIOS
    ponto_documento: ClassVar[str] = "campo_cpf"
    # O PDF tem o mesmo nome da PJ, `Certidao-{CPF}.pdf` (visto no piloto de
    # 17/09/2026), então o padrão é herdado.

    # A data do documento em curso. O ciclo do formulário (`_submeter`)
    # recebe só o número, porque é assim que a PJ refaz a consulta pela
    # matriz; a data acompanha a tentativa por aqui.
    _nascimento: date | None = field(default=None, repr=False)

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        if doc.data_nascimento is None:
            # Não abre o portal: sem a data, o formulário não envia, e
            # retentar não traz a data de lugar nenhum. A importação já
            # recusa essas linhas; isto sobra para CPF importado antes dela.
            log.warning("cpf_sem_data_de_nascimento",
                        extra={"orgao": self.orgao, "documento": doc.documento})
            return ResultadoTentativa(
                Desfecho.PENDENCIA_MANUAL,
                mensagem_portal=(
                    "CPF sem data de nascimento cadastrada: o formulário da "
                    "Receita não emite sem ela. Reenvie a planilha com a "
                    "coluna 'Data de Nascimento'."
                ),
            )

        self._nascimento = doc.data_nascimento
        try:
            return super().emitir(doc)
        finally:
            self._nascimento = None

    def _preencher_formulario(self, documento: str) -> None:
        super()._preencher_formulario(documento)

        # Só os dígitos: o campo tem máscara e põe as barras sozinho
        # (conferido em 17/09/2026: "22021948" virou "22/02/1948" no campo).
        time.sleep(random.uniform(0.3, 0.8))
        self._exigir_foco()
        entrada_real.clicar(*self._ponto("campo_nascimento"))
        time.sleep(random.uniform(0.2, 0.5))
        entrada_real.limpar_campo()
        entrada_real.digitar(f"{self._nascimento:%d%m%Y}")


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterRFBPFCego:
    return cego.criar_com(AdapterRFBPFCego, orgao, cfg)
