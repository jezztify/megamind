# Build MegaMind Windows executables
# Run from the repo root: .\build_windows.ps1
# Requires: pip install pyinstaller

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "=== MegaMind Windows Build ===" -ForegroundColor Cyan

# Ensure PyInstaller is available in the Python environment used for the build.
Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Yellow
python -m pip install pyinstaller
if (-not $?) { Write-Error "PyInstaller installation failed"; exit 1 }

# Install application dependencies before PyInstaller analyzes imports. This
# keeps the one-file executables self-contained when the global Python used by
# the build does not already have FastAPI/Uvicorn installed.
Write-Host "Installing MegaMind dependencies..." -ForegroundColor Yellow
python -m pip install -r requirements.txt
if (-not $?) { Write-Error "Dependency installation failed"; exit 1 }

# Build server
Write-Host "`nBuilding MegaMind_Server.exe..." -ForegroundColor Green
python -m PyInstaller megamind_server.spec --clean --noconfirm
if (-not $?) { Write-Error "Server build failed"; exit 1 }

# Build client
Write-Host "`nBuilding MegaMind_Client.exe..." -ForegroundColor Green
python -m PyInstaller megamind_client.spec --clean --noconfirm
if (-not $?) { Write-Error "Client build failed"; exit 1 }

# Build the local desktop control panel
Write-Host "`nBuilding MegaMind_Control_Panel.exe..." -ForegroundColor Green
python -m PyInstaller megamind_control_panel.spec --clean --noconfirm
if (-not $?) { Write-Error "Control panel build failed"; exit 1 }

Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "  dist\MegaMind_Server.exe  - run on the machine with keyboard/mouse (requires Admin)"
Write-Host "  dist\MegaMind_Client.exe  - run on machines to be controlled (requires Admin)"
Write-Host "  dist\MegaMind_Control_Panel.exe  - local GUI control panel (requires Admin for input control)"
Write-Host ""
Write-Host "Usage:"
Write-Host "  Server: .\dist\MegaMind_Server.exe"
Write-Host "  Client: .\dist\MegaMind_Client.exe http://<server-ip>:9090"
Write-Host "  Control panel: .\dist\MegaMind_Control_Panel.exe"
