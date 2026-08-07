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

### O Playwright fica de fora

O adapter `rfb_pj` está desligado — o portal da Receita o detecta
(comprovado em 07/08/2026, teste A/B com o mesmo CNPJ e o mesmo IP). Ele é
excluído do pacote junto com o Playwright, que traria ~100 MB de navegador
sem utilidade. O pacote fica em 50 MB.

## Instalar numa máquina do robô

1. copie a pasta `ACTA` para a máquina (por rede, pen drive ou o
   próprio AnyDesk);
2. edite o `config.toml` dela:
   - `[rede] nome` — como ela aparece no aplicativo, ex. `"PC-CND-01"`
   - `maquinas = []` — máquina de robô não consulta ninguém
   - o órgão que ela atende, em `[orgaos.*]`
3. calibre o robô cego naquela tela:
   `cnd.exe calibrar` — a calibragem é guardada em proporções da janela,
   mas a posição dos campos ainda depende do zoom e da resolução dela;
4. suba o painel para que o aplicativo a enxergue:
   `cnd.exe painel --host 0.0.0.0`
   (para subir sozinho no logon, ponha um atalho desse comando em
   `shell:startup`);
5. instale o AnyDesk e anote o número que aparece em "Este computador".

## Instalar no seu computador

Mesma pasta, mas o `config.toml` lista as outras:

```toml
[rede]
nome  = "Tiago"
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
