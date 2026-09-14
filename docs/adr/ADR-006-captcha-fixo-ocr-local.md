# ADR-006 — Captcha fixo da SEFAZ-MA: OCR local, conferido no próprio portal

**Status:** aceito · **Data:** 2026-09-14

## Contexto

O [ADR-004](ADR-004-estrategia-captcha.md) decidiu, para o captcha **heurístico**
da Receita, evitar por comportamento e nunca resolver automaticamente. Aquele
captcha é função do nosso jeito de navegar: comportando-se bem, ele fica raro.

O portal da SEFAZ-MA é outro caso, e o [roadmap](../06-roadmap.md) já previa que
"estados com captcha fixo (não heurístico) em toda consulta exigirão decisão à
parte — levantar isso **antes** de codar cada adapter e registrar em ADR". É o
que este ADR faz.

O que foi medido em 14/09/2026 (ver [docs/fluxos/sefaz-ma.md](../fluxos/sefaz-ma.md)):

- o captcha aparece em **100%** das emissões — não há comportamento que o torne
  raro, ao contrário do da Receita;
- ele é **trivial**: JPEG 100×25, quatro caracteres `[a-z0-9]`, sem ruído e sem
  distorção geométrica; os glifos ficam separados por colunas em branco;
- o portal **valida o palpite de graça**: um AJAX responde se o código está
  certo **sem emitir** certidão;
- **uma validação arma a sessão inteira**: acertar um captcha permite emitir
  vários documentos.

Requisitos que continuam valendo: sem resolução manual por item (RNF-02), fila
nunca trava (RF-05), dados de clientes não saem da máquina (RNF-06).

## Decisão

**Ler o captcha por OCR local, e conferir o palpite no próprio portal antes de
emitir.** Em camadas:

1. **Ler** — um leitor em `captcha_ma.py`, **Pillow puro** (sem `numpy`, sem
   `tesseract`, sem serviço externo): segmenta os quatro glifos, normaliza cada
   um numa caixa de 20×20 e casa contra um banco de templates por distância de
   Hamming.
2. **Conferir** — o palpite é validado no AJAX do portal, que não gasta emissão.
   O leitor **não precisa ser perfeito**: errou, pede outra imagem e tenta de
   novo, até um teto. Como basta acertar uma vez, isso fecha depressa.
3. **Aprender** — cada captcha que o portal confirma vira exemplo no banco, que
   cresce e melhora **na própria máquina**. O banco semente é feito uma vez, por
   máquina, com `ferramentas/treinar_ocr_sefaz_ma.py` — o análogo do
   `cnd calibrar` do ES.

**Não** usamos serviço externo de captcha (2Captcha e afins): a decisão do
ADR-004 sobre isso continua de pé, e aqui ela nem seria necessária.

## Por que isto não contradiz o ADR-004

O ADR-004 vetou (a) serviços externos de quebra de captcha e (b) resolução
manual por item. Nenhum dos dois acontece aqui:

- **Nada sai da máquina** (RNF-06): a leitura é local e o banco também. O
  contraste é justamente com os serviços pagos, que recebem a imagem da sessão.
- **Não há resolução manual por item** (RNF-02): a leitura é automática; o único
  toque humano é a semente inicial, uma vez por máquina — não por documento.
- O motivo de fundo do ADR-004 — "o captcha ser heurístico torna a evasão
  desnecessária" — **não se aplica** a um captcha fixo, que aparece sempre por
  desenho. Para ele, ler é a única forma de a fila andar sem operador.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Validação assistida por sessão (operador digita 1 captcha por sessão) | Respeita tudo, mas exige alguém presente e reautenticar quando a sessão JSF expira; o robô do escritório roda sem operador. Fica como o modo `sessao_por_documento=false` para quem quiser. |
| `tesseract`/`numpy` | Binário externo / dependência que o `acta.spec` hoje **exclui**; não se paga por um estado tão pequeno, e complica o `ACTA.exe`. |
| Serviço externo de captcha | Vetado pelo ADR-004: envia conteúdo da sessão a terceiros (RNF-06) e é dependência paga e frágil. |
| Robô cego (Edge), como o ES | Não ajuda: o captcha continuaria a ser resolvido, e sem os ganhos do HTTP direto (o PDF vem no POST). |

## Consequências

- O órgão **começa desligado** no config: sem banco treinado, o adapter recusa
  emitir (ERRO_TECNICO com recado), e o disjuntor pausa o órgão em vez de
  martelar o portal. Ligar exige treinar antes — passo único por máquina.
- A eficácia é **mensurável** sem gastar emissão: `--aferir` na ferramenta diz a
  taxa de acerto atual, e o log registra em quantas tentativas a sessão armou.
- **Achado defensivo** (o captcha visto do outro lado): este captcha protege
  muito pouco — sem distorção, com glifos separáveis, validável de graça e
  reutilizável por sessão, ele é vencido por um OCR simples rodando numa máquina
  de escritório. Registrado aqui porque é a conclusão de segurança da análise,
  não só um detalhe de implementação.
- O modo `sessao_por_documento` deixa as duas políticas disponíveis: página nova
  por documento (padrão, robusto) ou um captcha por sessão (econômico).
