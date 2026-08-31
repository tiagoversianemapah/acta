# Fluxo do portal — SEFAZ-GO (Certidão de Débito Inscrito em Dívida Ativa)

Fonte da verdade do
[src/cnd/adapters/sefaz_go.py](../../src/cnd/adapters/sefaz_go.py).
Tudo aqui foi conferido contra o
portal e contra documentos reais em **28/08/2026**, numa rodada que emitiu as
**299 certidões** da carteira de Goiás.

## 1. Por que não é o robô cego

A Receita **detecta** automação de navegador, e por isso o `rfb_cego` mexe no
mouse e no teclado do Windows. Aqui o problema não existe: o formulário é um
ASP antigo que devolve o **PDF no próprio POST**. Não há navegador, não há
tela para ler, não há calibragem.

Chegou a ser tentado com Playwright dirigindo o Edge. Ele passa pelo
formulário, mas a resposta PDF *inline* vira a página do visualizador interno
do Edge — `<embed type="application/pdf" src="about:blank">` — e não os bytes
do arquivo. O POST por HTTP entrega o PDF de verdade.

## 2. O caminho

```
GET  /Certidao/Emissao/001frmEmiteCertidao_c.asp   → sessão + cookies
POST /Certidao/Emissao/certidao.asp                → PDF, ou tela
        ↳ "Confirma o Nome do Contribuinte" → POSTa de novo com Sim
        ↳ "Acesso Negado" + política de segurança → bloqueio temporário
```

O endereço novo (`go.gov.br/servicos-digitais/economia/...`, portal *expresso*)
é outra casca do mesmo serviço. O endpoint legado continua no ar e é o que o
adapter usa.

## 3. Os campos do formulário

São **oito**, e o adapter manda todos. O POST é montado à mão, então nenhum
`checked` do HTML é herdado: **campo que não vai explícito não chega ao
servidor**.

| Campo | Valor | Observação |
|---|---|---|
| `Certidao.Tipo` | `01` | Dívida Ativa |
| `Certidao.TipoDocumento` | `2` | 1 = CPF, 2 = CNPJ. **CPF nasce marcado no HTML** |
| `Certidao.NumeroDocumento` | CNPJ | campo oculto que o JS do portal preenche |
| `Certidao.NumeroDocumentoCNPJ` | CNPJ | |
| `Certidao.Espolio` | `N` | |
| `Certidao.Render` | `pdf` | **decide o formato**: pdf, html ou xml |
| `Certidao.ValidarEmissao_Emitir` | `0` | 0 = emitir, 1 = validar |

`Render` e `ValidarEmissao_Emitir` faltavam na primeira versão. Sem `Render`, a
resposta podia voltar como página — e o adapter só entrega o que vem como PDF
de verdade, então a certidão simplesmente não sairia.

O portal aceita CPF (`Certidao.NumeroDocumentoCPF`, `TipoDocumento = 1`). O
adapter **não**: ele recusa documento que não seja CNPJ, porque mandar um CPF
nos campos de CNPJ não daria erro — consultaria outro documento e devolveria a
certidão de alguém.

## 4. Como o PDF é classificado

O documento tem título, um bloco `DESPACHO` com a decisão, e um rodapé. **O
rodapé é armadilha**: ele fala em "inscrever na dívida ativa" e "COBRAR
EVENTUAIS DÉBITOS" em *toda* certidão, inclusive na negativa. Por isso a
classificação olha primeiro o título, depois o `DESPACHO`, e só então o texto
inteiro.

| Desfecho | Título | DESPACHO |
|---|---|---|
| `NEGATIVA` | `CERTIDAO DE DEBITO **INSCRITO** EM DIVIDA ATIVA - NEGATIVA` | `NAO CONSTA DEBITO` |
| `POSITIVA` | `CERTIDAO DE DEBITO EM DIVIDA ATIVA - POSITIVA` | `POSSUI DEBITO INSCRITO NA DIVIDA ATIVA, RELATIVO A N PROCESSO(S)` |
| `CPEN` | idem positiva, **mais** `COM EFEITO NEGATIVO(PARCELAMENTO)` | texto do art. 195 da Lei 11651/91 |

