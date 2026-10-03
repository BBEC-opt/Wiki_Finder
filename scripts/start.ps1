param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8001,
    [switch]$NoReload,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualEnv = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $VirtualEnv "Scripts\python.exe"
$RuntimeRoot = Join-Path $ProjectRoot "data"
$CacheRoot = Join-Path $RuntimeRoot "cache"
$PortFile = Join-Path $RuntimeRoot "run\server.port"

New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PortFile) | Out-Null
$env:PYTHONPYCACHEPREFIX = Join-Path $CacheRoot "python"

Set-Location $ProjectRoot

if (-not (Test-Path $Python)) {
    Write-Host "Creating virtual environment in .venv..."
    python -m venv $VirtualEnv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the virtual environment."
    }
}

if (-not $SkipInstall) {
    Write-Host "Installing project dependencies..."
    & $Python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed; the server was not started."
    }
} else {
    & $Python -c "import uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Uvicorn is not installed in .venv. Run again without -SkipInstall."
    }
}

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Write-Warning "No .env file found; built-in offline defaults will be used."
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    $RequestedPort = $Port
    $Port = $null
    foreach ($CandidatePort in 8001..8099) {
        if (-not (Get-NetTCPConnection -LocalPort $CandidatePort -State Listen -ErrorAction SilentlyContinue)) {
            $Port = $CandidatePort
            break
        }
    }
    if (-not $Port) {
        throw "Port $RequestedPort is occupied and no free port was found from 8001 through 8099."
    }
    Write-Warning "Port $RequestedPort is occupied; using port $Port instead."
}

$UvicornArgs = @(
    "-m", "uvicorn", "app.main:app",
    "--host", $HostAddress,
    "--port", $Port.ToString()
)

if (-not $NoReload) {
    $UvicornArgs += "--reload"
}

Write-Host "Starting My Wiki at http://${HostAddress}:$Port"
Set-Content -Path $PortFile -Value $Port -Encoding ASCII
& $Python @UvicornArgs
$ServerExitCode = $LASTEXITCODE
Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
if ($ServerExitCode -ne 0) {
    throw "The server exited with code $ServerExitCode."
}
