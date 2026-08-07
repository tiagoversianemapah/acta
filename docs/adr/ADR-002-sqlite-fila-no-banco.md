# ADR-002 — SQLite (WAL) com fila no próprio banco; sem broker externo

**Status:** aceito · **Data:** 2026-08-06

## Contexto

Volume: milhares de jobs por lote, processados a **dezenas por hora** (o pacing
anti-captcha é o gargalo deliberado — doc 04 §3). Um único servidor, sempre
ligado. Poucos processos concorrentes: 1 orquestrador, N workers (N pequeno),
1 processo web majoritariamente de leitura.

## Decisão

**SQLite em modo WAL** como único armazenamento, com a **fila modelada como
estado na tabela `job`** (`status` + `proxima_execucao_em`, claim atômico via
`UPDATE ... RETURNING`). Sem Redis, sem RabbitMQ, sem Celery, sem Postgres.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Postgres | Correto tecnicamente, mas adiciona um serviço para operar/backupear num sistema cujo throughput é limitado a ~1 consulta/minuto por órgão. Custo operacional sem benefício presente |
| Redis + Celery/RQ | Broker + workers distribuídos resolvem um problema (escala horizontal) que este sistema não tem; e o estado de negócio teria que viver num banco de qualquer forma — duas fontes de verdade |
| Fila em arquivos/CSV | Sem transação, sem claim atômico, sem consulta para o dashboard |

## Consequências

- **Backup = copiar `data/`**; zero serviços de infraestrutura para operar (RNF-01).
- Dashboard, relatório e fila consultam a mesma fonte de verdade — os contadores
  nunca divergem da fila real.
- Escrita concorrente em SQLite serializa; com o volume descrito, irrelevante.
  **Limite explícito:** se o sistema um dia tiver múltiplos servidores ou
  escrita de alta frequência, migrar para Postgres (o schema da doc 03 é
  portável de propósito — sem features específicas de SQLite além dos PRAGMA).
- Jobs órfãos por crash são recuperáveis com um UPDATE na subida (RNF-08) — sem
  mensagens perdidas em broker.
