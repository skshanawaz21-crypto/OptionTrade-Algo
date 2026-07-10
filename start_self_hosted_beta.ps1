param(
    [switch]$QuickTunnel,
    [switch]$CloudAccessGuard,
    [string]$AllowedEmails = "",
    [string]$OwnerEmails = "",
    [string]$CloudflaredConfig = "",
    [string]$Hostname = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dashboardScript = Join-Path $projectRoot "run_dashboard.py"
$defaultCloudflaredConfig = Join-Path $env:USERPROFILE ".cloudflared\optiontrader.yml"
$dashboardUrl = "http://127.0.0.1:8877/"

if (-not $CloudflaredConfig) {
    $CloudflaredConfig = $defaultCloudflaredConfig
}

function Test-CommandExists {
    param([string]$Command)
    return $null -ne (Get-Command $Command -ErrorAction SilentlyContinue)
}

function Test-DashboardReady {
    try {
        $response = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Host "ERROR: Python not found at $pythonExe" -ForegroundColor Red
    Write-Host "Create/repair the virtual environment first." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path -LiteralPath $dashboardScript)) {
    Write-Host "ERROR: Dashboard script not found at $dashboardScript" -ForegroundColor Red
    exit 1
}

if (-not (Test-CommandExists "cloudflared")) {
    Write-Host "ERROR: cloudflared was not found in PATH." -ForegroundColor Red
    Write-Host "Install Cloudflare Tunnel/cloudflared first, then retry." -ForegroundColor Yellow
    exit 1
}

if ($QuickTunnel -and $CloudAccessGuard) {
    Write-Host "ERROR: -QuickTunnel cannot be combined with -CloudAccessGuard." -ForegroundColor Red
    Write-Host "Quick tunnels do not provide the Cloudflare Access identity header expected by the app guard." -ForegroundColor Yellow
    exit 1
}

$accessRequired = if ($CloudAccessGuard) { "1" } else { "0" }
$escapedProjectRoot = $projectRoot.Replace("'", "''")
$escapedPythonExe = $pythonExe.Replace("'", "''")
$escapedDashboardScript = $dashboardScript.Replace("'", "''")
$escapedAllowedEmails = $AllowedEmails.Replace("'", "''")
$escapedOwnerEmails = $OwnerEmails.Replace("'", "''")
$allowedEmailsCommand = ""
if ($AllowedEmails) {
    $allowedEmailsCommand = "`$env:OPTIONTRADER_CLOUD_ALLOWED_EMAILS = '$escapedAllowedEmails'`n"
}
$ownerEmailsCommand = ""
if ($OwnerEmails) {
    $ownerEmailsCommand = "`$env:OPTIONTRADER_OWNER_EMAILS = '$escapedOwnerEmails'`n"
}

if (Test-DashboardReady) {
    if ($CloudAccessGuard) {
        Write-Host "ERROR: OptionTrader dashboard is already running." -ForegroundColor Red
        Write-Host "The script cannot verify that the existing dashboard was started with the Cloudflare Access guard." -ForegroundColor Yellow
        Write-Host "Stop the existing dashboard, then rerun this command." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "OptionTrader dashboard is already running at $dashboardUrl" -ForegroundColor Green
} else {
    $dashboardCommand = @"
`$env:OPTIONTRADER_CLOUD_ACCESS_REQUIRED = '$accessRequired'
$allowedEmailsCommand$ownerEmailsCommand`$env:OPTIONTRADER_CLOUD_LOCAL_BYPASS = '1'
Set-Location -LiteralPath '$escapedProjectRoot'
& '$escapedPythonExe' '$escapedDashboardScript'
"@

    Write-Host "Starting OptionTrader dashboard window on $dashboardUrl" -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @("-NoExit", "-Command", $dashboardCommand)
    Start-Sleep -Seconds 3
}

if ($QuickTunnel) {
    $tunnelCommand = "cloudflared tunnel --url http://127.0.0.1:8877"
    $tunnelLabel = "Quick tunnel mode (temporary https://*.trycloudflare.com)"
} else {
    if (-not (Test-Path -LiteralPath $CloudflaredConfig)) {
        Write-Host "ERROR: OptionTrader Cloudflare config not found at $CloudflaredConfig" -ForegroundColor Red
        Write-Host "Create it with tools\new_optiontrader_cloudflare_config.ps1, or run with -QuickTunnel for temporary testing." -ForegroundColor Yellow
        exit 1
    }

    $tunnelCommand = "cloudflared tunnel --config `"$CloudflaredConfig`" run"
    $tunnelLabel = "Named tunnel mode from $CloudflaredConfig"
}

Write-Host "Starting Cloudflare tunnel window..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @("-NoExit", "-Command", "Set-Location -LiteralPath '$escapedProjectRoot'; $tunnelCommand")

Write-Host ""
Write-Host "OptionTrader self-hosted beta started." -ForegroundColor Green
Write-Host "Local dashboard: $dashboardUrl"
Write-Host "Tunnel: $tunnelLabel"
if ($Hostname) {
    Write-Host "Expected public hostname: https://$Hostname"
}
if ($CloudAccessGuard) {
    Write-Host "Cloudflare Access guard: ON"
    if ($AllowedEmails) {
        Write-Host "App-level email allowlist: $AllowedEmails"
    } else {
        Write-Host "App-level email allowlist: from .env OPTIONTRADER_CLOUD_ALLOWED_EMAILS, or unset if absent."
    }
    if ($OwnerEmails) {
        Write-Host "Owner email(s): $OwnerEmails"
    } else {
        Write-Host "Owner email(s): from .env OPTIONTRADER_OWNER_EMAILS / OPTIONTRADER_DEFAULT_USER_EMAIL"
    }
} else {
    Write-Host "Cloudflare Access guard: OFF"
    Write-Host "Do not expose this to friends until Cloudflare Access is configured." -ForegroundColor Yellow
}
