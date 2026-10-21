param(
    [ValidateSet('mock', 'real')][string]$Mode = 'mock',
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. Check the error above; fix it and rerun this launcher.' }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop (Linux containers) first.' }
$containerOs = & docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker is not available. Start Docker Desktop first.' }
if ($containerOs -ne 'linux') { throw 'Switch Docker Desktop to Linux containers first.' }
Invoke-Docker compose version
if ($Stop -and -not (Test-Path .env.deploy)) {
    throw 'Missing .env.deploy. Restore the original deployment configuration before stopping this stack.'
}
if (-not (Test-Path .env.deploy)) {
    Invoke-Docker run --rm --mount "type=bind,source=$PSScriptRoot,target=/workspace" --workdir /workspace python:3.12-slim python scripts/deploy/config.py
}
$compose = @('compose', '--env-file', '.env.deploy', '-f', 'compose.deploy.yaml')
if ($Stop) {
    Invoke-Docker @compose down --remove-orphans
    return
}
if ($Mode -eq 'real') { $compose += @('-f', 'compose.deploy.real.yaml') }
Invoke-Docker @compose config --quiet
if ($Mode -eq 'real') {
    Invoke-Docker @compose run --rm --no-deps --build dataset-init python scripts/deploy/initialize.py validate
}
Invoke-Docker @compose up -d --build --wait --wait-timeout 1800 --remove-orphans
Invoke-Docker @compose ps
$published = & docker @compose port api 8080
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the workbench port.' }
$publishedPort = [regex]::Match($published, ':(\d+)$').Groups[1].Value
Write-Host "FlowPay is ready: http://localhost:$publishedPort"
Write-Host 'Accounts: merchant_a, merchant_b, support_a, support_b.'
Write-Host 'Read SUPPORT_DEMO_PASSWORD in .env.deploy to log in. Existing files and data volumes were preserved.'
