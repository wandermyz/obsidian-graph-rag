# Runs the GraphRAG service in the foreground, logging to <data>\service.log.
# Bound to 127.0.0.1 with no auth of its own. Reach it from elsewhere only through an authenticating
# tunnel (e.g. a dev tunnel without --allow-anonymous); never change the host.
param(
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data"),
    [int]$Port = 2335
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$env:GRAPHRAG_DATA_DIR = $DataDir
$env:GRAPHRAG_HOST = "127.0.0.1"
$env:GRAPHRAG_PORT = "$Port"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:PYTHONIOENCODING = "utf-8"
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
# The model is cached after the first run; do not depend on the network at every start.
$cached = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--Qwen--Qwen3-Embedding-0.6B"
if (-not $env:GRAPHRAG_MODEL -and (Test-Path $cached)) { $env:HF_HUB_OFFLINE = "1" }

$log = Join-Path $DataDir "service.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 10MB) { Move-Item $log "$log.1" -Force }
Set-Location $Repo
& uv run --no-sync graphrag serve *>> $log
