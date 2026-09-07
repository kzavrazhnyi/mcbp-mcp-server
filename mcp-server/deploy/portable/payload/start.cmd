@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === mcbp-ai MCP server ===
echo Config: %~dp0server.env
echo Stop: Ctrl+C
echo.
python\python.exe serve.py
echo.
echo === Server exited with code %ERRORLEVEL% ===
pause
