# ACTA — Emissão de Certidões de Regularidade Fiscal em Lote

Robô que emite certidões de regularidade fiscal (CND/CPEN) para a carteira de
CNPJs da Mapah/BPYOU. Roda sem operador, distribui o trabalho entre várias
máquinas do escritório, avisa no Teams quando algo precisa de atenção e entrega
planilha e PDFs prontos para envio.

O produto se chama **ACTA**; `cnd` é o nome do pacote Python e da linha de
comando, e continua assim porque está em todos os comandos e na documentação.

Fase 1: Receita Federal, pessoa jurídica — ~2.850 CNPJs por rodada.

## Estado do projeto

| Camada | Situação |
|---|---|
| Ingestão da planilha (validação, dedupe, lote) | ✅ em uso |
| Banco, fila e máquina de estados | ✅ |
| Ritmo adaptativo (AIMD) e disjuntor por órgão | ✅ |
| Orquestrador (workers, retry, heartbeat, órfãos) | ✅ |
| Aplicativo de mesa (`ACTA.exe`) | ✅ |
| Painel web, API e visão de várias máquinas | ✅ |
| Relatório Excel e pacote ZIP das certidões | ✅ |
| Avisos no Microsoft Teams | ✅ em produção |
| Adapter da Receita Federal — leitura do PDF | ✅ validada contra certidão real |
| **Adapter da Receita Federal — emissão de ponta a ponta** | ✅ **em produção desde 10/08/2026** |
| Adapters CRF, RFB-PF e estaduais | ⬜ fases 2 a 4 |

Em produção na máquina `PC Receita Federal 01`: 2.829 CNPJs importados,
emissões saindo com zero falhas, PDFs conferidos contra certidão real.

**Dois ajustes pendentes antes de soltar a fila inteira:**

1. **A espera do formulário.** O aviso `formulario_nao_apareceu` dispara em
   todos os itens e a emissão funciona logo depois — o formulário estava
   lá e quem erra é o critério de detecção. A espera já caiu de 24s para
   8s; o aviso agora registra as cores medidas, e uma rodada curta fecha o
   critério de vez.
2. **A tela "informações insuficientes"** (ex.: CNPJ 15.388.203/0001-74) é
   resposta definitiva do portal, não falha: fecha o item na primeira vez,
   como `POSITIVA` — é assim que a Receita recusa quem tem débito, e não
   há certidão a baixar. Falta rodar um lote com essa classificação para
   conferir o total de positivas contra a conferência manual.

Para rodar um lote de teste, na máquina:

```powershell
cnd rodar --limite 5
```

A máquina precisa estar logada e destravada: o robô assume o mouse e o
teclado de verdade. Não precisa de ninguém na frente dela — mas ninguém
pode usá-la enquanto roda.

## Instalação

Para **usar**, não há instalação de ambiente: veja
[docs/07 — Instalação nas máquinas](docs/07-instalacao-nas-maquinas.md). Copiar a
pasta `dist/ACTA/` é a instalação.

