# Timelapse Manager Deployment

The project can publish a self-contained Windows x64 installer and a Linux source package. The Windows installer bundles the Python application dependencies and FFmpeg; the target does not need Python or FFmpeg installed. Linux uses a systemd installer and installs Python dependencies on the target server.

## Windows Install

Run PowerShell as Administrator. The command downloads the latest public Release installer, verifies its SHA-256 and starts setup:

```powershell
irm https://raw.githubusercontent.com/wsilveira25/TimeLapse_Manager_v1.0/main/scripts/install-from-github.ps1 | iex
```

The installer asks for the initial admin password and host name/IP, creates a self-signed TLS certificate, restricts credentials and the TLS key to Local Service and Administrators, opens TCP port 8000 in Windows Firewall, and registers the application in Task Scheduler as Local Service. Data, credentials, captures, and logs are stored under `%ProgramData%\TimelapseManager`.

The release installer is not code-signed. Windows may display SmartScreen or unknown-publisher warnings. Production signing requires a code-signing certificate stored as a protected GitHub Actions secret.

Check the scheduled task and logs with:

```powershell
Get-ScheduledTask -TaskName 'Timelapse Manager'
Get-Content "$env:ProgramData\TimelapseManager\logs\timelapse-manager.log" -Tail 100
```

## Linux Install

Run this from a systemd-based Debian/Ubuntu distribution. The command downloads the latest public Release, verifies its SHA-256, installs missing dependencies (Python venv, FFmpeg, unzip, Caddy) and runs the installer, which asks for the admin password and host name:

```sh
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/wsilveira25/TimeLapse_Manager_v1.0/main/scripts/install-from-github.sh)"
```

The installer requires Python 3.11+, FFmpeg, `systemd-creds`, and `unzip`. It prompts for the admin password and host name/IP, creates a local TLS certificate if needed, and enables `timelapse-manager.service`. Credentials are encrypted by systemd. Existing credentials and the database are preserved on reinstall.

Check the service with:

```sh
systemctl status timelapse-manager.service
journalctl -u timelapse-manager.service
```

## Updates

**Settings → Atualizações** shows the installed and latest public Release. On Linux, **Atualizar agora** (or the optional **Atualizar automaticamente** switch, checked daily at 03:30) writes a request file that a root `timelapse-update.path` unit picks up. `scripts/update-linux.sh` then downloads the Release ZIP, verifies its SHA-256, backs up the app and database (last 3 kept in `/var/lib/timelapse/backups`), applies the files, updates dependencies and restarts the service. If the service does not come back healthy, the previous version is restored. Progress is stored in `/var/lib/timelapse/update-status.json` and logs are in `journalctl -u timelapse-update.service`.

The installer enables the updater automatically. For an installation made before this feature, enable it once (use the real service name, default `timelapse-manager`):

```sh
sudo bash scripts/update-linux.sh --setup timelapse-manager
```

On Windows, the screen only reports new versions and links to the new installer, which upgrades in place.

## Google Drive OAuth In Production

### Domain And Automatic HTTPS

In **Configurações → Domínio**, save a public DNS hostname (for example, `camera.example.com`). Register the domain with a DNS provider if needed and point its `A` record to the network's public IPv4 address. Keep an `AAAA` record only if this server is reachable over IPv6.

On the router, forward inbound TCP ports **80** and **443** to this server. Allow both ports in the host firewall, for example with `sudo ufw allow 80/tcp` and `sudo ufw allow 443/tcp`. Do not forward the app's port `8000` to the Internet. The Linux installer installs Caddy from its official APT repository on Debian/Ubuntu and leaves it stopped until a domain is configured. For manual/older installations, use the [official Debian/Ubuntu instructions](https://caddyserver.com/docs/install#debian-ubuntu-raspbian). Wait for DNS propagation, then run the command shown in the Domain settings. The script creates a managed Caddy site and systemd override; Caddy obtains and renews the Let's Encrypt certificate automatically. The app then listens on loopback HTTP and Caddy provides public HTTPS.

If the ISP blocks inbound ports or the network is behind CGNAT, HTTP-01 certificate issuance cannot work without a reachable public address or a DNS-01 integration. The script does not change router/firewall rules.

### Google OAuth

Google does not accept a public or private IP literal as a web OAuth redirect host. Configure a DNS name that resolves to the server, serve the application at that name with a publicly trusted HTTPS certificate, and then set this exact callback URI as an **Authorized redirect URI** on the Google OAuth Web client:

```text
https://timelapse.example.com/api/system/storage/oauth/callback
```

The domain configurator sets this callback and the public base URL in the systemd service. The values must exactly match Google Cloud Console:

```ini
Environment=TIMELAPSE_OAUTH_REDIRECT_URI=https://timelapse.example.com/api/system/storage/oauth/callback
Environment=TIMELAPSE_PUBLIC_URL=https://timelapse.example.com
```

Replace `timelapse.example.com` with your domain. The URI, scheme, hostname, port, and path must exactly match the value registered in Google Cloud Console. The settings screen displays the expected URI and blocks public IP callbacks.

In Google Cloud Console, configure the OAuth consent screen as **External**, add the verified domain plus public homepage, privacy policy, and developer contact details, and set the app to **In production**. In **Data Access**, request `openid`, `email`, and `https://www.googleapis.com/auth/drive.file`; remove `https://www.googleapis.com/auth/drive.metadata.readonly` from both the application and the Console scope list. The app only accesses files it creates or that the user explicitly opens/shares with it. `drive.file` is a non-sensitive scope; Google still requires basic app/domain verification for production. While the consent screen is in **Testing**, add your Google account as a test user; testing-mode refresh tokens can expire after seven days. Google Drive OAuth uses the user's personal Drive account; no Cloud Storage bucket or service account is required.

## Build And Release

The Windows runner workflow tests the app, creates the Linux source ZIP, bundles the Windows executable with PyInstaller and FFmpeg, and creates an Inno Setup installer. Push a version tag to publish both packages and `SHA256SUMS.txt` to the public GitHub Release:

```sh
git tag v1.0.0
git push origin v1.0.0
```

For subsequent releases, increment `VERSION`, commit, and push the matching tag (`git tag vX.Y.Z && git push origin vX.Y.Z`). The tag must equal `VERSION`; clients see the release as an available update.

To build only the source package locally, use Python 3.11 or newer:

```sh
python scripts/package.py
```

## First Access And State

Open `https://<server-host-or-ip>:8000`. The default certificate is self-signed, so browsers warn until it is trusted. Replace it with an organization/public CA certificate for managed or public environments. Configure SMTP and the recovery email after first login.

The installer prompts for the developer signature shown in the UI. It can later be changed in `developer_signature.txt` in the data directory. Linux state is under `/var/lib/timelapse` and captures under `/var/temp`; Windows state is under `%ProgramData%\TimelapseManager`.

Back up application data and credentials before replacing an installation. Keep the session credential when updating because it also encrypts the saved SMTP password. Release packages exclude databases, camera credentials, SMTP credentials, TLS keys, and generated media.