# Copie le controleur et la table de reference depuis pearl_cloud_miner, puis construit l'image.
#   .\build.ps1                                        image locale pearl-salad:dev
#   .\build.ps1 -Tag ghcr.io/<toi>/pearl-salad:v1.0.0 -Push
#   .\build.ps1 -SyncOnly                              copie seulement (avant un commit pour la CI GitHub)
param(
    [string]$Tag = "pearl-salad:dev",
    [switch]$Push,
    [switch]$SyncOnly,
    [string]$Source = (Join-Path $PSScriptRoot "..\pearl_cloud_miner")
)
$ErrorActionPreference = "Stop"
$vendor = Join-Path $PSScriptRoot "vendor"
New-Item -ItemType Directory -Force $vendor | Out-Null
Copy-Item (Join-Path $Source "pearl_controller.py") (Join-Path $vendor "pearl_controller.py") -Force
Copy-Item (Join-Path $Source "dashboard\gpus.json") (Join-Path $vendor "gpus.json") -Force
$ver = (Select-String -Path (Join-Path $vendor "pearl_controller.py") -Pattern 'CTL_VERSION = "([^"]*)"').Matches[0].Groups[1].Value
Write-Host "vendor/ : controleur $ver"
if ($SyncOnly) { return }
docker build --platform linux/amd64 --build-arg "IMAGE_VERSION=$($Tag.Split(':')[-1])" -t $Tag $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw "docker build en echec" }
if ($Push) { docker push $Tag; if ($LASTEXITCODE -ne 0) { throw "docker push en echec" } }
Write-Host "image : $Tag"
