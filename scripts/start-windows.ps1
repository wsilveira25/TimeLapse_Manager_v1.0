$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path $PSScriptRoot -Parent
$ConfigPath = Join-Path $InstallRoot 'windows-config.json'
$Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json

$env:PYTHONUNBUFFERED = '1'
$env:TIMELAPSE_USERNAME = 'admin'
$env:TIMELAPSE_CREDENTIALS_DIR = $Config.CredentialRoot
$env:TIMELAPSE_DATA_DIR = $Config.DataRoot
$env:TIMELAPSE_TEMP_DIR = $Config.TempRoot
$env:TIMELAPSE_DATABASE_URL = $Config.DatabaseUrl
$env:TIMELAPSE_PUBLIC_URL = $Config.PublicUrl
$env:TIMELAPSE_HOST = '0.0.0.0'
$env:TIMELAPSE_PORT = '8000'
$env:TIMELAPSE_SSL_CERTFILE = $Config.Certificate
$env:TIMELAPSE_SSL_KEYFILE = $Config.PrivateKey
$env:TIMELAPSE_FFMPEG_PATH = $Config.FfmpegPath

$LogRoot = Join-Path $Config.DataRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot 'timelapse-manager.log'
$BundledApp = Join-Path $InstallRoot 'TimelapseManager.exe'
if (Test-Path -LiteralPath $BundledApp) {
	& $BundledApp *>> $LogPath
	exit $LASTEXITCODE
}
$Python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
& $Python (Join-Path $InstallRoot 'run_server.py') *>> $LogPath
exit $LASTEXITCODE