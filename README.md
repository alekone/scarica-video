# Scarica Video

App locale per macOS per scaricare video da **YouTube, TikTok, Instagram** e [centinaia di altri siti](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). Interfaccia pulita in una finestra dedicata, coda con download in parallelo, cronologia e taglio per intervallo di minutaggio.

Sotto il cofano usa [`yt-dlp`](https://github.com/yt-dlp/yt-dlp); il backend è **solo Python standard library** (nessuna dipendenza da installare via pip).

## Funzioni

- 🎬 **Multi-URL** — incolla tanti link insieme, uno per riga
- ⏱️ **Intervallo DA → A** — scarica solo un pezzo del video, con taglio preciso ai keyframe (`--force-keyframes-at-cuts`)
- 📊 **Progress bar** con percentuale, velocità ed ETA in tempo reale
- 🕑 **Cronologia** persistente (SQLite): apri file, mostra nel Finder, ri-scarica
- 🔐 **Login via cookie del browser** (Chrome/Safari/Firefox) per i siti che lo richiedono — TikTok, Instagram
- 🖼️ **Foto e caroselli** — fallback automatico su [`gallery-dl`](https://github.com/mikf/gallery-dl) quando non c'è un video
- 🎞️ **Qualità pensata per l'editing** — default 1080p **H.264/AAC**, pronto per DaVinci Resolve
- 🪟 **Finestra dedicata** senza barra del browser (Chrome/Brave/Edge in app-mode)

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

Crea **`Scarica Video.app`** in `~/Applications`. Aprila da Spotlight o Launchpad.

> Al primo avvio macOS può chiedere conferma (app non firmata da uno sviluppatore identificato): **click destro sull'app → Apri → Apri**. E autorizza il controllo del browser quando richiesto.

### Uso senza app

Puoi lanciare direttamente il backend:

```bash
python3 app.py    # poi apri http://127.0.0.1:8642
```

## Come funziona

- `app.py` — server HTTP locale (stdlib) + interfaccia web. Gestisce coda, progressi, cronologia SQLite in `~/Library/Application Support/Scarica Video/`.
- `launcher.applescript` — avvia il server e apre la finestra in app-mode.
- `build.sh` — compila la `.app`, genera e applica l'icona.
- `makeicon.py` — disegna l'icona (nessuna dipendenza grafica).

## Licenza

MIT — vedi [LICENSE](LICENSE). Fai quello che vuoi, a tuo rischio. Rispetta i termini di servizio dei siti e il diritto d'autore dei contenuti.
