# Scarica Video

App locale per macOS per scaricare video da **YouTube, TikTok, Instagram** e [centinaia di altri siti](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). Interfaccia pulita in una **finestra nativa** (app vera con icona nel Dock e in Cmd+Tab), coda con download in parallelo, cronologia e taglio per intervallo di minutaggio.

Sotto il cofano usa [`yt-dlp`](https://github.com/yt-dlp/yt-dlp); il backend è **solo Python standard library**. L'unica dipendenza è [`pywebview`](https://pywebview.flowrl.com/) per la finestra nativa, installata in automatico da `build.sh` in un venv dedicato.

## Funzioni

- 🎬 **Multi-URL** — incolla tanti link insieme, uno per riga
- 🎯 **Preview YouTube con IN/OUT** — incolli un link YouTube e parte la preview (stream diretto risolto da yt-dlp, funziona anche coi video che bloccano l'embed): fermati sul punto giusto e premi <kbd>I</kbd> (inizio) e <kbd>O</kbd> (fine) come in DaVinci, i campi DA/A si riempiono da soli
- ⏱️ **Intervallo DA → A** — scarica solo un pezzo del video, con taglio preciso ai keyframe (`--force-keyframes-at-cuts`); ogni taglio ha il minutaggio nel nome file, quindi tagli diversi dello stesso video convivono
- 🔗 **Link YouTube normalizzati** — incolli un link in qualunque formato (watch, youtu.be, Shorts, live, embed, con playlist o parametri di condivisione) e diventa l'URL canonico; se il link ha un secondaggio (`?t=…`), finisce nel campo DA come punto di partenza opzionale per "Solo un intervallo"
- 🎙️ **Trascrizione locale con speaker** — pulsante **Trascrivi** su ogni video: whisper.cpp (large-v3-turbo, Metal) trascrive con timestamp per parola e sherpa-onnx riconosce *chi parla quando* (2-3 speaker o rilevamento automatico). Tutto sul Mac, nessun upload, nessun account. Output accanto al video: `<nome>.transcript.txt` (leggibile, con speaker e timecode — perfetto da dare a Claude per trovare i punti da clippare) e `<nome>.transcript.json` (parole con start/end). Col pulsante **Trascrivi file…** in alto trascrivi anche un video/audio qualunque già sul Mac, non solo quelli scaricati
- ⚙️ **Impostazioni** — cartella di destinazione, qualità e login predefiniti, download simultanei e connessioni per video, lingua e numero di speaker della trascrizione (salvate in `config.json`)
- 📊 **Progress bar** con percentuale, velocità ed ETA in tempo reale; download a frammenti paralleli
- ♻️ **File già presente?** Nessun doppione silenzioso: la cronologia segnala "file già presente — non riscaricato"
- 🕑 **Cronologia** persistente (SQLite): apri file, mostra nel Finder, ri-scarica
- 🔐 **Login via cookie del browser** (Chrome/Safari/Firefox) per i siti che lo richiedono — TikTok, Instagram
- 🖼️ **Foto e caroselli** — fallback automatico su [`gallery-dl`](https://github.com/mikf/gallery-dl) quando non c'è un video
- 🎞️ **Qualità pensata per l'editing** — default 1080p **H.264/AAC**, pronto per DaVinci Resolve
- 🪟 **Finestra nativa macOS** (WKWebView via pywebview): icona propria nel Dock, voce in Cmd+Tab, si chiude come una qualsiasi app

I file finiscono in `~/Movies/Scarica Video`.

## Requisiti

```bash
brew install yt-dlp ffmpeg
brew install gallery-dl   # opzionale, per foto/caroselli Instagram
brew install whisper-cpp  # opzionale, per la trascrizione
```

Per la trascrizione, al primo uso l'app scarica da sola i modelli (~1.7 GB, una volta sola) in `~/Library/Application Support/Scarica Video/models/` e installa `sherpa-onnx` nel proprio venv.

macOS con Python 3 di sistema (`/usr/bin/python3`, già presente).

## Installazione

```bash
git clone https://github.com/alekone/scarica-video.git
cd scarica-video
./build.sh
```

`build.sh` crea un venv in `~/Library/Application Support/Scarica Video/venv`, ci installa `pywebview` e assembla **`Scarica Video.app`** in `~/Applications`. Aprila da Spotlight o Launchpad.

> Al primo avvio macOS può chiedere conferma (app non firmata da uno sviluppatore identificato): **click destro sull'app → Apri → Apri**.

### Uso senza app (headless)

Puoi lanciare direttamente il backend; senza `pywebview` resta un server web:

```bash
python3 app.py    # poi apri http://127.0.0.1:8642
```

## Come funziona

- `app.py` — server HTTP locale (stdlib) + interfaccia web + finestra nativa (pywebview, se disponibile). Gestisce coda, progressi, cronologia SQLite in `~/Library/Application Support/Scarica Video/`.
- `build.sh` — prepara il venv, genera l'icona e assembla il bundle `.app` (launcher nativo + `Info.plist`).
- `makeicon.py` — disegna l'icona (nessuna dipendenza grafica).

## Licenza

MIT — vedi [LICENSE](LICENSE). Fai quello che vuoi, a tuo rischio. Rispetta i termini di servizio dei siti e il diritto d'autore dei contenuti.
