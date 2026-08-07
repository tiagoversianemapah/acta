"""Vocabulário comum do sistema.

Estes são os únicos termos que o núcleo (fila, ritmo, breaker, painel)
conhece. Nenhum adapter pode inventar um desfecho novo — é isso que
permite adicionar órgãos sem alterar o resto.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    """Situação de um job na fila. Espelha o CHECK do schema.sql."""

    PENDING = "PENDING"          # esperando a vez
    RUNNING = "RUNNING"          # um worker está executando agora
    RETRY_WAIT = "RETRY_WAIT"    # falhou, aguardando a hora de tentar de novo
    DONE = "DONE"                # concluído (com qualquer desfecho de negócio)
    FAILED = "FAILED"            # esgotou as tentativas


class Desfecho(StrEnum):
    """Como uma tentativa terminou."""

    # --- respostas definitivas do órgão (não geram retry) ---
    NEGATIVA = "NEGATIVA"                    # certidão negativa, PDF baixado
    CPEN = "CPEN"                            # positiva com efeitos de negativa, PDF baixado
    POSITIVA = "POSITIVA"                    # pendência impeditiva; sem PDF
    PENDENCIA_MANUAL = "PENDENCIA_MANUAL"    # exige e-CAC/atendimento
    APROVEITADA = "APROVEITADA"              # já havia certidão vigente (RNF-04)

    # --- falhas (geram retry) ---
    CAPTCHA = "CAPTCHA"                      # heurística antirrobô acionou
    BLOQUEIO_TEMPORARIO = "BLOQUEIO_TEMPORARIO"  # portal pediu para tentar depois
    ERRO_TECNICO = "ERRO_TECNICO"            # timeout, seletor sumiu, 5xx, exceção


CONCLUSIVOS = frozenset({
    Desfecho.NEGATIVA,
    Desfecho.CPEN,
    Desfecho.POSITIVA,
    Desfecho.PENDENCIA_MANUAL,
    Desfecho.APROVEITADA,
})

RETENTAVEIS = frozenset({
    Desfecho.CAPTCHA,
    Desfecho.BLOQUEIO_TEMPORARIO,
    Desfecho.ERRO_TECNICO,
})

# O portal nos barrando de propósito — seja com desafio, seja pedindo para
# voltar depois. Nos dois casos a resposta certa é a mesma: desacelerar o
# ritmo e alimentar o disjuntor. Diferente de erro técnico, que é falha
# nossa ou instabilidade, e não um recado do outro lado.
BLOQUEIOS = frozenset({Desfecho.CAPTCHA, Desfecho.BLOQUEIO_TEMPORARIO})

COM_PDF = frozenset({Desfecho.NEGATIVA, Desfecho.CPEN})


@dataclass(frozen=True)
class Documento:
    """Quem será consultado."""

    empresa_id: int
    documento: str          # sem máscara
    tipo: str               # 'CNPJ' | 'CPF'
    nome: str
    lote_id: int = 0        # usado só para organizar a pasta dos PDFs


@dataclass(frozen=True)
class ResultadoTentativa:
    """O que o adapter devolve. Nunca lança exceção de negócio:
    todo desfecho, inclusive falha, vira uma instância disto."""

    desfecho: Desfecho
    caminho_pdf: Path | None = None
    validade: date | None = None
    codigo_controle: str | None = None
    mensagem_portal: str | None = None
    evidencia: Path | None = None

    @property
    def conclusivo(self) -> bool:
        return self.desfecho in CONCLUSIVOS


@dataclass(frozen=True)
class JobReivindicado:
    """Um job que o worker acabou de tirar da fila."""

    job_id: int
    lote_id: int
    orgao: str
    tentativas: int          # quantas já foram feitas antes desta
    doc: Documento
