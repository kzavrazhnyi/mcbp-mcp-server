@echo off
setlocal

echo === mcbp-ai MCP client setup for Codex ===
echo.

set "MCP_URL=%~1"
set "MCP_TOKEN=%~2"

if "%MCP_URL%"=="" set /p MCP_URL=Server URL (example https://YOUR-SERVER-IP:8443/mcp): 
if "%MCP_TOKEN%"=="" set /p MCP_TOKEN=Access token: 

if "%MCP_URL%"=="" (
  echo ERROR: server URL is required.
  goto :fail
)
if "%MCP_TOKEN%"=="" (
  echo ERROR: access token is required.
  goto :fail
)

set "CODEX="
for /f "delims=" %%I in ('where codex 2^>nul') do if not defined CODEX set "CODEX=%%I"
if not defined CODEX (
  for /f "delims=" %%I in ('dir /b /s "%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe" 2^>nul') do if not defined CODEX set "CODEX=%%I"
)
if not defined CODEX (
  echo ERROR: codex.exe not found in PATH or under %LOCALAPPDATA%\OpenAI\Codex\bin.
  echo Install the Codex CLI first, then run this script again.
  goto :fail
)
echo Codex: %CODEX%

setx MCBP_TOKEN "%MCP_TOKEN%" >nul
if errorlevel 1 (
  echo ERROR: could not store MCBP_TOKEN in the user environment.
  goto :fail
)
echo Token stored in the user variable MCBP_TOKEN.

"%CODEX%" mcp add mcbp --url "%MCP_URL%" --bearer-token-env-var MCBP_TOKEN
if errorlevel 1 (
  echo ERROR: 'codex mcp add' failed. See the message above.
  goto :fail
)

echo.
echo === Done ===
echo Close this window and open a NEW terminal so MCBP_TOKEN becomes visible.
echo Check with:  codex mcp list
echo.
pause
exit /b 0

:fail
echo.
echo === FAILED ===
pause
exit /b 1
