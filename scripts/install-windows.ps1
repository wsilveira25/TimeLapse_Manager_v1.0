#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$InstallLogRoot = Join-Path $env:ProgramData 'TimelapseManager\logs'
New-Item -ItemType Directory -Force -Path $InstallLogRoot | Out-Null
$InstallLogPath = Join-Path $InstallLogRoot 'install.log'
Start-Transcript -LiteralPath $InstallLogPath -Append | Out-Null
trap {
    $ErrorDetails = "Falha na instalação:`n$($_.Exception.Message)`n`nEtapa: $($_.InvocationInfo.ScriptLineNumber)`nLog completo: $InstallLogPath"
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [Windows.Forms.MessageBox]::Show($ErrorDetails, 'Timelapse Manager: erro na instalação', 'OK', 'Error') | Out-Null
    } catch {
        Write-Host $ErrorDetails -ForegroundColor Red
        Read-Host 'Pressione Enter para fechar esta janela'
    }
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
    exit 1
}

function Show-InitialConfiguration {
    param(
        [string]$DefaultHost,
        [string]$DefaultSignature,
        [bool]$PasswordAlreadyConfigured
    )

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object Windows.Forms.Form
    $form.Text = 'Configuração inicial | Timelapse Manager'
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object Drawing.Size(650, 460)
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.Font = New-Object Drawing.Font('Segoe UI', 9)

    $tooltip = New-Object Windows.Forms.ToolTip
    $tooltip.AutoPopDelay = 15000
    $tooltip.InitialDelay = 250
    $tooltip.ReshowDelay = 100

    $title = New-Object Windows.Forms.Label
    $title.Text = 'Vamos preparar o acesso ao Timelapse Manager'
    $title.Location = New-Object Drawing.Point(24, 20)
    $title.Size = New-Object Drawing.Size(590, 28)
    $title.Font = New-Object Drawing.Font('Segoe UI Semibold', 14)
    $form.Controls.Add($title)

    $intro = New-Object Windows.Forms.Label
    $intro.Text = 'Revise os dados abaixo. Os campos com (?) explicam onde cada informação será usada.'
    $intro.Location = New-Object Drawing.Point(26, 55)
    $intro.Size = New-Object Drawing.Size(590, 34)
    $form.Controls.Add($intro)

    $addHelp = {
        param([int]$Top, [string]$Text)
        $button = New-Object Windows.Forms.Button
        $button.Text = '?'
        $button.Location = New-Object Drawing.Point(598, $Top)
        $button.Size = New-Object Drawing.Size(28, 26)
        $button.AccessibleName = 'Ajuda'
        $tooltip.SetToolTip($button, $Text)
        $helpText = $Text
        $button.Add_Click({ [Windows.Forms.MessageBox]::Show($helpText, 'Ajuda', 'OK', 'Information') | Out-Null }.GetNewClosure())
        $form.Controls.Add($button)
    }

    $hostLabel = New-Object Windows.Forms.Label
    $hostLabel.Text = 'Hostname ou endereço IP'
    $hostLabel.Location = New-Object Drawing.Point(28, 111)
    $hostLabel.Size = New-Object Drawing.Size(260, 22)
    $form.Controls.Add($hostLabel)
    $hostBox = New-Object Windows.Forms.TextBox
    $hostBox.Location = New-Object Drawing.Point(30, 136)
    $hostBox.Size = New-Object Drawing.Size(550, 26)
    $hostBox.Text = $DefaultHost
    $form.Controls.Add($hostBox)
    & $addHelp 135 'Use o nome deste computador ou um endereço IPv4 fixo. Os outros dispositivos usarão esse endereço para abrir o sistema na rede.'

    $passwordLabel = New-Object Windows.Forms.Label
    $passwordLabel.Text = if ($PasswordAlreadyConfigured) { 'Nova senha do administrador (opcional)' } else { 'Senha inicial do administrador' }
    $passwordLabel.Location = New-Object Drawing.Point(28, 179)
    $passwordLabel.Size = New-Object Drawing.Size(440, 22)
    $form.Controls.Add($passwordLabel)
    $passwordBox = New-Object Windows.Forms.TextBox
    $passwordBox.Location = New-Object Drawing.Point(30, 204)
    $passwordBox.Size = New-Object Drawing.Size(550, 26)
    $passwordBox.UseSystemPasswordChar = $true
    $form.Controls.Add($passwordBox)
    $passwordConfirmLabel = New-Object Windows.Forms.Label
    $passwordConfirmLabel.Text = 'Confirme a senha'
    $passwordConfirmLabel.Location = New-Object Drawing.Point(28, 246)
    $passwordConfirmLabel.Size = New-Object Drawing.Size(260, 22)
    $form.Controls.Add($passwordConfirmLabel)
    $passwordConfirmBox = New-Object Windows.Forms.TextBox
    $passwordConfirmBox.Location = New-Object Drawing.Point(30, 271)
    $passwordConfirmBox.Size = New-Object Drawing.Size(550, 26)
    $passwordConfirmBox.UseSystemPasswordChar = $true
    $form.Controls.Add($passwordConfirmBox)
    $passwordHelp = if ($PasswordAlreadyConfigured) {
        'A senha existente será mantida se os dois campos ficarem vazios. Para trocá-la, informe e confirme uma nova senha com pelo menos 12 caracteres.'
    } else {
        'Crie a senha da conta admin. Ela deve ter pelo menos 12 caracteres; digite a mesma senha nos dois campos.'
    }
    & $addHelp 203 $passwordHelp
    & $addHelp 270 'A confirmação evita que um erro de digitação impeça o primeiro acesso.'

    $signatureLabel = New-Object Windows.Forms.Label
    $signatureLabel.Text = 'Assinatura de desenvolvimento'
    $signatureLabel.Location = New-Object Drawing.Point(28, 314)
    $signatureLabel.Size = New-Object Drawing.Size(440, 22)
    $form.Controls.Add($signatureLabel)
    $signatureBox = New-Object Windows.Forms.TextBox
    $signatureBox.Location = New-Object Drawing.Point(30, 339)
    $signatureBox.Size = New-Object Drawing.Size(550, 26)
    $signatureBox.Text = $DefaultSignature
    $form.Controls.Add($signatureBox)
    & $addHelp 338 'Texto mostrado no rodapé do sistema. É opcional; deixe o padrão ou escreva uma identificação da sua equipe.'

    $status = New-Object Windows.Forms.Label
    $status.ForeColor = [Drawing.Color]::DarkRed
    $status.Location = New-Object Drawing.Point(30, 378)
    $status.Size = New-Object Drawing.Size(580, 38)
    $form.Controls.Add($status)

    $continue = New-Object Windows.Forms.Button
    $continue.Text = 'Validar e continuar'
    $continue.Location = New-Object Drawing.Point(440, 420)
    $continue.Size = New-Object Drawing.Size(170, 30)
    $continue.DialogResult = [Windows.Forms.DialogResult]::None
    $form.Controls.Add($continue)
    $form.AcceptButton = $continue
    $form.CancelButton = $null

    $continue.Add_Click({
        $hostValue = $hostBox.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($hostValue) -or $hostValue -notmatch '^[A-Za-z0-9.-]+$') {
            $status.Text = 'Informe um hostname simples ou IPv4, sem espaços. Exemplo: 192.168.0.24.'
            $hostBox.Focus()
            return
        }
        if ([string]::IsNullOrWhiteSpace($signatureBox.Text.Trim())) {
            $status.Text = 'A assinatura pode ser personalizada ou deixada com o valor padrão.'
            $signatureBox.Text = $DefaultSignature
            $signatureBox.Focus()
            return
        }
        if ($passwordBox.Text.Length -eq 0 -and $passwordConfirmBox.Text.Length -eq 0) {
            if (-not $PasswordAlreadyConfigured) {
                $status.Text = 'Defina uma senha com pelo menos 12 caracteres e confirme-a.'
                $passwordBox.Focus()
                return
            }
        } elseif ($passwordBox.Text.Length -lt 12) {
            $status.Text = 'A senha precisa ter pelo menos 12 caracteres.'
            $passwordBox.Focus()
            return
        } elseif ($passwordBox.Text -cne $passwordConfirmBox.Text) {
            $status.Text = 'As senhas não coincidem. Confira os dois campos.'
            $passwordConfirmBox.Clear()
            $passwordConfirmBox.Focus()
            return
        }
        $form.Tag = @{
            Host = $hostValue
            Password = $passwordBox.Text
            Signature = $signatureBox.Text.Trim()
        }
        $form.DialogResult = [Windows.Forms.DialogResult]::OK
        $form.Close()
    }.GetNewClosure())

    $dialogResult = $form.ShowDialog()
    if ($dialogResult -ne [Windows.Forms.DialogResult]::OK) {
        throw 'Configuração cancelada. Nenhuma tarefa do serviço foi iniciada.'
    }
    $result = $form.Tag
    $form.Dispose()
    $tooltip.Dispose()
    return $result
}