Três armadilhas que custaram caro e estão travadas por teste:

1. **Os títulos diferem entre si.** A negativa diz "DÉBITO **INSCRITO** EM
   DÍVIDA ATIVA"; a positiva diz "DÉBITO EM DÍVIDA ATIVA", sem o "inscrito".
2. **`"consta debito"` é substring de `"nao consta debito"`.** Decidir pela
   prosa sem cuidado transforma negativa em positiva — ou pior, o contrário.
3. **A CPEN não diz "positiva com efeito de negativa".** Diz `- POSITIVA` numa
   linha e `COM EFEITO NEGATIVO(PARCELAMENTO)` na seguinte. Um marcador que
   dependesse das duas linhas coladas quebraria com outra quebra de linha — e
   CPEN **vale como regularidade**, então o cliente perderia uma certidão a que
   tem direito.

Validade: o documento diz `VALIDA POR 120 DIAS`, somados à data de emissão.
Código de controle: o `VALIDADOR`.

Na dúvida — texto que afirma e nega ao mesmo tempo, ou título irreconhecível —
o desfecho é `ERRO_TECNICO` e o PDF vai para conferência manual. **Melhor não
entregar do que entregar a positiva de alguém como negativa.**

## 5. Regras de negócio

- **Filiais são consultadas uma a uma.** O despacho diz "Certidão válida para a
  matriz e suas filiais", e na carteira de Goiás isso significaria 28 consultas
  a menos em 299 (9%). Mesmo assim consulta-se tudo: o PDF nomeia o CNPJ
  consultado, e entregar a certidão da matriz no lugar da filial seria entregar
  documento de outro estabelecimento. Decisão de operação, 31/08/2026.
- **Emissão é mensal, independente da validade** (RNF-04). A certidão vale 120
  dias, mas quem recebe exige emissão do mês.
- **Ritmo de 3 s**, com piso igual ao inicial. O AIMD não tem por que acelerar
  contra um portal que não empurra de volta, e a punição continua valendo.

## 6. O que nunca vimos acontecer

O adapter trata `Acesso Negado` + política de segurança como bloqueio
temporário, mas **isso é precaução**: em 299 consultas seguidas o portal não
recusou uma vez. É o oposto da Receita, que detecta automação e responde
bloqueio 106.

## 7. Conferido contra o processo anterior

As 299 certidões foram comparadas, CNPJ a CNPJ, com o log do sistema que o ACTA
substitui:

- **273 batem.**
- **10 divergem** — todas do mesmo grupo (`06296626`, dez estabelecimentos), que
  o log antigo tinha como positiva e o documento novo traz como `NAO CONSTA
  DEBITO`: o débito foi quitado entre as duas rodadas, e as dez viraram juntas
  porque a certidão cobre matriz e filiais.
- **2 divergem por vocabulário** — o processo antigo não tinha CPEN e chamava de
  "Positiva". São dois clientes cuja certidão vale como regularidade e não
  estava sendo entregue.

Nenhum erro de classificação.

## 8. Onde olhar quando quebrar

- [ferramentas/conferir_sefaz_go.py](../../ferramentas/conferir_sefaz_go.py) — consulta e mostra **a prova** por trás do
  desfecho: qual marcador casou, que título veio, o trecho do texto. Com
  `--pdf` reclassifica arquivo já baixado, sem gastar consulta.
- [tests/test_sefaz_go.py](../../tests/test_sefaz_go.py) — os textos reais de negativa, positiva e CPEN estão
  ali como fixture, com nome e CNPJ trocados.
- O log (`data/logs/`) guarda o detalhe técnico do erro. O relatório do cliente,
  de propósito, só diz **que** falhou.
