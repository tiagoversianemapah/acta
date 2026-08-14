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

Write-Host "1/5  Baixando de $Origem ..."
Invoke-WebRequest "$Origem/acta.zip" -OutFile $zip -UseBasicParsing

Write-Host "2/5  Parando o painel, se estiver de pe ..."
# O executavel em uso nao pode ser substituido; parar antes evita um erro
# no meio da troca, com a pasta pela metade.
try { schtasks /End /TN "ACTA Painel" 2>$null | Out-Null } catch {}
Get-Process cnd, ACTA -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host "3/5  Abrindo o pacote ..."
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp -Force

Write-Host "4/5  Trocando os arquivos em $Pasta ..."
Copy-Item "$tmp\*" $Pasta -Recurse -Force

Write-Host "5/5  Subindo o painel de novo ..."
schtasks /Run /TN "ACTA Painel" | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "     agendador acionado"
} else {
    Write-Host "     agendador indisponivel; testando inicio direto"
}
Start-Sleep -Seconds 5
if (-not (PainelRespondendo)) {
    IniciarPainelDireto
    Write-Host "     iniciado em janela minimizada por fallback direto"
} else {
    Write-Host "     painel respondendo"
}

Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Pronto. Calibragem, banco e config.toml ficaram intactos."
