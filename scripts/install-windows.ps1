#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$SourceRoot = Split-Path $PSScriptRoot -Parent
$InstallRoot = Join-Path $env:ProgramFiles 'TimelapseManager'
$DataRoot = Join-Path $env:ProgramData 'TimelapseManager'
$TempRoot = Join-Path $DataRoot 'temp'
$CredentialRoot = Join-Path $DataRoot 'credentials'
$CertificateRoot = Join-Path $DataRoot 'tls'
$SourceBundleApp = Join-Path $SourceRoot 'TimelapseManager.exe'
$InstallBundleApp = Join-Path $InstallRoot 'TimelapseManager.exe'
$BundledRuntime = (Test-Path -LiteralPath $SourceBundleApp) -or (Test-Path -LiteralPath $InstallBundleApp)
$PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue

$FfmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $BundledRuntime) {
    if (-not $PythonCommand) {
        throw 'Instale Python 3.11+ e habilite o comando python.exe no PATH, ou use o instalador completo de Release.'
    }
    if (-not $FfmpegCommand) {
        throw 'Instale FFmpeg e habilite ffmpeg.exe no PATH, ou use o instalador completo de Release.'
    }
    $PythonVersion = & python.exe -c 'import sys; print("%d.%d" % sys.version_info[:2])'
    if ([version]$PythonVersion -lt [version]'3.11') {
        throw 'Python 3.11 ou superior é necessário.'
    }
}

New-Item -ItemType Directory -Force -Path $InstallRoot, $DataRoot, $TempRoot, $CredentialRoot, $CertificateRoot | Out-Null
if ([IO.Path]::GetFullPath($SourceRoot).TrimEnd('\') -ne [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')) {
    & robocopy.exe $SourceRoot $InstallRoot /E /XD venv .venv __pycache__ tests dist build /XF '*.db' '*.pyc' '*.key' '*.crt'
    if ($LASTEXITCODE -gt 7) {
        throw "Falha ao copiar os arquivos do aplicativo (robocopy $LASTEXITCODE)."
    }
}

$BundledAppPath = Join-Path $InstallRoot 'TimelapseManager.exe'
$BundledFfmpegPath = Join-Path $InstallRoot 'ffmpeg.exe'
if ($BundledRuntime) {
    $FfmpegPath = $BundledFfmpegPath
} else {
    & python.exe -m venv (Join-Path $InstallRoot 'venv')
    $VenvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r (Join-Path $InstallRoot 'requirements.txt')
    $FfmpegPath = $FfmpegCommand.Source
}

$PasswordPath = Join-Path $CredentialRoot 'TIMELAPSE_PASSWORD'
if (-not (Test-Path -LiteralPath $PasswordPath)) {
    $AdminPassword = Read-Host 'Senha inicial do admin (mínimo 12 caracteres)' -AsSecureString
    $PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminPassword)
    try {
        $PasswordText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPointer)
        if ($PasswordText.Length -lt 12) {
            throw 'A senha deve ter pelo menos 12 caracteres.'
        }
        [IO.File]::WriteAllText($PasswordPath, $PasswordText, [Text.UTF8Encoding]::new($false))
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
        Remove-Variable PasswordText -ErrorAction SilentlyContinue
    }
}
$SessionSecretPath = Join-Path $CredentialRoot 'TIMELAPSE_SESSION_SECRET'
if (-not (Test-Path -LiteralPath $SessionSecretPath)) {
    $SessionSecret = & $VenvPython -c 'import secrets; print(secrets.token_urlsafe(48), end="")'
    [IO.File]::WriteAllText($SessionSecretPath, $SessionSecret, [Text.UTF8Encoding]::new($false))
    Remove-Variable SessionSecret
}

$DefaultHost = $env:COMPUTERNAME
$ServerHost = Read-Host "Hostname/IP usado pelos clientes [$DefaultHost]"
if ([string]::IsNullOrWhiteSpace($ServerHost)) { $ServerHost = $DefaultHost }
if ($ServerHost -notmatch '^[A-Za-z0-9.-]+$') {
    throw 'Use um hostname DNS ou endereço IPv4 simples.'
}

$Certificate = Join-Path $CertificateRoot 'timelapse.crt'
$PrivateKey = Join-Path $CertificateRoot 'timelapse.key'
if (-not (Test-Path -LiteralPath $Certificate) -or -not (Test-Path -LiteralPath $PrivateKey)) {
    if ($BundledRuntime) {
        & $BundledAppPath --create-certificate --hostname $ServerHost --cert $Certificate --key $PrivateKey
    } else {
        & $VenvPython (Join-Path $InstallRoot 'scripts\create_self_signed_cert.py') --hostname $ServerHost --cert $Certificate --key $PrivateKey
    }
}

$DefaultSignature = 'Desenvolvido com Timelapse Manager'
$SignaturePath = Join-Path $DataRoot 'developer_signature.txt'
if (-not (Test-Path -LiteralPath $SignaturePath)) {
    $Signature = Read-Host "Assinatura de desenvolvimento [$DefaultSignature]"
    if ([string]::IsNullOrWhiteSpace($Signature)) { $Signature = $DefaultSignature }
    [IO.File]::WriteAllText($SignaturePath, $Signature.Trim(), [Text.UTF8Encoding]::new($false))
}

$Config = @{
    DataRoot = $DataRoot
    TempRoot = $TempRoot
    CredentialRoot = $CredentialRoot
    Certificate = $Certificate
    PrivateKey = $PrivateKey
    FfmpegPath = $FfmpegPath
    DatabaseUrl = "sqlite:///$($DataRoot.Replace('\', '/'))/timelapse.db"
    PublicUrl = "https://$ServerHost`:8000"
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $InstallRoot 'windows-config.json'), $Config, [Text.UTF8Encoding]::new($false))

$ServiceSid = '*S-1-5-19'
& icacls.exe $InstallRoot /grant "${ServiceSid}:(OI)(CI)RX" /T | Out-Null
& icacls.exe $DataRoot /grant "${ServiceSid}:(OI)(CI)M" /T | Out-Null
& icacls.exe $CredentialRoot /inheritance:r /grant "${ServiceSid}:(OI)(CI)R" '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null
& icacls.exe $CertificateRoot /inheritance:r /grant "${ServiceSid}:(OI)(CI)R" '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null

$TaskName = 'Timelapse Manager'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot 'scripts\start-windows.ps1')`"" -WorkingDirectory $InstallRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
if (-not (Get-NetFirewallRule -DisplayName 'Timelapse Manager HTTPS' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'Timelapse Manager HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 | Out-Null
}
Start-ScheduledTask -TaskName $TaskName
Write-Host "Instalado. Abra https://$ServerHost`:8000 e confie no certificado local."
Write-Host "Logs: $(Join-Path $DataRoot 'logs\timelapse-manager.log')"