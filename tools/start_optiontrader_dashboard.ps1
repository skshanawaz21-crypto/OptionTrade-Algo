$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DashboardUrl = "http://127.0.0.1:8877/"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DashboardScript = Join-Path $ProjectRoot "run_dashboard.py"

function Test-DashboardReady {
    try {
        $response = Invoke-WebRequest -Uri $DashboardUrl -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

Set-Location -LiteralPath $ProjectRoot

if (Test-DashboardReady) {
    Write-Host "OptionTrader dashboard is already running at $DashboardUrl"
    Start-Process $DashboardUrl
    return
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "Python virtual environment was not found: $PythonExe"
    Write-Host "Create it first with: py -m venv .venv"
    return
}

if (-not (Test-Path -LiteralPath $DashboardScript)) {
    Write-Host "Dashboard script was not found: $DashboardScript"
    return
}

Write-Host "Starting OptionTrader dashboard from $ProjectRoot"
Write-Host "Dashboard URL: $DashboardUrl"

Start-Job -Name "OpenOptionTraderDashboard" -ScriptBlock {
    param($Url)
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Start-Process $Url
                return
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
} -ArgumentList $DashboardUrl | Out-Null

& $PythonExe $DashboardScript
