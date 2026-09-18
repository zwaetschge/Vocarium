#!/usr/bin/env bash
# Spiegelt die Quellen des nativen Android-Clients aus dem android-app-creator-
# Workspace nach android/. Quelle der Wahrheit bleibt der Workspace (dort baut
# und installiert der Android-Builder); das Repo hält den versionierten Stand.
# README.md und .gitignore in android/ gehören zum Repo und werden nicht angefasst.
set -euo pipefail
SRC="${ANDROID_WORKSPACE:-/mnt/user/AI/plum-code/android-app-creator/projects/242819cb-6367-4c34-bb0e-75fa70fc0b7f}"
DST="$(cd "$(dirname "$0")/.." && pwd)/android"
mkdir -p "$DST"
# Stale Quelldateien entfernen, Repo-eigene Dateien behalten.
find "$DST" -mindepth 1 -maxdepth 1 ! -name README.md ! -name .gitignore -exec rm -rf {} +
tar -C "$SRC" \
  --exclude='./build' --exclude='./app/build' --exclude='./.gradle' --exclude='./.work' \
  --exclude='./.appcreator.json' --exclude='./local.properties' \
  --exclude='*.apk' --exclude='*.aab' --exclude='*.keystore' --exclude='*.jks' \
  -cf - . | tar -C "$DST" -xf -
echo "android/ synced from $SRC"
