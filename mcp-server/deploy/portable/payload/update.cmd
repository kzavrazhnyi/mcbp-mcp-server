@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Updating mcbp[http] from TestPyPI ===
python\python.exe -m pip install --upgrade --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "mcbp[http]" mcbp-core
if errorlevel 1 (
  echo.
  echo === UPDATE FAILED. Check network access. ===
  pause
  exit /b 1
)
echo.
echo === Installed versions ===
python\python.exe -c "from importlib.metadata import version; print('mcbp     ', version('mcbp')); print('mcbp-core', version('mcbp-core'))"
echo.
pause
