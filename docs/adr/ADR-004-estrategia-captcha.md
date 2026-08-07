# ADR-004 — Estratégia de captcha: evitar por comportamento, não resolver

**Status:** aceito · **Data:** 2026-08-06

## Contexto

O captcha da RFB é **heurístico**: só aparece quando o sistema detecta indícios
de atividade robotizada (comunicado oficial jan/2026). Ou seja, a taxa de
captcha é uma **função do nosso comportamento** — diferente de um captcha fixo,
que apareceria em 100% das consultas independentemente de como navegássemos.
Requisitos: sem resolução manual por item (RNF-02), fila nunca trava (RF-05),
dados de clientes não saem do servidor (RNF-06).

## Decisão

Tratar o captcha como **sinal de bloqueio a ser evitado e respeitado**, em três
camadas:

1. **Evitar** — pacing com jitter, janela ativa, pausas longas periódicas,
   browser headed com perfil persistente, concorrência mínima (doc 04 §3).
   O objetivo é manter a taxa de captcha próxima de zero por construção.
2. **Recuar** — quando aparecer: o item vai para backoff longo com sessão nova,
   e o **circuit breaker por órgão** pausa o despacho para não reforçar o sinal
   de robô (doc 04 §§4–5). A fila continua nos demais órgãos.
3. **Fallback assistido** (fase 2+, se necessário) — itens persistentemente
   bloqueados entram numa fila em que o operador resolve o desafio uma única
   vez em sessão aquecida, de forma agregada (doc 04 §6).

**Não** integraremos serviços de resolução automática de captcha (2Captcha,
Anti-Captcha e similares).

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Serviço de quebra de captcha | (a) contorna deliberadamente um controle do órgão — risco jurídico/reputacional para um escritório regulado que atua em nome de clientes; (b) envia conteúdo da sessão a terceiros (atrito com RNF-06); (c) cria dependência paga e frágil; (d) o captcha ser heurístico torna a evasão desnecessária: comportamento adequado o mantém raro |
| Resolução manual por item | Vetada por requisito (volume) |
| Ignorar e martelar | Realimenta a heurística, tende a bloqueio progressivo do IP/sessão — exatamente o cenário que inviabiliza a operação |

## Consequências

- O ritmo é **adaptativo (AIMD)**, não um intervalo fixo: acelera gradualmente
  enquanto não há captcha, pune multiplicativamente ao primeiro sinal, converge
  sozinho para a velocidade máxima sustentável (doc 04 §3). Melhor caso: lote
  de 2.850 em horas; pior caso: ~2–4 dias — sem tuning manual em nenhum dos
  dois. Circuit breaker + alerta por e-mail seguem como freio de emergência.
  Amortizado pela idempotência (lotes seguintes só reconsultam certidões
  vencidas).
- A eficácia é **mensurável**: taxa de captcha por hora no dashboard (doc 05).
  Se os dados mostrarem bloqueio correlacionado a IP mesmo com bom
  comportamento, a decisão sobre estratégia de IP (risco R4) é reaberta com
  evidência — não por palpite.
- Reabrir a hipótese de serviço externo exige novo ADR com avaliação jurídica —
  não é decisão de engenharia isolada.
