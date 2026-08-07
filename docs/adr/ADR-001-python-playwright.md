# ADR-001 — Python + Playwright para automação de browser

**Status:** aceito · **Data:** 2026-08-06

## Contexto

O sistema é essencialmente automação de browser contra portais governamentais
com heurística antirrobô, mais ingestão de Excel, fila, e um dashboard simples.
Equipe pequena; manutenção frequente de adapters é o custo dominante (portais
mudam sem aviso).

## Decisão

**Python 3.12+** como linguagem única do projeto e **Playwright** (Chromium,
modo headed com display virtual, contextos persistentes) como ferramenta de
automação.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Selenium | API mais antiga, waits manuais, sem gestão nativa de download/contexto persistente tão limpa; fingerprint igualmente detectável sem ganho |
| Puppeteer/Node | Obrigaria o projeto inteiro a Node ou a duas linguagens; ecossistema de Excel/dados é mais fraco que pandas/openpyxl |
| Requisições HTTP diretas (sem browser) | Frágil contra portais SPA e antirrobô comportamental; qualquer mudança de token/fluxo quebra tudo; browser real é justamente o que a estratégia anti-captcha exige |
| RPA comercial (UiPath etc.) | Licença, lock-in, e o problema central (captcha heurístico, retry fino, circuit breaker) exigiria customização de código de qualquer forma |

## Consequências

- Uma linguagem para tudo (ingestão, fila, adapters, web) — onboarding e deploy
  simples.
- Playwright traz auto-wait, interceptação de download, `user-data-dir`
  persistente e screenshots nativos — tudo requisito direto das docs 02/04.
- Chromium headed em servidor exige display virtual (xvfb) no Linux — custo
  aceito e documentado no deploy.
