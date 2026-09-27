#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [string]$Version = 'latest'
)

$ErrorActionPreference = 'Stop'
$Gh = Get-Command gh.exe -ErrorAction SilentlyContinue
if (-not $Gh) {
    throw 'Instale GitHub CLI (gh) e autentique com gh auth login antes de baixar uma Release privada.'
}
& $Gh.Source auth status --hostname github.com
if ($LASTEXITCODE -ne 0) {
    throw 'A sessão GitHub não está autenticada. Execute gh auth login e repita.'
}

$DownloadRoot = Join-Path $env:TEMP ('TimelapseManager-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $DownloadRoot | Out-Null
try {
    $DownloadArgs = @('release', 'download')
    if ($Version -ne 'latest') { $DownloadArgs += $Version }
    $DownloadArgs += @('--repo', $Repository, '--pattern', 'TimelapseManager-Setup-*.exe', '--pattern', 'SHA256SUMS.txt', '--dir', $DownloadRoot)
    & $Gh.Source @DownloadArgs
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível baixar o instalador da Release.' }

    $Installer = Get-ChildItem -LiteralPath $DownloadRoot -Filter 'TimelapseManager-Setup-*.exe' | Select-Object -First 1
    if (-not $Installer) { throw 'A Release não contém o instalador Windows esperado.' }
    $ExpectedHashFile = Get-ChildItem -LiteralPath $DownloadRoot -Filter 'SHA256SUMS.txt' | Select-Object -First 1
    if ($ExpectedHashFile) {
        $ExpectedLine = Get-Content -LiteralPath $ExpectedHashFile.FullName | Where-Object { $_ -match [regex]::Escape($Installer.Name) } | Select-Object -First 1
        if (-not $ExpectedLine) { throw 'O SHA256 do instalador não foi encontrado no manifesto da Release.' }
        $ExpectedHash = ($ExpectedLine -split '\s+')[0]
        $ActualHash = (Get-FileHash -LiteralPath $Installer.FullName -Algorithm SHA256).Hash
        if ($ActualHash -ne $ExpectedHash) { throw 'O SHA256 do instalador não corresponde ao manifesto.' }
    }
    Start-Process -FilePath $Installer.FullName -Verb RunAs -Wait
} finally {
    Remove-Item -LiteralPath $DownloadRoot -Recurse -Force -ErrorAction SilentlyContinue
}