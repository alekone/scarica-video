# vendor/

Cache di build per release.sh (non versionata):
- runtime-*.tar.gz: Python standalone da astral-sh/python-build-standalone
- wheels/: wheel offline per il primo avvio (pip download, solo binarie)
- bin/: ffmpeg/ffprobe statici (ffmpeg.martin-riedl.de) e whisper-cli statico,
  copiati da "Video code"/vendor/bin (vedi il README là per rigenerarli)