> **Windows 11 com Controle Inteligente de Aplicativos ligado recusa o
> `ACTA.exe`**, porque ele não é assinado — e esse recurso não tem lista de
> exceção. São três saídas: atalho que roda pelo Python (grátis, imediato),
> desligar o controle (decisão sem volta) ou assinar com certificado (custa).
> As três estão em [docs/07](docs/07-instalacao-nas-maquinas.md#o-windows-11-bloqueia-o-executável).

Para **desenvolver**:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

pytest                         # 613 testes, sem rede e sem portal
ruff check src tests empacotar ferramentas   # sem apontamentos
python empacotar/construir.py  # gera o ACTA.exe e os atalhos
```

O pacote gerado assim **não leva o CRF**: o Playwright vive no extra
`navegador`, que `[dev]` não traz. É de propósito — ele acrescenta ~140 MB
(o driver, não o navegador: o adapter usa o Edge que a máquina já tem) e o
adapter ativo da Receita não precisa dele. Para a construção completa:

```powershell
pip install -e ".[dev,navegador]"
```

Sem `config.toml` na raiz, a construção copia o `config.exemplo.toml` e avisa
o que falta preencher — o config é da instalação, não do repositório.

A suíte roda em checkout limpo, **sem `config.toml`**: ele é da instalação e
não é versionado, então os testes caem no `config.exemplo.toml` sozinhos
(`CND_CONFIG` aponta para outro arquivo, se você quiser). E o `pytest` testa o
`src/` deste repositório, e não o ACTA instalado na máquina — que é o pior tipo
de suíte verde, a que não olhou para o código que se acabou de escrever.

## Como funciona

### O robô não automatiza o navegador

O portal da Receita **detecta** automação de navegador. Comprovado em 07/08/2026
com teste A/B: mesmo CNPJ, mesmo IP, no mesmo minuto — a consulta manual passou e
a do robô tomou bloqueio 106.

Por isso o adapter ativo (`rfb_cego`) não usa Playwright nem CDP. Ele abre o Edge
comum e mexe no **mouse e no teclado do Windows** por cima, via `SendInput`: o
cursor viaja em curva de Bézier com tremor, os cliques são cliques do sistema, e
a digitação é tecla por tecla — porque a máscara do campo de CNPJ é acionada por
tecla, e preencher o valor de uma vez faz o portal recusar um documento válido.

Ele enxerga a tela por pixel (Pillow) e reconhece o estado pela cor: véu do modal,
faixa amarela de aviso, faixa rosa de erro, e o PDF aparecendo na pasta Downloads
como sinal de sucesso.

A calibragem é guardada em **proporções da janela** (0..1), não em pixels — o que
faz a mesma calibragem servir em telas de resolução diferente.

### O robô se autorregula

- **Ritmo adaptativo (AIMD)**, no espírito do controle de congestionamento do TCP:
  acelera de pouco em pouco enquanto as consultas saem limpas, e multiplica o
  intervalo por 3 ao primeiro bloqueio. Encontra o ponto de equilíbrio sozinho e
  o guarda no banco, para não reaprender do zero a cada reinício.
- **Disjuntor por órgão**: bloqueios demais numa janela curta suspendem aquele
  órgão, com espera que dobra a cada reabertura. Insistir depois de um 106 piora
  a reputação do IP.
- **Retry com 3 tentativas** e espera diferente por motivo — erro nosso volta em
  segundos, bloqueio do portal em minutos, captcha em horas. Esgotadas as três, o
  item vai para conferência manual e sai um aviso.

### Entrega

A planilha lista **todos** os resultados, inclusive as positivas. O pacote ZIP
leva só o que vale como regularidade:

```
CERTIDOES NEGATIVAS/                    NEGATIVA
POSITIVAS COM EFEITO DE NEGATIVA/       CPEN (débito parcelado)
indice.csv
```

Certidão positiva é **relatada mas não baixada** — entregar uma positiva junto
com as negativas é o erro que ninguém percebe até o cliente perceber.

A idempotência olha a **data de emissão no mês corrente**, não a validade: a
certidão vale 180 dias, mas quem recebe exige emissão do mês. E vale **dentro
da mesma planilha**: mandar a mesma lista de novo é pedir certidões novas, não
um relatório de que já existem.

### Várias máquinas, sem servidor

Cada máquina roda o seu robô e o seu painel, e é dona do seu próprio banco. O
aplicativo no seu computador pergunta a todas por HTTP e junta o quadro — não há
banco central, pasta compartilhada nem servidor a instalar. (SQLite em pasta de
rede corrompe: o Windows não trava o arquivo de forma confiável.)

### Console e máquina de robô

O mesmo programa roda em dois papéis, definidos por `[rede] papel`:

- **`robo`** — emite certidões. Mostra *Iniciar robô* e *Importar planilha*.
- **`console`** — só acompanha. Esconde essas ações, porque importar ali
  colocaria itens num banco onde robô nenhum vai buscá-los. Manda trabalho
  pela rede: *Enviar planilha* e *Iniciar/Parar* no cartão de cada máquina.

Vazio, ele decide sozinho: quem lista outras máquinas está acompanhando-as.

### Entrega por mês, não por lote

O corte é o **mês de emissão** — a mesma régua que o robô usa para decidir
se reemite, e a que o cliente recebe. Um filtro de órgão comanda a tela
inteira: percentual, números, o pacote ZIP e a planilha. A federal costuma
fechar antes das estaduais, e entregar só ela é o caso normal.

O pacote vem de **todas as máquinas de uma vez**, com uma pasta por órgão.
Máquina fora do ar não cancela a entrega, mas o aviso passa a ser "pacote
incompleto" com a lista do que ficou de fora.

### Comandar as máquinas pela rede

`POST /api/planilha`, `/api/robo/iniciar` e `/api/robo/parar` têm duas
travas que as rotas de leitura não têm:

1. **Senha obrigatória.** Sem `[rede] senha` elas recusam tudo — a
   capacidade perigosa nasce desligada.
2. **Área de trabalho destravada.** O robô cego move o mouse de verdade;
   com a estação bloqueada ele gastaria consultas gravando erro. Iniciar
   recusa com o motivo; parar continua valendo, que é quando se quer parar.

A tela **Máquinas** mostra um cartão por computador, com o órgão em destaque,
situação com duração, checklist de preparo (calibragem, AnyDesk, painel,
órgão), disco com a projeção do próximo lote, e o acesso remoto.

**O nome da máquina é um link**: em azul, e clicar nele abre o AnyDesk já
apontado para ela — não é preciso decorar nove dígitos nem procurar numa lista.
O link funciona inclusive quando a máquina não responde, que é justamente quando
alguém precisa entrar nela. O número não é credencial: o AnyDesk continua
pedindo a senha de acesso ou a confirmação de quem estiver na outra ponta.

## Segurança

- **Nenhum dado de cliente sai do controle da empresa** (RNF-06). É o motivo de os
  avisos irem para o Teams e não para o Discord: o Teams fica dentro do tenant da
  Mapah.
- **O painel inteiro é protegido por senha** quando `[rede] senha` está definida —
  páginas, downloads e API. Só `/ping` fica aberto, e ele devolve apenas sinal de
  vida e tamanho da fila. Navegador entra por Basic, o aplicativo por cabeçalho
  próprio.
- **Comando só vale a partir da tela do próprio painel.** Basic é credencial
  *ambiente*: uma vez digitada, o navegador a reenvia sozinha em qualquer
  pedido para aquela máquina — inclusive num formulário escondido numa página
  qualquer. Por isso todo `POST` confere de onde veio (`Sec-Fetch-Site`,
  `Origin`, `Referer`) e recusa o que vem de fora. Vale mesmo sem senha
  configurada, que é o caso pior: aí não há nem senha a exigir, e
  `http://127.0.0.1:8000` é endereço conhecido. O aplicativo de mesa não é
  navegador, não manda nenhum desses cabeçalhos, e continua comandando.
- **Atualizar pela rede exige o SHA-256 do pacote**, e só aceita origem de
  endereço interno (incluída a faixa `100.64.0.0/10` da VPN). O fluxo baixa um
  zip por HTTP simples e o transforma em `ACTA.exe` e `cnd.exe`: conferir o
  hash é a única coisa entre o que o console publicou e o que passa a rodar na
  máquina. O `empacotar/publicar.py` imprime o hash junto com o endereço, e a
  conferência acontece **antes** de qualquer processo ser parado — falhar ali
  não deixa a instalação pela metade, que é o único estado do qual não se sai
  pela rede.
- **O `config.toml` não vai para o controle de versão.** Ele é da instalação, não
  do programa: traz a senha do painel, o endereço da máquina na rede e o número
  do AnyDesk dela. Versionado, a senha viajava junto — foi retirada do histórico
  inteiro em 18/08/2026, antes de o repositório ir para o GitHub. O que está
  versionado é o `config.exemplo.toml`, com os mesmos comentários e sem valor
  nenhum preenchido: para instalar noutra máquina, copie e preencha.
- **Segredo mesmo é melhor por variável de ambiente**, que não chega nem a ser
  escrita em arquivo: `CND_REDE_SENHA`, `CND_TEAMS_WEBHOOK`, `CND_GRAPH_SECRET`,
  `CND_SMTP_SENHA`. Todas têm prioridade sobre o que estiver no arquivo.
- Planilhas de clientes e a pasta `data/` estão no `.gitignore`.

## Comandos

```powershell
cnd importar "CND_MIA_0726.xlsx" --abas RFB   # cria o lote
cnd calibrar                                  # ensina onde ficam os campos
cnd rodar                                     # o robô
cnd painel --host 0.0.0.0                     # publica na rede
cnd relatorio --mes 2026-08 --orgao RFB_PJ    # Excel do mês, por órgão
cnd testar-alerta                             # confere o Teams
cnd simular 150                               # lote falso, sem tocar em portal
cnd app                                       # o aplicativo de mesa
```

Na máquina instalada, o mesmo com `cnd.exe` ao lado do `ACTA.exe`.

`cnd simular` vale a pena: o adapter falso **imita a heurística antirrobô** —
quanto mais rápido o robô consulta, maior a chance de "captcha". Rodar isso mostra
o ritmo adaptativo convergindo de verdade, sem gastar consulta no portal.

## Documentação

| Documento | Conteúdo |
|---|---|
| [01 — Visão geral e requisitos](docs/01-visao-geral-e-requisitos.md) | Problema, escopo, requisitos |
| [02 — Arquitetura](docs/02-arquitetura.md) | Componentes, adapters, fluxo ponta a ponta |
| [03 — Modelo de dados](docs/03-modelo-de-dados.md) | Entidades, schema, armazenamento de PDFs |
| [04 — Ciclo de vida, retry e captcha](docs/04-ciclo-de-vida-retry-captcha.md) | Estados, ritmo adaptativo, disjuntor |
| [05 — Observabilidade](docs/05-observabilidade-dashboard.md) | Painel, logs, alertas, relatório |
| [06 — Roadmap](docs/06-roadmap.md) | Fases e critérios de aceite |
| [07 — Instalação nas máquinas](docs/07-instalacao-nas-maquinas.md) | Empacotamento, atalhos, AnyDesk |
| [Fluxo do portal RFB PJ](docs/fluxos/rfb-pj.md) | O caminho real na tela — fonte da verdade do adapter |
| [ADRs](docs/adr/) | As decisões de arquitetura e por que foram tomadas |

## Como o código está organizado

```
src/cnd/
├── core/          regras que valem para qualquer órgão
│   ├── documentos.py   valida CNPJ (numérico e alfanumérico) e CPF
│   ├── modelos.py      o vocabulário: Status e Desfecho
│   ├── fila.py         reivindicar, concluir, reagendar, recuperar órfãos
│   ├── ritmo.py        acelerador/freio adaptativo (AIMD)
│   ├── breaker.py      disjuntor por órgão
│   └── tempo.py        datas em UTC, ordenáveis como texto
├── adapters/      um arquivo por portal — o que muda quando o site muda
│   ├── base.py         o contrato que todos seguem
│   ├── rfb_cego.py     Receita Federal PJ, por mouse e teclado reais  ← ativo
│   ├── rfb_pj.py       o mesmo portal por Playwright — DETECTADO, desligado
│   ├── calibragem.py   ensina ao robô cego onde ficam os campos
│   └── fake.py         simulador para teste offline
├── ingestao/      única camada que conhece Excel
├── orquestrador/  decide quando e o quê executar; vigia e alerta
├── web/           painel, API de leitura, relatório e ZIP
├── desktop/       o aplicativo: telas, marca, acesso remoto
├── infra/         banco, config, log, tela, entrada, Teams, arquivos
└── lancador.py    ponto de entrada do ACTA.exe: sem argumentos abre a
                   janela, com argumentos vira linha de comando
```

A regra que amarra tudo: as camadas de cima importam as de baixo, **nunca o
contrário**. O `core` não sabe que existe portal, tela ou navegador. É isso que
faz uma mudança no site da Receita afetar um arquivo só.

## Decisões principais

- **SQLite (WAL) com a fila no próprio banco**, sem broker externo — [ADR-002](docs/adr/ADR-002-sqlite-fila-no-banco.md)
- **Um adapter por órgão** atrás de uma interface única — [ADR-003](docs/adr/ADR-003-adapter-por-orgao.md)
- **Captcha evitado por comportamento**, sem serviço de quebra — [ADR-004](docs/adr/ADR-004-estrategia-captcha.md)
- **Painel FastAPI com páginas renderizadas no servidor** — [ADR-005](docs/adr/ADR-005-dashboard-fastapi-htmx.md)
- **Playwright abandonado em favor de entrada real do Windows** — o portal detecta
  o primeiro; [ADR-001](docs/adr/ADR-001-python-playwright.md) registra a decisão original e por que caiu.
- **Nada pago**: sem API do SERPRO, sem serviço de quebra de captcha, sem nuvem.
