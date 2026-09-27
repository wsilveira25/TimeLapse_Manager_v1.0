gh repo clone <OWNER>/TimeLapse_Manager_v1.0 "$env:TEMP\TimelapseManager-source"; & "$env:TEMP\TimelapseManager-source\scripts\install-from-github.ps1" -Repository '<OWNER>/TimeLapse_Manager_v1.0'
# Timelapse Manager Deployment

The project can publish a self-contained Windows x64 installer and a Linux source package. The Windows installer bundles the Python application dependencies and FFmpeg; the target does not need Python or FFmpeg installed. Linux uses a systemd installer and installs Python dependencies on the target server.

## Private GitHub Access

The repository is private, so the machine downloading a release must have GitHub CLI (`gh`) and Git installed, with access to the repository. Authenticate once with:

```sh
gh auth login
```

Replace `<OWNER>` below with the GitHub account that owns the repository.

## Windows Install

Run PowerShell as Administrator. The single command below clones the private repository and launches its release bootstrap. The bootstrap downloads the installer and checksum, verifies SHA-256, then starts setup:

```powershell
gh repo clone <OWNER>/TimeLapse_Manager_v1.0 "$env:TEMP\TimelapseManager-source"; & "$env:TEMP\TimelapseManager-source\scripts\install-from-github.ps1" -Repository '<OWNER>/TimeLapse_Manager_v1.0'
```

The installer asks for the initial admin password and host name/IP, creates a self-signed TLS certificate, restricts credentials and the TLS key to Local Service and Administrators, opens TCP port 8000 in Windows Firewall, and registers the application in Task Scheduler as Local Service. Data, credentials, captures, and logs are stored under `%ProgramData%\TimelapseManager`.

The release installer is not code-signed. Windows may display SmartScreen or unknown-publisher warnings. Production signing requires a code-signing certificate stored as a protected GitHub Actions secret.

Check the scheduled task and logs with:

```powershell
Get-ScheduledTask -TaskName 'Timelapse Manager'
Get-Content "$env:ProgramData\TimelapseManager\logs\timelapse-manager.log" -Tail 100
```

## Linux Install

Run this from a systemd-based distribution such as Ubuntu or Debian. The one-line command clones the private repository and runs the installer, which downloads and checksum-verifies the source archive first:

```sh
gh repo clone <OWNER>/TimeLapse_Manager_v1.0 /tmp/timelapse-bootstrap && sudo /tmp/timelapse-bootstrap/scripts/install-from-github.sh <OWNER>/TimeLapse_Manager_v1.0
```

The installer requires Python 3.11+, FFmpeg, `systemd-creds`, and `unzip`. It prompts for the admin password and host name/IP, creates a local TLS certificate if needed, and enables `timelapse-manager.service`. Credentials are encrypted by systemd. Existing credentials and the database are preserved on reinstall.

Check the service with:

```sh
systemctl status timelapse-manager.service
journalctl -u timelapse-manager.service
```

## Build And Release

The Windows runner workflow tests the app, creates the Linux source ZIP, bundles the Windows executable with PyInstaller and FFmpeg, and creates an Inno Setup installer. Push a version tag to publish both packages and `SHA256SUMS.txt` to a private GitHub Release:

```sh
git tag v1.0.0
git push origin v1.0.0
```

For subsequent releases, increment `VERSION` and push the matching tag. GitHub Actions must be enabled for the private repository.

To build only the source package locally, use Python 3.11 or newer:

```sh
python scripts/package.py
```

## First Access And State

Open `https://<server-host-or-ip>:8000`. The default certificate is self-signed, so browsers warn until it is trusted. Replace it with an organization/public CA certificate for managed or public environments. Configure SMTP and the recovery email after first login.

The installer prompts for the developer signature shown in the UI. It can later be changed in `developer_signature.txt` in the data directory. Linux state is under `/var/lib/timelapse` and captures under `/var/temp`; Windows state is under `%ProgramData%\TimelapseManager`.

Back up application data and credentials before replacing an installation. Keep the session credential when updating because it also encrypts the saved SMTP password. Release packages exclude databases, camera credentials, SMTP credentials, TLS keys, and generated media.