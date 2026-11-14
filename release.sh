#!/bin/zsh
# Costruisce il pacchetto DISTRIBUIBILE di Scarica Video: un'app autosufficiente
# (runtime Python, yt-dlp, gallery-dl, ffmpeg, whisper-cli tutti inclusi) che
# chiunque può usare senza Homebrew, con auto-aggiornamento dalle GitHub Releases.
#
#   ./release.sh            build in dist/ (app + zip)
#   ./release.sh publish    build + release su GitHub (serve gh autenticato)
#
# Per lo sviluppo locale resta ./build.sh (usa il Python di Homebrew).
set -e
cd "$(dirname "$0")"

VERSION="$(/usr/bin/python3 -c "import re; print(re.search(r'VERSION = \"([^\"]+)\"', open('app.py').read()).group(1))")"

# Runtime Python standalone ricollocabile (astral-sh/python-build-standalone).
# 3.12: massima compatibilità con le wheel binarie (sherpa-onnx, curl_cffi).
PY_VER="3.12.16"
PY_TAG="20261110"
RUNTIME_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}+${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"

# ffmpeg/ffprobe statici (ffmpeg.martin-riedl.de) e whisper-cli statico con
# Metal incorporato: già verificati e documentati in "Video code"/vendor
VIDEOCODE_BIN="../Video code/vendor/bin"

VENDOR="vendor"
DIST="dist"
APP="$DIST/Scarica Video.app"
RES="$APP/Contents/Resources"
STAMP="py${PY_VER}+${PY_TAG}"   # cambia solo se cambiano runtime o wheel di base

echo "→ Scarica Video v$VERSION (arm64, runtime Python $PY_VER)"

# ---------------------------------------------------------------- 1. runtime
mkdir -p "$VENDOR"
RT_TAR="$VENDOR/runtime-${PY_VER}-${PY_TAG}.tar.gz"
if [ ! -f "$RT_TAR" ]; then
  echo "→ Scarico il runtime Python…"
  curl -fsSL --retry 3 -o "$RT_TAR.part" "$RUNTIME_URL"
  mv "$RT_TAR.part" "$RT_TAR"
fi

# ---------------------------------------------------------------- 2. wheel
# Scaricate con lo stesso runtime che le installerà sul Mac di destinazione,
# così pip prende le wheel giuste (cp312, arm64). Solo wheel binarie: il primo
# avvio installa offline, niente build da sorgenti.
if [ ! -f "$VENDOR/wheels/.done" ]; then
  echo "→ Scarico le wheel (pywebview, yt-dlp, gallery-dl)…"
  rm -rf "$VENDOR/rt-build" "$VENDOR/wheels"
  mkdir -p "$VENDOR/rt-build" "$VENDOR/wheels"
  tar -xzf "$RT_TAR" -C "$VENDOR/rt-build" --strip-components=1
  "$VENDOR/rt-build/bin/python3" -m pip download --quiet \
      --disable-pip-version-check --only-binary=:all: \
      -d "$VENDOR/wheels" "pywebview" "yt-dlp[default,curl-cffi]" "gallery-dl"
  touch "$VENDOR/wheels/.done"
fi

# ---------------------------------------------------------------- 3. binari
mkdir -p "$VENDOR/bin"
for b in ffmpeg ffprobe whisper-cli; do
  if [ ! -f "$VENDOR/bin/$b" ]; then
    [ -f "$VIDEOCODE_BIN/$b" ] || { echo "manca $VIDEOCODE_BIN/$b"; exit 1; }
    cp "$VIDEOCODE_BIN/$b" "$VENDOR/bin/$b"
  fi
done

# ---------------------------------------------------------------- 4. bundle
echo "→ Assemblo il bundle…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/bin" "$RES/wheels"
cp app.py "$RES/app.py"
cp AppIcon.icns "$RES/applet.icns"
cp "$RT_TAR" "$RES/runtime.tar.gz"
cp "$VENDOR"/wheels/*.whl "$RES/wheels/"
cp "$VENDOR"/bin/* "$RES/bin/"
chmod +x "$RES/bin/"*
echo "$STAMP" > "$RES/stamp.txt"
printf 'pywebview\nyt-dlp[default,curl-cffi]\ngallery-dl\n' > "$RES/requirements.txt"

# Launcher: al primo avvio (o quando cambia il runtime) prepara tutto in
# Application Support — estrae il runtime, crea il venv, installa le wheel
# incluse (offline). Poi avvia l'app col venv, come in sviluppo.
cat > "$APP/Contents/MacOS/ScaricaVideo" <<'LAUNCH'
#!/bin/zsh
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
APP_ROOT="$(cd "$RES/../.." && pwd)"
SUPPORT="$HOME/Library/Application Support/Scarica Video"
RT="$SUPPORT/runtime"
VENV="$SUPPORT/venv"
MARKER="$SUPPORT/bootstrap.stamp"
STAMP="$(cat "$RES/stamp.txt" 2>/dev/null)"
LOG="$SUPPORT/bootstrap.log"

if [ "$(uname -m)" != "arm64" ]; then
  osascript -e 'display alert "Scarica Video" message "Questa app funziona solo sui Mac con chip Apple (M1 o successivi)."' >/dev/null 2>&1
  exit 1
fi

# App aperta senza essere stata spostata col Finder: macOS la esegue da una
# copia temporanea ("App Translocation") che può sparire sotto i piedi
# (ffmpeg & co. introvabili). Auto-riparazione: trova l'app vera, le toglie
# la quarantena (l'utente l'ha già approvata: sta girando) e la rilancia.
case "$APP_ROOT" in
  *"/AppTranslocation/"*)
    REAL="$(mdfind "kMDItemCFBundleIdentifier == 'com.alekone.scaricavideo'" 2>/dev/null | grep -v AppTranslocation | grep -m1 "/Applications/")"
    [ -d "$REAL" ] || REAL="$(mdfind "kMDItemCFBundleIdentifier == 'com.alekone.scaricavideo'" 2>/dev/null | grep -v AppTranslocation | head -1)"
    if [ -d "$REAL" ]; then
      xattr -dr com.apple.quarantine "$REAL" 2>/dev/null
      open "$REAL"
      exit 0
    fi
    osascript -e 'display alert "Scarica Video" message "macOS ha aperto l'"'"'app da una copia temporanea. Trascina Scarica Video nella cartella Applicazioni col Finder e riaprila da lì."' >/dev/null 2>&1
    exit 1
    ;;
esac
# Dopo il primo avvio approvato, via la quarantena: mai più translocation
xattr -dr com.apple.quarantine "$APP_ROOT" 2>/dev/null || true

if [ "$(cat "$MARKER" 2>/dev/null)" != "$STAMP" ] || ! "$VENV/bin/python3" -V >/dev/null 2>&1; then
  mkdir -p "$SUPPORT"
  osascript -e 'display notification "Preparo i componenti: solo il primo avvio, circa un minuto…" with title "Scarica Video"' >/dev/null 2>&1 || true
  ok=1
  {
    echo "== bootstrap $(date) stamp=$STAMP ==" > "$LOG"
    rm -rf "$RT.new" "$VENV" &&
    mkdir -p "$RT.new" &&
    tar -xzf "$RES/runtime.tar.gz" -C "$RT.new" --strip-components=1 &&
    xattr -dr com.apple.quarantine "$RT.new" 2>/dev/null;
    rm -rf "$RT" &&
    mv "$RT.new" "$RT" &&
    "$RT/bin/python3" -m venv "$VENV" &&
    "$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check \
        --no-index --find-links "$RES/wheels" -r "$RES/requirements.txt" &&
    echo "$STAMP" > "$MARKER"
  } >> "$LOG" 2>&1 || ok=0
  if [ "$ok" = 0 ]; then
    osascript -e 'display alert "Scarica Video" message "La preparazione dei componenti non è riuscita. Riapri l'"'"'app per riprovare (serve la connessione? no: è tutto incluso). Dettagli in: ~/Library/Application Support/Scarica Video/bootstrap.log"' >/dev/null 2>&1
    exit 1
  fi
fi

export PATH="$RES/bin:$VENV/bin:$PATH"
export SV_APP="$APP_ROOT"
# L'ffmpeg statico usa OpenSSL, che da solo non trova i certificati CA del
# sistema: senza questo, i tagli (--download-sections, input https) falliscono
# con "certificate verify failed"
if [ -z "$SSL_CERT_FILE" ] && [ -f /etc/ssl/cert.pem ]; then
  export SSL_CERT_FILE=/etc/ssl/cert.pem
fi
exec "$VENV/bin/python3" "$RES/app.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/ScaricaVideo"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Scarica Video</string>
  <key>CFBundleDisplayName</key><string>Scarica Video</string>
  <key>CFBundleIdentifier</key><string>com.alekone.scaricavideo</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>ScaricaVideo</string>
  <key>CFBundleIconFile</key><string>applet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
</dict></plist>
PLIST

# ---------------------------------------------------------------- 5. firma
# Ad-hoc: niente account Apple Developer. Il destinatario al primo avvio passa
# da Impostazioni di Sistema > Privacy e Sicurezza > "Apri comunque".
# --deep non firma i binari in Resources: firmali uno per uno prima.
echo "→ Firmo (ad-hoc)…"
for b in "$RES/bin/"*; do codesign --force -s - "$b" 2>/dev/null; done
codesign --force --deep -s - "$APP" 2>/dev/null

# ---------------------------------------------------------------- 6. zip
ZIP="$DIST/Scarica-Video-${VERSION}.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "✅ Build: $APP"
echo "✅ Zip:   $ZIP ($(du -h "$ZIP" | cut -f1 | tr -d ' '))"

# ---------------------------------------------------------------- 7. release
if [ "$1" = "publish" ]; then
  echo "→ Pubblico la release v$VERSION su GitHub…"
  gh release create "v$VERSION" "$ZIP" \
    --title "Scarica Video $VERSION" \
    --notes "App autosufficiente per Mac Apple Silicon (macOS 13+): scarica lo zip, trascina l'app in Applicazioni e aprila. Al primo avvio macOS chiede conferma: Impostazioni di Sistema → Privacy e Sicurezza → \"Apri comunque\". Da qui in poi l'app si aggiorna da sola."
  echo "✅ Release: https://github.com/alekone/scarica-video/releases/tag/v$VERSION"
fi
