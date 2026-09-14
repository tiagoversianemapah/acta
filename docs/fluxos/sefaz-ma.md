# Fluxo do portal — SEFAZ-MA (Certidão Negativa de Débito)

Fonte da verdade do
[src/cnd/adapters/estadual/sefaz_ma.py](../../src/cnd/adapters/estadual/sefaz_ma.py)
e do leitor de captcha em
[captcha_ma.py](../../src/cnd/adapters/estadual/captcha_ma.py). Mapeado contra
o portal real em **14/09/2026**.

## 1. O serviço

App **JSF/RichFaces** antigo (rodapé "Sefaz/COTEC — 2005-2026"), em
**ISO-8859-1**. São dois endpoints irmãos, idênticos no fluxo:

```
CND   .../certidoes/jsp/emissaoCertidaoNegativa/emissaoCertidaoNegativa.jsf
CNDA  .../certidoes/jsp/emissaoCertidaoNegativaDividaAtiva/…DividaAtiva.jsf
```

Como o GO, ele devolve o **PDF no próprio POST** — não precisa de robô cego.
O que muda é o captcha.

## 2. O caminho

```
GET  …/emissaoCertidaoNegativa.jsf     → sessão, cookies, ViewState, captcha
(arma a sessão, com o captcha:)
  AJAX troca o tipo de documento para CPF/CNPJ   (radio tipoEmissao = 2)
  lê o captcha e o confere no portal              (AJAX do botão)
     ↳ acertou  → "if(true){…btn.click()}"  a sessão fica armada
     ↳ errou    → "if(false){…}"            pede outra imagem e tenta de novo
POST form1:btn                          → PDF, ou 302 …/util/paginaErro.jsf
```

Por documento é **página nova** por padrão (`sessao_por_documento = true`):
consultou, pegou o PDF, reinicia e vai de novo. É mais robusto do que herdar
uma sessão que pode expirar no meio da carteira.

Os campos do POST final: `form1`, `form1:tipoEmissao=2`, `form1:cpfCnpj=<CNPJ>`,
o campo do captcha (`form1:j_id20`), `javax.faces.ViewState` e
`form1:btn=Emitir Certidão`. Os ids `form1:j_idNN` são gerados pelo RichFaces e
**lidos da página** a cada vez, com os valores observados como reserva —
um redeploy do portal pode trocá-los.

## 3. O captcha

Aparece em **100% das emissões**. É **fixo, não heurístico** — o oposto da
Receita (ADR-004), e por isso exige decisão à parte, registrada no
[ADR-006](../adr/ADR-006-captcha-fixo-ocr-local.md).

Como ele é (medido em amostras reais):

- JPEG **100×25**, quatro caracteres `[a-z0-9]`;
- **sem linhas de ruído e sem distorção geométrica** — só o peso (normal/negrito)
  e o itálico são sorteados por glifo;
- os quatro caracteres ficam **separados por colunas em branco** — segmentar por
  projeção vertical dá quatro blocos limpos, sem heurística de corte.

Ou seja: é trivial de ler por OCR local. E há dois fatos que decidem o desenho
do adapter:

1. **O portal valida o palpite de graça.** O AJAX do botão responde se o código
   está certo **sem emitir** certidão. É um oráculo grátis: o leitor não precisa
   ser perfeito — erra, pede outra imagem e tenta de novo. Poucas tentativas
   fecham.
2. **Uma validação arma a sessão inteira.** Confirmado: validado um captcha,
   emitem-se vários CNPJs sem novo desafio. É a economia que o modo
   `sessao_por_documento = false` liga (um captcha por sessão em vez de um por
   documento).

O leitor (`captcha_ma.py`) é **Pillow puro** — sem `numpy`, sem `tesseract`,
sem serviço externo: casa cada glifo, normalizado numa caixa de 20×20, contra
um banco de templates por distância de Hamming. O banco é **aprendido**: cada
captcha que o portal confirma vira exemplo, então a leitura melhora sozinha na
máquina, e **nada sai dela** (RNF-06). O banco semente é construído com
[ferramentas/treinar_ocr_sefaz_ma.py](../../ferramentas/treinar_ocr_sefaz_ma.py).

Por isso o órgão **começa desligado**: sem o banco treinado nesta máquina, o
robô não lê o captcha e recusa emitir (ERRO_TECNICO com recado). É o mesmo
padrão do ES, que exige `cnd calibrar` antes de rodar.

## 4. Como o PDF é classificado

A linha operativa da certidão negativa, conferida no documento real:

> "…**não constam débitos** relativos aos tributos estaduais, administrados por
> esta Secretaria, em nome do sujeito passivo acima identificado."

- **NEGATIVA** — tem "não constam débitos" (e não a afirmação contrária).
- **POSITIVA** — afirma débito ("constam débitos", "possui débito") e não o nega.
- **ERRO_TECNICO** — diz as duas coisas, ou nenhuma: vai para conferência
  manual. Melhor não entregar do que entregar a positiva de alguém como
  negativa (mesma regra do GO).

A armadilha do `(?<!nao )`: `"consta debito"` é substring de
`"nao consta debito"`. A classificação normaliza acentos e espaços antes de
comparar, e o "consta débito" só conta como débito quando não vem precedido de
"nao ".

Validade: "Validade da Certidão: 90 (noventa) dias: **13/12/2026**" → a data.
Código de controle: o número "**Nº 226555/26**".

**Um caso observado:** um CNPJ **não inscrito** no cadastro de ICMS do MA ainda
recebe uma certidão negativa válida (o Estado certifica que não há débito). O
adapter entrega como NEGATIVA, mas põe o aviso "não inscrito" na mensagem, para
quem confere perceber.

## 5. POSITIVA: o portal não emite PDF, mostra "é devedor"

Conferido em 14/09/2026 com um CNPJ real com débito: quando há débito, o
portal **não gera PDF** — mostra a faixa amarela **"Este CPF/CNPJ é devedor."**.

E o detalhe que custou uma depuração: como o botão "Emitir" é AJAX, essa faixa
já vem na **resposta da validação do captcha**, junto com `if(false)` — o mesmo
`if(false)` de um captcha errado. Então a validação distingue **três** casos:

| Resposta da validação | Significado | Desfecho |
|---|---|---|
| `if(true){…btn.click()}` | captcha certo, sem débito | segue e emite → NEGATIVA |
| contém "é devedor" | captcha **certo**, com débito | **POSITIVA** (sem PDF) |
| `if(false)` e nada mais | captcha errado | tenta outra imagem |

Olhar só o `if(true)` era o bug: um devedor caía no mesmo balde do captcha
errado, o robô retentava as 12 imagens sem sucesso e reportava CAPTCHA — e o
item ficava retentando para sempre até FAILED, quando a resposta do Estado já
era definitiva. Como reforço, o "é devedor" também é reconhecido na resposta do
POST do botão, caso um dia o portal o mostre só ali.

POSITIVA aqui é conclusiva, sem retry e sem baixar nada (não há documento a
entregar, como na Receita e no GO); a sessão segue armada para o próximo. É por
isso que a classificação do PDF (seção 4) quase nunca vê uma positiva —
positiva não vira PDF; aquele caminho fica como defesa.
