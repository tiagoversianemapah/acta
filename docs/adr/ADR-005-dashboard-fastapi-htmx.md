# ADR-005 — Dashboard: FastAPI + páginas server-rendered (HTMX)

**Status:** aceito · **Data:** 2026-08-06

## Contexto

RF-07 pede acompanhamento visual de progresso; doc 05 define três telas
(lote, triagem, saúde) e a exportação Excel. Público: operadores internos na
rede local. Poucos usuários simultâneos; atualização "quase em tempo real"
(segundos) é suficiente.

## Decisão

Processo web separado (`cnd web`) em **FastAPI**, servindo **HTML renderizado no
servidor (Jinja2) com HTMX** para atualização parcial por polling (5 s). Leitura
direta do mesmo SQLite (conexão read-only). Exportação Excel gerada sob demanda
com openpyxl.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| SPA (React/Vue) + API JSON | Segundo ecossistema (Node, build, deps) para três telas internas; custo de manutenção desproporcional |
| Streamlit/Gradio | Rápido para protótipo, mas fraco para tabela com ações (reenfileirar, pausar órgão) e para layout estável de operação |
| WebSocket/SSE para tempo real | Polling de 5 s entrega a mesma percepção para acompanhamento humano, com fração da complexidade |
| Grafana sobre o banco | Ótimo para métricas, mas não cobre as **ações** (reenfileirar, pausar, exportar); viraria segundo sistema além do dashboard, não substituto |

## Consequências

- Stack única Python de ponta a ponta (coerente com ADR-001); nenhuma etapa de
  build de frontend.
- Ações operacionais (reenfileirar `FAILED`, pausar órgão) são endpoints POST
  simples com confirmação — auditáveis em log como qualquer transição.
- Processo web isolado do orquestrador: deploy/restart independentes, e uma
  falha de renderização nunca para a fila.
- Limite aceito: não é multiusuário com autenticação sofisticada; se o dashboard
  precisar sair da rede interna, adicionar autenticação vira pré-requisito.
