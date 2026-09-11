<#
.SYNOPSIS
    Складає портабельну Windows-поставку MCP-сервера mcbp-ai.

.DESCRIPTION
    Тягне embeddable-дистрибутив Python з python.org, ставить у нього pip і пакет
    `mcbp[http]` з TestPyPI, потім розкладає поруч лаунчер і скрипти супроводу.
    Результат — самодостатня тека, яку копіюють на іншу Windows-машину.

    Типова тека виводу лежить у tmp/ (у .gitignore), тому сама поставка в репозиторій
    не потрапляє — комітиться лише цей скрипт і payload\.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -OutDir D:\dist\mcbp -Force
#>
[CmdletBinding()]
param(
    [string] $OutDir = "C:\PYTHON\mcbp\tmp\mcbp-mcp-portable",
    [string] $PythonVersion = "3.11.9",
    [string] $McbpVersion = "0.2.4",
    [string] $CacheDir = (Join-Path $env:TEMP "mcbp-portable-build"),
    [switch] $Force
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$payload = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "payload"
if (-not (Test-Path $payload)) { throw "Не знайдено теку payload: $payload" }

$pyTag = "python" + ($PythonVersion.Split(".")[0]) + ($PythonVersion.Split(".")[1])
$embedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$testPypi = "https://test.pypi.org/simple/"
$pypi = "https://pypi.org/simple/"

if (Test-Path $OutDir) {
    if (-not $Force) { throw "Тека виводу вже існує: $OutDir. Додайте -Force, щоб перезібрати." }
    Write-Host "Видаляю попередню збірку: $OutDir"
    Remove-Item -Recurse -Force $OutDir
}
New-Item -ItemType Directory -Path $OutDir | Out-Null
if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir | Out-Null }

# --- 1. Embeddable Python -----------------------------------------------------------------
$embedZip = Join-Path $CacheDir "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Write-Host "Завантажую $embedUrl ..."
    Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
} else {
    Write-Host "Беру з кешу: $embedZip"
}

$pyDir = Join-Path $OutDir "python"
Write-Host "Розпаковую Python у $pyDir ..."
Expand-Archive -Path $embedZip -DestinationPath $pyDir -Force

# Без розкоментованого `import site` embeddable-Python не бачить Lib\site-packages,
# тому pip не працює взагалі, а не «працює криво».
$pth = Join-Path $pyDir "$pyTag._pth"
if (-not (Test-Path $pth)) { throw "Не знайдено $pth — перевірте параметр -PythonVersion" }
Write-Host "Вмикаю import site у $(Split-Path -Leaf $pth) ..."
$pthText = (Get-Content $pth -Raw) -replace '(?m)^\s*#\s*import\s+site\s*$', 'import site'
if ($pthText -notmatch '(?m)^\s*import\s+site\s*$') { $pthText += "`r`nimport site`r`n" }
Set-Content -Path $pth -Value $pthText -Encoding ASCII

# --- 2. pip -------------------------------------------------------------------------------
$getPip = Join-Path $CacheDir "get-pip.py"
if (-not (Test-Path $getPip)) {
    Write-Host "Завантажую get-pip.py ..."
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPip -UseBasicParsing
}
$python = Join-Path $pyDir "python.exe"
Write-Host "Встановлюю pip ..."
& $python $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip.py завершився з кодом $LASTEXITCODE" }

# --- 3. Пакет mcbp[http] ------------------------------------------------------------------
Write-Host "Встановлюю mcbp[http]==$McbpVersion з TestPyPI ..."
& $python -m pip install --no-warn-script-location `
    --index-url $testPypi --extra-index-url $pypi "mcbp[http]==$McbpVersion"
if ($LASTEXITCODE -ne 0) { throw "pip install mcbp[http] завершився з кодом $LASTEXITCODE" }

# cryptography потрібна лише make-cert.ps1 для конвертації PFX -> PEM. Ставимо тут, щоб
# створення сертифіката не вимагало доступу до мережі на цільовій машині.
Write-Host "Встановлюю cryptography (для make-cert.ps1) ..."
& $python -m pip install --no-warn-script-location cryptography
if ($LASTEXITCODE -ne 0) { throw "pip install cryptography завершився з кодом $LASTEXITCODE" }

# --- 4. Файли поставки --------------------------------------------------------------------
Write-Host "Розкладаю файли поставки ..."
Copy-Item -Path (Join-Path $payload "*") -Destination $OutDir -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $OutDir "certs") -Force | Out-Null

# Комплект клієнта їде разом із поставкою: адміністратор роздає ці файли, не всю теку.
$clientKit = Join-Path (Split-Path -Parent (Split-Path -Parent $payload)) "client-kit"
if (-not (Test-Path $clientKit)) { throw "Не знайдено теку client-kit: $clientKit" }
$clientKitOut = Join-Path $OutDir "client-kit"
New-Item -ItemType Directory -Path $clientKitOut -Force | Out-Null
Copy-Item -Path (Join-Path $clientKit "*") -Destination $clientKitOut -Recurse -Force

# --- 5. Підсумок --------------------------------------------------------------------------
$installed = & $python -c "from importlib.metadata import version; print(version('mcbp'), version('mcbp-core'))"
$size = (Get-ChildItem $OutDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host "Готово: $OutDir" -ForegroundColor Green
Write-Host ("Python $PythonVersion, mcbp/mcbp-core: $installed")
Write-Host ("Розмір: {0:N1} МБ, файлів: {1}" -f ($size / 1MB), (Get-ChildItem $OutDir -Recurse -File).Count)
Write-Host ""
Write-Host "Далі: відредагуйте server.env і запустіть start.cmd."
