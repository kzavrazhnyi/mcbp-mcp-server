<#
.SYNOPSIS
    Створює самопідписаний TLS-сертифікат для портабельної поставки MCP-сервера mcbp-ai
    і вносить його в довірені кореневі центри цієї машини.

.DESCRIPTION
    УВАГА: скрипт ПОТРІБНО запускати від імені АДМІНІСТРАТОРА.
    Він вносить кореневий сертифікат у сховище Cert:\LocalMachine\Root — тобто ця машина
    почне БЕЗЗАСТЕРЕЖНО довіряти всьому, що підписано цим ключем. Робіть це лише на
    машинах, які ви контролюєте, і зберігайте certs\ у безпеці: приватний ключ лежить
    там незашифрованим (інакше uvicorn не зможе його прочитати без пароля).

    Результат у теці certs\:
        mcbp-mcp.pfx       — сертифікат разом із ключем (пароль -PfxPassword)
        mcbp-mcp.cer       — лише публічна частина, для перенесення на інші машини
        mcbp-mcp.crt.pem   — сертифікат у форматі PEM   -> MCP_HTTP_SSL_CERTFILE
        mcbp-mcp.key.pem   — приватний ключ у PEM       -> MCP_HTTP_SSL_KEYFILE

    Після виконання розкоментуйте два рядки MCP_HTTP_SSL_* у server.env.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\make-cert.ps1
    powershell -ExecutionPolicy Bypass -File .\make-cert.ps1 -DnsName <імʼя машини> -IPAddress <LAN-адреса>
#>
[CmdletBinding()]
param(
    [string] $DnsName = "localhost",
    [string] $IPAddress = "127.0.0.1",
    [int]    $Years = 5,
    [string] $PfxPassword = "mcbp"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$certDir = Join-Path $root "certs"
$python = Join-Path $root "python\python.exe"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "ПОТРІБНІ ПРАВА АДМІНІСТРАТОРА." -ForegroundColor Red
    Write-Host "Скрипт створює сертифікат і вносить його в Cert:\LocalMachine\Root."
    Write-Host "Відкрийте PowerShell через 'Запуск від імені адміністратора' і повторіть:"
    Write-Host "    powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    Write-Host ""
    exit 1
}

if (-not (Test-Path $python)) {
    Write-Host "Не знайдено вбудований Python: $python" -ForegroundColor Red
    Write-Host "Схоже, поставка неповна — розпакуйте архів цілком."
    exit 1
}

if (-not (Test-Path $certDir)) { New-Item -ItemType Directory -Path $certDir | Out-Null }

$pfxPath = Join-Path $certDir "mcbp-mcp.pfx"
$cerPath = Join-Path $certDir "mcbp-mcp.cer"
$crtPem  = Join-Path $certDir "mcbp-mcp.crt.pem"
$keyPem  = Join-Path $certDir "mcbp-mcp.key.pem"

$san = "2.5.29.17={text}DNS=$DnsName"
if ($IPAddress) { $san = "$san&IPAddress=$IPAddress" }

Write-Host "Створюю сертифікат для CN=$DnsName (SAN: DNS=$DnsName, IP=$IPAddress), строк $Years р. ..."
$cert = New-SelfSignedCertificate `
    -Subject "CN=$DnsName" `
    -TextExtension @($san) `
    -CertStoreLocation "Cert:\LocalMachine\My" `
    -KeyAlgorithm RSA -KeyLength 2048 `
    -KeyExportPolicy Exportable `
    -KeyUsage DigitalSignature, KeyEncipherment `
    -Type SSLServerAuthentication `
    -NotAfter (Get-Date).AddYears($Years) `
    -FriendlyName "mcbp-ai MCP portable"

Write-Host "Відбиток: $($cert.Thumbprint)"

$securePwd = ConvertTo-SecureString -String $PfxPassword -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $securePwd | Out-Null
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null

Write-Host "Вношу сертифікат у Cert:\LocalMachine\Root (довірені кореневі) ..."
Import-Certificate -FilePath $cerPath -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null

Write-Host "Конвертую PFX у PEM ..."
$converter = Join-Path $env:TEMP "mcbp-pfx2pem.py"
$pyCode = @'
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

pfx_path, password, crt_out, key_out = sys.argv[1:5]
with open(pfx_path, "rb") as fh:
    key, cert, chain = pkcs12.load_key_and_certificates(fh.read(), password.encode("utf-8"))

with open(crt_out, "wb") as fh:
    fh.write(cert.public_bytes(serialization.Encoding.PEM))
    for extra in chain or []:
        fh.write(extra.public_bytes(serialization.Encoding.PEM))

with open(key_out, "wb") as fh:
    fh.write(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))

print("PEM written:", crt_out, key_out)
'@
Set-Content -Path $converter -Value $pyCode -Encoding UTF8

& $python -c "import cryptography" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Доставляю пакет cryptography (потрібен доступ до мережі) ..."
    & $python -m pip install --quiet cryptography
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Не вдалося встановити cryptography — PEM не створено." -ForegroundColor Red
        exit 1
    }
}

& $python $converter $pfxPath $PfxPassword $crtPem $keyPem
if ($LASTEXITCODE -ne 0) {
    Write-Host "Конвертація в PEM не вдалася." -ForegroundColor Red
    exit 1
}
Remove-Item $converter -Force

Write-Host ""
Write-Host "Готово. Файли в $certDir" -ForegroundColor Green
Write-Host "Тепер розкоментуйте в server.env два рядки:"
Write-Host "    MCP_HTTP_SSL_CERTFILE=certs\mcbp-mcp.crt.pem"
Write-Host "    MCP_HTTP_SSL_KEYFILE=certs\mcbp-mcp.key.pem"
Write-Host "і запустіть start.cmd — сервер підніметься на https://${DnsName}:порт/mcp"
Write-Host ""
Write-Host "Щоб інша машина довіряла цьому серверу, скопіюйте на неї mcbp-mcp.cer"
Write-Host "і внесіть у 'Довірені кореневі центри сертифікації' локального комп'ютера."
