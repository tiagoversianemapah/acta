# 06 — Roadmap

Entrega em fases; cada fase termina com critério de aceite objetivo. O núcleo
(fila, estados, retry, dashboard) é construído na Fase 1 e **não muda** nas
seguintes — as fases 2+ são essencialmente novos adapters (RF-09).

## Fase 0 — Fundação (≈ 1 semana)

- Esqueleto do projeto (estrutura da [doc 02 §4](02-arquitetura.md#4-estrutura-de-diretórios-proposta)), banco, migrações.
- Ingestão da planilha com validação de CNPJ (numérico e alfanumérico) e CPF.
- Máquina de estados + fila + orquestrador com pacing e circuit breaker, testados
  com um **adapter fake** (sem rede) que simula todos os desfechos.
- **Aceite:** lote sintético de 100 itens processa ponta a ponta com retries e
  circuit breaker observáveis nos logs; crash no meio do lote não perde nem
  duplica jobs.

## Fase 1 — RFB PJ + dashboard (≈ 2–3 semanas)

1. ✅ **Mapeamento manual do fluxo** — feito em 07/08/2026, documentado em
   [docs/fluxos/rfb-pj.md](fluxos/rfb-pj.md).
2. ✅ **Adapter `rfb_pj`** + testes de classificação contra textos reais do
   portal e de um PDF emitido (sem rede).
3. ⬜ Piloto controlado: **50 CNPJs** com o ritmo adaptativo (doc 04 §3); medir
   taxa de captcha e até onde o portal deixa acelerar.
4. ✅ Dashboard (telas 1, 2 e 3) + relatório Excel.
5. ⬜ Rodar o lote completo (2.849) no servidor; canário diário ativo.

Pendências conhecidas para fechar a fase: o caso `POSITIVA` (pendência
impeditiva) ainda não foi observado, e o captcha nunca apareceu no mapeamento.
Os dois estão tratados no código e vão gerar evidência quando ocorrerem.
- **Aceite:** lote RFB completo processado sem intervenção; relatório Excel
  entregue; taxa de captcha estabilizada (breaker não abre mais que N vezes/dia);
  PDFs íntegros e nomeados corretamente.

## Fase 2 — CRF/Caixa (≈ 1–2 semanas)

- Mapear fluxo do consulta-crf, escrever adapter, piloto de 50, lote completo
  (2.080).
- Fila de resolução assistida ([doc 04 §6](04-ciclo-de-vida-retry-captcha.md#6-fila-de-resolução-assistida-fallback-opcional-fase-2)), se a Fase 1 mostrar necessidade.
- **Aceite:** mesmos critérios da Fase 1, e RFB + CRF rodando em paralelo sem
  interferência (pacing e breaker independentes).

## Fase 3 — RFB PF (≈ 1 semana)

- Mapear fluxo PF (mesmo domínio RFB; provável reuso de grande parte do adapter
  PJ), lote de 80 CPFs.

## Fase 4 — Estaduais GO, ES, DF, SP (≈ 1–2 semanas por estado)

- Ordem por volume: GO (287) → ES (42) → DF (24) → SP (18).
- Cada estado: mapear site/captcha → adapter → piloto → produção.
- Estados com captcha fixo (não heurístico) em toda consulta exigirão decisão à
  parte — levantar isso **antes** de codar cada adapter e registrar em ADR.

## Fase 5 — Operação contínua

- Agendamento recorrente: gerar lote automático mensal (ou por vencimento de
  certidão — a idempotência já garante que só vencidas são reconsultadas).
- Avaliar com dados reais: necessidade de estratégia de IP (risco R4), migração
  da fonte Excel para integração direta, alertas adicionais.

## Backlog explícito (decisões adiadas de propósito)

| Item | Gatilho para decidir |
|---|---|
| Variação de IP / proxy | Dados da tela Saúde mostrarem bloqueio correlacionado a IP |
| Postgres no lugar de SQLite | Mais de um servidor, ou escrita concorrente virar gargalo real |
| Fonte de dados definitiva (Excel × integração) | Definição do processo pela operação |
| Serviço externo de captcha | Não previsto — reabrir só com decisão formal (ver [ADR-004](adr/ADR-004-estrategia-captcha.md)) |
| CNDT/trabalhista, municipais | Demanda do negócio |
