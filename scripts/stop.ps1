param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PortFile = Join-Path $ProjectRoot "data\run\server.port"

if ($Port -eq 0) {
    if (Test-Path $PortFile) {
        $Port = [int](Get-Content $PortFile -Raw).Trim()
    } else {
        $Port = 8001
    }
}

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Host "No service is listening on port $Port."
    exit 0
}

$ownerIds = $listeners.OwningProcess | Sort-Object -Unique
$owners = foreach ($processId in $ownerIds) {
    Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
}
if (-not ($owners | Where-Object { $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "app\.main:app" })) {
    Write-Warning "Port $Port is not owned by this project's Uvicorn service. Nothing was stopped."
    exit 1
}

$portPattern = "--port\s+" + [regex]::Escape($Port.ToString()) + "(?:\s|$)"
$processes = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "uvicorn" -and
    $_.CommandLine -match "app\.main:app" -and
    $_.CommandLine -match $portPattern
}

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped My Wiki process PID $($process.ProcessId) on port $Port."
}
Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