$SourceRoot = Split-Path $PSScriptRoot -Parent
$InstallRoot = Join-Path $env:ProgramFiles 'TimelapseManager'
$DataRoot = Join-Path $env:ProgramData 'TimelapseManager'
$TempRoot = Join-Path $DataRoot 'temp'
$CredentialRoot = Join-Path $DataRoot 'credentials'
$CertificateRoot = Join-Path $DataRoot 'tls'
$ServiceSid = '*S-1-5-19'
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
& icacls.exe $CredentialRoot /inheritance:r /grant "${ServiceSid}:(OI)(CI)R" '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Não foi possível proteger a pasta de credenciais.'
}
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
$PasswordAlreadyConfigured = Test-Path -LiteralPath $PasswordPath
$DefaultHost = $env:COMPUTERNAME
$DefaultSignature = 'Desenvolvido com Timelapse Manager'
$SignaturePath = Join-Path $DataRoot 'developer_signature.txt'
if (Test-Path -LiteralPath $SignaturePath) {
    $DefaultSignature = (Get-Content -LiteralPath $SignaturePath -Raw).Trim()
}
$InitialConfiguration = Show-InitialConfiguration `
    -DefaultHost $DefaultHost `
    -DefaultSignature $DefaultSignature `
    -PasswordAlreadyConfigured $PasswordAlreadyConfigured
$ServerHost = $InitialConfiguration.Host
if ($InitialConfiguration.Password.Length -gt 0) {
    [IO.File]::WriteAllText($PasswordPath, $InitialConfiguration.Password, [Text.UTF8Encoding]::new($false))
}
[IO.File]::WriteAllText($SignaturePath, $InitialConfiguration.Signature, [Text.UTF8Encoding]::new($false))
$InitialConfiguration.Password = $null
Remove-Variable InitialConfiguration

$SessionSecretPath = Join-Path $CredentialRoot 'TIMELAPSE_SESSION_SECRET'
if (-not (Test-Path -LiteralPath $SessionSecretPath)) {
    $RandomBytes = New-Object byte[] 48
    $RandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $RandomGenerator.GetBytes($RandomBytes)
        $SessionSecret = [Convert]::ToBase64String($RandomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        [IO.File]::WriteAllText($SessionSecretPath, $SessionSecret, [Text.UTF8Encoding]::new($false))
    } finally {
        $RandomGenerator.Dispose()
        [Array]::Clear($RandomBytes, 0, $RandomBytes.Length)
        Remove-Variable SessionSecret -ErrorAction SilentlyContinue
    }
}

$Certificate = Join-Path $CertificateRoot 'timelapse.crt'
$PrivateKey = Join-Path $CertificateRoot 'timelapse.key'
if (-not (Test-Path -LiteralPath $Certificate) -or -not (Test-Path -LiteralPath $PrivateKey)) {
    if ($BundledRuntime) {
        & $BundledAppPath --create-certificate --hostname $ServerHost --cert $Certificate --key $PrivateKey
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Certificate) -or -not (Test-Path -LiteralPath $PrivateKey)) {
            throw 'Não foi possível criar o certificado HTTPS. Confira o endereço informado e tente novamente.'
        }
    } else {
        & $VenvPython (Join-Path $InstallRoot 'scripts\create_self_signed_cert.py') --hostname $ServerHost --cert $Certificate --key $PrivateKey
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Certificate) -or -not (Test-Path -LiteralPath $PrivateKey)) {
            throw 'Não foi possível criar o certificado HTTPS. Confira o endereço informado e tente novamente.'
        }
    }
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

& icacls.exe $InstallRoot /grant "${ServiceSid}:(OI)(CI)RX" /T | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível ajustar as permissões dos arquivos instalados.' }
& icacls.exe $DataRoot /grant "${ServiceSid}:(OI)(CI)M" /T | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível ajustar as permissões da pasta de dados.' }
& icacls.exe $CertificateRoot /inheritance:r /grant "${ServiceSid}:(OI)(CI)R" '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível proteger o certificado HTTPS.' }

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
$TaskStarted = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
    if ($TaskState -eq 'Running') {
        $TaskStarted = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $TaskStarted) {
    $TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    throw "O serviço não iniciou (resultado da tarefa: $($TaskInfo.LastTaskResult)). Consulte $(Join-Path $DataRoot 'logs\timelapse-manager.log') e tente corrigir o erro."
}
Write-Host "Instalado. Abra https://$ServerHost`:8000 e confie no certificado local."
Write-Host "Logs: $(Join-Path $DataRoot 'logs\timelapse-manager.log')"
Stop-Transcript | Out-Null