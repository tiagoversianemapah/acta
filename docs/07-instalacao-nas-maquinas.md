# 07 — Instalação nas máquinas

Este documento é operacional: como sair do código-fonte e chegar a um
programa instalado nas máquinas do escritório.

## O que é entregue

Uma pasta, `dist/ACTA/`, com tudo dentro:

```
ACTA/
  ACTA.exe       ← a janela; é o que vai no atalho
  cnd.exe        ← a linha de comando e o processo do robô
  config.toml    ← ajustes da máquina
  data/          ← banco, certidões, evidências, logs
  _internal/     ← Python e bibliotecas
```

O produto se chama ACTA; `cnd` continua sendo o nome do pacote e da linha
de comando, porque está em toda a documentação e em todo comando que a
operação já conhece.

Não há instalador e não há Python na máquina de destino. Copiar a pasta
**é** a instalação. A decisão é deliberada: são computadores de escritório
sem administrador dedicado, e exigir ambiente Python em cada um levaria,
em pouco tempo, a versões diferentes rodando em lugares diferentes — o tipo
de divergência que só aparece quando alguém já entregou certidão errada.

## Como construir

Na máquina de desenvolvimento, com o ambiente montado:

```
python empacotar/construir.py
```

O que ele faz, em ordem:

1. desenha o ícone a partir de `marca.py` — a identidade visual tem uma
   fonte só, e o ícone do atalho acompanha qualquer mudança na marca;
2. escreve `versao.txt`, que preenche Propriedades > Detalhes do
   executável. Programa sem isso aparece como "desconhecido" para o
   SmartScreen e para as políticas de aplicativo do Windows;
3. roda o PyInstaller com `empacotar/acta.spec`;
4. leva `config.toml` e a calibragem para junto do executável — **sem
   sobrescrever** o que já existir, para que reconstruir não apague o
   ajuste de quem opera;
5. cria os atalhos na Área de Trabalho e no Menu Iniciar, e **apaga os de
   nomes antigos** (`NOMES_ANTIGOS` no script) — atalho apontando para um
   executável que não existe mais é a primeira coisa que alguém clica.

Opções: `--sem-atalhos` (só constrói) e `--so-atalhos` (só refaz os
atalhos, útil depois de copiar a pasta para outra máquina).

### Modo pasta, não arquivo único

O PyInstaller sabe gerar um `.exe` sozinho. Não usamos: ele se descompacta
inteiro num diretório temporário a cada abertura, o que custa segundos de
espera e chama a atenção do antivírus corporativo. A pasta abre instantâneo
e deixa o `config.toml` visível ao lado do programa, que é onde o operador
espera encontrá-lo.

### Dois executáveis

`ACTA.exe` não tem console — é um aplicativo, e uma janela preta
piscando atrás dele seria amadorismo. Mas o robô **precisa** de console: a
janela lê a saída dele para mostrar no Registro, e um executável sem
console entregaria essa saída no vazio. Daí `cnd.exe`, que é o mesmo
programa compilado com console e roda escondido
(`CREATE_NO_WINDOW`).

Os dois compartilham o mesmo `_internal/`, então o custo é de 5 MB, não do
pacote inteiro.

### Onde o programa procura os arquivos

`RAIZ_PROJETO`, em `infra/db.py`, muda de significado conforme o modo:

| modo | raiz |
|---|---|
| código-fonte | a raiz do repositório |
| empacotado | a pasta onde está o `.exe` |

É o que faz o mesmo código achar `config.toml` e `data/` nos dois casos.

## O Windows 11 bloqueia o executável

Em máquina com o **Controle Inteligente de Aplicativos** ligado, abrir o
`ACTA.exe` produz:

> Bloqueamos …\ACTA.exe porque não conseguimos verificar seu fornecedor e
> confirmar se ele é seguro para execução.

Não é defeito nosso nem falso positivo de antivírus. O recurso recusa
**qualquer** executável sem assinatura digital de uma autoridade
certificadora reconhecida, e — ao contrário do SmartScreen — **não oferece
"executar assim mesmo" nem aceita exceção**. Adicionar a pasta às exclusões
do Windows Defender não muda nada: são mecanismos diferentes.

