# Fluxo mapeado — Receita Federal, pessoa física

Mapeado em **17/09/2026**. Fonte da verdade para
[src/cnd/adapters/federal/rfb/cego_pf.py](../../src/cnd/adapters/federal/rfb/cego_pf.py).
É o mesmo portal da [pessoa jurídica](rfb-pj.md): o que não estiver aqui
vale como está lá.

## Por que é o robô cego

O formulário de CPF foi dirigido por **Playwright com a mesma configuração do
`pj.py`**: Edge instalado, perfil persistente, sem `--enable-automation` e
com `navigator.webdriver` escondido (o teste confirmou `undefined`). A
digitação foi tecla a tecla, e os dois campos mostravam os valores certos
antes do clique.

| CPF | Resultado |
|---|---|
| 030.102.361-15 | 106, cerca de 3 s depois do clique |
| 805.391.571-04 | 106, imediato, na mesma sessão |

> Não foi possível concluir a ação para o contribuinte informado. Por favor,
> tente novamente dentro de alguns minutos. *106 - 17/09/2026 12:27:54*

Nenhum captcha apareceu. É o mesmo 106 que o robô levou no teste A/B da PJ
em 07/08/2026. Por isso `rfb_pf` é o `rfb_cego` com outro formulário, e não
um adapter por Playwright.

O controle veio no piloto do mesmo dia: o CPF 030.102.361-15, que levou 106
pelo Playwright às 12:27, **saiu pelo robô cego às 13:25, no mesmo IP**. O
bloqueio era a automação de navegador, e não o CPF nem a máquina.

## Endereço

| O quê | Endereço |
|---|---|
| Formulário | `https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cpf` |

Como na PJ, o robô entra direto no formulário.

## O formulário

| Campo | `name` | Observação |
|---|---|---|
| CPF | `niContribuinte` | o mesmo `name` do CNPJ; o `id` é gerado a cada renderização |
| Data de nascimento | `dataNascimento` | **obrigatória**; máscara `dd/mm/aaaa`, e digitar só os dígitos basta |

Botões: *Voltar*, *Consultar Certidão* e *Emitir Certidão*.

Sem a data, o portal não envia ("Data de nascimento não informada"). Ela não
existe em lugar nenhum do portal, então **vem da planilha**:

- a coluna é achada **só pelo título**: qualquer um com "nasc" ("Data de
  Nascimento", "DT_NASCIMENTO", "Nasc."). Não se adivinha pela primeira data
  da linha: as abas da carteira trazem a competência na coluna C, e numa
  planilha reimportada meses depois ela já parece nascimento;
- aba sem essa coluna, célula vazia ou ilegível e data no futuro são
  **recusadas na importação**, cada uma com o seu motivo;
- no banco, fica em `empresa.data_nascimento`. Bancos antigos ganham a coluna
  sozinhos, ao abrir.

Se mesmo assim um CPF chegar ao robô sem data (importado antes disto), o
adapter nem abre o portal: devolve `PENDENCIA_MANUAL` com o motivo.

## Calibragem

`cnd calibrar --orgao RFB_PF`, salva em `data/calibragem/rfb_pf.json`. O
layout é outro, com um campo a mais, e por isso a calibragem da PJ não serve.

| Ponto | Obrigatório |
|---|---|
| `campo_cpf` | sim |
| `campo_nascimento` | sim |
| `botao_emitir` | sim |
| `fundo_pagina` | sim |
| `faixa_alerta` | sim |
| `botao_emitir_nova` | sim, como na PJ: a janela de certidão vigente apareceu nos dois CPFs do piloto. Mede-se no fim, com a janela aberta |

Calibre com o Edge na janela que a calibragem abrir (ela maximiza no monitor
principal) e sem abrir outra: se a janela mudar no meio, a calibragem avisa e
não salva.

## Casos observados

| Data | Tela | Desfecho |
|---|---|---|
| 17/09/2026 | Piloto com 2 CPFs, robô cego: formulário pronto em 0,1 s, janela "Certidão Válida Encontrada" nos dois, "Emitir Nova Certidão", PDF baixado 6 s depois como `Certidao-{CPF}.pdf` | `CPEN` (030.102.361-15) e `NEGATIVA` (805.391.571-04), na 1ª tentativa, 24 s e 29 s. Título, nome, CPF, validade (180 dias) e código de controle lidos do PDF |
| 17/09/2026 | Em `#/home/cpf/resultado`, caixa branca: "Estamos analisando seu pedido de emissão de certidão. Retorne em alguns minutos para o resultado." | `RESULTADO_PENDENTE`, a mesma frase e o mesmo tratamento da PJ: o item volta para a fila, e três destes dentro da janela do disjuntor pausam o órgão por 30 minutos (`pendentes_para_pausar`, `cooldown_pendente_s`) |

A tela de resultado da PF tem o mesmo desenho da PJ: título "Resultado da
Emissão de Certidão", o documento logo abaixo, a mensagem numa caixa branca e
os botões "Avaliar Serviço" e "Nova Consulta".

## Casos ainda não observados

| Caso | O que o adapter assume |
|---|---|
| "Insuficientes" (débito) em CPF | a mesma frase da PJ, que vira `POSITIVA` |
| CPF irregular (suspenso, cancelado, titular falecido) | texto desconhecido: vira `ERRO_TECNICO` com print |
| Data de nascimento que não bate com o CPF | desconhecido: cai no diagnóstico com print |

A evidência de cada falha fica em `data/evidencias/RFB_PF/{cpf}/`.

## Operação

O mouse é um só. As automações cegas — RFB PJ, RFB PF e SEFAZ-ES — e o CRF,
que abre um Edge visível, podem ficar ligadas na mesma máquina porque
**usam a tela uma de cada vez**, na
ordem da fila ([vez_da_tela.py](../../src/cnd/orquestrador/vez_da_tela.py)):

- a planilha que chegou primeiro (ou foi mandada "Rodar agora") termina a
  fila dela antes da seguinte começar;
- se ela pausar — disjuntor, fora da janela de horário ou só retentativas
  para mais tarde —, a seguinte usa a tela no meio-tempo;
- quando ela volta a ter item, a tela volta para ela no fim do item em
  andamento, e quem retoma a tela reabre o próprio Edge.

GO e MA falam HTTP e rodam em paralelo com qualquer uma delas.
