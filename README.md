# Timelapse Manager

FastAPI application for camera capture, timelapse generation, storage synchronization, and email alerts.

See [INSTALL.md](INSTALL.md) for Linux deployment and the Windows self-contained installer workflow.

## Windows Release Build

Push a version tag such as `v1.0.0`. GitHub Actions runs the tests on Windows, bundles the Python application with PyInstaller and FFmpeg, builds an Inno Setup installer, and publishes the installer plus checksums to a private GitHub Release. The same Release includes a source package for Linux.

The Windows package requires Windows x64, but does not require Python or FFmpeg to be installed on the target. It creates a scheduled task running as Local Service and serves HTTPS on port 8000.