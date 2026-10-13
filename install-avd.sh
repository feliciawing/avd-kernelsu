#!/bin/bash
# Install KernelSU kernel + matching modules into an AVD
# Usage: ./install-avd.sh <artifact-dir> <avd-name>
# Example: ./install-avd.sh ./6.1.162-android14-2026-03-x86_64 ksu_test

set -e

ARTIFACT_DIR="${1:-.}"
AVD_NAME="${2:-ksu_test}"

# Find AVD path
if [ -n "$ANDROID_AVD_HOME" ]; then
  AVD_HOME="$ANDROID_AVD_HOME"
elif [ -d "$HOME/.android/avd" ]; then
  AVD_HOME="$HOME/.android/avd"
elif [ -d "$LOCALAPPDATA/Android/Sdk" ]; then
  AVD_HOME="$USERPROFILE/.android/avd"
fi

AVD_DIR="$AVD_HOME/${AVD_NAME}.avd"

if [ ! -d "$AVD_DIR" ]; then
  echo "ERROR: AVD directory not found: $AVD_DIR"
  exit 1
fi

# Find system image path from config.ini
SYSDIR=$(grep "image.sysdir.1" "$AVD_DIR/config.ini" | cut -d= -f2 | tr -d '[:space:]')
if [ -n "$ANDROID_SDK_ROOT" ]; then
  SYSIMG="$ANDROID_SDK_ROOT/$SYSDIR"
elif [ -n "$ANDROID_HOME" ]; then
  SYSIMG="$ANDROID_HOME/$SYSDIR"
else
  SYSIMG="$HOME/Android/Sdk/$SYSDIR"
fi

echo "=== AVD KernelSU Installer ==="
echo "Artifact: $ARTIFACT_DIR"
echo "AVD: $AVD_NAME"
echo "AVD Dir: $AVD_DIR"
echo "System Image: $SYSIMG"

# Check for kernel
if [ -f "$ARTIFACT_DIR/bzImage" ]; then
  KERNEL="$ARTIFACT_DIR/bzImage"
elif [ -f "$ARTIFACT_DIR/Image" ]; then
  KERNEL="$ARTIFACT_DIR/Image"
else
  echo "ERROR: No kernel found in $ARTIFACT_DIR"
  exit 1
fi

# Check for ramdisk
RAMDISK="$SYSIMG/ramdisk.img"
if [ ! -f "$RAMDISK" ]; then
  echo "ERROR: ramdisk.img not found at $RAMDISK"
  exit 1
fi

# Backup original ramdisk
if [ ! -f "$AVD_DIR/ramdisk.img.bak" ]; then
  echo "Backing up original ramdisk..."
  cp "$RAMDISK" "$AVD_DIR/ramdisk.img.bak"
fi

# Repack ramdisk with our modules
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Repacking ramdisk with matching modules..."
python3 "$SCRIPT_DIR/repack-ramdisk.py" "$RAMDISK" "$ARTIFACT_DIR/modules" "$AVD_DIR/ramdisk-ksu.img"

# Copy kernel
echo "Copying kernel..."
cp "$KERNEL" "$AVD_DIR/kernel-ksu"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Run your AVD with:"
echo "  emulator -avd $AVD_NAME -kernel $AVD_DIR/kernel-ksu -ramdisk $AVD_DIR/ramdisk-ksu.img -no-snapshot-load -show-kernel"
echo ""
