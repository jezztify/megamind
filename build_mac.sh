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

# Ensure Quartz bindings are present for smooth cursor injection
pip install pyobjc-framework-Quartz --quiet

# Build the single binary
echo ""
echo "Building MegaMind_Client binary..."
pyinstaller megamind_client.spec --clean --noconfirm

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

echo ""
echo "=== Done ==="
echo "  ${PKG_OUT}  — installs megamind-client to ${INSTALL_DIR}"
echo ""
echo "Usage after install:"
echo "  megamind-client http://<server-ip>:8080"
echo ""
echo "NOTE: macOS will require Accessibility permission on first run."
echo "      System Settings > Privacy & Security > Accessibility"
