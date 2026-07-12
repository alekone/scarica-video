#!/bin/zsh
# Costruisce "Scarica Video.app" in ~/Applications a partire da questo repo.
set -e
cd "$(dirname "$0")"
REPO="$PWD"
APP="$HOME/Applications/Scarica Video.app"

echo "→ Genero l'icona…"
/usr/bin/python3 makeicon.py

echo "→ Creo il set d'icone (.icns)…"
rm -rf build.iconset && mkdir build.iconset
for sz in 16 32 128 256 512; do
  sips -z $sz $sz icon_1024.png --out build.iconset/icon_${sz}x${sz}.png >/dev/null
  d=$((sz*2))
  sips -z $d $d icon_1024.png --out build.iconset/icon_${sz}x${sz}@2x.png >/dev/null
done
iconutil -c icns build.iconset -o AppIcon.icns
rm -rf build.iconset

echo "→ Compilo l'app…"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" launcher.applescript

echo "→ Inserisco app.py e l'icona…"
cp app.py "$APP/Contents/Resources/app.py"
cp AppIcon.icns "$APP/Contents/Resources/applet.icns"
rm -f "$APP/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile applet" "$APP/Contents/Info.plist" 2>/dev/null || true

echo "→ Firmo e registro…"
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

echo "✅ Fatto → $APP"