Conferir numa máquina:

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
# 0 = desligado   1 = ligado   2 = em avaliação
```

Ele só liga sozinho em **instalação limpa** do Windows 11. Máquina de
escritório que veio de atualização quase sempre está com 0, e nesse caso não
há nada a fazer.

### Saída 1 — atalho que roda pelo Python (grátis, imediato)

```powershell
python empacotar/construir.py --atalho-fonte
```

O atalho passa a apontar para o `pythonw.exe` do ambiente, que é assinado
pela Python Software Foundation e portanto **passa** pelo controle. Ícone,
nome, janela e comportamento são os mesmos — a diferença não aparece para
quem usa.

O preço: essa máquina precisa do repositório e do ambiente Python, o que
anula a vantagem de "copiar a pasta e pronto". Faz sentido no computador de
quem acompanha o robô; nas máquinas do robô, prefira uma das outras saídas.

### Saída 2 — desligar o Controle Inteligente

Segurança do Windows > Controle de aplicativo e navegador > Controle
inteligente de aplicativos > Desativado.

**É uma decisão sem volta:** uma vez desligado, o Windows só permite ligá-lo
de novo com uma reinstalação do sistema. Nas máquinas dedicadas ao robô é
defensável — elas rodam um programa só, num escopo conhecido. No computador
pessoal de alguém, pense duas vezes.

### Saída 3 — assinar o executável (custa dinheiro, resolve de vez)

Um certificado de assinatura de código de uma autoridade reconhecida
(DigiCert, Sectigo e afins) faz o bloqueio desaparecer em qualquer máquina,
e ainda tira o aviso do SmartScreen. É o que qualquer software distribuído
faz. Custa na faixa de R$ 1.000 a R$ 2.500 por ano, com validação da empresa
— e como o CNPJ da Mapah é real e verificável, a emissão é rotina.

O `construir.py` já assina sozinho quando encontra o certificado:

```powershell
setx CND_CERT_PFX   "C:\certificados\mapah.pfx"
setx CND_CERT_SENHA "..."
python empacotar/construir.py
```

Ele carimba a hora num servidor público, para a assinatura continuar válida
depois que o certificado expirar.

**Certificado autoassinado não resolve.** O Controle Inteligente avalia
contra o serviço de reputação da Microsoft, não contra o armazenamento de
confiança da máquina — um certificado criado por nós mesmos, ainda que
distribuído por diretiva de grupo, não passa. Ele serve para diretiva de
aplicativo dentro do domínio, e só.

### O Playwright agora vai junto — por causa do CRF

Isto mudou em 18/08/2026 e vale entender por quê, porque a frase antiga
misturava duas coisas.

O adapter `rfb_pj` continua excluído: o portal da **Receita** detecta
automação de navegador (comprovado em 07/08/2026, teste A/B com o mesmo
CNPJ e o mesmo IP), e quem atende a Receita é o `rfb_cego`, que move o
mouse de verdade e lê a tela.

Mas isso é sobre a Receita, não sobre o Playwright. O portal do **CRF da
Caixa** usa ShieldSquare/Radware, que barra `urllib` (uma requisição
direta volta página de captcha) e **não** barrou o Playwright dirigindo o
Edge. Então o `crf.py` é por elemento — sem calibragem, sem coordenada de
tela, sem depender da resolução da máquina.

E não são "~100 MB de navegador": o adapter usa `channel="msedge"`, o Edge
que toda máquina já tem. O que entra é o *driver* do Playwright (node +
protocolo). **O pacote foi de 50 MB para 190 MB.** Vale saber antes de
atualizar várias máquinas pela rede.

O `cnd.adapters.federal.crf` precisa estar nos `hiddenimports` do `acta.spec`:
adapter é escolhido pelo `config.toml` e importado por nome, coisa que o
PyInstaller não enxerga lendo o código. Sem essa linha o pacote sai limpo
e o robô morre ao subir o worker.

## Instalar numa máquina do robô

1. copie a pasta `ACTA` para a máquina (por rede, pen drive ou o
   próprio AnyDesk);
2. edite o `config.toml` dela:
   - `[rede] nome` — como ela aparece no aplicativo, ex. `"PC-CND-01"`
   - `[rede] papel = "robo"` — mostra *Iniciar robô* e *Importar planilha*
   - `[rede] senha` — **obrigatória** para ela aceitar planilha e comando
     pela rede; sem isso essas rotas recusam tudo. A mesma em todas.
   - `[rede] anydesk` — ou cadastre pela tela, em Ajustes
   - `maquinas = []` — máquina de robô não consulta ninguém
   - o órgão que ela atende, em `[orgaos.*]`, com `nome` de exibição
3. calibre o robô cego naquela tela:
   `cnd.exe calibrar` para Receita PJ,
   `cnd.exe calibrar --orgao RFB_PF` para Receita PF (é outro formulário,
   com o campo da data de nascimento), ou
   `cnd.exe calibrar --orgao SEFAZ_ES` para Espírito Santo. A calibragem é
   guardada em proporções da janela, mas a posição dos campos ainda depende do
   zoom e da resolução dela;
4. **libere a porta 8000 no Firewall do Windows** (uma vez, num prompt como
   administrador):

   ```
   netsh advfirewall firewall add rule name="ACTA" dir=in action=allow protocol=TCP localport=8000
   ```

   Sem isso o painel sobe, responde em `127.0.0.1` na própria máquina, e é
   silenciosamente recusado para qualquer outro computador — o sintoma é a
   máquina aparecer como "Sem resposta" no aplicativo, sem erro nenhum no
   log dela. É a causa número um de a tela Máquinas não funcionar.
5. **dê um endereço fixo à máquina.** O `config.toml` do seu computador
   guarda o IP dela; se o DHCP trocar o número num reinício, o cartão
   simplesmente para de responder. Ou reserve o IP no roteador, ou use o
   nome da máquina na URL (`http://PC-CND-01:8000`), que a rede Windows
   resolve sozinha e não muda;
