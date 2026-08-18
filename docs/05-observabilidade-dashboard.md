# 05 — Observabilidade e dashboard

## 1. Dashboard web (RF-07)

Servido pelo processo `cnd web` (FastAPI + HTMX, [ADR-005](adr/ADR-005-dashboard-fastapi-htmx.md)),
acessível na rede interna. Atualização por polling HTMX a cada 5 s — suficiente
para acompanhamento humano, sem WebSocket.

### Tela 1 — Visão do lote (principal)

```
┌─ Lote #3 · CND_MIA_0726.xlsx · iniciado 05/08 14:02 ─────────────────┐
│  RFB_PJ  ████████████████████░░░░░░░░  1.940 / 2.850   ETA ~14h      │
│                                                                      │
│  ✅ Negativas: 1.612   🟡 CPEN: 204   🔴 Positivas: 87               │
│  📋 Pendência manual: 12   ♻️ Aproveitadas: 0                        │
│  ⏳ Na fila: 890   🔁 Aguardando retry: 21   ❌ Failed: 4            │
│                                                                      │
│  Circuit breaker RFB_PJ: FECHADO · última consulta 14:31:07          │
│  Taxa de captcha (últimas 100): 2%  · Ritmo: 46 consultas/h          │
└──────────────────────────────────────────────────────────────────────┘
```

- Barra de progresso e contadores por desfecho, por órgão.
- ETA calculada com o ritmo real das últimas horas (não o teórico).
- Estado do circuit breaker de cada órgão, com motivo e hora da última transição.

### Tela 2 — Detalhe/triagem

- Tabela filtrável de jobs (status, desfecho, órgão, busca por CNPJ/nome).
- Linha expande para o histórico de tentativas com mensagem do portal e link
  para evidência (screenshot/HTML).
- Ações: reenfileirar job(s) `FAILED`, pausar/retomar órgão manualmente,
  download do PDF de uma certidão.

### Tela 3 — Saúde

- Gráfico de taxa de captcha por hora do dia (alimenta a decisão sobre janela
  ativa e sobre o risco R4/IP).
- Duração média de consulta por órgão; resultado dos canários diários.

## 2. Relatório de saída (RF-08)

Exportação Excel por mês e órgão, botão no dashboard e no ACTA. Quatro abas,
cada uma respondendo uma pergunta da operação:

| Aba | Pergunta | Conteúdo |
|---|---|---|
| `Resumo` | Quanto saiu? | Totais por órgão e por desfecho, recorte e data de geração |
| `Certidões` | O que eu entrego? | `NEGATIVA`, `CPEN` e `APROVEITADA` — empresa, CNPJ, órgão, tipo, emissão, validade, código de controle e nome do PDF |
| `Pendências` | Quem precisa de providência? | `POSITIVA`, `PENDENCIA_MANUAL` e `INAPTA` — empresa, CNPJ, órgão, situação, data da consulta e mensagem do portal |
| `Erros` | O que não terminou? | Empresa, CNPJ, órgão, última falha, nº de tentativas e mensagem |

Era uma aba por desfecho, mais uma de auditoria (uma linha por tentativa do
robô). Nove no total, quatro delas com colunas idênticas e só o desfecho
mudando: para saber quantas certidões havia em mãos era preciso somar guias, e
a auditoria — que é diagnóstico técnico, não relatório — vinha no meio do
caminho. O desfecho virou coluna (`Tipo` / `Situação`), que o Excel filtra
melhor do que uma guia separada.

Junto do Excel, opção de baixar um `.zip` com os PDFs do lote na mesma estrutura
de pastas do storage.

## 3. Logs e métricas

- **Log estruturado** (JSON por linha, `structlog`) com `job_id`, `lote_id`,
  `orgao`, `tentativa`, `desfecho`, duração — em `data/logs/`, rotação diária,
  retenção 90 dias.
- Screenshot + HTML **apenas em falha** (RNF-05); sucesso não gera evidência
  (o PDF é a evidência).
- As métricas do dashboard são queries sobre `tentativa`/`job` — sem stack de
  métricas separada (Prometheus etc.) enquanto for um servidor único; a decisão
  pode ser revista se surgir necessidade de alerta externo.

## 4. Alertas por e-mail

Requisito definido: **e-mail sempre que o robô parar de funcionar ou der erro,
por qualquer motivo.** A máquina roda sem ninguém olhando — o e-mail é o canal
primário de incidente; o dashboard é para acompanhamento ativo.

| Evento | Quando dispara | Urgência |
|---|---|---|
| **Circuit breaker abriu** (captcha/erros em sequência — o robô se pausou) | Imediato, com o motivo e os últimos desfechos | Alta |
| **Orquestrador parou** (crash, máquina reiniciou, processo morto) | Heartbeat sem atualização por 5 min (ver abaixo) | Alta |
| **Erro técnico repetido** (mesmo tipo de falha ≥3× seguidas — provável mudança de layout) | Imediato | Alta |
| Canário diário falhou | Imediato | Alta |
| Jobs indo para `FAILED` | Resumo diário consolidado (evita rajada de e-mails) | Média |
| Lote concluído | Ao encerrar, com o resumo e link do relatório | Info |

### Detecção de "parou de funcionar" (heartbeat)

O orquestrador grava um timestamp na tabela `heartbeat` a cada 60 s. O processo
`cnd web` verifica a cada minuto: heartbeat parado há mais de 5 min ⇒ e-mail de
"orquestrador fora do ar" + banner vermelho no dashboard. Como são dois
processos independentes, um vigia o outro; para o caso de a máquina inteira
cair, os dois serviços sobem automaticamente com o boot (systemd/NSSM) e o
primeiro e-mail após a subida informa o reinício.

Anti-spam: cada tipo de alerta tem supressão de repetição (não reenviar o mesmo
alerta em menos de 30 min) e e-mail de "normalizado" quando a condição se
resolve.

E-mail via SMTP simples configurado no `config.toml` (destinatários em lista);
sem dependência de serviço externo de alerta.
