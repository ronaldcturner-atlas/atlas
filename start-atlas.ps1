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
    Write-Host 'Keep this terminal open while the frontend dev server is running.' -ForegroundColor Yellow
    Write-Host 'Press Ctrl+C to stop the frontend dev server.'
    Write-Host ''

    & npm run dev --prefix frontend
    if ($LASTEXITCODE -ne 0) {
        throw "The frontend dev server exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
