# CND Bot — Emissão de Certidões de Regularidade Fiscal em Lote

Sistema de RPA para consulta e emissão em lote de certidões de regularidade fiscal
(CND/CPEN) para a carteira de CNPJs/CPFs da Mapah/BPYOU, rodando de forma contínua
em servidor próprio, resiliente a captcha e com acompanhamento visual de progresso.

## Estado do projeto

| Camada | Situação |
|---|---|
| Ingestão da planilha (validação, dedupe, lote) | ✅ pronta e em uso |
| Banco, fila e máquina de estados | ✅ prontos |
| Ritmo adaptativo (AIMD) e circuit breaker | ✅ prontos |
| Orquestrador (workers, retry, heartbeat) | ✅ pronto |
| Adapter de simulação (teste offline) | ✅ pronto |
| Painel web e relatório Excel | ✅ prontos |
| Alertas por e-mail | ✅ prontos (falta preencher SMTP no `config.toml`) |
| **Adapter da Receita Federal (RFB_PJ)** | ⚠️ **esqueleto — falta mapear o portal** |
| Adapters CRF, RFB-PF e estaduais | ⬜ fases 2 a 4 |

O que falta para entrar em produção é **uma coisa só**: mapear o fluxo real do
portal da Receita e preencher os seletores em
[src/cnd/adapters/rfb_pj.py](src/cnd/adapters/rfb_pj.py). Todo o resto do sistema
já funciona e está coberto por testes.

## Começando

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                                    # 67 testes, sem rede
```

### Importar a planilha

```powershell
cnd importar "CND_MIA_0726.xlsx" --abas RFB
```

Valida cada documento (CNPJ numérico e alfanumérico, CPF), descarta duplicatas e
cria um job por item. O que não passar é listado com aba, linha e motivo — para
corrigir na planilha, não no sistema.

### Ver o sistema funcionando sem tocar em portal nenhum

```powershell
cnd simular 150                           # cria um lote falso
# no config.toml: [orgaos.FAKE] ativo = true
cnd rodar --ate-esvaziar
```

O adapter de simulação **imita a heurística antirrobô**: quanto mais rápido o
robô consulta, maior a chance de "captcha". Rodar isso mostra o ritmo adaptativo
convergindo de verdade — acelera, apanha, freia e encontra o ponto de equilíbrio
sozinho.

### Operação

```powershell
cnd rodar                                 # o robô (deixa rodando)
cnd painel                                # http://127.0.0.1:8000
cnd relatorio 1                           # Excel do lote 1
```

## Documentação

| Documento | Conteúdo |
|---|---|
| [01 — Visão geral e requisitos](docs/01-visao-geral-e-requisitos.md) | Problema, escopo, requisitos funcionais e não-funcionais |
| [02 — Arquitetura](docs/02-arquitetura.md) | Componentes, padrão de adapters, fluxo ponta a ponta |
| [03 — Modelo de dados](docs/03-modelo-de-dados.md) | Entidades, schema SQL, armazenamento de PDFs |
| [04 — Ciclo de vida, retry e captcha](docs/04-ciclo-de-vida-retry-captcha.md) | Máquina de estados, ritmo adaptativo, circuit breaker |
| [05 — Observabilidade e dashboard](docs/05-observabilidade-dashboard.md) | Painel, logs, alertas, relatório de saída |
| [06 — Roadmap](docs/06-roadmap.md) | Fases de entrega e critérios de aceite |
| [ADRs](docs/adr/) | As decisões de arquitetura e por que foram tomadas |

## Decisões principais

- **Python 3.12+ / Playwright** (Chromium com janela, perfil persistente) — [ADR-001](docs/adr/ADR-001-python-playwright.md)
- **SQLite (WAL) com a fila no próprio banco**, sem broker externo — [ADR-002](docs/adr/ADR-002-sqlite-fila-no-banco.md)
- **Um adapter por órgão** atrás de uma interface única — [ADR-003](docs/adr/ADR-003-adapter-por-orgao.md)
- **Captcha evitado por comportamento** (ritmo adaptativo + circuit breaker), sem serviço de quebra — [ADR-004](docs/adr/ADR-004-estrategia-captcha.md)
- **Painel FastAPI com páginas renderizadas no servidor** — [ADR-005](docs/adr/ADR-005-dashboard-fastapi-htmx.md)

## Como o código está organizado

```
src/cnd/
├── core/          regras que valem para qualquer órgão
│   ├── documentos.py   valida CNPJ (numérico e alfanumérico) e CPF
│   ├── modelos.py      o vocabulário: Status e Desfecho
│   ├── fila.py         reivindicar job, concluir, reagendar, recuperar órfãos
│   ├── ritmo.py        acelerador/freio adaptativo (AIMD)
│   ├── breaker.py      disjuntor por órgão
│   └── tempo.py        datas em UTC, ordenáveis como texto
├── adapters/      um arquivo por portal — a parte que muda quando o site muda
│   ├── base.py         o contrato que todos seguem
│   ├── fake.py         simulador para teste offline
│   └── rfb_pj.py       Receita Federal PJ  ⚠️ falta mapear
├── ingestao/      única camada que conhece Excel
├── orquestrador/  decide quando e o quê executar
├── web/           painel, consultas e relatório
└── infra/         banco, config, log, e-mail, arquivos
```

A regra que amarra tudo: as camadas de cima importam as de baixo, **nunca o
contrário**. O `core` não sabe que Playwright existe. É isso que faz uma mudança
no site da Receita afetar um arquivo só.

## Próximo passo

Mapear o portal da Receita (Fase 1, item 1 do [roadmap](docs/06-roadmap.md)) —
as instruções estão no topo de [rfb_pj.py](src/cnd/adapters/rfb_pj.py).
