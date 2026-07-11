$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
if (-not $projectRoot) {
    throw 'Unable to determine the Atlas project root.'
}

Push-Location -LiteralPath $projectRoot
try {
    & docker compose exec backend python manage.py test apps.scheduling -v 2
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduling tests failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
