@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo === New access token for the mcbp-ai MCP server ===
echo Checks a BAS account, adds it to the tokens file (MCP_TOKENS_FILE in server.env)
echo and prints the ready "claude mcp add" command.
echo.
if not exist "python\python.exe" (
  echo === FAILED: bundled python not found next to this script ===
  pause
  exit /b 1
)
python\python.exe new_token.py
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo === Finished with code %RC% ===
pause
exit /b %RC%
