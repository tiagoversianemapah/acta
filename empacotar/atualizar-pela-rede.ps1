# Atualiza o ACTA de uma maquina robo baixando o pacote do console.
#
# Sem compartilhamento e sem senha: o console publica a pasta por HTTP na
# rede local por alguns minutos, a maquina baixa e troca os arquivos. As
# tentativas por compartilhamento SMB morreram em credencial (erro 1326) e
# a transferencia pelo AnyDesk e manual demais para repetir a cada ajuste.
#
# NAO toca em data\ nem em config.toml: calibragem, banco e o endereco da
# maquina sao dela, e uma atualizacao que apaga a calibragem obriga a
# recalibrar tudo - e o passo mais caro de instalar uma maquina nova.

param(
    [Parameter(Mandatory = $true)][string]$Origem,   # ex: http://10.1.11.86:8899
    # O SHA-256 sai impresso pelo publicar.py, junto com o endereco. E
    # obrigatorio: o zip baixado por HTTP simples vira ACTA.exe e cnd.exe
    # nesta maquina, e conferir e a unica coisa entre o que o console
    # publicou e o que passa a rodar aqui.
    [Parameter(Mandatory = $true)][string]$Sha256,
    [string]$Pasta = "C:\ACTA"
)

$ErrorActionPreference = "Stop"
$zip = Join-Path $env:TEMP "acta-atualizacao.zip"
$tmp = Join-Path $env:TEMP "acta-atualizacao"

function PainelRespondendo() {
    try {
        Invoke-WebRequest "http://127.0.0.1:8000/ping" `
            -UseBasicParsing -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function IniciarPainelDireto() {
    Start-Process -FilePath (Join-Path $Pasta "cnd.exe") `
        -ArgumentList "painel --host 0.0.0.0" -WindowStyle Minimized
}

Write-Host "1/6  Baixando de $Origem ..."
Invoke-WebRequest "$Origem/acta.zip" -OutFile $zip -UseBasicParsing

Write-Host "2/6  Conferindo o pacote ..."
# Antes de parar processo nenhum: falhar aqui nao deixa a maquina no meio
# do caminho, que e o unico estado do qual nao da para sair pela rede.
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne $Sha256.ToLowerInvariant()) {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    throw "O pacote baixado nao confere: recebi $hash, esperava $Sha256. Nada foi trocado."
}
Write-Host "     hash confere"

Write-Host "3/6  Parando o painel, se estiver de pe ..."
# O executavel em uso nao pode ser substituido; parar antes evita um erro
# no meio da troca, com a pasta pela metade.
# Conferir que morreram, e nao dormir 2s torcendo: em 17/08/2026 o robo
# ainda estava encerrando, segurou _internal\libcrypto-3.dll, a copia parou
# no meio e a maquina ficou com painel morto - so voltou por AnyDesk.
try { schtasks /End /TN "ACTA Painel" 2>$null | Out-Null } catch {}
$vivos = @()
$limiteMorte = (Get-Date).AddSeconds(30)
while ($true) {
    $vivos = @(Get-Process cnd, ACTA -ErrorAction SilentlyContinue)
    if ($vivos.Count -eq 0 -or (Get-Date) -ge $limiteMorte) { break }
    $vivos | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}
if ($vivos.Count -gt 0) {
    Write-Host "     AVISO: $($vivos.Count) processo(s) ainda vivos; a copia pode falhar"
} else {
    Write-Host "     processos encerrados"
}

Write-Host "4/6  Abrindo o pacote ..."
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp -Force

Write-Host "5/6  Trocando os arquivos em $Pasta ..."
# O Windows solta o arquivo com atraso (antivirus, indexador). Insistir
# custa menos que deixar a pasta pela metade.
$tentativaCopia = 0
while ($true) {
    try {
        Copy-Item "$tmp\*" $Pasta -Recurse -Force -ErrorAction Stop
        break
    } catch {
        $tentativaCopia++
        if ($tentativaCopia -ge 5) { throw }
        Write-Host "     copia falhou ($tentativaCopia/5); tentando de novo ..."
        Start-Sleep -Seconds 3
    }
}

Write-Host "6/6  Subindo o painel de novo ..."
schtasks /Run /TN "ACTA Painel" | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "     agendador acionado"
} else {
    Write-Host "     agendador indisponivel; testando inicio direto"
}
# Dar tempo ao agendador: com 5s fixos o fallback abria um SEGUNDO painel,
# e o processo extra e justamente o que trava a copia da proxima vez.
$limitePainel = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $limitePainel -and -not (PainelRespondendo)) {
    Start-Sleep -Seconds 2
}
if (-not (PainelRespondendo)) {
    IniciarPainelDireto
    Write-Host "     iniciado em janela minimizada por fallback direto"
} else {
    Write-Host "     painel respondendo"
}

Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Pronto. Calibragem, banco e config.toml ficaram intactos."
