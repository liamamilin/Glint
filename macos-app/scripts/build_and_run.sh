#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Glint"
BUILD_DIR="$ROOT_DIR/build"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
SIGN_IDENTITY="${GLINT_SIGN_IDENTITY:--}"

SCRATCH_DIR="/tmp/glint-swift-build"

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

echo "[Glint] Signing bundle with identity: $SIGN_IDENTITY"
codesign --force --deep --sign "$SIGN_IDENTITY" "$APP_BUNDLE"

echo "[Glint] Launching $APP_BUNDLE..."
pkill -x "$APP_NAME" 2>/dev/null || true
open "$APP_BUNDLE"
echo "[Glint] Done! Check your macOS menu bar for the ✨ icon."
