#!/bin/zsh
# Costruisce "Scarica Video.app" (finestra nativa via pywebview) in ~/Applications.
set -e
cd "$(dirname "$0")"
APP="$HOME/Applications/Scarica Video.app"
SUPPORT="$HOME/Library/Application Support/Scarica Video"
VENV="$SUPPORT/venv"
PYBREW="${PYBREW:-/opt/homebrew/bin/python3}"

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

echo "→ Preparo l'ambiente Python (venv + pywebview)…"
mkdir -p "$SUPPORT"
[ -x "$VENV/bin/python3" ] || "$PYBREW" -m venv "$VENV"
"$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null
"$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check pywebview >/dev/null
# Trascrizione con speaker: sherpa-onnx + numpy (se fallisce, l'app li
# installa comunque da sola al primo uso)
"$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check sherpa-onnx numpy >/dev/null || true
# yt-dlp con impersonificazione browser (curl_cffi: serve a TikTok & co.)
# e gallery-dl nel venv: l'app li preferisce a quelli di Homebrew e li
# auto-aggiorna una volta al giorno
"$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check -U "yt-dlp[default,curl-cffi]" gallery-dl >/dev/null || true

echo "→ Assemblo il bundle .app…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp app.py "$APP/Contents/Resources/app.py"
cp AppIcon.icns "$APP/Contents/Resources/applet.icns"

cat > "$APP/Contents/MacOS/ScaricaVideo" <<'LAUNCH'
#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
VENV="$HOME/Library/Application Support/Scarica Video/venv"
exec "$VENV/bin/python3" "$DIR/app.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/ScaricaVideo"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Scarica Video</string>
  <key>CFBundleDisplayName</key><string>Scarica Video</string>
  <key>CFBundleIdentifier</key><string>com.alekone.scaricavideo</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>ScaricaVideo</string>
  <key>CFBundleIconFile</key><string>applet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict></plist>
PLIST

echo "→ Firmo e registro…"
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

echo "✅ Fatto → $APP"
