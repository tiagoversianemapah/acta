@echo off
REM Atualiza o ACTA desta maquina a partir da pasta compartilhada.
REM
REM Copiar a pasta inteira na mao toda vez e trabalhoso e arriscado: e facil
REM sobrescrever o config.toml ajustado ou o banco com as certidoes ja
REM emitidas. Este script copia so o programa e PRESERVA os dados.
REM
REM COMO USAR
REM   1. no computador que constroi, compartilhe a pasta dist\ACTA na rede
REM   2. ajuste ORIGEM abaixo para o caminho compartilhado
REM   3. deixe este .bat na pasta do ACTA de cada maquina do robo
REM   4. feche o ACTA e o painel, e rode o .bat
REM
REM O que ele NUNCA toca: config.toml, data\ (banco, certidoes, calibragem).

set ORIGEM=\\TIAGO\ACTA
set DESTINO=%~dp0

echo.
echo   Atualizando o ACTA a partir de %ORIGEM%
echo   Preservando config.toml e a pasta data\
echo.

robocopy "%ORIGEM%" "%DESTINO%" /MIR /XD data /XF config.toml /NFL /NDL /NJH /NP

if %ERRORLEVEL% GEQ 8 (
  echo.
  echo   FALHOU. Confira se a pasta compartilhada esta acessivel:
  echo      %ORIGEM%
  echo.
  pause
  exit /b 1
)

echo.
echo   Pronto. Suba o painel de novo:  cnd.exe painel --host 0.0.0.0
echo.
pause
