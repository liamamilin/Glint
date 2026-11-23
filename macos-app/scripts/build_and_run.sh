#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Glint"
BUNDLE_ID="com.glint.app"
BUILD_DIR="$ROOT_DIR/build"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
SCRATCH_DIR="/tmp/glint-swift-build"

resolve_sign_identity() {
  if [[ -n "${GLINT_SIGN_IDENTITY:-}" ]]; then
    echo "$GLINT_SIGN_IDENTITY"
    return
  fi

  local identities
  identities="$(security find-identity -v -p codesigning 2>/dev/null || true)"

  # Prefer dedicated Glint certificate if present
  if echo "$identities" | grep -q '"Glint Local Dev"'; then
    echo "Glint Local Dev"
    return
  fi

  # Otherwise reuse the first valid local codesigning identity so TCC permissions persist across rebuilds
  local first_sha
  first_sha="$(echo "$identities" | awk '/^[[:space:]]*[0-9]+\)/ { print $2; exit }')"
  if [[ -n "$first_sha" ]]; then
    echo "$first_sha"
    return
  fi

  echo "-"
}

SIGN_IDENTITY="$(resolve_sign_identity)"

echo "[Glint] Building Swift release binary..."
cd "$ROOT_DIR"
swift build -c release --scratch-path "$SCRATCH_DIR"

BIN_PATH="$(swift build -c release --scratch-path "$SCRATCH_DIR" --show-bin-path)/$APP_NAME"
if [[ ! -f "$BIN_PATH" ]]; then
  echo "[Glint] Error: binary not found at $BIN_PATH"
  exit 1
fi

echo "[Glint] Assembling $APP_BUNDLE..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/$APP_NAME"
cp "$ROOT_DIR/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
if [[ -f "$ROOT_DIR/Resources/AppIcon.icns" ]]; then
  cp "$ROOT_DIR/Resources/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
fi

echo "[Glint] Signing bundle with identity: $SIGN_IDENTITY"
codesign --force --deep --identifier "$BUNDLE_ID" --sign "$SIGN_IDENTITY" "$APP_BUNDLE"
if [[ "$SIGN_IDENTITY" == "-" ]]; then
  echo "[Glint] Warning: using ad-hoc signing; Accessibility permission may reset after rebuilds."
fi

LAUNCH_TARGET="$APP_BUNDLE"
if [[ "${1:-}" != "--local" ]] && [[ -w "/Applications" ]]; then
  INSTALL_TARGET="/Applications/$APP_NAME.app"
  echo "[Glint] Installing to $INSTALL_TARGET (pass --local to skip)..."
  pkill -x "$APP_NAME" 2>/dev/null || true
  rm -rf "$INSTALL_TARGET"
  ditto "$APP_BUNDLE" "$INSTALL_TARGET"
  LAUNCH_TARGET="$INSTALL_TARGET"
else
  pkill -x "$APP_NAME" 2>/dev/null || true
fi

echo "[Glint] Launching $LAUNCH_TARGET..."
open "$LAUNCH_TARGET"
echo "[Glint] Done! Check your macOS menu bar for the ✨ icon."
