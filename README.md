# Scarica Video

App locale per macOS per scaricare video da **YouTube, TikTok, Instagram** e [centinaia di altri siti](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). Interfaccia pulita in una **finestra nativa** (app vera con icona nel Dock e in Cmd+Tab), coda con download in parallelo, cronologia e taglio per intervallo di minutaggio.

Sotto il cofano usa [`yt-dlp`](https://github.com/yt-dlp/yt-dlp); il backend è **solo Python standard library**. L'unica dipendenza è [`pywebview`](https://pywebview.flowrl.com/) per la finestra nativa, installata in automatico da `build.sh` in un venv dedicato.

## Funzioni

- 🎬 **Multi-URL** — incolla tanti link insieme, uno per riga
- ⏱️ **Intervallo DA → A** — scarica solo un pezzo del video, con taglio preciso ai keyframe (`--force-keyframes-at-cuts`); ogni taglio ha il minutaggio nel nome file, quindi tagli diversi dello stesso video convivono
- 📊 **Progress bar** con percentuale, velocità ed ETA in tempo reale; download a frammenti paralleli (`-N 4`)
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
```

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
