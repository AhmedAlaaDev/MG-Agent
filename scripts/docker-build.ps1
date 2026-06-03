# Build and run Intelligent B/L Extractor with all environment variables from .env
# Usage (from repo root):
#   .\scripts\docker-build.ps1
#   .\scripts\docker-build.ps1 -RunOnly
#   .\scripts\docker-build.ps1 -BuildOnly

param(
    [switch]$BuildOnly,
    [switch]$RunOnly,
    [string]$EnvFile = ".env",
    [string]$ImageTag = "intelligent-bl-extractor:latest",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path $EnvFile)) {
    Write-Error "Missing $EnvFile. Copy .env.example to .env and set your credentials."
}

if ($Port -eq 0) {
    $portLine = Get-Content $EnvFile | Where-Object { $_ -match '^\s*API_PORT\s*=' } | Select-Object -First 1
    if ($portLine -match '=\s*(\d+)') { $Port = [int]$Matches[1] } else { $Port = 8000 }
}

Write-Host "Using env file: $EnvFile | Host port: $Port -> container 8000"

if (-not $RunOnly) {
    Write-Host "Building image $ImageTag ..."
    docker build -t $ImageTag .
}

if (-not $BuildOnly) {
    Write-Host "Starting container (env-file + published port $Port) ..."
    docker compose --env-file $EnvFile up -d --build
    Write-Host ""
    Write-Host "API:     http://localhost:$Port/docs"
    Write-Host "Health:  http://localhost:$Port/health"
    Write-Host "Logs:    docker compose logs -f intelligent-bl-extractor"
}
