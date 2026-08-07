# 01 — Visão geral e requisitos

## 1. Problema

A Mapah/BPYOU precisa consultar/emitir certidões de regularidade fiscal para uma
carteira grande de CNPJs (e alguns CPFs), de forma recorrente. Os portais dos
órgãos usam captcha acionado por heurística antirrobô, o que inviabiliza tanto a
automação headless ingênua (bloqueia) quanto a resolução manual item a item
(volume).

## 2. Fonte de dados

Planilha Excel `CND_MIA_0726.xlsx`, uma aba por tipo de certidão/órgão:

| Aba | Registros | Documento | Órgão / tipo |
|---|---|---|---|
| `RFB` | 2.850 | CNPJ | Receita Federal — PJ (Certidão de Débitos Relativos a Créditos Tributários Federais e à Dívida Ativa da União) |
| `CRF` | 2.080 | CNPJ | Caixa — Certificado de Regularidade do FGTS |
| `CPF` | 80 | CPF | Receita Federal — PF |
| `GO` | 287 | CNPJ | Sefaz Goiás |
| `DF` | 24 | CNPJ | Sefaz Distrito Federal |
| `ES` | 42 | CNPJ | Sefaz Espírito Santo |
| `SP` | 18 | CNPJ | Sefaz São Paulo |

> A planilha pode não ser a fonte definitiva. Por isso a **ingestão é uma camada
> isolada** ([docs/02-arquitetura.md](02-arquitetura.md#22-ingestão)): trocar Excel por
> API/ERP no futuro não toca no resto do sistema.

## 3. Sites-alvo

| Órgão | URL | Status de mapeamento |
|---|---|---|
| RFB PJ | https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj | ✅ **mapeado** em 07/08/2026 — ver [docs/fluxos/rfb-pj.md](fluxos/rfb-pj.md) |
| CRF/Caixa | https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf | Identificado; fluxo não mapeado |
| RFB PF | Mesmo domínio da RFB | Fluxo não mapeado |
| GO, DF, ES, SP | — | Sites e captchas não mapeados |

Sobre o captcha da RFB: comunicado oficial (jan/2026) confirma que o captcha do
e-CAC/Portal de Serviços aparece **apenas quando o sistema detecta indícios de
atividade robotizada** — é heurístico/comportamental, não fixo. O critério exato
(IP, sessão, comportamento, combinação) não é documentado. Isso fundamenta a
estratégia do [ADR-004](adr/ADR-004-estrategia-captcha.md): o captcha é tratado
como um **evento de bloqueio a ser evitado e contornado por espera**, não como um
desafio a ser resolvido em linha.

## 4. Requisitos funcionais

| ID | Requisito |
|---|---|
| RF-01 | Ingerir a lista de documentos (CNPJ/CPF) a partir da planilha Excel, com validação de dígito verificador e deduplicação |
| RF-02 | Consultar/emitir a certidão de cada item no portal do órgão correspondente, via automação de browser |
| RF-03 | Baixar e armazenar o PDF quando a certidão for emitida (negativa ou positiva com efeitos de negativa), com metadados (data de emissão, validade, código de controle quando disponível) |
| RF-04 | Classificar cada item em um resultado de negócio: **NEGATIVA**, **POSITIVA COM EFEITOS DE NEGATIVA (CPEN)**, **POSITIVA** (pendência real, sem PDF emitido pelo fluxo padrão), **PENDÊNCIA MANUAL** (órgão exige atendimento/e-CAC) — distinto de **ERRO TÉCNICO** (falha de processo) |
| RF-05 | Ao encontrar captcha ou erro em um item, registrar, reagendar e **seguir para o próximo** — a fila nunca trava por causa de um item |
| RF-06 | Reprocessar automaticamente itens bloqueados/errados com backoff, até um limite de tentativas |
| RF-07 | Exibir progresso em tempo real em dashboard web (por lote, por órgão, por status) |
| RF-08 | Exportar relatório final do lote em Excel: abas de negativas, CPEN, positivas, pendências manuais e erros |
| RF-09 | Suportar novos órgãos (CRF, PF, estaduais) sem alterar o núcleo — apenas adicionando um adapter |

## 5. Requisitos não-funcionais

| ID | Requisito |
|---|---|
| RNF-01 | Rodar 24/7 em servidor próprio Windows ou Linux, sem intervenção humana no caminho feliz |
| RNF-02 | Nenhuma resolução manual de captcha por item; intervenção humana só em fila assistida opcional e agregada |
| RNF-03 | Comportamento de navegação "humano": pacing configurável com jitter, concorrência baixa, sessões persistentes — projetado para **minimizar o acionamento** da heurística antirrobô |
| RNF-04 | Idempotência: reprocessar um lote não reconsulta empresas cuja certidão **já foi emitida no mês corrente**. O critério é a data de emissão, não a validade — a certidão da RFB vale 180 dias, mas quem a recebe exige emissão do mês (ver [fluxo RFB PJ](fluxos/rfb-pj.md#regras-de-negócio)) |
| RNF-05 | Toda tentativa é auditável: log estruturado + screenshot/HTML em caso de falha inesperada |
| RNF-06 | Dados de terceiros (CNPJs/CPFs, PDFs) ficam no servidor próprio; nenhum dado é enviado a serviços externos |
| RNF-07 | Suportar o CNPJ alfanumérico (novo formato da RFB) na validação e normalização |
| RNF-08 | Perda de energia/crash no meio de um lote não corrompe o estado: jobs em `RUNNING` órfãos voltam para a fila na subida |

## 6. Fora de escopo (por ora)

- Resolução automática de captcha via serviço terceirizado (ver [ADR-004](adr/ADR-004-estrategia-captcha.md)).
- Integração direta com ERP/sistema contábil (a ingestão isolada deixa a porta aberta).
- Certidões trabalhistas (TST/CNDT), municipais e de cartórios.
- Multi-tenant / múltiplas carteiras com isolamento.

## 7. Premissas e riscos

| # | Premissa/risco | Mitigação |
|---|---|---|
| P1 | ~~O portal da RFB permite emissão sem login para PJ regular~~ | ✅ **Confirmado** no mapeamento de 07/08/2026: emissão e download do PDF sem login. Empresas sem condição de emitir online recebem "informações insuficientes" e viram PENDÊNCIA MANUAL |
| R1 | RFB muda o HTML/fluxo do portal sem aviso | Seletores centralizados no adapter; teste-canário diário com 1 CNPJ conhecido; alerta no dashboard quando o canário falha |
| R2 | Heurística antirrobô fica mais agressiva | Parâmetros de pacing são configuração, não código; circuit breaker pausa o órgão automaticamente |
| R3 | Volume de 2.850 itens × pacing conservador = lote longo | Aceitável por requisito (servidor sempre ligado); dashboard mostra ETA; pacing ajustável por config |
| R4 | Bloqueio por IP do servidor | Registrar taxa de captcha por faixa de horário para diagnosticar; estratégia de IP (proxy/segundo link) só entra se os dados provarem necessidade — decisão adiada de propósito |
