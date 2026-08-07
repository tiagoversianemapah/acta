# ADR-003 — Um adapter por órgão atrás de interface única

**Status:** aceito · **Data:** 2026-08-06

## Contexto

Sete origens de certidão já identificadas (RFB PJ, CRF, RFB PF, GO, DF, ES, SP),
cada uma com site, fluxo, captcha e formatos próprios; mais órgãos podem entrar.
Os portais mudam sem aviso (risco R1) — a manutenção contínua dos fluxos é o
custo dominante do sistema.

## Decisão

Cada órgão é um **adapter**: uma classe que implementa o protocolo
`AdapterOrgao.emitir(page, doc) -> ResultadoTentativa` (doc 02 §2.5). Todo o
conhecimento específico do site (URLs, seletores, assinaturas de captcha,
extração de metadados, classificação do desfecho) mora exclusivamente no
adapter. O núcleo — fila, máquina de estados, pacing, circuit breaker,
dashboard — opera apenas sobre o vocabulário comum de `Desfecho`.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Script monolítico por órgão (um "robô" independente por site) | Duplicaria fila, retry, relatório e dashboard em cada script; comportamento inconsistente entre órgãos; foi descartado como pergunta em aberto do briefing |
| Motor genérico dirigido por configuração (seletores em YAML) | Sedutor, mas fluxos reais têm ramificações (CPEN × positiva × dados insuficientes) que viram uma DSL caseira pior que Python; adapters em código são testáveis com fixtures |

## Consequências

- Adicionar um órgão = 1 arquivo novo + fixtures de teste + registro no config.
  Fases 2–4 do roadmap não tocam o núcleo (RF-09).
- Quebra de layout fica contida: o breaker pausa o órgão, o fix é local ao
  adapter.
- Testes de adapter rodam contra HTML gravado (sem rede), permitindo CI sem
  bater nos portais.
- Parâmetros por órgão (pacing, workers, janela ativa) acompanham o adapter no
  `config.toml` — o vocabulário comum não impede tuning individual.
