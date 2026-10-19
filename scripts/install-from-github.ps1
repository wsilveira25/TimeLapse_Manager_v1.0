#Requires -RunAsAdministrator
# Instalação pública no Windows:
#   irm https://raw.githubusercontent.com/wsilveira25/TimeLapse_Manager_v1.0/main/scripts/install-from-github.ps1 | iex
param(
    [string]$Repository = 'wsilveira25/TimeLapse_Manager_v1.0',
    [string]$Version = 'latest'
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Headers = @{ 'User-Agent' = 'TimelapseManager-Installer' }
$ApiUrl = if ($Version -eq 'latest') { "https://api.github.com/repos/$Repository/releases/latest" } else { "https://api.github.com/repos/$Repository/releases/tags/$Version" }
$Release = Invoke-RestMethod -Uri $ApiUrl -Headers $Headers
$InstallerAsset = $Release.assets | Where-Object { $_.name -like 'TimelapseManager-Setup-*.exe' } | Select-Object -First 1
$SumsAsset = $Release.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
if (-not $InstallerAsset) { throw 'A Release não contém o instalador Windows esperado.' }
if (-not $SumsAsset) { throw 'A Release não contém SHA256SUMS.txt.' }

$DownloadRoot = Join-Path $env:TEMP ('TimelapseManager-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $DownloadRoot | Out-Null
try {
    $InstallerPath = Join-Path $DownloadRoot $InstallerAsset.name
    $SumsPath = Join-Path $DownloadRoot 'SHA256SUMS.txt'
    Invoke-WebRequest -Uri $InstallerAsset.browser_download_url -OutFile $InstallerPath -Headers $Headers
    Invoke-WebRequest -Uri $SumsAsset.browser_download_url -OutFile $SumsPath -Headers $Headers

    $ExpectedLine = Get-Content -LiteralPath $SumsPath | Where-Object { $_ -match [regex]::Escape($InstallerAsset.name) } | Select-Object -First 1
    if (-not $ExpectedLine) { throw 'O SHA256 do instalador não foi encontrado no manifesto da Release.' }
    $ExpectedHash = ($ExpectedLine -split '\s+')[0]
    $ActualHash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash
    if ($ActualHash -ne $ExpectedHash) { throw 'O SHA256 do instalador não corresponde ao manifesto.' }
    Start-Process -FilePath $InstallerPath -Wait
} finally {
    Remove-Item -LiteralPath $DownloadRoot -Recurse -Force -ErrorAction SilentlyContinue
}