6. suba o painel para que o aplicativo a enxergue:
   `cnd.exe painel --host 0.0.0.0`

   Para subir sozinho quando alguém liga a máquina: `Win+R`,
   `shell:startup`, e ponha ali um atalho para esse comando. O robô também
   precisa estar rodando — o painel só mostra, quem trabalha é o `cnd rodar`;
7. instale o AnyDesk e anote o número que aparece em "Este computador".

### Conferir antes de sair da máquina

Do **seu** computador, com a máquina do robô ligada:

```powershell
curl http://192.168.0.21:8000/ping
```

- respondeu um JSON → está tudo certo, pode cadastrar no `config.toml`
- "não foi possível conectar" → firewall, ou o painel não está no ar
- pediu senha (401) → certo também: significa que `[rede] senha` está
  valendo. O aplicativo manda a senha; o `curl` não.

## Instalar no seu computador

Mesma pasta, mas o `config.toml` lista as outras:

```toml
[rede]
nome  = "Tiago"
papel = "console"      # não emite: acompanha e manda trabalho
senha = "..."          # a mesma em todas; prefira CND_REDE_SENHA
maquinas = [
  { orgao = "ACTA CND FEDERAL",    nome = "PC-CND-01", url = "http://192.168.0.21:8000", anydesk = "123 456 789" },
  { orgao = "ACTA CND ESTADUAIS",  nome = "PC-CND-02", url = "http://192.168.0.22:8000", anydesk = "234 567 890" },
]
```

A tela **Máquinas** passa a mostrar um cartão por computador: o órgão em
destaque, o nome e o endereço abaixo, progresso do lote, e os botões de
baixar planilha e baixar certidões.

### O nome da máquina é o acesso remoto

O `orgao` aparece em **azul** no cartão, e clicar nele abre o AnyDesk já
apontado para aquela máquina. Não é um botão separado de propósito: o que
a pessoa está olhando quando decide entrar na máquina é o nome dela, e é
ali que a mão já está.

O link procura o AnyDesk nos caminhos usuais de instalação e, se não achar,
tenta o protocolo `anydesk:` — que existe se o programa já rodou ali, mesmo
em versão portátil. O número pode ser colado com os espaços do jeito que o
AnyDesk mostra (`123 456 789`); apelidos (`mapah-cnd@ad`) passam inteiros.

O número **não é credencial**: para conectar, o AnyDesk continua exigindo a
senha de acesso não vigiado ou a confirmação de quem estiver na outra
ponta. Guardá-lo no `config.toml` não abre porta nenhuma.

O link funciona **inclusive na máquina que não respondeu**. É de propósito:
é justamente quando o painel diz "sem resposta" que alguém precisa entrar
nela.
