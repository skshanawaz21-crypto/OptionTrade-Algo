param(
    [Parameter(Mandatory = $true)]
    [string]$TunnelId,

    [Parameter(Mandatory = $true)]
    [string]$Hostname,

    [string]$CredentialsFile = "",
    [string]$OutputPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

if (-not $OutputPath) {
    $OutputPath = Join-Path $env:USERPROFILE ".cloudflared\optiontrader.yml"
}

if (-not $CredentialsFile) {
    $CredentialsFile = Join-Path $env:USERPROFILE ".cloudflared\$TunnelId.json"
}

$outputDir = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) {
    Write-Host "ERROR: $OutputPath already exists." -ForegroundColor Red
    Write-Host "Use -Force to overwrite it." -ForegroundColor Yellow
    exit 1
}

$config = @"
tunnel: $TunnelId
credentials-file: $CredentialsFile

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:8877
  - service: http_status:404
"@

Set-Content -LiteralPath $OutputPath -Value $config -Encoding utf8

Write-Host "OptionTrader Cloudflare config written:" -ForegroundColor Green
Write-Host $OutputPath
Write-Host ""
Write-Host "Next steps:"
Write-Host "1) Make sure the Cloudflare DNS route points $Hostname to tunnel $TunnelId."
Write-Host "2) Configure Cloudflare Access for $Hostname before sharing it."
Write-Host "3) Start with:"
Write-Host "   .\start_self_hosted_beta.ps1 -CloudAccessGuard -Hostname $Hostname"
