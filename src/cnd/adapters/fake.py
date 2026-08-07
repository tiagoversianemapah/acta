"""Adapter de simulação — não acessa site nenhum.

Existe para testar o sistema inteiro (fila, ritmo, breaker, retry, painel,
alertas) sem tocar no portal da Receita e sem risco de bloqueio.

O detalhe que o torna útil: ele **imita a heurística antirrobô**. Quanto
menor o intervalo entre duas consultas, maior a chance de "captcha". Com
isso, rodar um lote falso demonstra o ritmo adaptativo convergindo de
verdade — o robô acelera, toma captcha, freia, e encontra o ponto de
equilíbrio sozinho.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from cnd.core.modelos import Desfecho, Documento, ResultadoTentativa
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter

log = obter("adapter.fake")

# Distribuição dos desfechos de negócio, aproximando uma carteira real.
PESOS_NEGOCIO = [
    (Desfecho.NEGATIVA, 0.78),
    (Desfecho.CPEN, 0.10),
    (Desfecho.POSITIVA, 0.09),
    (Desfecho.PENDENCIA_MANUAL, 0.03),
]


@dataclass
class AdapterFake:
    orgao: str
    limiar_heuristica_s: float = 3.0    # abaixo disso, o "portal" desconfia
    captcha_maximo: float = 0.75        # chance de captcha em velocidade máxima
    captcha_base: float = 0.0           # piso fixo, independente do ritmo
    chance_erro_tecnico: float = 0.03
    duracao_min_s: float = 0.05
    duracao_max_s: float = 0.20

    _ultima_consulta: float | None = None

    def preparar(self) -> None:
        log.info("adapter_fake_pronto", extra={"orgao": self.orgao})

    def reiniciar_sessao(self) -> None:
        # Sessão nova "esfria" a suspeita, como um navegador recém-aberto.
        self._ultima_consulta = None
        log.info("adapter_fake_sessao_reiniciada", extra={"orgao": self.orgao})

    def encerrar(self) -> None:
        log.info("adapter_fake_encerrado", extra={"orgao": self.orgao})

    # ------------------------------------------------------------------
    def _chance_de_captcha(self) -> float:
        """Quanto mais rápido, mais suspeito.

        Sessão recém-aberta nunca é suspeita — é o que torna
        `reiniciar_sessao()` uma jogada útil depois de um captcha.
        """
        if self._ultima_consulta is None:
            return self.captcha_base
        intervalo = time.monotonic() - self._ultima_consulta
        if intervalo >= self.limiar_heuristica_s:
            return self.captcha_base
        proporcao = 1.0 - (intervalo / self.limiar_heuristica_s)
        return max(self.captcha_base, self.captcha_maximo * proporcao)

    def emitir(self, doc: Documento) -> ResultadoTentativa:
        chance = self._chance_de_captcha()
        self._ultima_consulta = time.monotonic()

        # Simula o tempo de navegação no portal.
        time.sleep(random.uniform(self.duracao_min_s, self.duracao_max_s))

        if random.random() < chance:
            return ResultadoTentativa(
                desfecho=Desfecho.CAPTCHA,
                mensagem_portal="[simulado] desafio de segurança exibido",
            )

        if random.random() < self.chance_erro_tecnico:
            return ResultadoTentativa(
                desfecho=Desfecho.ERRO_TECNICO,
                mensagem_portal="[simulado] tempo esgotado ao carregar a página",
            )

        desfecho = random.choices(
            [d for d, _ in PESOS_NEGOCIO],
            weights=[p for _, p in PESOS_NEGOCIO],
        )[0]

        return ResultadoTentativa(
            desfecho=desfecho,
            mensagem_portal=f"[simulado] {desfecho} para {doc.documento}",
        )


def criar(orgao: ConfigOrgao, cfg: Config) -> AdapterFake:
    """Aceita ajustes em [orgaos.FAKE.simulacao] do config.toml."""
    ajustes = orgao.extras.get("simulacao", {})
    campos = {c: ajustes[c] for c in AdapterFake.__dataclass_fields__ if c in ajustes}
    return AdapterFake(orgao=orgao.codigo, **campos)
