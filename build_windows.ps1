# Build MegaMind Windows executables
# Run from the repo root: .\build_windows.ps1
# Requires: pip install pyinstaller

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "=== MegaMind Windows Build ===" -ForegroundColor Cyan

# Ensure PyInstaller is available
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Yellow
    pip install pyinstaller
}

# Build server
Write-Host "`nBuilding MegaMind_Server.exe..." -ForegroundColor Green
pyinstaller megamind_server.spec --clean --noconfirm
if (-not $?) { Write-Error "Server build failed"; exit 1 }

# Build client
Write-Host "`nBuilding MegaMind_Client.exe..." -ForegroundColor Green
pyinstaller megamind_client.spec --clean --noconfirm
if (-not $?) { Write-Error "Client build failed"; exit 1 }

Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "  dist\MegaMind_Server.exe  - run on the machine with keyboard/mouse (requires Admin)"
Write-Host "  dist\MegaMind_Client.exe  - run on machines to be controlled (requires Admin)"
Write-Host ""
Write-Host "Usage:"
Write-Host "  Server: .\dist\MegaMind_Server.exe"
Write-Host "  Client: .\dist\MegaMind_Client.exe http://<server-ip>:8080"
