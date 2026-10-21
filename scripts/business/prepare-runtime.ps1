param([switch]$Offline)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $projectRoot
$env:HTTP_PROXY='http://127.0.0.1:7890'; $env:HTTPS_PROXY=$env:HTTP_PROXY; $env:NO_PROXY='localhost,127.0.0.1,::1'
New-Item -ItemType Directory -Force var/business | Out-Null
uv export --project backend --frozen --no-dev --no-emit-project --output-file var/business/runtime-requirements.txt | Out-Null
if($LASTEXITCODE -ne 0){throw 'Dependency export failed'}
$arguments=@('pip','install','--target','var/business/python-deps','--python-version','3.12','--python-platform','x86_64-manylinux_2_28','--only-binary',':all:','-r','var/business/runtime-requirements.txt')
if($Offline){$arguments+='--offline'}
uv @arguments
if($LASTEXITCODE -ne 0){throw 'Dependency installation failed'}
[IO.File]::WriteAllText((Join-Path $projectRoot 'var/business/.dockerignore'),"*`n!python-deps`n!python-deps/**`n")
docker build --pull=false -f infra/business/Agent.Dockerfile -t flowpay-support-agent var/business
if($LASTEXITCODE -ne 0){throw 'Agent image build failed'}
