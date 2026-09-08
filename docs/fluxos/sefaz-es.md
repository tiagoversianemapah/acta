# Fluxo do portal - SEFAZ-ES (Certidao Negativa de Debitos)

Fonte da verdade do
[src/cnd/adapters/sefaz_es.py](../../src/cnd/adapters/sefaz_es.py).
Mapeado em **03/09/2026** e convertido para adapter cego em **08/09/2026**.

Portal:

```text
https://s2-internet.sefaz.es.gov.br/certidao/cnd
```

## 1. O caminho no portal

```text
GET  /certidao/cnd
click "Certidao Negativa de Debito"
POST /certidao/renderiza                    passo=R1
POST /certidao/emitir-certidao-internet     numIdentificacao + captcha
```

A tela inicial nao traz o formulario pronto. O link do menu chama
`abreTela('R1')`, que renderiza o formulario por AJAX dentro da pagina.

## 2. Por que o adapter e cego

O formulario tem Cloudflare Turnstile:

```html
<div class="cf-turnstile" data-sitekey="0x4AAAAAAB4i1okB7ECebDlO">
```

Testes em 08/09/2026 separaram IP, perfil frio e automacao:

| Cenario | Resultado |
|---|---|
| Edge comum, operador humano | Emitiu. Turnstile passou invisivel |
| Playwright, contexto efemero | Sem token |
| Playwright, perfil persistente | Sem token |
| Playwright + operador clicando no desafio | Erro de verificacao |

Conclusao operacional: o problema nao era IP nem falta de clique humano; era o
navegador dirigido por CDP (`navigator.webdriver = True`). O adapter atual abre
o Edge comum e usa entrada real do Windows, sem ler DOM e sem capturar resposta
AJAX pelo Playwright.

## 3. Calibragem

Antes de ligar o orgao numa maquina:

```powershell
cnd calibrar --orgao SEFAZ_ES
```

Pontos salvos em `data/calibragem/sefaz_es.json`:

| Ponto | Uso |
|---|---|
| `menu_cnd` | abrir o formulario pelo menu lateral |
| `campo_documento` | focar e digitar o CNPJ |
| `botao_emitir` | submeter a consulta |
| `visor_pdf` | focar o visualizador embutido antes do Ctrl+S |
| `fundo_pagina` | detectar modal pelo veu escuro |
| `faixa_alerta` | detectar alerta/bloqueio por cor |

O ponto `visor_pdf` so existe depois de uma emissao real. A calibragem pede que
o operador emita uma certidao valida no portal de producao e aponte para dentro
do PDF aberto no modal.

## 4. Como o PDF chega

Quando a emissao e aceita, o portal recebe JSON com PDF em base64:

```json
{
  "success": true,
  "data": {
    "blbCertidao": "JVBERi0x..."
  }
}
```

Mas isso nao vira download HTTP nem navegacao para `application/pdf`. A pagina
monta um modal com:

```html
<object data="data:application/pdf;base64, ..." type="application/pdf">
```

Por isso a opcao do Edge "sempre baixar arquivos PDF" nao ajuda. O robo cego
faz o mesmo caminho da tela: clica dentro do visor, envia `Ctrl+S`, digita um
caminho unico em `data/evidencias/SEFAZ_ES/{cnpj}/...-certidao.pdf` e so aceita
o arquivo se os bytes comecarem com `%PDF`.

Depois disso, o PDF e lido:

- `NEGATIVA` ou `CPEN` vai para `data/certidoes/...`.
- `POSITIVA` ou PDF desconhecido fica em `data/evidencias/...`.

## 5. Classificacao

Marcadores aceitos no PDF:

| Desfecho | Marcador |
|---|---|
| `NEGATIVA` | `CERTIDAO NEGATIVA` ou `NAO CONSTA DEBITO` |
| `CPEN` | `POSITIVA COM EFEITO` ou `EFEITO DE NEGATIVA` |
| `POSITIVA` | `CERTIDAO POSITIVA` ou `CONSTA DEBITO` |

Sem PDF, `NEGATIVA` e `CPEN` nunca sao conclusivas. Mensagem de tela que pareca
sucesso sem documento vira `ERRO_TECNICO`, porque nao ha certidao entregavel.

Mensagens sem PDF:

| Texto | Desfecho |
|---|---|
| `Complete a verificacao` ou `Verificacao de seguranca invalida` | `CAPTCHA` |
| `CNPJ invalido` ou `CPF/CNPJ incompleto` | `PENDENCIA_MANUAL` |
| `Nao foi possivel emitir certidao negativa`, `possui debito`, `consta debito` | `POSITIVA` |
| texto inesperado ou modal sem PDF salvo | `ERRO_TECNICO` |

## 6. Nao existe sandbox

Versoes anteriores deste documento descreviam um ambiente
`sandbox.sefaz.es.gov.br`. Esse ambiente nao existe para a CND publica. O unico
ambiente e o portal real `s2-internet.sefaz.es.gov.br`.

O bloco antigo `[orgaos.SEFAZ_ES.captcha]` pode continuar em configs velhos sem
quebrar leitura, mas nao e chamado. O adapter nao usa provider externo.

## 7. Situacao

Estado em 08/09/2026: adapter cego integrado e coberto por testes de unidade.
No `config.exemplo.toml`, o orgao continua `ativo = false` porque cada maquina
precisa ser calibrada antes de rodar:

```powershell
cnd calibrar --orgao SEFAZ_ES
cnd calibrar --orgao SEFAZ_ES --conferir
```
