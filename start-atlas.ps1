$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
if (-not $projectRoot) {
    throw 'Unable to determine the Atlas project root.'
}

Push-Location -LiteralPath $projectRoot
try {
    Write-Host 'Starting Atlas Docker services...'
    & docker compose up -d
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up -d failed with exit code $LASTEXITCODE."
    }

    Write-Host ''
    Write-Host 'Atlas frontend: http://localhost:5173' -ForegroundColor Green
    Write-Host 'The frontend is supervised by Docker and restarts automatically.' -ForegroundColor Yellow
    Write-Host ''
}
finally {
    Pop-Location
}
