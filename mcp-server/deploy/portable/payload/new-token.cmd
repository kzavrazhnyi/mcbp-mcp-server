@echo off
setlocal
cd /d "%~dp0"

echo === New access token for the mcbp-ai MCP server ===
echo Put it into the tokens file (MCP_TOKENS_FILE) as a new [[users]] entry
echo and hand the same string to the client machine.
echo.
python\python.exe -c "import secrets;print(secrets.token_urlsafe(32))"
if errorlevel 1 (
  echo.
  echo === FAILED: bundled python not found next to this script ===
)
echo.
pause
