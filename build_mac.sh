#!/usr/bin/env bash
# Build MegaMind_Client.pkg for macOS
# Run from the repo root: bash build_mac.sh
# Requires: pip install pyinstaller pyobjc-framework-Quartz

set -euo pipefail

VERSION="1.0"
PKG_ID="com.megamind.client"
INSTALL_DIR="/usr/local/bin"
BINARY_NAME="megamind-client"

echo "=== MegaMind macOS Build ==="

# Ensure PyInstaller is available
if ! command -v pyinstaller &>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

# Install application dependencies before PyInstaller analyzes client and server imports.
pip install -r requirements.txt

# Build the single binary
echo ""
echo "Building MegaMind_Client binary..."
pyinstaller megamind_client.spec --clean --noconfirm

# Build the local desktop control panel as a macOS application bundle.
echo ""
echo "Building MegaMind_Control_Panel.app..."
pyinstaller megamind_control_panel.spec --clean --noconfirm

# Rename to the install name
BINARY="dist/MegaMind_Client"
if [ ! -f "$BINARY" ]; then
    echo "ERROR: expected binary at $BINARY" >&2
    exit 1
fi

# Stage the payload mimicking the install tree
STAGING="dist/pkg_staging"
rm -rf "$STAGING"
mkdir -p "$STAGING${INSTALL_DIR}"
cp "$BINARY" "$STAGING${INSTALL_DIR}/${BINARY_NAME}"
chmod +x "$STAGING${INSTALL_DIR}/${BINARY_NAME}"

# Create the .pkg
PKG_OUT="dist/MegaMind_Client.pkg"
echo ""
echo "Creating ${PKG_OUT}..."
pkgbuild \
    --root "$STAGING" \
    --identifier "$PKG_ID" \
    --version "$VERSION" \
    --install-location / \
    "$PKG_OUT"

# Package the app bundle separately so users can install it in /Applications.
PANEL_APP="dist/MegaMind_Control_Panel.app"
if [ ! -d "$PANEL_APP" ]; then
    echo "ERROR: expected app bundle at $PANEL_APP" >&2
    exit 1
fi

PANEL_STAGING="dist/control_panel_pkg_staging"
rm -rf "$PANEL_STAGING"
mkdir -p "$PANEL_STAGING/Applications"
cp -R "$PANEL_APP" "$PANEL_STAGING/Applications/"

PANEL_PKG="dist/MegaMind_Control_Panel.pkg"
echo ""
echo "Creating ${PANEL_PKG}..."
pkgbuild \
    --root "$PANEL_STAGING" \
    --identifier "com.megamind.controlpanel" \
    --version "$VERSION" \
    --install-location / \
    "$PANEL_PKG"

echo ""
echo "=== Done ==="
echo "  ${PKG_OUT}  — installs megamind-client to ${INSTALL_DIR}"
echo "  ${PANEL_PKG}  — installs MegaMind_Control_Panel.app to /Applications"
echo ""
echo "Usage after install:"
echo "  megamind-client http://<server-ip>:9090"
echo "  Open /Applications/MegaMind_Control_Panel.app"
echo ""
echo "NOTE: macOS will require Accessibility permission on first run."
echo "      System Settings > Privacy & Security > Accessibility"
